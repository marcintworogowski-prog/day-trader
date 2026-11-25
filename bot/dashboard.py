"""The paper bot's panel: one hand-written SVG, 800 wide, readable on a phone.
Standard library only. render() is pure: plain numbers in, SVG text out.
Colors are plain attributes on a light card, so every viewer draws the same thing.
"""

import datetime
import math
from xml.sax.saxutils import escape

W, PAD = 800, 32
INK, INK2, MUTED = "#0b0b0b", "#52514e", "#6b6a66"
PAGE, CARD, GRID, AXIS = "#f9f9f7", "#fcfcfb", "#e1e0d9", "#c3c2b7"
BLUE, ORANGE = "#2a78d6", "#eb6834"          # account, SPY
UP, DOWN = "#006300", "#d03b3b"
STATUS = {"GRA": "#0ca30c", "POZA SESJĄ": "#898781", "BŁĄD": "#d03b3b"}
FONT = "system-ui, -apple-system, 'Segoe UI', Roboto, Arial, sans-serif"
MAX_CLOSED = 8


# ---------------------------------------------------------------- Polish time

def _last_sunday(year, month):
    d = datetime.date(year, month, 31)
    return d - datetime.timedelta(days=(d.weekday() + 1) % 7)


def to_poland(utc):
    """Naive UTC -> Polish time (EU rule: last Sundays of March and October, 01:00 UTC)."""
    one = datetime.time(1, 0)
    start = datetime.datetime.combine(_last_sunday(utc.year, 3), one)
    end = datetime.datetime.combine(_last_sunday(utc.year, 10), one)
    return utc + datetime.timedelta(hours=2 if start <= utc < end else 1)


# ---------------------------------------------------------------- text

def usd(x, sign=False):
    s = "{:,.2f}".format(abs(x)).replace(",", " ").replace(".", ",")
    lead = "−" if round(x, 2) < 0 else ("+" if sign and round(x, 2) > 0 else "")
    return lead + s + " $"


def price(x):
    return "{:.2f}".format(x).replace(".", ",")


def pct(x):
    lead = "−" if round(x, 2) < 0 else ("+" if round(x, 2) > 0 else "")
    return lead + "{:.2f}".format(abs(x)).replace(".", ",") + "%"


def tone(x):
    return UP if round(x, 2) > 0 else DOWN if round(x, 2) < 0 else INK


def short(text, n):
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


class Page(object):
    def __init__(self):
        self.out, self.y = [], 0

    def add(self, text):
        self.out.append(text)

    def text(self, x, y, s, size=24, fill=INK, weight=400, anchor="start"):
        self.add('<text x="%g" y="%g" font-size="%d" fill="%s" font-weight="%d" text-anchor="%s">%s</text>'
                 % (x, y, size, fill, weight, anchor, escape(str(s))))

    def line(self, x1, y1, x2, y2, color=GRID, width=1):
        self.add('<line x1="%g" y1="%g" x2="%g" y2="%g" stroke="%s" stroke-width="%g"/>'
                 % (x1, y1, x2, y2, color, width))

    def title(self, s):
        self.y += 60
        self.text(PAD, self.y, s, 28, INK, 600)

    def note(self, s):
        self.y += 44
        self.text(PAD, self.y, s, 24, INK2)


# ---------------------------------------------------------------- parts

def header(p, d):
    pl, ny = to_poland(d["utc"]), d["new_york"]
    p.text(PAD, 60, "Bot papierowy", 40, INK, 700)
    p.text(W - PAD, 60, pl.strftime("%d.%m.%Y"), 28, INK2, 400, "end")
    p.text(PAD, 102, "Polska %s   ·   Nowy Jork %s" % (pl.strftime("%H:%M"), ny.strftime("%H:%M")), 24, INK2)
    p.add('<circle cx="%d" cy="141" r="10" fill="%s"/>' % (PAD + 10, STATUS[d["status"]]))
    p.text(PAD + 32, 150, d["status"], 28, INK, 700)
    x = PAD + 32 + {"GRA": 80, "POZA SESJĄ": 205, "BŁĄD": 105}[d["status"]]
    p.text(x, 150, short(d["reason"], 38 if d["status"] == "POZA SESJĄ" else 46), 24, INK2)
    p.line(PAD, 174, W - PAD, 174)
    p.y = 174


def tiles(p, d):
    cells = (("Wynik dziś", usd(d["today"], True), tone(d["today"])),
             ("Od startu", usd(d["total"], True), tone(d["total"])),
             ("Transakcje", str(d["trades"]), INK))
    for i, (name, value, color) in enumerate(cells):
        x = PAD + i * 256
        p.text(x, p.y + 42, name, 24, INK2)
        p.text(x, p.y + 88, value, 36, color, 700)
    p.y += 100


def _ticks(lo, hi):
    """About four round gridline values inside lo..hi."""
    raw = (hi - lo) / 4.0
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw - 1e-9)
    v, out = math.ceil(lo / step) * step, []
    while v <= hi + 1e-9:
        out.append(v)
        v += step
    return out


def chart(p, d):
    p.title("Wartość konta, start %s" % usd(d["budget"]))
    p.y += 40
    x = PAD
    legend = [(BLUE, "Konto %s" % usd(d["account"][-1]))]
    if d["spy"]:
        legend.append((ORANGE, "SPY kup i trzymaj %s" % usd(d["spy"][-1])))
    for color, name in legend:
        p.line(x, p.y - 8, x + 28, p.y - 8, color, 4)
        p.text(x + 38, p.y, name, 24, INK2)
        x += 330
    if not d["spy"]:
        p.text(x, p.y, "SPY: brak danych", 24, INK2)
    top, height, left, right = p.y + 30, 260, PAD + 84, W - PAD - 12
    values = list(d["account"]) + [v for v in d["spy"] if v is not None]
    lo, hi = min(values), max(values)
    room = max((hi - lo) * 0.12, d["budget"] * 0.005)
    lo, hi = lo - room, hi + room
    n = len(d["days"])

    def px(i):
        return (left + right) / 2.0 if n == 1 else left + (right - left) * i / float(n - 1)

    def py(v):
        return top + height * (hi - v) / (hi - lo)
    for v in _ticks(lo, hi):
        p.line(left, py(v), right, py(v))
        p.text(left - 12, py(v) + 8, "{:,.0f}".format(v).replace(",", " "), 22, MUTED, 400, "end")
    p.line(left, top + height, right, top + height, AXIS)
    for color, name, series in ((ORANGE, "SPY", d["spy"]), (BLUE, "Konto", d["account"])):
        pts = [(px(i), py(v), d["days"][i], v) for i, v in enumerate(series) if v is not None]
        if not pts:
            continue
        p.add('<polyline fill="none" stroke="%s" stroke-width="4" stroke-linejoin="round" stroke-linecap="round" '
              'points="%s"><title>%s</title></polyline>' % (color, " ".join("%.1f,%.1f" % q[:2] for q in pts), name))
        x, y, day, v = pts[-1]
        p.add('<circle cx="%.1f" cy="%.1f" r="7" fill="%s" stroke="%s" stroke-width="2"><title>%s</title></circle>'
              % (x, y, color, CARD, escape("%s %s: %s" % (name, day.strftime("%d.%m"), usd(v)))))
    marks = sorted({0, n // 2, n - 1}) if n > 2 else range(n)
    for i in marks:
        anchor = "middle" if n == 1 or 0 < i < n - 1 else ("start" if i == 0 else "end")
        p.text(px(i), top + height + 34, d["days"][i].strftime("%d.%m"), 22, MUTED, 400, anchor)
    p.y = top + height + 40


def positions(p, d):
    p.title("Otwarte pozycje")
    if not d["open"]:
        p.note("Brak otwartych pozycji.")
        return
    cols = ((PAD, "start", "Ticker"), (265, "end", "Wejście"), (385, "end", "Stop"), (505, "end", "Cel"),
            (625, "end", "Cena"), (W - PAD, "end", "Wynik"))
    p.y += 44
    for x, anchor, name in cols:
        p.text(x, p.y, name, 22, MUTED, 400, anchor)
    for r in d["open"]:
        p.y += 14
        p.line(PAD, p.y, W - PAD, p.y)
        p.y += 32
        known = r["last"] is not None
        cells = (r["ticker"], price(r["entry"]), price(r["stop"]), price(r["target"]),
                 price(r["last"]) if known else "brak", usd(r["result"], True) if known else "brak")
        for i, ((x, anchor, _), s) in enumerate(zip(cols, cells)):
            color = tone(r["result"]) if i == 5 and known else INK
            p.text(x, p.y, s, 24, color, 700 if i in (0, 5) else 400, anchor)


def closed(p, d):
    p.title("Zamknięte dziś")
    if not d["closed"]:
        p.note("Dziś brak zamkniętych transakcji.")
        return
    for r in d["closed"][-MAX_CLOSED:]:
        p.y += 44
        p.text(PAD, p.y, r["ticker"], 24, INK, 700)
        p.text(PAD + 120, p.y, "%s → %s" % (price(r["entry"]), price(r["exit"])), 24, INK2)
        p.text(520, p.y, pct(r["pct"]), 24, INK2, 400, "end")
        p.text(W - PAD, p.y, usd(r["result"], True), 24, tone(r["result"]), 700, "end")
    more = len(d["closed"]) - MAX_CLOSED
    if more > 0:
        p.note("oraz %d wcześniejszych" % more)


def render(d):
    """d: utc, new_york (naive datetimes), status, reason, budget, days, account, spy (same length or
    empty, None = no data), open, closed, today, total, trades -> SVG text."""
    p = Page()
    header(p, d)
    tiles(p, d)
    chart(p, d)
    positions(p, d)
    closed(p, d)
    p.y += 40
    p.line(PAD, p.y, W - PAD, p.y)
    p.y += 38
    p.text(PAD, p.y, "Fikcyjny budżet, bez prawdziwych pieniędzy.", 22, MUTED)
    h = int(p.y + 30)
    head = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" height="%d" '
            'font-family="%s" role="img">\n<title>Panel bota papierowego</title>\n'
            '<rect width="%d" height="%d" fill="%s"/>\n'
            '<rect x="8" y="8" width="%d" height="%d" rx="16" fill="%s" stroke="%s"/>'
            % (W, h, W, h, FONT, W, h, PAGE, W - 16, h - 16, CARD, GRID))
    return head + "\n" + "\n".join(p.out) + "\n</svg>\n"
