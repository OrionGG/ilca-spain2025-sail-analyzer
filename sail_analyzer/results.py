"""
results.py — download and parse official regatta results from a results-page URL.

Works with Sailti result pages (the table is loaded by the page from a
`resultsajax?...` request, which is fetched directly) and, as a fallback, with any
page whose results are a plain HTML table (sail number, name, net/total points and
one column per race). Stdlib only.

Parsed form:
    {"races": ["1", "2", ...],                       # race column headers, in order
     "rows": [{"rank": 1, "sail": "212517", "sail_raw": "ESP 212517",
               "name": "...", "club": "...", "cat": "...", "net": 29.0, "total": 43.0,
               "scores": [{"pts": 5.0, "code": None, "disc": False}, ...]}, ...]}
"""

from __future__ import annotations

import re
import urllib.parse
import urllib.request
from html.parser import HTMLParser

MAX_BYTES = 10 * 1024 * 1024
UA = "Mozilla/5.0 (Sail Analyzer; official results import)"


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "X-Requested-With": "XMLHttpRequest"})
    with urllib.request.urlopen(req, timeout=25) as r:
        raw = r.read(MAX_BYTES + 1)
        charset = r.headers.get_content_charset() or "utf-8"
    if len(raw) > MAX_BYTES:
        raise ValueError("The results page is too large.")
    try:
        return raw.decode(charset)
    except (UnicodeDecodeError, LookupError):
        return raw.decode("cp1252", errors="replace")


_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
         "source", "track", "wbr"}


class _Tables(HTMLParser):
    """Collects every <table> as rows of cell texts, skipping display:none content
    (Sailti puts sort keys in hidden spans) and keeping <br> as a space."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self._stack, self._open = [], [], []     # _open: [(tag, hidden)]
        self._row, self._cell, self._in_head = None, None, False

    def _is_hidden(self):
        return bool(self._open) and self._open[-1][1]

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        hidden = "display:none" in (a.get("style") or "").replace(" ", "").lower()
        if tag in ("td", "th", "tr", "table"):          # cells/rows never inherit a stray hidden state
            while self._open and self._open[-1][0] not in ("td", "th", "tr", "table", "thead", "tbody"):
                self._open.pop()
        if tag not in _VOID:
            self._open.append((tag, hidden or self._is_hidden()))
        if tag == "table":
            self._stack.append({"head": [], "rows": []})
        elif not self._stack:
            return
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
            self._in_head = tag == "th"
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag):
        if any(t == tag for t, _h in self._open):     # pop back to the matching open tag
            while self._open and self._open.pop()[0] != tag:
                pass
        if not self._stack:
            return
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append((re.sub(r"\s+", " ", "".join(self._cell)).strip(), self._in_head))
            self._cell = None
        elif tag in ("tr", "thead", "tbody", "table"):     # a row may be left unclosed
            if self._row:
                t = self._stack[-1]
                (t["head"] if all(h for _, h in self._row) else t["rows"]).append([c for c, _ in self._row])
            self._row = None
            if tag == "table":
                self.tables.append(self._stack.pop())

    def handle_data(self, data):
        if self._cell is not None and not self._is_hidden():
            self._cell.append(data)


def _split_rows(html: str):
    """Sailti opens a <tr> without closing the previous one; normalise that first."""
    html = re.sub(r"(</td>)\s*<tr", r"\1</tr><tr", html, flags=re.I)
    p = _Tables()
    p.feed(html)
    p.close()
    return p.tables


_HEAD = {
    "sail": re.compile(r"^(vela|sail|sail ?no\.?|n[º°o]? ?vela|nat\.? ?sail|bow)$", re.I),
    "name": re.compile(r"^(tripulantes?|name|helm|skipper|crew|regatista|sailor)s?$", re.I),
    "club": re.compile(r"^(club)$", re.I),
    "cat": re.compile(r"^(cat|category|categor[ií]a|div)$", re.I),
    "net": re.compile(r"^(net|nett|neto)( ?(points|pts|puntos))?$", re.I),
    "total": re.compile(r"^(total|tot)( ?(points|pts|puntos))?$", re.I),
}
_RACE = re.compile(r"^(r|race|regata|prueba|q|f)?\s*(\d{1,2})$", re.I)


def _num(s):
    m = re.search(r"-?\d+(?:[.,]\d+)?", s or "")
    return float(m.group(0).replace(",", ".")) if m else None


def _score(cell: str):
    """None for an empty cell (race not sailed / not scored yet)."""
    code = re.search(r"\b([A-Z]{2,5})\b", cell or "")
    pts = _num(cell)
    if pts is None and not code:
        return None
    return {"pts": pts, "code": code.group(1) if code else None, "disc": "(" in (cell or "")}


def _parse_table(t):
    head = t["head"][0] if t["head"] else (t["rows"][0] if t["rows"] else [])
    rows = t["rows"] if t["head"] else t["rows"][1:]
    col = {}
    races = []
    for i, h in enumerate(head):
        for k, rx in _HEAD.items():
            if k not in col and rx.match(h.strip()):
                col[k] = i
        m = _RACE.match(h.strip())
        if m:
            races.append((i, m.group(2)))
    if "sail" not in col or not races:
        return None
    out = []
    for r in rows:
        if len(r) <= max(i for i, _ in races):
            continue
        sail_raw = r[col["sail"]]
        digits = re.sub(r"\D", "", sail_raw)
        if not digits:
            continue
        out.append({
            "rank": int(_num(r[0])) if _num(r[0]) is not None else None,
            "sail": digits, "sail_raw": sail_raw,
            "name": r[col["name"]] if "name" in col else "",
            "club": r[col["club"]] if "club" in col else "",
            "cat": r[col["cat"]] if "cat" in col else "",
            "net": _num(r[col["net"]]) if "net" in col else None,
            "total": _num(r[col["total"]]) if "total" in col else None,
            "scores": [_score(r[i]) for i, _ in races],
        })
    return {"races": [n for _, n in races], "rows": out} if out else None


def parse_results(html: str):
    """The results table with the most boats on the page, or None."""
    best = None
    for t in _split_rows(html):
        res = _parse_table(t)
        if res and (best is None or len(res["rows"]) > len(best["rows"])):
            best = res
    return best


def fetch_results(url: str):
    """Download a results page (following a Sailti results request) and parse it."""
    u = urllib.parse.urlparse(url.strip())
    if u.scheme not in ("http", "https") or not u.netloc:
        raise ValueError("Give the http(s) link of the results page.")
    try:
        html = _fetch(url)
        m = re.search(r"""['"]([^'"]*resultsajax\?[^'"]*allResults=1[^'"]*)['"]""", html) or \
            re.search(r"""['"]([^'"]*resultsajax\?[^'"]+)['"]""", html)
        if m:                                      # Sailti: the table comes from this request
            html = _fetch(urllib.parse.urljoin(url, m.group(1)))
    except OSError as e:
        raise ValueError(f"Could not download the results page ({e}).")
    res = parse_results(html)
    if not res:
        raise ValueError("No results table found on that page (expected sail numbers and race columns).")
    return res
