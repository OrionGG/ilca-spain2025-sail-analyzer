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

import html as html_lib
import http.client
import re
import urllib.parse
import urllib.request
from html.parser import HTMLParser

MAX_BYTES = 10 * 1024 * 1024
_FETCH_ERRORS = (OSError, http.client.HTTPException, ValueError)   # network, bad URL, too large
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
_SECT = ("table", "thead", "tbody", "tfoot")
_CLOSE_TO = {"td": ("tr",) + _SECT, "th": ("tr",) + _SECT, "tr": _SECT,          # a start tag closes
             "thead": ("table",), "tbody": ("table",), "tfoot": ("table",)}       # open tags up to these


class _Tables(HTMLParser):
    """Collects every <table> as rows of cell texts, skipping display:none content
    (Sailti puts sort keys in hidden spans), keeping <br> as a space and expanding
    colspan. Row/cell state is per table, so a table nested in a cell can't break
    the outer row; end tags may be omitted (the next row/cell start closes them)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self._stack, self._open = [], [], []     # _open: [(tag, hidden)]

    def _is_hidden(self):
        return bool(self._open) and self._open[-1][1]

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        hidden = "display:none" in (a.get("style") or "").replace(" ", "").lower()
        stop = _CLOSE_TO.get(tag)
        if stop:                                       # cells/rows never inherit a stray hidden state
            while self._open and self._open[-1][0] not in stop:   # (an unclosed cell/row ends here)
                self._open.pop()
        if tag not in _VOID:
            self._open.append((tag, hidden or self._is_hidden()))
        if tag == "table":
            self._stack.append({"head": [], "rows": [], "row": None, "cell": None, "th": False, "span": 1})
            return
        if not self._stack:
            return
        t = self._stack[-1]
        if tag == "tr":
            self._flush_cell(t)
            self._flush_row(t)
            t["row"] = []
        elif tag in ("td", "th") and t["row"] is not None:
            self._flush_cell(t)
            t["cell"], t["th"] = [], tag == "th"
            try:
                t["span"] = max(1, min(50, int(a.get("colspan") or 1)))
            except ValueError:
                t["span"] = 1
        elif tag == "br" and t["cell"] is not None:
            t["cell"].append(" ")

    @staticmethod
    def _flush_cell(t):
        if t["cell"] is not None and t["row"] is not None:
            text = re.sub(r"\s+", " ", "".join(t["cell"])).strip()
            t["row"].extend([(text, t["th"])] + [("", t["th"])] * (t["span"] - 1))
        t["cell"] = None

    @staticmethod
    def _flush_row(t):
        if t["row"]:
            (t["head"] if all(h for _, h in t["row"]) else t["rows"]).append([c for c, _ in t["row"]])
        t["row"] = None

    def handle_endtag(self, tag):
        if any(o == tag for o, _h in self._open):     # pop back to the matching open tag
            while self._open and self._open.pop()[0] != tag:
                pass
        if not self._stack:
            return
        t = self._stack[-1]
        if tag in ("td", "th"):
            self._flush_cell(t)
        elif tag in ("tr", "thead", "tbody", "table"):     # a row may be left unclosed
            self._flush_cell(t)
            self._flush_row(t)
            if tag == "table":
                self.tables.append(self._stack.pop())

    def handle_data(self, data):
        if self._stack and self._stack[-1]["cell"] is not None and not self._is_hidden():
            self._stack[-1]["cell"].append(data)


def _split_rows(html: str):
    p = _Tables()
    p.feed(html)
    p.close()
    return p.tables


_HEAD = {
    "sail": re.compile(r"^((nat\.?|nation)\s*)?(vela|sail)(\s*(no\.?|nr\.?|number|num\.?|#))?$|^n[º°o]?\.?\s*(de\s*)?vela$|^bow(\s*(no\.?|#))?$", re.I),
    "name": re.compile(r"^(tripulantes?|name|helm|skipper|crew|regatista|sailor|nombre|patr[oó]n|timonel|deportista)s?$", re.I),
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


def _columns(head):
    col, races = {}, []
    for i, h in enumerate(head):
        for k, rx in _HEAD.items():
            if k not in col and rx.match(h.strip()):
                col[k] = i
        m = _RACE.match(h.strip())
        if m:
            base = re.sub(r"\s+", "", h.strip()).upper() if m.group(1) else m.group(2)
            name, k = base, 2
            while name in {n for _, n in races}:      # grouped headers repeat 1, 2, ...: keep labels unique
                name, k = f"{base} ({k})", k + 1
            races.append((i, name))
    return col, races


def _parse_table(t):
    # the header row is the one naming the sail and race columns (pages may add grouping rows
    # above it, or use <td> for the header)
    cands = [(h, t["rows"]) for h in t["head"]] + [(t["rows"][i], t["rows"][i + 1:]) for i in range(min(3, len(t["rows"])))]
    for head, rows in cands:
        col, races = _columns(head)
        if "sail" in col and races:
            break
    else:
        return None
    out = []
    for r in rows:
        if len(r) <= max(i for i, _ in races):
            continue
        def cell(k):                               # text across the header's colspan (e.g. flag + number)
            if k not in col:
                return ""
            i = col[k]
            j = i + 1
            while j < len(head) and not head[j].strip():
                j += 1
            return " ".join(x for x in r[i:j] if x).strip()
        sail_raw = cell("sail")
        digits = max(re.findall(r"\d+", sail_raw), key=len, default="")
        if not digits:
            continue
        rank = _num(r[0]) if 0 not in col.values() and races[0][0] != 0 else None
        out.append({
            "rank": int(rank) if rank is not None else None,
            "sail": digits, "sail_raw": sail_raw,
            "name": cell("name"), "club": cell("club"), "cat": cell("cat"),
            "net": _num(cell("net")), "total": _num(cell("total")),
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
        page = _fetch(url)
    except _FETCH_ERRORS as e:
        raise ValueError(f"Could not download the results page ({e}).")
    res, problem = None, None
    m = re.search(r"""['"]([^'"]*resultsajax\?[^'"]*allResults=1[^'"]*)['"]""", page) or \
        re.search(r"""['"]([^'"]*resultsajax\?[^'"]+)['"]""", page)
    if m:                                          # Sailti: the table comes from this request
        sub = urllib.parse.urljoin(url, html_lib.unescape(m.group(1)))
        su = urllib.parse.urlparse(sub)
        if su.scheme not in ("http", "https") or su.netloc != u.netloc:
            problem = "The results page points to another site; give the results table's own link."
        else:
            try:
                res = parse_results(_fetch(sub))
            except _FETCH_ERRORS as e:
                problem = f"Could not download the results table ({e})."
    res = res or parse_results(page)                # else a plain table on the page itself
    if not res:
        raise ValueError(problem or "No results table found on that page (expected sail numbers and race columns).")
    return res
