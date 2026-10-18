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


def wind_timeseries(boats, wind0, grid, win=240.0):
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
        # track shifts: tight search around the mean so it shows real shifts, not noise
        c = _nogo_center(alla[m], wind0, search=20, halfwidth=30, minpts=60)
        if c is not None:
            out[i] = c
    return _circ_smooth(out, 9)


def apply_wind(b: Boat, wind_from):
    """Compute TWA, VMG (to windward, knots), and tack side."""
    rel = wrap180(b.cog - wind_from)        # -180..180, 0 = pointing at wind
    b.twa = np.abs(rel)
    b.vmg = b.sog * np.cos(np.radians(b.twa))   # + = gaining to windward
    # heading right of the wind => wind over the port side => +1 PORT, -1 STARBOARD
    b.tack = np.where(rel >= 0, 1, -1)
    return b


def dirty_air(boats, grid, wind_dir, dmax=35.0, half_angle=35.0):
    """
    Wind-shadow exposure on the common grid. Boat i is in dirty air at grid time
    k when another boat j sits upwind of it: closer than `dmax` m (~8 ILCA
    lengths) and inside a +-`half_angle` cone around the downwind direction from
    j. Works upwind and downwind (a blanketing boat behind you is upwind too).
    Returns {sail: str} one char per grid step: '-' not on the water, else the
    number of boats shadowing it (capped at 9).
    """
    nb, ng = len(boats), len(grid)
    X = np.full((nb, ng), np.nan); Y = np.full((nb, ng), np.nan)
    for i, b in enumerate(boats):
        m = (grid >= b.t[0]) & (grid <= b.t[-1])
        X[i, m] = np.interp(grid[m], b.t, b.x)
        Y[i, m] = np.interp(grid[m], b.t, b.y)
    cos_a = math.cos(math.radians(half_angle))
    out = np.full((nb, ng), -1, dtype=int)
    for k in range(ng):
        xs, ys = X[:, k], Y[:, k]
        ok = ~np.isnan(xs)
        if ok.sum() < 2:
            continue
        w = math.radians(float(wind_dir[k]))
        dwx, dwy = -math.sin(w), -math.cos(w)             # downwind unit
        dx = xs[:, None] - xs[None, :]                    # rows i = shadowed, cols j = shadowing
        dy = ys[:, None] - ys[None, :]
        d = np.hypot(dx, dy)
        with np.errstate(invalid="ignore", divide="ignore"):
            c = (dx * dwx + dy * dwy) / d
        sh = (d > 3.0) & (d < dmax) & (c >= cos_a) & ok[:, None] & ok[None, :]
        cnt = sh.sum(axis=1)
        out[ok, k] = cnt[ok]
    return {b.sail: "".join("-" if v < 0 else str(min(int(v), 9)) for v in out[i])
            for i, b in enumerate(boats)}


def detect_penalty_turns(b, t0, t1):
    """
    Penalty-turn candidates between t0 and t1: a large NET signed rotation in a
    short window while moving. A two-turn penalty is ~720 deg in one direction;
    a one-turn (mark touch) ~360. Tacks/gybes alternate direction and mark
    roundings are < ~200 deg, and GPS jitter has no net rotation, so they don't
    trigger. Returns [{t, type, deg, dur}].
    """
    idx = np.where((b.t >= t0) & (b.t <= t1))[0]
    if len(idx) < 12:
        return []
    t = b.t[idx]; cog = b.cog[idx]; sog = b.sog[idx]
    d = wrap180(np.diff(cog))                              # signed per-step turn
    cs = np.concatenate([[0.0], np.cumsum(d)])
    out, i, n = [], 0, len(t)
    while i < n - 2:
        best = None
        for win, need, kind in ((90.0, 600.0, "720"), (45.0, 300.0, "360")):
            j = int(np.searchsorted(t, t[i] + win))
            j = min(j, n - 1)
            net = cs[j] - cs[i]
            if abs(net) >= need and float(np.mean(sog[i:j + 1])) >= 1.2:
                best = (j, kind, net); break
        if best:
            j, kind, net = best
            out.append({"t": float(t[i]), "type": kind, "deg": round(float(net)),
                        "dur": round(float(t[j] - t[i]))})
            i = j + 1
        else:
            i += 1
    return out


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
# Leg segmentation — fits the race's course template (leg types) to each track
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


def detect_gun(boats, wind_from):
    """
    Estimate the start gun. TracTrac logging begins at the warning signal, so the
    first few minutes are the PRE-START; the gun is when the fleet stops jockeying
    and surges upwind together. We find that onset from the fleet-median upwind
    VMG and snap to the nearest round clock-minute (starts are set on the minute).
    """
    # window from the 25th percentile of track starts: a few trackers switched on early
    # can't shift it, nor can some switched on after the gun
    t0 = float(np.percentile([b.t[0] for b in boats], 25))
    grid = np.arange(t0, t0 + 900, 5.0)              # first 15 min
    V = [np.interp(grid, b.t, b.vmg, left=np.nan, right=np.nan)
         for b in boats if b.vmg is not None]
    if not V:
        return t0
    Vs = _movavg(np.nanmedian(np.array(V), axis=0), 7)
    gun = t0
    for k in range(len(grid) - 12):
        if Vs[k] > 1.0 and np.all(Vs[k:k + 12] > 0.3):   # sustained beating begins
            gun = grid[k]; break
    return float(round(gun / 60.0) * 60.0)               # snap to the clock minute


def segment_legs(b: Boat, wind_from, template=("U", "R", "D", "U", "D", "R"), gun_t=None,
                 with_index=False):
    """
    Split the race into the legs of the race's course by fitting the template's
    leg-type sequence (U/R/D, e.g. the trapezoid U,R,D,U,D,R) to the track with
    dynamic programming. Leg 1 is anchored at the gun (`gun_t`) so the pre-start
    is excluded; the DP labels the legs and a free trailing segment drops
    post-finish sailing, so shortened races (fewer legs) fall out on their own.
    `with_index=True` also returns each leg's template index (None for the
    template-free fallback split).
    """
    if b.vmg is None or len(b.t) < 30:
        return ([], []) if with_index else []
    # consecutive legs of the same type (e.g. the two reaches of a triangle) can't be
    # told apart by sailing mode: fit them as one block, then split the block at its
    # sharpest turns (the marks between them)
    groups = []                                      # [type, first template index, count]
    for k, c in enumerate(template):
        if groups and groups[-1][0] == c:
            groups[-1][2] += 1
        else:
            groups.append([c, k, 1])
    tmpl = [_MODE[gr[0]] for gr in groups]
    K = len(tmpl)
    n = len(b.t)
    mode = _mode_array(b)
    # f[m][i] = number of points in [0,i) whose mode != m  (segment cost helper)
    f = {m: np.concatenate([[0.0], np.cumsum((mode != m).astype(float))]) for m in (0, 1, 2)}

    INF = float("inf")
    g = [INF] * (n + 1)
    g0 = 0 if gun_t is None else int(np.argmin(np.abs(b.t - gun_t)))
    g[g0] = 0.0                                      # leg 1 starts at the gun (pre-start excluded)
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

    legs, ks = [], []
    for gi in range(K):
        a, c = bounds[gi], min(bounds[gi + 1], n - 1)
        if c - a < 2:
            continue
        typ, k0, cnt = groups[gi]
        edges = [a] + _block_splits(b, wind_from, typ, a, c, cnt) + [c]
        for j in range(len(edges) - 1):
            if edges[j + 1] - edges[j] >= 2:
                legs.append((edges[j], edges[j + 1], _KIND[tmpl[gi]]))
                ks.append(k0 + j)

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
    if len(legs) < 2:
        legs, ks = _segment_legs_vmg(b, g0), None
    return (legs, ks) if with_index else legs


def _block_splits(b, wind_from, typ, a, c, cnt, w=4, edge_s=60.0, min_turn=45.0):
    """Split indices for a block of cnt consecutive same-type legs over [a, c].

    Candidates stay >= edge_s from the block ends (the fitted boundaries lag the real
    roundings, whose turns would otherwise win). Two beats split at the furthest-upwind
    point and two runs at the furthest-downwind point (a tack/gybe turns more than the
    mark between them); otherwise at the sharpest turns of >= min_turn degrees, >= 60 s
    apart. Fewer cuts are returned when the block doesn't show them (shortened race)."""
    if cnt <= 1:
        return []
    lo = int(np.searchsorted(b.t, b.t[a] + edge_s)); hi = int(np.searchsorted(b.t, b.t[c] - edge_s))
    lo, hi = max(lo, a + w), min(hi, c - w)
    if hi - lo < 2:
        return []
    if typ in ("U", "D") and cnt == 2:
        wf = math.radians(wind_from)
        p = b.x[lo:hi] * math.sin(wf) + b.y[lo:hi] * math.cos(wf)
        j = int(np.argmax(p) if typ == "U" else np.argmin(p))
        return [lo + j] if 3 <= j < len(p) - 3 else []   # extreme at a block end: one leg only
    # persistent heading change: mean heading over 10-30 s after vs before, so a penalty
    # spin (which ends on its old heading) doesn't count as a mark rounding
    rad = np.radians(b.cog)
    cs = np.concatenate([[0.0], np.cumsum(np.cos(rad))]); sn = np.concatenate([[0.0], np.cumsum(np.sin(rad))])
    idx = np.arange(lo, hi)
    def mean_head(i0, i1):
        i0 = np.clip(i0, 0, len(rad)); i1 = np.clip(i1, 0, len(rad))
        return np.degrees(np.arctan2(sn[i1] - sn[i0], cs[i1] - cs[i0]))
    turn = np.abs(wrap180(mean_head(idx + 3, idx + 10) - mean_head(idx - 10, idx - 3)))
    cuts = []
    for o in np.argsort(-turn):
        if turn[o] < min_turn:
            break
        i = int(idx[o])
        if all(abs(b.t[i] - b.t[x]) >= 60 for x in cuts):
            cuts.append(i)
            if len(cuts) == cnt - 1:
                break
    return sorted(cuts)


def course_index(course, legs):
    """Course leg index for each of a boat's legs when the split didn't come from
    the course template (fallback): its n-th leg of a type is the course's n-th
    leg of that type (None where the course has no such leg)."""
    pos, seen = {}, {}
    for ci, leg in enumerate(course):
        pos[(leg["type"], seen.get(leg["type"], 0))] = ci
        seen[leg["type"]] = seen.get(leg["type"], 0) + 1
    letter = {_KIND[m]: t for t, m in _MODE.items()}
    out, cnt = [], {}
    for (_a, _c, kind) in legs:
        t = letter.get(kind)
        out.append(pos.get((t, cnt.get(t, 0))))
        cnt[t] = cnt.get(t, 0) + 1
    return out


def finish_by_convergence(boats, starts, origin, target, ref, step=5.0, max_spread=60.0,
                          funnel=0.7, min_reach=0.8, margin=40.0):
    """
    The finish line where the finishers' tracks converge: boats funnel between the
    committee boat and the pin, then spread out again as they keep sailing.

    `starts`: sail -> time the boat rounded the course's last mark before the finish;
    `origin`: that mark (x, y); `target`: rough finish area (x, y); `ref`: sails of the
    finishers. Along the axis origin -> target each boat's first passage of distance s
    is taken with its lateral offset; the line goes where those offsets are narrowest
    (10-90% width <= max_spread m and <= funnel x the median width along the scan — a
    funnel, not a single-file stretch — reached by >= min_reach of the finishers),
    spanning the 5-95% offsets. A boat finishes at its first crossing of that segment (+- margin).
    The finishers must slow right after it (they stop racing; a funnel mid-leg is followed by
    racing speed). Returns None (no convergence, e.g. tracking stopped first) or
    {"ends": [(x, y), (x, y)], "times": {sail: t}, "near": {sail: t}} — "near": for a boat
    with no crossing of the segment, its crossing of the line extended nearest the ends
    (<= 200 m out).
    """
    o = np.asarray(origin, float)
    D = float(np.hypot(*(np.asarray(target, float) - o)))
    if D < 1.0:
        return None
    u = (np.asarray(target, float) - o) / D
    nrm = np.array([-u[1], u[0]])
    tracks = {}
    for b in boats:
        t0 = starts.get(b.sail)
        if t0 is None or b.x is None:
            continue
        i0 = int(np.searchsorted(b.t, t0))
        if len(b.t) - i0 < 3:
            continue
        P = np.c_[b.x[i0:], b.y[i0:]] - o
        tracks[b.sail] = (b.t[i0:], P @ u, P @ nrm)
    n_ref = sum(1 for s in tracks if s in ref)
    if n_ref < 3:
        return None
    def width(s, need):
        lat = [c[int(np.argmax(a >= s))] for sail, (_t, a, c) in tracks.items() if sail in ref and a.max() >= s]
        return (lat, np.percentile(lat, [5, 10, 90, 95])) if len(lat) >= need else (lat, None)

    best, widths = None, []
    for s in np.arange(max(100.0, 0.3 * D), D + 400.0, step):
        _lat, q = width(s, max(3, min_reach * n_ref))
        if q is None:
            continue
        lo, p10, p90, hi = q
        widths.append(p90 - p10)
        if best is None or p90 - p10 < best[1]:
            best = (s, p90 - p10, lo, hi)
    if best is None or best[1] > max_spread or best[1] > funnel * float(np.median(widths)):
        return None
    # after the line the finishers spread out again (they stop racing); a single-file stretch
    # of the leg doesn't. Checked only while most of them are still tracked there.
    after = [width(best[0] + d, max(3, 0.5 * n_ref))[1] for d in (50.0, 100.0, 150.0)]
    after = [q[2] - q[1] for q in after if q is not None]
    if after and max(after) < 1.5 * max(best[1], 5.0):
        return None
    s, _w, lo, hi = best
    times, near = {}, {}
    for sail, (t, a, c) in tracks.items():
        best_out = None
        for j in np.where((a[:-1] < s) & (a[1:] >= s))[0]:
            f = (s - a[j]) / (a[j + 1] - a[j])
            tj, cj = float(t[j] + f * (t[j + 1] - t[j])), float(c[j] + f * (c[j + 1] - c[j]))
            if lo - margin <= cj <= hi + margin:
                times[sail] = tj
                break
            out = lo - cj if cj < lo else cj - hi       # how far outside the line's ends
            if out <= 200.0 and (best_out is None or out < best_out[0]):
                best_out = (out, tj)
        if sail not in times and best_out:
            near[sail] = best_out[1]
    # finishers slow from racing speed within a few minutes of the line; without that evidence
    # (most of them still tracked a while after it) a funnel isn't taken as the finish
    slow = []
    for b in boats:
        ft = times.get(b.sail)
        if ft is None or b.sail not in ref or b.sog is None:
            continue
        m1, m2 = (b.t >= ft - 60) & (b.t < ft), (b.t >= ft + 60) & (b.t < ft + 180)
        if m1.sum() > 2 and m2.sum() > 2:
            slow.append(float(np.median(b.sog[m2])) / max(float(np.median(b.sog[m1])), 0.1))
    if len(slow) < max(3, 0.5 * len(times)) or float(np.median(slow)) > 0.7:
        return None
    ends = [tuple(o + s * u + w * nrm) for w in (lo - 10.0, hi + 10.0)]
    return {"ends": ends, "times": times, "near": near}


def _med_xy(pts, need=3):
    if len(pts) < need or not pts:
        return None
    P = np.array(pts, float)
    return [float(np.median(P[:, 0])), float(np.median(P[:, 1]))]


def _refine_turn(b, idx, win=12):
    """Index of the sharpest heading change near idx (the rounding/bear-away apex)."""
    n = len(b.cog); lo, hi = max(0, idx - win), min(n - 1, idx + win)
    best, bj = -1.0, idx
    for k in range(lo, hi):
        d = abs(((b.cog[min(k + 4, n - 1)] - b.cog[max(k - 4, 0)] + 180) % 360) - 180)
        if d > best:
            best, bj = d, k
    return bj


def detect_finish(boats, legs_by_sail, need=3):
    """Finish ~ end of the last detected leg, fleet median."""
    pts = []
    for b in boats:
        legs = legs_by_sail.get(b.sail) or []
        if legs:
            pts.append((b.lat[legs[-1][1]], b.lon[legs[-1][1]]))
    return _med_xy(pts, need)


def _split_gate(pts, need=3, min_sep=15.0, sharp=2.8):
    """Gate buoys from (lat, lon, crosswind) rounding points: two buoys when the
    cross-wind positions form two clear groups, else one. The best two-way split
    (least within-group variance, >= 15% of the roundings each side so a stray
    point can't decide it) counts when its group medians are >= min_sep m apart
    and >= `sharp` x the groups' spread (IQR): one buoy's roundings split that way
    give ~2 (p99 ~3); a 40 m ILCA gate gives 3-7."""
    if len(pts) < 6:
        gm = _med_xy([(la, lo) for la, lo, _ in pts], need)
        return [gm] if gm else []
    pts = sorted(pts, key=lambda g: g[2])
    cw = np.array([g[2] for g in pts])
    n = len(cw)
    m = max(3, int(0.15 * n))
    best = None
    for k in range(m, n - m + 1):
        w = k * cw[:k].var() + (n - k) * cw[k:].var()
        if best is None or w < best[0]:
            best = (w, k)
    if best:
        k = best[1]
        a, b = cw[:k], cw[k:]
        iqr = lambda v: float(np.subtract(*np.percentile(v, [75, 25])))
        sep = float(np.median(b) - np.median(a))
        if sep >= min_sep and sep >= sharp * max((iqr(a) + iqr(b)) / 2, 3.0):
            out = [_med_xy([(la, lo) for la, lo, _ in grp], need) for grp in (pts[:k], pts[k:])]
            return [g for g in out if g]
    gm = _med_xy([(la, lo) for la, lo, _ in pts], need)
    return [gm] if gm else []


def detect_marks(boats, wind_from, legs_by_sail, course, cix_by_sail):
    """
    Locate the course marks at the fleet's actual rounding points, driven by the
    race's course definition: a list of legs {"type": U|R|D, "mark": label,
    "gate": bool}, each ending at its mark; the last leg ends at the finish.

    Each boat's legs are matched to course legs by `cix_by_sail` (course leg
    index per boat leg, None for legs outside the course). Candidate rounding points per boat: the
    furthest-upwind point of a beat / furthest-downwind point of a run, and the
    turn apex at the leg end. Per mark the candidate the fleet's tracks pass
    closest to wins (beats into the top mark favour the furthest-upwind point,
    beats into a wing mark the bear-away apex). Gates use the furthest-downwind
    points of the runs followed by a beat, split into two buoys when the fleet
    used both.
    Returns a list of {"label", "ll": [lat, lon]}.
    """
    wf = math.radians(wind_from)
    ux, uy = math.sin(wf), math.cos(wf)
    lx, ly = uy, -ux                                  # cross-wind (along line)
    K = len(course)
    fin_label = course[-1]["mark"] if K else "F"
    gate, order = {}, []
    for leg in course[:-1]:
        L = leg["mark"]
        gate[L] = gate.get(L, False) or bool(leg.get("gate"))
        if L not in order:
            order.append(L)
    cand = {L: {"ext": [], "apex": []} for L in order}
    wins = {L: [] for L in order}                     # (boat, i0, i1) track spans around the rounding
    for b in boats:
        legs = legs_by_sail.get(b.sail) or []
        if b.x is None or not legs:
            continue
        p = b.x * ux + b.y * uy
        for li, ci in enumerate(cix_by_sail[b.sail]):
            if ci is None or ci >= K - 1:             # unmatched leg / the finish leg
                continue
            a, c, _ = legs[li]
            L, T = course[ci]["mark"], course[ci]["type"]
            # a run's furthest-downwind point is its rounding only when a beat follows (a reach
            # after it, e.g. to the finish, goes further downwind)
            if T == "U" or (T == "D" and course[ci + 1]["type"] == "U"):
                j = a + int(np.argmax(p[a:c + 1]) if T == "U" else np.argmin(p[a:c + 1]))
                cand[L]["ext"].append((b.lat[j], b.lon[j], float(b.x[j] * lx + b.y[j] * ly)))
            j = _refine_turn(b, c)
            cand[L]["apex"].append((b.lat[j], b.lon[j], float(b.x[j] * lx + b.y[j] * ly)))
            nxt = legs[li + 1][1] if li + 1 < len(legs) else min(len(b.t) - 1, c + 60)
            wins[L].append((b, a, nxt))

    def closest_pass(ll, spans):
        d = [float(np.min(np.hypot(*to_xy(b.lat[i0:i1 + 1], b.lon[i0:i1 + 1], ll[0], ll[1])[:2])))
             for b, i0, i1 in spans if i1 > i0]
        return float(np.median(d)) if d else float("inf")

    need = min(3, len(boats))                     # tiny fleets (e.g. 2 training boats) still get marks
    marks = []
    for L in order:
        if gate[L]:
            src = cand[L]["ext"] if len(cand[L]["ext"]) >= need else cand[L]["apex"]
            lls = _split_gate(src, need)
        else:
            opts = [_med_xy([(la, lo) for la, lo, _ in cand[L][k]], need) for k in ("ext", "apex")]
            opts = [o for o in opts if o]
            lls = [min(opts, key=lambda ll: closest_pass(ll, wins[L]))] if opts else []
        marks += [{"label": L, "ll": ll} for ll in lls]
    fin = detect_finish(boats, legs_by_sail, need)
    if fin:
        marks.append({"label": fin_label, "ll": fin})
    return marks


def _segment_legs_vmg(b: Boat, g0=0):
    """Fallback: split by sign of smoothed windward VMG (upwind/downwind only),
    from index g0 (the gun) so the pre-start isn't counted as a leg."""
    if b.vmg is None or len(b.vmg) - g0 < 20:
        return []
    vw = _movavg(b.vmg, 21)
    sign = np.where(vw >= 0, 1, -1)
    legs, start = [], g0
    for i in range(g0 + 1, len(sign)):
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
