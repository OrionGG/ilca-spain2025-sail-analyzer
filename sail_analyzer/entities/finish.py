"""The finish: where the finishers' tracks converge; the fallback estimate."""

from __future__ import annotations

import numpy as np

from .kinematics import _med_xy


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


def detect_finish(boats, legs_by_sail, need=3):
    """Finish ~ end of the last detected leg, fleet median."""
    pts = []
    for b in boats:
        legs = legs_by_sail.get(b.sail) or []
        if legs:
            pts.append((b.lat[legs[-1][1]], b.lon[legs[-1][1]]))
    return _med_xy(pts, need)
