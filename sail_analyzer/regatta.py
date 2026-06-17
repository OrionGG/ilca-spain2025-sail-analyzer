"""
regatta.py — GPS track analytics engine for ILCA / dinghy racing.

Input: TracTrac native KML exports (position + UTC timestamp at ~3s).
Everything else (speed, course, wind, VMG, tacks, legs, start, ranking)
is *derived* from position+time. There is NO instrument or IMU data in
these files, so heel / fore-aft trim are intentionally not produced.

Pure-ish: uses numpy. Designed to be called by app.py (the local server).
"""

from __future__ import annotations

import glob
import math
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache

import numpy as np

KN = 1.94384            # m/s -> knots
ILCA_LOA = 4.2          # boat length (m), used for "boatlengths lost"
KML_NS = "{http://www.opengis.net/kml/2.2}"


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _parse_ts(s: str) -> float:
    # "2025-09-19T10:45:01Z" -> epoch seconds (UTC)
    dt = datetime.strptime(s.strip(), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return dt.timestamp()


def parse_kml(path: str):
    """Return (t[N] epoch s, lat[N], lon[N]) sorted by time, deduped."""
    t, lat, lon = [], [], []
    # Stream-parse: files are flat lists of <Placemark><TimeStamp><when>..</when>
    # <Point><coordinates>lon,lat,alt</coordinates>.
    for _, elem in ET.iterparse(path, events=("end",)):
        if elem.tag == KML_NS + "Placemark":
            when = elem.find(f"{KML_NS}TimeStamp/{KML_NS}when")
            coord = elem.find(f"{KML_NS}Point/{KML_NS}coordinates")
            if when is not None and coord is not None and when.text and coord.text:
                try:
                    parts = coord.text.strip().split(",")
                    lo, la = float(parts[0]), float(parts[1])
                    t.append(_parse_ts(when.text))
                    lat.append(la)
                    lon.append(lo)
                except (ValueError, IndexError):
                    pass
            elem.clear()
    if not t:
        return np.array([]), np.array([]), np.array([])
    t = np.array(t)
    order = np.argsort(t)
    t, lat, lon = t[order], np.array(lat)[order], np.array(lon)[order]
    # dedupe identical timestamps
    keep = np.concatenate(([True], np.diff(t) > 0))
    return t[keep], lat[keep], lon[keep]


# --------------------------------------------------------------------------- #
# Geometry helpers (local ENU tangent plane, good for a few-km course)
# --------------------------------------------------------------------------- #
def to_xy(lat, lon, lat0, lon0):
    """Equirectangular projection to local meters: x=East, y=North."""
    mlat = 111320.0
    mlon = 111320.0 * math.cos(math.radians(lat0))
    x = (lon - lon0) * mlon
    y = (lat - lat0) * mlat
    return x, y, mlat, mlon


def _circ_smooth(deg, win):
    """Smooth an angle series (degrees) with a centered moving average."""
    if win <= 1 or len(deg) < 3:
        return deg
    r = np.radians(deg)
    cs = _movavg(np.cos(r), win)
    sn = _movavg(np.sin(r), win)
    return (np.degrees(np.arctan2(sn, cs))) % 360.0


def _movavg(a, win):
    if win <= 1 or len(a) < 3:
        return a
    win = int(win) | 1                       # force odd
    pad = win // 2
    ap = np.pad(a, pad, mode="edge")
    k = np.ones(win) / win
    return np.convolve(ap, k, mode="valid")


def wrap180(deg):
    """Wrap to (-180, 180]."""
    return (np.asarray(deg) + 180.0) % 360.0 - 180.0


# --------------------------------------------------------------------------- #
# Per-boat derived track
# --------------------------------------------------------------------------- #
@dataclass
class Boat:
    sail: str
    name: str
    file: str
    t: np.ndarray
    lat: np.ndarray
    lon: np.ndarray
    x: np.ndarray = field(default=None)
    y: np.ndarray = field(default=None)
    sog: np.ndarray = field(default=None)   # knots
    cog: np.ndarray = field(default=None)   # deg
    vmg: np.ndarray = field(default=None)   # knots, + = to windward
    twa: np.ndarray = field(default=None)   # deg, 0..180
    tack: np.ndarray = field(default=None)  # +1 stbd / -1 port (rel to wind)


def derive_kinematics(b: Boat, lat0, lon0, smooth_win=3):
    x, y, _, _ = to_xy(b.lat, b.lon, lat0, lon0)
    b.x, b.y = x, y
    t = b.t
    n = len(t)
    if n < 2:
        b.sog = np.zeros(n); b.cog = np.zeros(n)
        return b
    # centered differences for velocity
    vx = np.gradient(x, t)        # m/s East
    vy = np.gradient(y, t)        # m/s North
    sog = np.hypot(vx, vy) * KN
    cog = (np.degrees(np.arctan2(vx, vy))) % 360.0     # 0=N,90=E
    b.sog = _movavg(sog, smooth_win)
    b.cog = _circ_smooth(cog, smooth_win)
    return b


# --------------------------------------------------------------------------- #
# Wind estimation from the track(s)
# --------------------------------------------------------------------------- #
def estimate_wind_dir(boats, t_ref=None):
    """
    Estimate true wind direction (degrees FROM) for a fleet.

    Method:
      1. Axial (doubled-angle) mean of all course vectors -> the dominant
         windward/leeward *line* (0-180), robust to up/down ambiguity.
      2. Orient that line geometrically: a windward/leeward race starts and
         finishes at the LEEWARD end and the fleet's biggest excursion is
         toward the WINDWARD mark. Wind comes FROM the windward end.
         (Pre-start reaching along the line makes "first-3-min displacement"
         unreliable, so we use the start->extreme excursion instead.)
    Returns wind_from in degrees.
    """
    C = S = 0.0
    for b in boats:
        if b.sog is None or len(b.cog) < 5:
            continue
        # weight by speed (moving boats define the axis), ignore near-stationary
        w = np.clip(b.sog, 0, 12)
        a = np.radians(b.cog)
        C += np.sum(w * np.cos(2 * a))
        S += np.sum(w * np.sin(2 * a))
    if C == 0 and S == 0:
        return 0.0
    axis = (math.degrees(math.atan2(S, C)) / 2.0) % 180.0   # 0..180 line
    ux, uy = math.sin(math.radians(axis)), math.cos(math.radians(axis))

    # orient by fleet excursion from start toward the windward extreme
    excursions = []
    for b in boats:
        if b.x is None or len(b.x) < 10:
            continue
        p = b.x * ux + b.y * uy
        m0 = b.t - b.t[0] <= 60
        p0 = float(np.mean(p[m0])) if m0.any() else float(p[0])
        far = p.max() if (p.max() - p0) > (p0 - p.min()) else p.min()
        excursions.append(far - p0)
    sign = 1.0
    if excursions and np.median(excursions) < 0:
        sign = -1.0
    wind_from = (axis if sign > 0 else axis + 180) % 360.0

    # refine: a boat cannot point within ~38 deg of the wind, so the wind is
    # the centre of the emptiest heading arc (the upwind "no-go zone").  This is
    # robust to tack-time imbalance (unlike a cluster-mean bisector).  Several
    # empty arcs exist (reaching gaps), so we search only NEAR the geometric
    # estimate above to lock onto the *upwind* gap.
    cog = np.concatenate([b.cog[b.sog > 1.0] for b in boats if b.sog is not None])
    refined = _nogo_center(cog, wind_from, search=48, halfwidth=33)
    if refined is not None:
        wind_from = refined
    return wind_from % 360.0


def _circ_mean(deg, w):
    a = np.radians(deg)
    return math.degrees(math.atan2(np.sum(w * np.sin(a)), np.sum(w * np.cos(a)))) % 360.0


def _nogo_center(cog, coarse, search=48, halfwidth=33, minpts=40):
    """
    Wind FROM = centre of the emptiest +/-halfwidth heading arc, searched within
    +/-`search` deg of `coarse`. `cog` is an array of headings (deg) for moving
    boats. Returns None if too little data.
    """
    if len(cog) < minpts:
        return None
    h, _ = np.histogram(cog % 360, bins=np.arange(0, 361, 2))
    k = np.ones(7) / 7                                   # circular smoothing
    h = np.convolve(np.concatenate([h[-6:], h, h[:6]]), k, "same")[6:-6]
    centers = np.arange(0, 360, 2)
    best = None
    for c in centers:
        if abs(((c - coarse + 180) % 360) - 180) > search:
            continue
        d = np.abs(((centers - c + 180) % 360) - 180)
        score = h[d <= halfwidth].sum()
        if best is None or score < best[0]:
            best = (score, c)
    return float(best[1]) if best else None


def wind_timeseries(boats, wind0, grid, win=150.0):
    """
    Time-varying wind direction (shift track) on a common time grid.

    For each window we collect *upwind close-hauled* course vectors across
    the fleet and take their bisector (doubled-angle axial mean oriented to
    wind0). Falls back to wind0 where data is thin.
    """
    out = np.full(len(grid), wind0, float)
    # gather all moving headings across the fleet (need both sides of the gap)
    allt, alla = [], []
    for b in boats:
        if b.sog is None:
            continue
        m = b.sog > 1.0
        allt.append(b.t[m]); alla.append(b.cog[m])
    if not allt:
        return out
    allt = np.concatenate(allt); alla = np.concatenate(alla)
    for i, tc in enumerate(grid):
        m = np.abs(allt - tc) <= win / 2
        # track shifts: search a tight window around the running wind estimate
        c = _nogo_center(alla[m], wind0, search=30, halfwidth=30, minpts=30)
        if c is not None:
            out[i] = c
    return _circ_smooth(out, 5)


def apply_wind(b: Boat, wind_from):
    """Compute TWA, VMG (to windward, knots), and tack side."""
    rel = wrap180(b.cog - wind_from)        # -180..180, 0 = pointing at wind
    b.twa = np.abs(rel)
    b.vmg = b.sog * np.cos(np.radians(b.twa))   # + = gaining to windward
    b.tack = np.where(rel >= 0, 1, -1)          # +1 stbd-ish / -1 port-ish
    return b


# --------------------------------------------------------------------------- #
# Maneuver detection (tacks / gybes) and loss
# --------------------------------------------------------------------------- #
@dataclass
class Maneuver:
    kind: str          # "tack" | "gybe"
    t: float           # epoch s at apex (min speed)
    lat: float
    lon: float
    entry_sog: float
    min_sog: float
    exit_sog: float
    sog_loss: float
    duration: float    # s of the turn
    recover_s: float   # s to return to ~entry speed
    bl_lost: float     # boatlengths lost vs sailing at target VMG
    twa_before: float
    twa_after: float


def detect_maneuvers(b: Boat, wind_from):
    """Find tacks/gybes via crossings of head-to-wind / dead-downwind."""
    rel = wrap180(b.cog - wind_from)
    n = len(rel)
    if n < 10:
        return []
    mans = []
    # crossing of 0 => tack (through head to wind); crossing of ±180 => gybe
    cross0 = np.where(np.sign(rel[:-1]) != np.sign(rel[1:]))[0]
    # downwind crossing: rel jumps from near +180 to near -180 (or vice versa)
    relshift = rel.copy()
    jump = np.where(np.abs(np.diff(rel)) > 180)[0]   # wrap of the wrapped angle
    events = [("tack", i) for i in cross0 if abs(rel[i]) < 90] + \
             [("gybe", i) for i in jump]
    events.sort(key=lambda e: e[1])

    for kind, i in events:
        # local window around the crossing
        lo = max(0, i - 12)
        hi = min(n, i + 13)
        seg = b.sog[lo:hi]
        if len(seg) < 5:
            continue
        japex = lo + int(np.argmin(seg))
        # require an actual speed dip and a real direction change
        # entry speed = median over ~15 s before turn start
        pre = b.sog[max(0, japex - 8):max(1, japex - 2)]
        post = b.sog[min(n - 1, japex + 2):min(n, japex + 10)]
        if len(pre) == 0 or len(post) == 0:
            continue
        entry = float(np.median(pre)); exitv = float(np.median(post))
        minv = float(b.sog[japex])
        if entry < 1.0 or (entry - minv) < 0.3:      # not a real maneuver
            continue
        twa_b = float(np.median(b.twa[max(0, japex - 6):japex])) if japex > 0 else 0
        twa_a = float(np.median(b.twa[japex:min(n, japex + 6)]))
        # turn duration: speed below 92% of mean(entry,exit)
        thr = 0.92 * (entry + exitv) / 2
        l = japex
        while l > lo and b.sog[l] < thr:
            l -= 1
        r = japex
        while r < hi - 1 and b.sog[r] < thr:
            r += 1
        dur = float(b.t[r] - b.t[l])
        # recovery: time after apex to reach 0.97*entry
        rr = japex
        while rr < n - 1 and b.sog[rr] < 0.97 * entry:
            rr += 1
        recover = float(b.t[rr] - b.t[japex])
        # boatlengths lost vs sailing at target VMG through the window
        tw = slice(l, r + 1)
        if r > l:
            target_vmg = max(np.median(np.abs(b.vmg[max(0, l - 4):l + 1])),
                             np.median(np.abs(b.vmg[r:r + 5])))
            actual = np.trapz(np.abs(b.vmg[tw]), b.t[tw]) / KN     # m made good
            ref = target_vmg / KN * (b.t[r] - b.t[l])
            bl = max(0.0, (ref - actual)) / ILCA_LOA
        else:
            bl = 0.0
        mans.append(Maneuver(
            kind=kind, t=float(b.t[japex]), lat=float(b.lat[japex]), lon=float(b.lon[japex]),
            entry_sog=round(entry, 2), min_sog=round(minv, 2), exit_sog=round(exitv, 2),
            sog_loss=round(entry - minv, 2), duration=round(dur, 1),
            recover_s=round(recover, 1), bl_lost=round(bl, 2),
            twa_before=round(twa_b, 0), twa_after=round(twa_a, 0),
        ))
    # merge maneuvers detected within 6 s of each other (debounce)
    merged = []
    for m in mans:
        if merged and abs(m.t - merged[-1].t) < 6:
            continue
        merged.append(m)
    return merged


# --------------------------------------------------------------------------- #
# Leg segmentation — matches the KNOWN course template (trapezoid w/ reaches)
# --------------------------------------------------------------------------- #
# Modes: 0 = upwind, 1 = reach, 2 = downwind
_MODE = {"U": 0, "R": 1, "D": 2}
_KIND = {0: "upwind", 1: "reach", 2: "downwind"}


def _mode_array(b: Boat, vw_smooth=31, twa_smooth=15, strong=1.5):
    """
    Per-point sailing mode (0=up, 1=reach, 2=down).

    Strong windward VMG (made good fast to windward/leeward) forces up/down and
    is robust to a few degrees of wind-estimate error (it fixes a beat whose TWA
    reads high when the wind is slightly off). Otherwise a mid TWA marks a reach.
    """
    vw = _movavg(b.vmg, vw_smooth)
    twa = _movavg(b.twa, twa_smooth)
    up = vw > strong
    dn = vw < -strong
    reach = (~up) & (~dn) & (twa > 55) & (twa < 130)
    return np.where(up, 0, np.where(dn, 2, np.where(reach, 1, np.where(vw >= 0, 0, 2))))


def segment_legs(b: Boat, wind_from, template=("U", "R", "D", "U", "D", "R")):
    """
    Split the race into the legs of the KNOWN course (trapezoid w/ reaches) by
    fitting the template's mode sequence to the track with dynamic programming.

    Course: start -U-> mark1 -R-> mark2 -D-> gate -U-> mark2 -D-> gate -R->
    finish  (Prueba 5 shortened to U,R,D,U). The DP segments [0,n) into K
    contiguous legs labelled by the template plus a free trailing segment, so
    post-finish sailing is dropped and no single noisy stretch can hijack a leg.
    """
    if b.vmg is None or len(b.t) < 30:
        return []
    tmpl = [_MODE[c] for c in template]
    K = len(tmpl)
    n = len(b.t)
    mode = _mode_array(b)
    # f[m][i] = number of points in [0,i) whose mode != m  (segment cost helper)
    f = {m: np.concatenate([[0.0], np.cumsum((mode != m).astype(float))]) for m in (0, 1, 2)}

    INF = float("inf")
    g = [INF] * (n + 1); g[0] = 0.0                  # g[j]: cost of legs done so far covering [0,j)
    back = [[0] * (n + 1) for _ in range(K)]
    for k in range(K):
        fm = f[tmpl[k]]
        best_val, best_i = INF, 0
        gnew = [INF] * (n + 1)
        for j in range(n + 1):
            cur = g[j] - fm[j]                       # cost(i,j,m) = fm[j]-fm[i]
            if cur < best_val:
                best_val, best_i = cur, j
            gnew[j] = fm[j] + best_val
            back[k][j] = best_i
        g = gnew
    # finish boundary: trailing [bK,n) is post-race. Charge it eps<1 per point so
    # it is cheaper than MISCLASSIFYING real post-race sailing, but far costlier
    # than correctly labelling true legs (else the all-empty solution wins).
    eps = 0.5
    bK = min(range(n + 1), key=lambda j: g[j] + eps * (n - j))
    bounds = [0] * (K + 1); bounds[K] = bK
    for k in range(K - 1, -1, -1):
        bounds[k] = back[k][bounds[k + 1]]

    legs = []
    for k in range(K):
        a, c = bounds[k], bounds[k + 1]
        if c - a >= 2:
            legs.append((a, min(c, n - 1), _KIND[tmpl[k]]))

    # Trim the final leg at peak progress along its own axis: after the finish a
    # boat turns back (to shore / milling), so projection onto the leg direction
    # stops rising there. Stops post-race sailing inflating the last leg.
    if legs:
        a, c, kind = legs[-1]
        mid = a + max(5, (c - a) // 4)
        if mid < c:
            ax, ay = b.x[mid] - b.x[a], b.y[mid] - b.y[a]
            nrm = math.hypot(ax, ay)
            if nrm > 1.0:
                ax, ay = ax / nrm, ay / nrm
                proj = (b.x[a:c + 1] - b.x[a]) * ax + (b.y[a:c + 1] - b.y[a]) * ay
                cmax = a + int(np.argmax(proj))
                if a + 2 <= cmax < c:
                    legs[-1] = (a, cmax, kind)
    return legs if len(legs) >= 2 else _segment_legs_vmg(b)


def _med_xy(pts):
    if len(pts) < 3:
        return None
    P = np.array(pts, float)
    return [float(np.median(P[:, 0])), float(np.median(P[:, 1]))]


def _upwind_legs(legs):   return [i for i, (a, c, k) in enumerate(legs) if k == "upwind"]
def _downwind_legs(legs): return [i for i, (a, c, k) in enumerate(legs) if k == "downwind"]


def detect_windward_mark(boats, legs_by_sail, ux, uy):
    """Mark 1 = each boat's furthest-upwind point on the FIRST beat; fleet median."""
    pts = []
    for b in boats:
        legs = legs_by_sail.get(b.sail) or []
        up = _upwind_legs(legs)
        if up and b.x is not None:
            a, c, _ = legs[up[0]]; p = b.x * ux + b.y * uy
            j = a + int(np.argmax(p[a:c + 1])); pts.append((b.lat[j], b.lon[j]))
    return _med_xy(pts)


def detect_wing_mark(boats, legs_by_sail, ux, uy):
    """Mark 2 (wing) = each boat's furthest-upwind point on the SECOND beat."""
    pts = []
    for b in boats:
        legs = legs_by_sail.get(b.sail) or []
        up = _upwind_legs(legs)
        if len(up) >= 2 and b.x is not None:
            a, c, _ = legs[up[1]]; p = b.x * ux + b.y * uy
            j = a + int(np.argmax(p[a:c + 1])); pts.append((b.lat[j], b.lon[j]))
    return _med_xy(pts)


def detect_gate(boats, legs_by_sail, ux, uy, lx, ly):
    """Leeward gate = furthest-downwind point of each run; split into two buoys
    when the cross-wind rounding positions are clearly bimodal. Returns 1-2 lls."""
    pts = []
    for b in boats:
        legs = legs_by_sail.get(b.sail) or []
        if b.x is None:
            continue
        p = b.x * ux + b.y * uy
        for di in _downwind_legs(legs):
            a, c, _ = legs[di]; j = a + int(np.argmin(p[a:c + 1]))
            pts.append((b.lat[j], b.lon[j], float(b.x[j] * lx + b.y[j] * ly)))
    if len(pts) < 6:
        gm = _med_xy([(la, lo) for la, lo, _ in pts])
        return [gm] if gm else []
    cw = np.sort(np.array([g[2] for g in pts]))
    gaps = np.diff(cw)
    if len(gaps) and gaps.max() > 80:                 # two distinct buoys
        split = cw[int(np.argmax(gaps))] + gaps.max() / 2
        out = []
        for grp in ([(la, lo) for la, lo, c in pts if c < split],
                    [(la, lo) for la, lo, c in pts if c >= split]):
            gm = _med_xy(grp)
            if gm:
                out.append(gm)
        return out
    gm = _med_xy([(la, lo) for la, lo, _ in pts])
    return [gm] if gm else []


def detect_finish(boats, legs_by_sail):
    """Finish ~ end of the last detected leg, fleet median."""
    pts = []
    for b in boats:
        legs = legs_by_sail.get(b.sail) or []
        if legs:
            pts.append((b.lat[legs[-1][1]], b.lon[legs[-1][1]]))
    return _med_xy(pts)


def detect_marks(boats, wind_from, legs_by_sail):
    """
    Locate the course marks at the fleet's actual rounding points by calling a
    dedicated detector per mark. Returns a list of {"label","ll":[lat,lon]}.
    """
    wf = math.radians(wind_from)
    ux, uy = math.sin(wf), math.cos(wf)
    lx, ly = uy, -ux                                  # cross-wind (along line)
    marks = []
    m1 = detect_windward_mark(boats, legs_by_sail, ux, uy)
    if m1:
        marks.append({"label": "1", "ll": m1})
    m2 = detect_wing_mark(boats, legs_by_sail, ux, uy)
    if m2:
        marks.append({"label": "2", "ll": m2})
    for g in detect_gate(boats, legs_by_sail, ux, uy, lx, ly):
        marks.append({"label": "G", "ll": g})
    fin = detect_finish(boats, legs_by_sail)
    if fin:
        marks.append({"label": "F", "ll": fin})
    return marks


def _segment_legs_vmg(b: Boat):
    """Fallback: split by sign of smoothed windward VMG (upwind/downwind only)."""
    if b.vmg is None or len(b.vmg) < 20:
        return []
    vw = _movavg(b.vmg, 21)
    sign = np.where(vw >= 0, 1, -1)
    legs, start = [], 0
    for i in range(1, len(sign)):
        if sign[i] != sign[start] and (b.t[i] - b.t[start]) > 60:
            legs.append((start, i, "upwind" if sign[start] > 0 else "downwind"))
            start = i
    legs.append((start, len(sign) - 1, "upwind" if sign[start] > 0 else "downwind"))
    return [L for L in legs if (b.t[L[1]] - b.t[L[0]]) > 90]


# --------------------------------------------------------------------------- #
# Fleet ranking over time (progress toward windward direction)
# --------------------------------------------------------------------------- #
def fleet_progress(boats, wind_from, grid):
    """
    For each boat, project position onto the wind axis (toward wind = +),
    interpolate onto a common time grid, and rank by who is furthest to
    windward at each instant (a proxy for race position upwind).
    """
    wf = math.radians(wind_from)
    ux, uy = math.sin(wf), math.cos(wf)        # unit vector pointing UPWIND
    prog = {}
    for b in boats:
        if b.x is None or len(b.x) < 2:
            continue
        p = b.x * ux + b.y * uy                 # along-wind coordinate (m)
        prog[b.sail] = np.interp(grid, b.t, p, left=np.nan, right=np.nan)
    return prog


# --------------------------------------------------------------------------- #
# Start line analysis (line inferred from fleet positions at the gun)
# --------------------------------------------------------------------------- #
def infer_start(boats, wind_from, start_t):
    """
    Infer the start line as the line through the fleet at the gun,
    perpendicular to the wind. Returns line endpoints + per-boat metrics.
    """
    wf = math.radians(wind_from)
    ux, uy = math.sin(wf), math.cos(wf)         # upwind unit
    lx, ly = uy, -ux                            # along-line unit (perp to wind)
    xs, ys = [], []
    for b in boats:
        if b.x is None:
            continue
        j = int(np.argmin(np.abs(b.t - start_t)))
        if abs(b.t[j] - start_t) <= 5:
            xs.append(b.x[j]); ys.append(b.y[j])
    if len(xs) < 5:
        return None
    xs = np.array(xs); ys = np.array(ys)

    # Reject stragglers: some trackers report a stale/shore position at the
    # first fix (boats parked km away at ~0 kt). Keep only boats within a robust
    # radius of the fleet centre so the line spans the real starters, not glitches.
    cx, cy = np.median(xs), np.median(ys)
    dist = np.hypot(xs - cx, ys - cy)
    mad = np.median(np.abs(dist - np.median(dist))) + 1e-6
    keep = (dist < np.median(dist) + 4 * 1.4826 * mad) & (dist < 600.0)
    if keep.sum() < 5:                          # fall back if too aggressive
        keep = dist < np.percentile(dist, 85)
    xs, ys = xs[keep], ys[keep]

    along = xs * lx + ys * ly
    perp = xs * ux + ys * uy
    line_perp = float(np.median(perp))
    a0, a1 = float(np.percentile(along, 2)), float(np.percentile(along, 98))
    return {
        "ux": ux, "uy": uy, "lx": lx, "ly": ly,
        "line_perp": line_perp, "along_min": a0, "along_max": a1,
        "n_starters": int(keep.sum()),
    }
