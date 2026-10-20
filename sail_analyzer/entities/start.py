"""The gun and the start line."""

from __future__ import annotations

import math
from datetime import datetime, timezone

import numpy as np

from .kinematics import _movavg


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


def gun_from_clock(clock: str, t0: float):
    """'HH:MM[:SS]' (UTC) on the day of the first fix -> epoch seconds."""
    parts = [int(x) for x in clock.strip().split(":")]
    h, m, sec = (parts + [0, 0])[:3]
    day = datetime.fromtimestamp(t0, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    g = day.timestamp() + h * 3600 + m * 60 + sec
    if g < t0 - 3 * 3600:                         # track started the evening before
        g += 86400
    return float(g)
