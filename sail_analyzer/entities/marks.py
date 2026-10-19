"""Course marks at the fleet's actual rounding points; gates."""

from __future__ import annotations

import math

import numpy as np

from .finish import detect_finish
from .kinematics import _med_xy, to_xy


def _refine_turn(b, idx, win=12):
    """Index of the sharpest heading change near idx (the rounding/bear-away apex)."""
    n = len(b.cog); lo, hi = max(0, idx - win), min(n - 1, idx + win)
    best, bj = -1.0, idx
    for k in range(lo, hi):
        d = abs(((b.cog[min(k + 4, n - 1)] - b.cog[max(k - 4, 0)] + 180) % 360) - 180)
        if d > best:
            best, bj = d, k
    return bj


def _split_gate(pts, need=3, min_sep=25.0, max_sep=150.0, max_ratio=0.2):
    """Gate buoys from (lat, lon, crosswind) rounding points: two buoys when the
    cross-wind positions form two clear groups, else one. Each group needs >= 15% of
    the roundings (at least 4), so strays (fewer than that, beyond a 30 m gap at either
    end) are dropped first, when enough roundings remain for two groups. The best
    two-way split (least within-group variance) counts when the groups keep
    <= max_ratio of the variance and their medians are min_sep..max_sep m apart (an
    ILCA gate is ~30-50 m wide; a far-off group is strays, not a buoy). One buoy's
    roundings keep ~0.36 (normal spread; < 0.2 in <0.5% of draws up to sd 10 m), the
    Copa 2025 gates 0.05-0.19."""
    if len(pts) < 6:
        gm = _med_xy([(la, lo) for la, lo, _ in pts], need)
        return [gm] if gm else []
    pts = sorted(pts, key=lambda g: g[2])
    cw = np.array([g[2] for g in pts])
    n = len(cw)
    m = max(4, int(0.15 * n))
    gaps = np.diff(cw)
    i0 = max([t for t in range(1, m) if gaps[t - 1] >= 30.0], default=0)
    i1 = n - max([t for t in range(1, m) if gaps[n - 1 - t] >= 30.0], default=0)
    if i1 - i0 >= 2 * m:
        pts, cw = pts[i0:i1], cw[i0:i1]
        n = len(cw)
    if n >= 2 * m:
        s1, s2 = np.cumsum(cw), np.cumsum(cw * cw)    # within-group sum of squares per split
        ks = np.arange(m, n - m + 1)
        sse = (s2[ks - 1] - s1[ks - 1] ** 2 / ks) + ((s2[-1] - s2[ks - 1]) - (s1[-1] - s1[ks - 1]) ** 2 / (n - ks))
        tot = s2[-1] - s1[-1] ** 2 / n
        j = int(np.argmin(sse))
        k = int(ks[j])
        sep = float(np.median(cw[k:]) - np.median(cw[:k]))
        if tot > 0 and sse[j] <= max_ratio * tot and min_sep <= sep <= max_sep:
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
