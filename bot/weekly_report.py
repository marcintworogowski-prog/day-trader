#!/usr/bin/env python3
"""Weekly report for the paper bot. Standard library only. Writes nothing but the report.

    python bot/weekly_report.py                        # this week (Mon-Sun, New York), to the screen
    python bot/weekly_report.py --now "2026-11-27 17:30"
    python bot/weekly_report.py --out report.md --title-out title.txt     # for the GitHub workflow

The numbers are trader-review's own weekly summary of journal/trades.csv. The
list of bad days comes from the bot's log, journal/log/YYYY-MM-DD.txt: no log,
data errors, a gap of more than 30 minutes between steps, or a late close.
"""

import argparse
import contextlib
import datetime
import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paper_bot  # noqa: E402

entry, review = paper_bot.entry, paper_bot.review
BOT_START = paper_bot.BOT_START      # no logs are expected before this day
MAX_GAP_MINUTES = 30                         # the schedule is every 10 minutes; GitHub may be late


def summary(root, today, limits, offline=None):
    """(trader-review's weekly text, number of trades closed in the week)."""
    journal = os.path.join(root, "journal", "trades.csv")
    argv = ["tydzien", "--now", today.isoformat(), "--journal", journal, "--config", limits]
    if offline:
        argv += ["--offline", offline]
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = review.main(argv)
    if code:
        raise paper_bot.BotError(err.getvalue().strip() or "trader-review nie policzył tygodnia")
    start, end = review.week_of(today)
    return out.getvalue().rstrip(), len(review.closed_between(review.load(journal), start, end))


def day_problems(day, text, until):
    """What went wrong on one trading day. `until` is the New York time the day is judged up to."""
    o, c = entry.session(day)
    opens, closes = datetime.datetime.combine(day, o), datetime.datetime.combine(day, c)
    limit = min(closes, until)
    if text is None:
        return ["brak logu, bot nie wykonał żadnego kroku w sesji"]
    out, steps, errors, data = [], [], 0, 0
    for line in text.splitlines():
        m = re.match(r"^(\d{2}):(\d{2}) (\S+)(.*)$", line)
        if not m:
            continue
        kind, rest = m.group(3), m.group(4)
        if kind == "KROK":
            steps.append(datetime.datetime.combine(day, datetime.time(int(m.group(1)), int(m.group(2)))))
            bad = re.search(r"błędy danych: (\d+)", rest)
            data += bool(bad and int(bad.group(1)))
        elif kind == "BŁĄD":
            errors += 1
        if "zamknięcie po sesji" in rest:
            out.append("pozycja zamknięta dopiero po sesji (krok przed końcem sesji nie zadziałał)")
    if errors:
        out.append("błędy ceny otwartej pozycji: %d" % errors)
    if data:
        out.append("kroki z błędami danych (Yahoo): %d" % data)
    if not steps:
        out.append("brak kroków w sesji")
        return out
    marks = [opens] + sorted(steps) + [limit]
    gaps = [(int((b - a).total_seconds() // 60), a, b) for a, b in zip(marks, marks[1:])]
    long = [g for g in gaps if g[0] > MAX_GAP_MINUTES]
    if long:
        worst = max(long, key=lambda g: g[0])
        out.append("pominięte kroki: %d przerw(y) ponad %d min, najdłuższa %d min (%s do %s ET)" % (
            len(long), MAX_GAP_MINUTES, worst[0], worst[1].strftime("%H:%M"), worst[2].strftime("%H:%M")))
    return out


def bad_days(root, today, now_local):
    """[(day, [problem])] for the trading days of the week so far."""
    start, end = review.week_of(today)
    out = []
    day = max(start, BOT_START)
    while day <= min(end, today):
        if entry.trading_day(day)[0] and now_local >= datetime.datetime.combine(day, entry.session(day)[0]):
            path = os.path.join(root, "journal", "log", day.isoformat() + ".txt")
            text = None
            if os.path.exists(path):
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
            problems = day_problems(day, text, now_local)
            if problems:
                out.append((day, problems))
        day += datetime.timedelta(days=1)
    return out


def build(root, now_utc, limits, offline=None):
    """(title, Markdown body)."""
    local = entry.to_eastern(now_utc)
    today = local.date()
    if today.year not in entry.CALENDAR_YEARS:
        raise paper_bot.BotError("kalendarz NYSE jest tylko na lata 2026-2027")
    text, n = summary(root, today, limits, offline)
    lines = []
    if n == 0:
        lines += ["**Brak transakcji zamkniętych w tym tygodniu.**", ""]
    lines += ["```text", text, "```", "", "### Dni, w których bot nie zadziałał w pełni", ""]
    bad = bad_days(root, today, local)
    for day, problems in bad:
        lines.append("- **%s**: %s" % (day.isoformat(), "; ".join(problems)))
    if not bad:
        lines.append("Brak problemów w logach.")
    start = review.week_of(today)[0]
    if start < BOT_START <= today:
        lines += ["", "Bot działa od %s, wcześniejsze dni nie są oceniane." % BOT_START.isoformat()]
    lines += ["", "Stan na %s w Nowym Jorku. Tylko papier." % local.strftime("%Y-%m-%d %H:%M")]
    return "Raport tygodnia %s" % today.isoformat(), "\n".join(lines) + "\n"


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Weekly report of the paper bot, in Markdown.")
    ap.add_argument("--now", help='pretend it is this New York time: "YYYY-MM-DD HH:MM"')
    ap.add_argument("--root", default=paper_bot.PROJECT, help="folder that holds journal/")
    ap.add_argument("--limits", default=os.path.join(paper_bot.HERE, "limits.json"))
    ap.add_argument("--offline", metavar="PATH", help="saved Yahoo chart JSON for SPY (tests)")
    ap.add_argument("--out", help="write the body here")
    ap.add_argument("--title-out", help="write the title here")
    a = ap.parse_args(argv)
    try:
        title, body = build(a.root, entry.parse_now(a.now), a.limits, a.offline)
        for path, text in ((a.out, body), (a.title_out, title)):
            if path:
                with open(path, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(text)
        print("# %s\n\n%s" % (title, body))
        return 0
    except (paper_bot.BotError, entry.EntryError, review.ReviewError, OSError) as e:
        print("weekly_report.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
