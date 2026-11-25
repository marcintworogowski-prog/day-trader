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
"""

import argparse
import concurrent.futures
import contextlib
import datetime
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
        self.no_cash = []
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
        """(portfolio, today's state) for trader-risk, from every row of the journal."""
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
                placed.append({"status": "placed", "date": r["data"], "symbol": r["ticker"], "side": "buy",
                               "kind": "entry", "max_loss": float((e - s) * n)})
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

    def can_enter(self):
        """None, or why no entry is possible now (then there is nothing to scan)."""
        _, state = self.account()
        for key in ("max_trades_per_day", "max_orders_per_day"):
            cap = int(self.cfg.get(key) or 0)
            if cap and len(state["orders"]) >= cap:
                return "limit transakcji na dziś"
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
            s = sizer.size_trade(plan, self.cfg, price=float(fill), cash=portfolio["cash"],
                                 today=self.today.isoformat())
        except (exitp.PlanError, sizer.SizeError) as e:
            self.log("POMINIĘTO %s: %s" % (sym, e))
            return
        order = sizer.bracket_order(s)
        if order is None:
            if s["bound_by"] == "cash" and s["qty"] == 0:
                self.no_cash.append(sym)     # one line for all of them, in step()
            else:
                self.log("POMINIĘTO %s: %s" % (sym, s["refusal"]))
            return
        got = risk.check([order], self.cfg, portfolio, {sym: float(fill)}, state, now=self.now,
                         price_times={sym: known})["results"][0]
        if got["verdict"] != "PASS":
            self.log("BLOCK %s: %s" % (sym, "; ".join(got["reasons"])))
            return
        if fill <= Decimal(order["stop_loss_stop_price"]):
            self.log("POMINIĘTO %s: cena jest już przy stopie" % sym)
            return
        self.call_review(["dodaj", "--ticker", sym, "--setup", "orb", "--wejscie", str(fill),
                          "--stop", order["stop_loss_stop_price"], "--cel", order["take_profit_limit_price"],
                          "--wielkosc", order["qty"], "--config", self.cfg_path,
                          "--notatka", "%s sygnał %s" % (BOT_TAG, trig["time"])])
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
        if self.no_cash:
            self.log("POMINIĘTO, brak wolnej gotówki (%d): %s" % (len(self.no_cash), ", ".join(self.no_cash)))
        self.log("KROK otwarte: %d, świeże sygnały: %d, błędy danych: %d%s" % (
            len(self.positions()), self.signals, len(self.errors), " (%s)" % why if why else ""))
        for e in self.errors[:5]:
            print("  dane: " + e)
        return 1 if self.failed else 0


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
        return Bot(root, cfg, a.limits, read_universe(a.universe), now, bool(a.now), a.offline).step()
    except (BotError, entry.EntryError, risk.RiskError, review.ReviewError, OSError) as e:
        print("paper_bot.py: %s" % e, file=sys.stderr)
        return 1
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
