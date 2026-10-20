"""Tacks, gybes and penalty turns."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .constants import ILCA_LOA, KN
from .kinematics import wrap180
from .track import Boat


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
