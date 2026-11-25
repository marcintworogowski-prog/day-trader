#!/usr/bin/env python3
"""Trade Review for trader-review: a paper-trade journal and a weekly summary.
Standard library only. Long side only. It never places an order.

    python review.py dodaj --ticker TSLA --setup orb --wejscie 364.18 --stop 358.32 --cel 370.04 --wielkosc 8
    python review.py zamknij --id 1 --wyjscie 370.04 --plan tak --notatka "cel"
    python review.py lista --otwarte
    python review.py tydzien                       # this week (Mon-Sun, New York), closed trades
    python review.py tydzien --data 2026-10-19     # the week that holds this date
    python review.py tydzien --od 2026-10-01 --do 2026-10-31

English names work too: add, close, list, week.

Journal: journal/trades.csv (UTF-8, opens in Excel). Only closed trades count,
in the week of their exit. Results are prices only: no fees, no taxes.

  result $      (exit - entry) x size
  result R      (exit - entry) / (entry - stop), so -1R is a full planned loss
  expectancy    the average result per closed trade, in $ and in R
  max drawdown  the deepest fall of the running result from its peak, counted
                from 0 at the start of the period, trade by trade
  SPY           Yahoo's daily chart, adjusted closes: from the last close before
                the period to the last one in it, on the config's "budget"

Mistakes are found from the numbers (loss past -1.1R, no setup, risk over
max_loss_per_trade_usd, target under min_reward_risk, held overnight) or named
by you with --blad. The limits come from trader/config.json.
"""

import argparse
import csv
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"
YAHOO_HOSTS = ("https://query2.finance.yahoo.com", "https://query1.finance.yahoo.com")
BACKOFF = 5.0
SLEEP = time.sleep
CENT = Decimal("0.01")
BROKEN_STOP_R = Decimal("-1.1")    # past the stop by more than normal slippage
SMALL_SAMPLE = 30
EPOCH = datetime.datetime(1970, 1, 1)
JOURNAL = os.path.join("journal", "trades.csv")
FIELDS = ["id", "data", "ticker", "setup", "wejscie", "stop", "cel", "wyjscie", "wielkosc",
          "wynik_usd", "wynik_r", "plan", "notatka", "data_wyjscia", "bledy"]
NO_SETUP = ("", "brak", "none", "-")
DEFAULTS = {"budget": 5000, "max_loss_per_trade_usd": 50, "min_reward_risk": 1.0, "paper": True}
MISTAKES = {
    "zlamany-stop": "złamany stop (strata większa niż plan)",
    "bez-sygnalu": "wejście bez sygnału",
    "za-duze-ryzyko": "za duże ryzyko na transakcję",
    "slaby-cel": "cel za blisko względem ryzyka",
    "przez-noc": "pozycja trzymana przez noc",
    "wyjscie-za-wczesnie": "wyjście za wcześnie",
    "przesuniety-stop": "przesunięty stop",
    "gonienie-ceny": "gonienie ceny",
    "odgrywanie-sie": "odgrywanie się po stracie",
    "inny-blad": "inne odejście od planu",
}


class ReviewError(Exception):
    pass


# ---------------------------------------------------------------- time

def _nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def eastern_offset(utc):
    """Hours from UTC for New York: -4 in daylight time, -5 otherwise (US rule since 2007)."""
    y = utc.year
    start = datetime.datetime.combine(_nth_sunday(y, 3, 2), datetime.time(7, 0))
    end = datetime.datetime.combine(_nth_sunday(y, 11, 1), datetime.time(6, 0))
    return -4 if start <= utc < end else -5


def to_eastern(utc):
    return utc + datetime.timedelta(hours=eastern_offset(utc))


def parse_date(text, what):
    t = str(text).strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", t):
        raise ReviewError("%s: podaj datę jak 2026-10-19, a nie %r" % (what, text))
    try:
        return datetime.date(*[int(x) for x in t.split("-")])
    except ValueError:
        raise ReviewError("%s: nie ma takiej daty: %s" % (what, text))


def today_ny(text=None):
    """Today's date in New York, or --now YYYY-MM-DD."""
    if text:
        return parse_date(text, "--now")
    return to_eastern(datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)).date()


def week_of(d):
    monday = d - datetime.timedelta(days=d.weekday())
    return monday, monday + datetime.timedelta(days=6)


# ---------------------------------------------------------------- numbers

def number(text, what):
    """A positive number. 364.18 and 364,18 both work; 1,000 is refused as unclear."""
    t = str(text).strip().lstrip("$")
    if re.match(r"^\d+,\d{1,2}$", t):
        t = t.replace(",", ".")
    if not re.match(r"^\d+(\.\d+)?$", t):
        raise ReviewError("%s: to nie jest liczba, jaką rozumiem: %r (użyj np. 364.18)" % (what, text))
    v = Decimal(t)
    if v <= 0:
        raise ReviewError("%s: liczba musi być większa od 0" % what)
    return v


def cents(x):
    return Decimal(x).quantize(CENT, rounding=ROUND_HALF_UP)


def money(x):
    v = cents(x)
    return "%s$%s" % ("-" if v < 0 else "", format(abs(v), ",f"))


def signed_r(x):
    return "%+.2fR" % x


def result(entry, stop, exit_price, size):
    """(result in $, result in R), both unrounded."""
    return (exit_price - entry) * size, (exit_price - entry) / (entry - stop)


# ---------------------------------------------------------------- files

def read_config(path):
    cfg = dict(DEFAULTS)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8-sig") as fh:
                doc = json.load(fh)
        except (OSError, ValueError) as e:
            raise ReviewError("nie mogę odczytać %s: %s" % (path, e))
        if not isinstance(doc, dict):
            raise ReviewError("%s powinien być obiektem JSON" % path)
        cfg.update(doc)
    for k in ("budget", "max_loss_per_trade_usd", "min_reward_risk"):
        try:
            cfg[k] = Decimal(str(cfg[k]))
        except InvalidOperation:
            raise ReviewError("\"%s\" w %s nie jest liczbą" % (k, path))
        if not cfg[k].is_finite() or cfg[k] <= 0:
            raise ReviewError("\"%s\" w %s musi być większe od 0" % (k, path))
    return cfg


def load(path):
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            rd = csv.DictReader(fh)
            missing = [f for f in FIELDS if f not in (rd.fieldnames or [])]
            if missing:
                raise ReviewError("w %s brakuje kolumn: %s (jeśli Excel zapisał plik ze średnikami, zapisz go "
                                  "z przecinkami)" % (path, ", ".join(missing)))
            rows = [{f: (r.get(f) or "") for f in FIELDS} for r in rd]
    except (OSError, UnicodeDecodeError) as e:
        raise ReviewError("nie mogę odczytać %s: %s" % (path, e))
    for n, r in enumerate(rows, 2):
        if not r["id"].isdigit():
            raise ReviewError("%s, wiersz %d: złe id %r" % (path, n, r["id"]))
    return rows


def save(path, rows):
    d = os.path.dirname(path)
    tmp = path + ".tmp"
    try:
        if d:
            os.makedirs(d, exist_ok=True)
        with open(tmp, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, FIELDS, lineterminator="\n")
            w.writeheader()
            w.writerows(rows)
        os.replace(tmp, path)
    except OSError as e:
        raise ReviewError("nie mogę zapisać %s: %s (czy plik jest otwarty w Excelu?)" % (path, e))


# ---------------------------------------------------------------- mistakes

def named_mistakes(items):
    out = []
    for text in items or []:
        for m in text.split(","):
            m = m.strip().lower()
            if m and m not in MISTAKES:
                raise ReviewError("--blad: nie znam %r. Do wyboru: %s" % (m, ", ".join(MISTAKES)))
            out.append(m)
    return out


def plan_mistakes(setup, entry, stop, target, size, cfg):
    """What is already wrong when the trade is opened."""
    out = []
    if setup.strip().lower() in NO_SETUP:
        out.append("bez-sygnalu")
    if (entry - stop) * size > cfg["max_loss_per_trade_usd"]:
        out.append("za-duze-ryzyko")
    if (target - entry) / (entry - stop) < cfg["min_reward_risk"]:
        out.append("slaby-cel")
    return out


def exit_mistakes(r_value, opened, closed):
    out = []
    if r_value < BROKEN_STOP_R:
        out.append("zlamany-stop")
    if closed > opened:
        out.append("przez-noc")
    return out


def merge(*lists):
    out = []
    for items in lists:
        for m in items:
            if m and m not in out:
                out.append(m)
    return out


# ---------------------------------------------------------------- summary

def closed_between(rows, start, end):
    """Closed trades with an exit in [start, end], in exit order, numbers recomputed from the prices."""
    out = []
    for r in rows:
        if not r["wyjscie"].strip():
            continue
        what = "transakcja %s" % r["id"]
        day = parse_date(r["data_wyjscia"], what)
        if not start <= day <= end:
            continue
        e, s = number(r["wejscie"], what), number(r["stop"], what)
        if s >= e:
            raise ReviewError("%s: stop nie jest poniżej wejścia" % what)
        usd, rr = result(e, s, number(r["wyjscie"], what), number(r["wielkosc"], what))
        out.append({"id": int(r["id"]), "day": day, "usd": usd, "r": rr,
                    "plan": r["plan"].strip().lower(),
                    "mistakes": [m for m in r["bledy"].split("|") if m]})
    return sorted(out, key=lambda t: (t["day"], t["id"]))


def max_drawdown(values):
    total = peak = worst = Decimal(0)
    for v in values:
        total += v
        peak = max(peak, total)
        worst = max(worst, peak - total)
    return worst


def mean(values):
    return sum(values, Decimal(0)) / len(values) if values else None


def top_mistakes(trades, n=3):
    """[(code, count, summed result $)], most frequent first; a tie goes to the costlier one."""
    tally = {}
    for t in trades:
        for m in t["mistakes"]:
            c, usd = tally.get(m, (0, Decimal(0)))
            tally[m] = (c + 1, usd + t["usd"])
    rows = [(m, c, usd) for m, (c, usd) in tally.items()]
    return sorted(rows, key=lambda x: (-x[1], x[2], x[0]))[:n]


def summarize(trades):
    wins = [t["usd"] for t in trades if t["usd"] > 0]
    losses = [t["usd"] for t in trades if t["usd"] < 0]
    n = len(trades)
    return {"n": n, "wins": len(wins), "losses": len(losses), "even": n - len(wins) - len(losses),
            "win_pct": 100.0 * len(wins) / n if n else None,
            "total": sum((t["usd"] for t in trades), Decimal(0)),
            "avg_win": mean(wins), "avg_loss": mean(losses),
            "exp_usd": mean([t["usd"] for t in trades]), "exp_r": mean([t["r"] for t in trades]),
            "drawdown": max_drawdown([t["usd"] for t in trades]),
            "on_plan": sum(1 for t in trades if t["plan"] == "tak"),
            "mistakes": top_mistakes(trades)}


# ---------------------------------------------------------------- SPY from Yahoo

def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def yahoo_get(path):
    """query2 first, query1 on an error, and one more round after BACKOFF if a 429 was seen."""
    last = "no answer"
    for attempt in (0, 1):
        saw_429 = False
        for host in YAHOO_HOSTS:
            try:
                return http_get(host + path)
            except urllib.error.HTTPError as e:
                last = "HTTP %d" % e.code
                saw_429 = saw_429 or e.code == 429
                e.close()
                if 400 <= e.code < 500 and e.code != 429:
                    raise ReviewError("Yahoo: %s" % last)
            except (urllib.error.URLError, OSError) as e:
                last = str(getattr(e, "reason", e))
        if attempt == 0 and saw_429:
            SLEEP(BACKOFF)
            continue
        break
    raise ReviewError("Yahoo: %s" % last)


def spy_path(start, end):
    """Daily bars from 10 days before the period (for the close before it) to its end."""
    def ts(d):
        return int((datetime.datetime.combine(d, datetime.time()) - EPOCH).total_seconds())
    return "/v8/finance/chart/SPY?period1=%d&period2=%d&interval=1d" % (
        ts(start - datetime.timedelta(days=10)), ts(end + datetime.timedelta(days=2)))


def parse_daily(text):
    """Yahoo v8 daily chart -> [(date, adjusted close)]."""
    try:
        res = json.loads(text)["chart"]["result"][0]
        ind = res["indicators"]
        closes = ind["quote"][0]["close"]
        adj = (ind.get("adjclose") or [{}])[0].get("adjclose") or closes
        out = []
        for t, c in zip(res["timestamp"], adj):
            if c is not None:
                day = to_eastern(EPOCH + datetime.timedelta(seconds=int(t))).date()
                out.append((day, Decimal(repr(c))))
    except (ValueError, KeyError, IndexError, TypeError, AttributeError, InvalidOperation):
        raise ReviewError("Yahoo zwrócił dane SPY, których nie rozumiem")
    return sorted(out)


def spy_return(bars, start, end):
    """(return in %, base date, last date) for buy & hold over [start, end]."""
    before = [b for b in bars if b[0] < start]
    inside = [b for b in bars if start <= b[0] <= end]
    if not before or not inside:
        raise ReviewError("brak notowań SPY w tym okresie")
    base, last = before[-1], inside[-1]
    return (last[1] / base[1] - 1) * 100, base[0], last[0]


def get_spy(start, end, offline=None):
    if offline:
        try:
            with open(offline, encoding="utf-8") as fh:
                text = fh.read()
        except OSError as e:
            raise ReviewError("nie mogę odczytać %s: %s" % (offline, e))
    else:
        text = yahoo_get(spy_path(start, end))
    return spy_return(parse_daily(text), start, end)


# ---------------------------------------------------------------- commands

def cmd_add(a):
    cfg = read_config(a.config)
    if cfg.get("paper") is not True:
        raise ReviewError("\"paper\" w %s nie jest true: ten dziennik jest tylko do papieru" % a.config)
    ticker = a.ticker.strip().upper()
    if not re.match(r"^[A-Z][A-Z0-9.\-]{0,9}$", ticker):
        raise ReviewError("--ticker: to nie wygląda na symbol: %r" % a.ticker)
    today = today_ny(a.now)
    day = parse_date(a.data, "--data") if a.data else today
    if day > today:
        raise ReviewError("data %s jest w przyszłości (w Nowym Jorku jest %s)" % (day, today))
    e, s, t = number(a.wejscie, "wejście"), number(a.stop, "stop"), number(a.cel, "cel")
    size = number(a.wielkosc, "wielkość")
    if s >= e:
        raise ReviewError("stop %s musi być poniżej wejścia %s (tylko pozycje długie)" % (money(s), money(e)))
    if t <= e:
        raise ReviewError("cel %s musi być powyżej wejścia %s" % (money(t), money(e)))
    rows = load(a.journal)
    errs = merge(plan_mistakes(a.setup, e, s, t, size, cfg), named_mistakes(a.blad))
    row = dict.fromkeys(FIELDS, "")
    row.update(id=str(max([int(r["id"]) for r in rows] or [0]) + 1), data=day.isoformat(), ticker=ticker,
               setup=a.setup.strip() or "brak", wejscie=str(e), stop=str(s), cel=str(t), wielkosc=str(size),
               notatka=a.notatka, bledy="|".join(errs))
    save(a.journal, rows + [row])
    print("Dodano transakcję nr %s (papier): %s %s szt. po %s, stop %s, cel %s." % (
        row["id"], ticker, size, money(e), money(s), money(t)))
    print("Ryzyko przy stopie: %s. Zysk do ryzyka: %.2f." % (money((e - s) * size), (t - e) / (e - s)))
    for m in errs:
        print("Uwaga: %s." % MISTAKES[m])
    print("Nic nie zostało zlecone.")
    return 0


def find_open(rows, a):
    if a.id is not None:
        hits = [r for r in rows if int(r["id"]) == a.id]
        if not hits:
            raise ReviewError("nie ma transakcji nr %d" % a.id)
        if hits[0]["wyjscie"].strip():
            raise ReviewError("transakcja nr %d jest już zamknięta" % a.id)
        return hits[0]
    if not a.ticker:
        raise ReviewError("podaj --id albo --ticker")
    sym = a.ticker.strip().upper()
    hits = [r for r in rows if r["ticker"] == sym and not r["wyjscie"].strip()]
    if not hits:
        raise ReviewError("nie ma otwartej transakcji na %s" % sym)
    if len(hits) > 1:
        raise ReviewError("%s ma kilka otwartych transakcji (nr %s): podaj --id" % (
            sym, ", ".join(r["id"] for r in hits)))
    return hits[0]


def cmd_close(a):
    rows = load(a.journal)
    row = find_open(rows, a)
    what = "transakcja %s" % row["id"]
    x = number(a.wyjscie, "wyjście")
    e, s, size = number(row["wejscie"], what), number(row["stop"], what), number(row["wielkosc"], what)
    if s >= e:
        raise ReviewError("%s: stop nie jest poniżej wejścia" % what)
    opened = parse_date(row["data"], what)
    today = today_ny(a.now)
    day = parse_date(a.data, "--data") if a.data else today
    if day < opened:
        raise ReviewError("wyjście %s jest przed wejściem %s" % (day, opened))
    if day > today:
        raise ReviewError("data %s jest w przyszłości (w Nowym Jorku jest %s)" % (day, today))
    usd, rr = result(e, s, x, size)
    errs = merge(row["bledy"].split("|"), exit_mistakes(rr, opened, day), named_mistakes(a.blad))
    if a.plan == "nie" and not errs:
        errs = ["inny-blad"]
    row.update(wyjscie=str(x), wynik_usd=str(cents(usd)), wynik_r=str(cents(rr)), plan=a.plan,
               data_wyjscia=day.isoformat(), bledy="|".join(errs))
    if a.notatka:
        row["notatka"] = (row["notatka"] + " / " if row["notatka"] else "") + a.notatka
    save(a.journal, rows)
    print("Zamknięto transakcję nr %s (%s): wynik %s, %s." % (row["id"], row["ticker"], money(usd), signed_r(rr)))
    for m in errs:
        print("Błąd: %s." % MISTAKES.get(m, m))
    if a.plan == "tak" and errs:
        print("Uwaga: zapisano \"zgodnie z planem\", ale są też błędy. Sprawdź, czy to na pewno prawda.")
    return 0


def cmd_list(a):
    rows = load(a.journal)
    if a.otwarte:
        rows = [r for r in rows if not r["wyjscie"].strip()]
    if not rows:
        print("Brak transakcji.")
        return 0
    for r in rows:
        end = "otwarta" if not r["wyjscie"].strip() else "wyjście %s, wynik %s USD, %sR, plan: %s" % (
            r["wyjscie"], r["wynik_usd"], r["wynik_r"], r["plan"])
        print("%s. %s %s [%s] %s szt. wejście %s, stop %s, cel %s: %s" % (
            r["id"], r["data"], r["ticker"], r["setup"], r["wielkosc"], r["wejscie"], r["stop"], r["cel"], end))
    return 0


def period(a):
    if bool(a.od) != bool(a.do):
        raise ReviewError("podaj razem --od i --do")
    if a.od:
        start, end = parse_date(a.od, "--od"), parse_date(a.do, "--do")
        if end < start:
            raise ReviewError("--do jest przed --od")
        return start, end
    return week_of(parse_date(a.data, "--data") if a.data else today_ny(a.now))


def cmd_week(a):
    budget = read_config(a.config)["budget"]
    start, end = period(a)
    rows = load(a.journal)
    s = summarize(closed_between(rows, start, end))
    n_open = sum(1 for r in rows if not r["wyjscie"].strip())
    print("PODSUMOWANIE %s do %s (papier)" % (start, end))
    print("  Transakcje zamknięte     %d (otwartych teraz: %d)" % (s["n"], n_open))
    if s["n"]:
        print("  Wygrane                  %.1f%% (%d wygr., %d przegr., %d na zero)" % (
            s["win_pct"], s["wins"], s["losses"], s["even"]))
        print("  Wynik razem              %s" % money(s["total"]))
        print("  Średni zysk              %s" % (money(s["avg_win"]) if s["wins"] else "brak"))
        print("  Średnia strata           %s" % (money(s["avg_loss"]) if s["losses"] else "brak"))
        print("  Wartość oczekiwana       %s na transakcję (%s)" % (money(s["exp_usd"]), signed_r(s["exp_r"])))
        print("  Maks. obsunięcie         %s (%.2f%% budżetu)" % (money(s["drawdown"]), s["drawdown"] / budget * 100))
        print("  Zgodnie z planem         %d z %d" % (s["on_plan"], s["n"]))
    try:
        pct, base, last = get_spy(start, end, a.offline)
        mine = s["total"] / budget * 100
        print("  Twój wynik               %+.2f%% (na budżecie %s)" % (mine, money(budget)))
        print("  SPY kup i trzymaj        %+.2f%% (%s, zamknięcie %s do notowania %s)" % (
            pct, money(budget * pct / 100), base, last))
        print("  Różnica                  %+.2f pkt proc." % (mine - pct))
    except ReviewError as e:
        print("  SPY: nie udało się porównać (%s)" % e)
    print("Najczęstsze błędy:")
    for i, (m, c, usd) in enumerate(s["mistakes"], 1):
        print("  %d. %s: %dx, łączny wynik tych transakcji %s" % (i, MISTAKES.get(m, m), c, money(usd)))
    if not s["mistakes"]:
        print("  brak zapisanych błędów")
    if s["n"] < SMALL_SAMPLE:
        print("Mniej niż %d transakcji: to za mało, żeby odróżnić umiejętność od szczęścia." % SMALL_SAMPLE)
    print("Bez prowizji i podatków. To nie jest porada inwestycyjna.")
    return 0


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Trade Review (trader-review): paper journal and weekly summary.")
    sub = ap.add_subparsers(dest="cmd")

    def command(name, alias, func, text):
        p = sub.add_parser(name, aliases=[alias], help=text)
        p.set_defaults(func=func)
        p.add_argument("--journal", default=JOURNAL)
        p.add_argument("--now", help="pretend today (New York) is YYYY-MM-DD")
        return p

    p = command("dodaj", "add", cmd_add, "dodaj transakcję")
    p.add_argument("--ticker", required=True)
    p.add_argument("--setup", required=True, help="np. orb, level; \"brak\" = wejście bez sygnału")
    for k in ("wejscie", "stop", "cel", "wielkosc"):
        p.add_argument("--" + k, required=True)
    p.add_argument("--data", help="data wejścia YYYY-MM-DD (domyślnie dziś w Nowym Jorku)")
    p.add_argument("--notatka", default="")
    p.add_argument("--blad", action="append", help=", ".join(MISTAKES))
    p.add_argument("--config", default="trader/config.json")

    p = command("zamknij", "close", cmd_close, "zamknij transakcję")
    p.add_argument("--id", type=int)
    p.add_argument("--ticker")
    p.add_argument("--wyjscie", required=True)
    p.add_argument("--plan", required=True, choices=["tak", "nie"], help="czy trzymano się planu")
    p.add_argument("--data", help="data wyjścia YYYY-MM-DD (domyślnie dziś w Nowym Jorku)")
    p.add_argument("--notatka", default="")
    p.add_argument("--blad", action="append", help=", ".join(MISTAKES))

    p = command("lista", "list", cmd_list, "pokaż dziennik")
    p.add_argument("--otwarte", action="store_true")

    p = command("tydzien", "week", cmd_week, "podsumowanie tygodnia")
    p.add_argument("--data", help="dowolny dzień tygodnia, o który chodzi")
    p.add_argument("--od")
    p.add_argument("--do")
    p.add_argument("--offline", metavar="PATH", help="saved Yahoo chart JSON for SPY")
    p.add_argument("--config", default="trader/config.json")

    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return a.func(a)
    except ReviewError as e:
        print("review.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
