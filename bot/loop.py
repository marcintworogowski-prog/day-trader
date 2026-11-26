#!/usr/bin/env python3
"""Paper bot loop: one GitHub Actions run = many steps. Standard library only. Paper only.

    python bot/loop.py                                   # live: a step every 5 minutes, commits journal/
    python bot/loop.py --dry-run                         # same steps on a copy of journal/, no commit
    python bot/loop.py --dry-run --now "2026-11-25 15:30"  # replay from that New York time, no waiting

It starts with a step at once (a late schedule loses nothing), then runs a step on every
5-minute mark (+40 s, so the 5-minute bar is closed) until --max-minutes is used up or the
NYSE session is over and no bot position is open. The marks include the flat step 10 minutes
before the close (15:50, or 12:50 on an early close). The NYSE calendar (holidays, early
closes) is trader-entry's; on a closed day it does one step (it closes leftovers) and stops.

journal/ is committed right after a step that changed journal/trades.csv (a trade), and
otherwise at most every --commit-minutes, and once more at the end.
"""

import argparse
import datetime
import hashlib
import os
import subprocess
import sys
import shutil
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import paper_bot  # noqa: E402

entry = paper_bot.entry
review = paper_bot.review

EVERY = 5 * 60            # seconds between steps
OFFSET = 40               # seconds after the 5-minute mark
STEP_TIMEOUT = 270        # seconds one step may take
AFTER_CLOSE_TRIES = 3     # steps after the close while a position is still open


def next_slot(now, every=EVERY, offset=OFFSET):
    """The first mark + offset strictly after `now` (naive UTC)."""
    ts = entry.ts_of(now)
    slot = (ts - offset) // every * every + every + offset
    return entry.utc_of(slot)


def session_of(now):
    """(open, close) as naive New York datetimes for the New York day of `now`, or None."""
    day = entry.to_eastern(now).date()
    hours = entry.session(day)
    if not hours:
        return None
    return datetime.datetime.combine(day, hours[0]), datetime.datetime.combine(day, hours[1])


def open_positions(root):
    path = os.path.join(root, "journal", "trades.csv")
    if not os.path.exists(path):
        return []
    try:
        rows = review.load(path)
    except review.ReviewError as e:
        print("loop.py: %s" % e, file=sys.stderr, flush=True)
        return []
    return [r for r in rows if not r["wyjscie"].strip() and r["notatka"].startswith(paper_bot.BOT_TAG)]


def file_hash(path):
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return ""


def git(*args, check=True):
    print("$ git " + " ".join(args), flush=True)
    return subprocess.run(["git"] + list(args), cwd=PROJECT, check=check).returncode


def commit_journal(now):
    """Commit and push journal/. True when there was nothing to do or the push worked."""
    if subprocess.run(["git", "add", "-A", "journal"], cwd=PROJECT).returncode:
        return False
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=PROJECT).returncode == 0:
        print("Nic nowego w journal/.", flush=True)
        return True
    msg = "bot: journal %s ET" % entry.to_eastern(now).strftime("%Y-%m-%d %H:%M")
    if git("commit", "-q", "-m", msg, check=False):
        return False
    for _ in range(3):
        if git("push", "-q", check=False) == 0:
            return True
        git("pull", "-q", "--rebase", check=False)
    print("loop.py: push się nie udał, spróbuję przy następnym zapisie", file=sys.stderr, flush=True)
    return False


class Loop(object):
    def __init__(self, root, bot_args, start, max_minutes, commit_minutes, simulated, commit, clock, sleep, run):
        self.root, self.bot_args = root, bot_args
        self.now = start
        self.deadline = start + datetime.timedelta(minutes=max_minutes)
        self.commit_every = datetime.timedelta(minutes=commit_minutes)
        self.simulated, self.commit, self.clock, self.sleep, self.run = simulated, commit, clock, sleep, run
        self.steps = self.failed = 0
        self.last_commit = start
        self.dirty = False

    def tick(self):
        return self.now if self.simulated else self.clock()

    def one_step(self, now):
        trades = os.path.join(self.root, "journal", "trades.csv")
        before = file_hash(trades)
        args = list(self.bot_args)
        if self.simulated:
            args += ["--now", entry.to_eastern(now).strftime("%Y-%m-%d %H:%M")]
        code = self.run(args)
        self.steps += 1
        if code:
            self.failed += 1
        self.dirty = True
        if not self.commit:
            return
        if file_hash(trades) != before or now - self.last_commit >= self.commit_every:
            if self.commit(now):
                self.last_commit, self.dirty = now, False

    def done(self, now, after_close):
        """Why to stop after this step, or None."""
        hours = session_of(now)
        local = entry.to_eastern(now)
        if hours and local < hours[1]:
            return None
        held = open_positions(self.root)
        if not held:
            return "giełda dziś zamknięta" if not hours else "sesja zakończona, brak otwartych pozycji"
        if after_close >= AFTER_CLOSE_TRIES:
            return "po sesji wciąż otwarte pozycje (%d), zamknie je następny krok" % len(held)
        return None

    def go(self):
        after_close = 0
        why = None
        while True:
            now = self.tick()
            hours = session_of(now)
            if not hours or entry.to_eastern(now) >= hours[1]:
                after_close += 1
            self.one_step(now)
            why = self.done(now, after_close)
            if why:
                break
            slot = next_slot(now, offset=0) if self.simulated else next_slot(self.clock())
            if slot + datetime.timedelta(seconds=STEP_TIMEOUT) > self.deadline:
                why = "koniec czasu tego uruchomienia, dalej przejmie następne"
                break
            if self.simulated:
                self.now = slot
            else:
                wait = (slot - self.clock()).total_seconds()
                if wait > 0:
                    self.sleep(wait)
        print("PĘTLA koniec: %s. Kroki: %d, z błędem: %d." % (why, self.steps, self.failed), flush=True)
        if self.commit and self.dirty:
            self.commit(self.tick())
        return 1 if self.failed else 0


def run_bot(args):
    cmd = [sys.executable, os.path.join(HERE, "paper_bot.py")] + args
    print("--- krok %s UTC" % entry.utc_now().strftime("%H:%M:%S"), flush=True)
    try:
        return subprocess.run(cmd, timeout=STEP_TIMEOUT).returncode
    except subprocess.TimeoutExpired:
        print("loop.py: krok trwał dłużej niż %d s, przerwany" % STEP_TIMEOUT, file=sys.stderr, flush=True)
        return 1


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Paper bot loop: a step every 5 minutes. Paper only.")
    ap.add_argument("--dry-run", action="store_true", help="steps on a copy of journal/, nothing committed")
    ap.add_argument("--now", help='with --dry-run: replay from this New York time, "YYYY-MM-DD HH:MM"')
    ap.add_argument("--max-minutes", type=float, default=345, help="stop before this (default 345 = 5 h 45 min)")
    ap.add_argument("--commit-minutes", type=float, default=30, help="commit journal/ at least this often")
    a, rest = ap.parse_known_args(argv)
    if a.now and not a.dry_run:
        print("loop.py: --now działa tylko z --dry-run", file=sys.stderr)
        return 2
    try:
        paper_bot.read_limits(os.path.join(HERE, "limits.json"))      # paper must be true
        start = entry.parse_now(a.now)
    except (paper_bot.BotError, entry.EntryError) as e:
        print("loop.py: %s" % e, file=sys.stderr)
        return 1
    tmp = None
    root = PROJECT
    try:
        if a.dry_run:
            tmp = tempfile.mkdtemp()
            src = os.path.join(PROJECT, "journal", "trades.csv")
            os.makedirs(os.path.join(tmp, "journal"))
            if os.path.exists(src):
                shutil.copy(src, os.path.join(tmp, "journal", "trades.csv"))
            root = tmp
            print("PRÓBA: kroki na kopii dziennika, nic nie zostanie zapisane ani wypchnięte.", flush=True)
        bot_args = ["--root", root] + rest if a.dry_run else rest
        loop = Loop(root, bot_args, start, a.max_minutes, a.commit_minutes, bool(a.now),
                    None if a.dry_run else commit_journal, entry.utc_now, time.sleep, run_bot)
        code = loop.go()
        if a.dry_run:
            log_dir = os.path.join(root, "journal", "log")
            for name in sorted(os.listdir(log_dir)) if os.path.isdir(log_dir) else []:
                with open(os.path.join(log_dir, name), encoding="utf-8") as fh:
                    print("=== log próby %s\n%s" % (name, fh.read().rstrip()))
        return code
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
