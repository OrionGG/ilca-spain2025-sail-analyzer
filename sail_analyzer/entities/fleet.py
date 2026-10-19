"""Fleet-level rules: wind shadow (dirty air) and progress ranking."""

from __future__ import annotations

import math

import numpy as np


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
