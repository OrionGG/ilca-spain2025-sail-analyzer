"""Wind direction from the fleet's tracks (no-go zone), over time, and per boat."""

from __future__ import annotations

import math

import numpy as np

from .kinematics import _circ_smooth, wrap180
from .track import Boat


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
