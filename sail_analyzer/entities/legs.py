"""Splitting a track into the course's legs."""

from __future__ import annotations

import math

import numpy as np

from .kinematics import _movavg, wrap180
from .track import Boat


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
