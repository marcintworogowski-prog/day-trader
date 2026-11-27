#!/usr/bin/env python3
"""Paper bot: one run = one step. Standard library only. Paper only: it never
talks to a broker, needs no key and does not use SEC data.

    python bot/paper_bot.py                                      # one live step
    python bot/paper_bot.py --dry-run                            # show, write nothing
    python bot/paper_bot.py --dry-run --now "2026-11-24 10:20"   # pretend it is this New York time

The rules are the skills' own code: trader-entry (15-minute opening-range
breakout on 5-minute bars, NYSE calendar 2026-2027), trader-exit (stop at the
range midpoint, 2R target), trader-budget (size), trader-risk (PASS or BLOCK)
and trader-review (the journal, which is also the bot's state).

Fills are the last price, 0.05% against us, rounded to the cent against us.
Stops and targets are checked on the last price at each step, so a late step
can fill well past a stop. A position still open after the close (a missed
step) is closed at that session's last price.
Every step also draws journal/dashboard.svg.
"""

import argparse
import concurrent.futures
import contextlib
import datetime
import hashlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import dashboard  # noqa: E402

SKILLS = os.path.join(PROJECT, ".claude", "skills")


def _load(folder, name):
    spec = importlib.util.spec_from_file_location("bot_" + name, os.path.join(SKILLS, folder, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


entry = _load("trader-entry", "entry")
exitp = _load("trader-exit", "exit")
sizer = _load("trader-budget", "size")
risk = _load("trader-risk", "check")
review = _load("trader-review", "review")

BOT_TAG = "[bot]"
BOT_START = datetime.date(2026, 11, 25)      # the panel and the weekly report start here
SLIPPAGE = Decimal("0.0005")
FLAT_MINUTES = 10                # flat this long before the close
MAX_SIGNAL_MINUTES = 20          # an older breakout is not chased
MAX_PRICE_MINUTES = 15           # an older price is not traded on
STEP = 300
WORKERS = 4


class BotError(Exception):
    pass


# ---------------------------------------------------------------- prices

def buy_fill(price):
    return (Decimal(repr(float(price))) * (1 + SLIPPAGE)).quantize(Decimal("0.01"), rounding=ROUND_CEILING)


def sell_fill(price):
    return (Decimal(repr(float(price))) * (1 - SLIPPAGE)).quantize(Decimal("0.01"), rounding=ROUND_FLOOR)


def last_price(chart, day, now_utc, simulated):
    """(price, naive UTC time it was known) inside the session of `day`, or None.
    In a simulation a bar that is still forming would show the future, so it is skipped."""
    o, c = entry.session(day)
    now_ts = entry.ts_of(now_utc)
    best = None
    for b in chart["bars"]:
        et = entry.to_eastern(entry.utc_of(b["t"]))
        if et.date() != day or not o <= et.time() <= c or b["t"] > now_ts:
            continue
        known = b["t"] + (STEP if b["t"] % STEP == 0 and et.time() < c else 0)
        if known > now_ts:
            if simulated:
                continue
            known = now_ts
        if best is None or b["t"] >= best[0]:
            best = (b["t"], b["c"], known)
    return (best[1], entry.utc_of(best[2])) if best else None


def get_charts(symbols, day, today, offline):
    """({SYM: chart}, [error text])."""
    charts, errors = {}, []

    def one(sym):
        return entry.get_chart(sym, "5m", day=day, offline=offline, today=today)
    with concurrent.futures.ThreadPoolExecutor(WORKERS) as pool:
        jobs = [(s, pool.submit(one, s)) for s in symbols]
        for sym, job in jobs:
            try:
                charts[sym] = job.result()
            except (entry.EntryError, OSError, ValueError, KeyError, TypeError) as e:
                errors.append("%s: %s" % (sym, e))
    return charts, errors


# ---------------------------------------------------------------- files

def read_universe(path):
    with open(path, encoding="utf-8") as fh:
        rows = [ln.split("#")[0].strip().upper() for ln in fh]
    return list(dict.fromkeys(r for r in rows if r))


def read_limits(path):
    try:
        with open(path, encoding="utf-8-sig") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError) as e:
        raise BotError("nie mogę odczytać %s: %s" % (path, e))
    if not isinstance(cfg, dict) or cfg.get("paper") is not True:
        raise BotError("\"paper\" w %s musi być true: ten bot działa tylko na papierze" % path)
    return cfg


class Bot(object):
    def __init__(self, root, cfg, cfg_path, universe, now_utc, simulated=False, offline=None):
        self.root, self.cfg, self.cfg_path, self.universe = root, cfg, cfg_path, universe
        self.now, self.simulated, self.offline = now_utc, simulated, offline
        self.local = entry.to_eastern(now_utc)
        self.today = self.local.date()
        self.journal = os.path.join(root, "journal", "trades.csv")
        self.signals = 0
        self.errors = []
        self.too_small = []
        self.day_loss_blocked = []
        self.prices = {}                 # {SYM: last price}, for the panel
        self.status, self.reason = "GRA", ""
        self.failed = False

    # ------------------------------------------------------------ journal

    def log(self, text):
        line = "%s %s" % (self.local.strftime("%H:%M"), text)
        print(line)
        path = os.path.join(self.root, "journal", "log", self.today.isoformat() + ".txt")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")

    def rows(self):
        return review.load(self.journal)

    def positions(self):
        return [r for r in self.rows() if not r["wyjscie"].strip() and r["notatka"].startswith(BOT_TAG)]

    def call_review(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = review.main(argv + ["--journal", self.journal, "--now", self.today.isoformat()])
        if code:
            raise BotError(err.getvalue().strip() or "trader-review odmówił zapisu")

    def account(self):
        """(portfolio, today's state) for trader-risk, from every row of the journal.
        Each of today's entries carries its REALIZED loss as max_loss (0 while open, minus the result
        once closed), so trader-risk's daily-loss rule counts the money lost today, not the risk taken."""
        cash = Decimal(str(self.cfg["budget"]))
        held, placed = [], []
        for r in self.rows():
            what = "transakcja %s" % r["id"]
            e, s, n = (review.number(r[k], what) for k in ("wejscie", "stop", "wielkosc"))
            if r["wyjscie"].strip():
                cash += (review.number(r["wyjscie"], what) - e) * n
            else:
                cash -= e * n
                held.append({"ticker": r["ticker"], "lots": [{"shares": float(n), "cost": float(e * n)}]})
            if r["data"] == self.today.isoformat():
                lost = (e - review.number(r["wyjscie"], what)) * n if r["wyjscie"].strip() else Decimal(0)
                placed.append({"status": "placed", "date": r["data"], "symbol": r["ticker"], "side": "buy",
                               "kind": "entry", "max_loss": float(lost)})
        return {"cash": float(cash), "positions": held}, {"orders": placed}

    # ------------------------------------------------------------ exits

    def close(self, row, price, why, day):
        fill = sell_fill(price)
        self.call_review(["zamknij", "--id", row["id"], "--wyjscie", str(fill), "--plan", "tak",
                          "--data", day.isoformat(), "--notatka", why])
        done = [r for r in self.rows() if r["id"] == row["id"]][0]
        self.log("WYJŚCIE %s %s szt. po %s (%s), wynik %s USD, %sR" % (
            row["ticker"], row["wielkosc"], fill, why, done["wynik_usd"], done["wynik_r"]))

    def check_position(self, row, close_dt):
        day = review.parse_date(row["data"], "transakcja %s" % row["id"])
        over = day < self.today or self.local >= close_dt
        charts, errors = get_charts([row["ticker"]], day, self.today, self.offline)
        got = last_price(charts[row["ticker"]], day, self.now, self.simulated) if charts else None
        if got is None:
            self.failed = True
            self.log("BŁĄD brak ceny %s, pozycja zostaje otwarta (%s)" % (
                row["ticker"], "; ".join(errors) or "brak notowań"))
            return
        price, known = got
        self.prices[row["ticker"]] = price
        if over:
            self.close(row, price, "zamknięcie po sesji, ostatnia cena sesji", day)
            return
        age = (self.now - known).total_seconds() / 60.0
        if age > MAX_PRICE_MINUTES:
            self.failed = True
            self.log("BŁĄD stara cena %s (%d min), czekam" % (row["ticker"], age))
            return
        p = Decimal(repr(float(price)))
        if p <= review.number(row["stop"], "stop"):
            self.close(row, price, "stop", day)
        elif p >= review.number(row["cel"], "cel"):
            self.close(row, price, "cel", day)
        elif self.local >= close_dt - datetime.timedelta(minutes=FLAT_MINUTES):
            self.close(row, price, "koniec sesji", day)

    # ------------------------------------------------------------ entries

    def realized(self, today_only):
        """Result in USD of the closed trades (today's entries only, or all of them)."""
        total = Decimal(0)
        for r in self.rows():
            if r["wyjscie"].strip() and (not today_only or r["data"] == self.today.isoformat()):
                what = "transakcja %s" % r["id"]
                total += (review.number(r["wyjscie"], what) - review.number(r["wejscie"], what)) *                     review.number(r["wielkosc"], what)
        return total

    def can_enter(self):
        """None, or why no entry is possible now (then there is nothing to scan)."""
        _, state = self.account()
        daily = Decimal(str(self.cfg.get("max_daily_loss_usd") or 0))
        if daily and -self.realized(True) >= daily:
            return "limit straty dziennej"
        for key in ("max_trades_per_day", "max_orders_per_day"):
            cap = int(self.cfg.get(key) or 0)
            if cap and len(state["orders"]) >= cap:
                return "limit transakcji na dziś"
        cap = int(self.cfg.get("max_open_positions") or 0)
        if cap and len(self.positions()) >= cap:
            return "limit otwartych pozycji"
        reasons, _ = risk.window_check(self.local, dict(risk.DEFAULTS, **self.cfg))
        return "poza oknem wejść" if reasons else None

    def find_signals(self):
        """Fresh breakouts, the highest volume first: [(result, price, time)]."""
        taken = {r["ticker"] for r in self.rows()
                 if r["data"] == self.today.isoformat() or not r["wyjscie"].strip()}
        symbols = [s for s in self.universe if s not in taken]
        charts, self.errors = get_charts(symbols, self.today, self.today, self.offline)
        if symbols and len(charts) < len(symbols) / 2.0:
            self.failed = True
        found = []
        for sym, ch in charts.items():
            res = entry.evaluate(ch, self.now, cfg=dict(entry.DEFAULTS, **self.cfg))
            if res["status"] != "triggered" or res["minutes_since"] > MAX_SIGNAL_MINUTES:
                continue
            if res["opening_range"]["bars"] < res["opening_range"]["of"]:
                continue                     # an incomplete range is not a range
            got = last_price(ch, self.today, self.now, self.simulated)
            if got is None or (self.now - got[1]).total_seconds() > MAX_PRICE_MINUTES * 60:
                continue
            if got[0] <= res["level"]:
                continue                     # already back inside the range
            found.append((res, got[0], got[1]))
        found.sort(key=lambda x: (-(x[0]["trigger"]["volume_x"] or 0), x[0]["symbol"]))
        self.signals = len(found)
        return found

    def try_enter(self, res, price, known):
        sym, orr, trig = res["symbol"].replace("-", "."), res["opening_range"], res["trigger"]
        fill = buy_fill(price)
        portfolio, state = self.account()
        try:
            c = exitp.compute(trig["price"], "or_mid", None, "r", Decimal(2), orr["high"], orr["low"])
            plan = {"symbol": sym, "side": "buy", "date": res["date"], "setup": "orb", "entry": float(c["entry"]),
                    "stop": float(c["stop"]), "target": float(c["target"]), "entry_time": trig["time"]}
            # the journal records `fill`, so a fill under the plan's entry is sized at the plan's (higher) entry
            # the position cap is a share of the budget, or of a smaller account after losses
            # (trader-risk measures it on the account, so both agree)
            budget = min(float(self.cfg["budget"]), float(self.cfg["budget"]) + float(self.realized(False)))
            s = sizer.size_trade(plan, self.cfg, budget=budget, price=float(fill), cash=portfolio["cash"],
                                 today=self.today.isoformat())
        except (exitp.PlanError, sizer.SizeError) as e:
            self.log("POMINIĘTO %s: %s" % (sym, e))
            return
        order = sizer.bracket_order(s)
        if order is None:
            small = s["qty"] > 0 and s["cost"] < float(self.cfg.get("min_order_usd") or 0)
            if small or (s["bound_by"] == "cash" and s["qty"] == 0):
                self.too_small.append(sym)     # one line for all of them, in step()
            else:
                self.log("POMINIĘTO %s: %s" % (sym, s["refusal"]))
            return
        if fill * int(order["qty"]) < Decimal(str(self.cfg.get("min_order_usd") or 0)):
            self.too_small.append(sym)
            return
        got = risk.check([order], self.cfg, portfolio, {sym: float(fill)}, state, now=self.now,
                         price_times={sym: known})["results"][0]
        if got["verdict"] != "PASS":
            if all(r.startswith("daily loss") for r in got["reasons"]):
                self.day_loss_blocked.append(sym)     # one line for all of them, in step()
                return
            self.log("BLOCK %s: %s" % (sym, "; ".join(got["reasons"])))
            return
        if fill <= Decimal(order["stop_loss_stop_price"]):
            self.log("POMINIĘTO %s: cena jest już przy stopie" % sym)
            return
        self.call_review(["dodaj", "--ticker", sym, "--setup", "orb", "--wejscie", str(fill),
                          "--stop", order["stop_loss_stop_price"], "--cel", order["take_profit_limit_price"],
                          "--wielkosc", order["qty"], "--config", self.cfg_path,
                          "--notatka", "%s sygnał %s" % (BOT_TAG, trig["time"])])
        self.prices[sym] = price
        self.log("WEJŚCIE %s %s szt. po %s, stop %s, cel %s, ryzyko %.2f USD" % (
            sym, order["qty"], fill, order["stop_loss_stop_price"], order["take_profit_limit_price"],
            (float(fill) - s["stop"]) * s["qty"]))

    # ------------------------------------------------------------ one step

    def step(self):
        """0, or 1 when data was missing."""
        if self.today.year not in entry.CALENDAR_YEARS:
            raise BotError("kalendarz NYSE jest tylko na lata 2026-2027: uzupełnij go na %d" % self.today.year)
        hours = entry.session(self.today) if entry.trading_day(self.today)[0] else None
        midnight = datetime.datetime.combine(self.today, datetime.time())
        opens = datetime.datetime.combine(self.today, hours[0]) if hours else midnight
        closes = datetime.datetime.combine(self.today, hours[1]) if hours else midnight
        in_session = opens <= self.local < closes
        held = self.positions()
        if not in_session:
            self.status, self.reason = "POZA SESJĄ", self.session_note(hours)
        if not in_session and not held:
            print("Poza sesją NYSE (%s w Nowym Jorku). Brak transakcji, kończę." %
                  self.local.strftime("%Y-%m-%d %H:%M"))
            return 0
        for row in held:
            self.check_position(row, closes)
        if not in_session:
            return 1 if self.failed else 0
        why = self.can_enter()
        if why is None:
            for res, price, known in self.find_signals():
                if self.can_enter():
                    break
                self.try_enter(res, price, known)
        self.reason = why or "szukam wybić, świeże sygnały: %d" % self.signals
        if self.too_small:
            self.log("POMINIĘTO, za mała pozycja lub brak gotówki (%d): %s" % (
                len(self.too_small), ", ".join(self.too_small)))
        if self.day_loss_blocked:
            self.log("BLOCK, ryzyko ponad limit straty dziennej (%d): %s" % (
                len(self.day_loss_blocked), ", ".join(self.day_loss_blocked)))
        self.log("KROK otwarte: %d, świeże sygnały: %d, błędy danych: %d%s" % (
            len(self.positions()), self.signals, len(self.errors), " (%s)" % why if why else ""))
        for e in self.errors[:5]:
            print("  dane: " + e)
        return 1 if self.failed else 0


    # ------------------------------------------------------------ the panel

    def session_note(self, hours):
        if not hours:
            return "giełda dziś zamknięta"
        if self.local.time() < hours[0]:
            return "sesja od %s w Nowym Jorku" % hours[0].strftime("%H:%M")
        return "sesja zakończona"

    def spy_by_day(self, days):
        """Buy & hold SPY worth the budget on days[0], one value per day, or [] without data."""
        try:
            if self.offline:
                with open(os.path.join(self.offline, "SPY-daily.json"), encoding="utf-8") as fh:
                    text = fh.read()
            else:
                text = review.yahoo_get(review.spy_path(days[0], days[-1]))
            closes = {d: float(c) for d, c in review.parse_daily(text)}
        except (review.ReviewError, OSError) as e:
            print("  SPY: %s" % e)
            return []
        base = closes.get(days[0])
        if not base:
            return []
        return [float(self.cfg["budget"]) * closes[d] / base if d in closes else None for d in days]

    def panel_data(self):
        try:
            rows = self.rows()
            review.closed_between(rows, datetime.date.min, datetime.date.max)     # every number is readable
        except review.ReviewError as e:
            rows, self.failed, self.reason = [], True, str(e)
        budget, what = float(self.cfg["budget"]), "dziennik"
        num = {r["id"]: [float(review.number(r[k], what)) for k in ("wejscie", "stop", "cel", "wielkosc")]
               for r in rows}
        shut = [r for r in rows if r["wyjscie"].strip()]
        held = [r for r in rows if not r["wyjscie"].strip()]
        missing = sorted({r["ticker"] for r in held} - set(self.prices))
        if missing and entry.session(self.today):
            charts, _ = get_charts(missing, self.today, self.today, self.offline)
            for sym, ch in charts.items():
                got = last_price(ch, self.today, self.now, self.simulated)
                if got:
                    self.prices[sym] = got[0]

        def gain(r):
            e, _, _, n = num[r["id"]]
            return (float(review.number(r["wyjscie"], what)) - e) * n
        opened, floating = [], 0.0
        for r in held:
            e, s, t, n = num[r["id"]]
            last = self.prices.get(r["ticker"])
            res = None if last is None else (last - e) * n
            floating += res or 0.0
            opened.append({"ticker": r["ticker"], "entry": e, "stop": s, "target": t, "last": last, "result": res})
        done = [r for r in shut if r["data_wyjscia"] == self.today.isoformat()]
        start = min(BOT_START, self.today)
        base = start - datetime.timedelta(days=1)
        while not entry.session(base):
            base -= datetime.timedelta(days=1)
        days, d = [base], start
        while d <= self.today:
            s = entry.session(d)
            if s and self.local >= datetime.datetime.combine(d, s[0]):
                days.append(d)
            d += datetime.timedelta(days=1)
        account = [budget + sum(gain(r) for r in shut if r["data_wyjscia"] <= d.isoformat()) for d in days]
        account[-1] = budget + sum(gain(r) for r in shut) + floating
        return {"utc": self.now, "new_york": self.local, "status": "BŁĄD" if self.failed else self.status,
                "reason": self.reason or ("problem z danymi" if self.failed else ""), "budget": budget,
                "days": days, "account": [round(x, 2) for x in account], "spy": self.spy_by_day(days),
                "open": opened,
                "closed": [{"ticker": r["ticker"], "entry": num[r["id"]][0],
                            "exit": float(review.number(r["wyjscie"], what)),
                            "pct": 100.0 * gain(r) / (num[r["id"]][0] * num[r["id"]][3]),
                            "result": gain(r)} for r in done],
                "today": round(sum(gain(r) for r in done) + floating, 2),
                "total": round(account[-1] - budget, 2), "trades": len(rows)}

    def write_panel(self):
        """journal/dashboard.svg. Outside the session a picture that differs only by the clock is left
        alone, so the workflow does not commit every ten minutes."""
        d = self.panel_data()
        path = os.path.join(self.root, "journal", "dashboard.svg")
        key = json.dumps({k: v for k, v in d.items() if k not in ("utc", "new_york")}, sort_keys=True, default=str)
        mark = "<!-- %s %s -->\n" % (dashboard.to_poland(self.now).date(),
                                     hashlib.sha256(key.encode("utf-8")).hexdigest()[:16])
        if d["status"] != "GRA" and os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                if fh.read().endswith(mark):
                    return
        svg = dashboard.render(d)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w", encoding="utf-8", newline="\n") as fh:
            fh.write(svg + mark)
        os.replace(path + ".tmp", path)


def run_step(bot):
    """One step, then the panel, also after an error. Returns the exit code."""
    crashed = ""
    try:
        code = bot.step()
    except (BotError, entry.EntryError, risk.RiskError, review.ReviewError, OSError) as e:
        print("paper_bot.py: %s" % e, file=sys.stderr)
        crashed, bot.failed, code = str(e), True, 1
    if bot.failed:
        bot.reason = crashed or "problem z danymi (Yahoo)"
    try:
        bot.write_panel()
    except Exception as e:               # the picture must never stop the trading step
        print("paper_bot.py: panel: %s" % e, file=sys.stderr)
        code = 1
    return code


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Paper bot: one step of the opening-range strategy. Paper only.")
    ap.add_argument("--dry-run", action="store_true", help="show what it would do, write nothing")
    ap.add_argument("--now", help='pretend it is this New York time: "YYYY-MM-DD HH:MM"')
    ap.add_argument("--root", help="folder that holds journal/ (default: the project)")
    ap.add_argument("--limits", default=os.path.join(HERE, "limits.json"))
    ap.add_argument("--universe", default=os.path.join(HERE, "universe.txt"))
    ap.add_argument("--offline", metavar="DIR", help="saved Yahoo charts, SYM.json (tests)")
    a = ap.parse_args(argv)
    tmp = None
    try:
        if a.now and not a.dry_run and not a.root:
            raise BotError("--now działa tylko z --dry-run (albo z osobnym --root), żeby nie zaśmiecić dziennika")
        cfg = read_limits(a.limits)
        now = entry.parse_now(a.now)
        root = a.root or PROJECT
        if a.dry_run:
            tmp = tempfile.mkdtemp()
            src = os.path.join(root, "journal", "trades.csv")
            if os.path.exists(src):
                os.makedirs(os.path.join(tmp, "journal"))
                shutil.copy(src, os.path.join(tmp, "journal", "trades.csv"))
            root = tmp
            print("PRÓBA: nic nie zostanie zapisane.")
        return run_step(Bot(root, cfg, a.limits, read_universe(a.universe), now, bool(a.now), a.offline))
    except (BotError, entry.EntryError, risk.RiskError, review.ReviewError, OSError) as e:
        print("paper_bot.py: %s" % e, file=sys.stderr)
        return 1
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
