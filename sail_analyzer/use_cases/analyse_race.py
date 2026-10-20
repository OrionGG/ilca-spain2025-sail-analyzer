"""AnalyseRace: from a race's boat tracks and course to the race analysis.

The phases follow the race: kinematics and wind, the gun, legs and their course legs, the
finishers, the finish estimate, the marks, the start line, the mark roundings, the finish line
where the tracks converge, tactical inputs, fleet statistics and each boat's summary.

The response model holds unrounded values; shaping them for display (rounding, colours, the
API's JSON) is the presenter's job.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np

from sail_analyzer.entities.finish import finish_by_convergence
from sail_analyzer.entities.fleet import dirty_air, fleet_progress
from sail_analyzer.entities.kinematics import derive_kinematics
from sail_analyzer.entities.legs import course_index, segment_legs
from sail_analyzer.entities.maneuvers import detect_maneuvers, detect_penalty_turns
from sail_analyzer.entities.marks import _refine_turn, detect_marks
from sail_analyzer.entities.start import detect_gun, gun_from_clock, infer_start
from sail_analyzer.entities.track import Boat
from sail_analyzer.entities.wind import apply_wind, estimate_wind_dir, wind_timeseries

from .errors import AnalysisError
from .gateways import TrackGateway


# --------------------------------------------------------------------------- #
# Request and response models
# --------------------------------------------------------------------------- #
@dataclass
class RaceRequest:
    tracks_dir: str               # the race's tracks folder (resolved by the TrackGateway)
    name: str
    course: list                  # legs: {"type": "U" | "R" | "D", "mark": label, "gate": bool}
    gun: str | None = None        # "HH:MM[:SS]" UTC; None = detect it from the tracks
    key: str | None = None        # hand-placed-marks key; None = the race name


@dataclass
class BoatAnalysis:
    boat: Boat                    # the track with its derived kinematics
    legs: list                    # [(i0, i1, kind)] in the boat's track
    course_ix: list               # course leg of each leg (None = outside the course)
    rounds: list                  # rounding / finish time per leg (None = not seen)
    maneuvers: list               # the race's maneuvers (gun -> finish)
    penalties: list
    shadow: str                   # dirty-air exposure on the time grid
    course_grid: np.ndarray       # cumulative made-good distance on the time grid
    progress: np.ndarray | None
    summary: dict                 # race summary, unrounded


@dataclass
class RaceAnalysis:
    name: str
    key: str
    course: list
    gun_manual: bool
    start_t: float
    gun_t: float
    wind_from: float
    lat0: float
    lon0: float
    marks: list                   # [{"label", "ll": [lat, lon]}]
    start_line: list | None       # [pin, rc]
    grid: np.ndarray              # common 5 s time grid
    wind_series: np.ndarray       # wind direction on the grid
    fleet: dict                   # sog_p50 / sog_p25 / sog_p75 / vmg_p50 on the grid (finishers)
    boats: list                   # BoatAnalysis, by sail


# --------------------------------------------------------------------------- #
# The interactor
# --------------------------------------------------------------------------- #
class AnalyseRace:
    def __init__(self, tracks: TrackGateway):
        self.tracks = tracks

    def execute(self, req: RaceRequest) -> RaceAnalysis | None:
        """The race analysis, or None when the folder has no usable tracks."""
        boats = self.tracks.boats(req.tracks_dir)
        if not boats:
            return None
        if any(b.x is not None for b in boats):          # derived into below: must be fresh entities
            raise ValueError("TrackGateway returned boats that were already analysed (return fresh ones).")
        if len({b.sail for b in boats}) != len(boats):
            raise ValueError("TrackGateway returned boats with the same sail id.")
        course = req.course
        lat0, lon0 = _kinematics(boats)
        wind_from = estimate_wind_dir(boats)
        for b in boats:
            apply_wind(b, wind_from)

        # common time grid (5 s)
        t_start = min(b.t[0] for b in boats)
        t_end = max(b.t[-1] for b in boats)
        grid = np.arange(t_start, t_end + 1, 5.0)

        wind_series = wind_timeseries(boats, wind_from, grid)
        progress = fleet_progress(boats, wind_from, grid)

        gun_t = _gun(boats, wind_from, req.gun)
        legs_by_sail, finished, cix_by_sail, mode_legs, line_legs = _legs(boats, wind_from, course, gun_t)
        finish_t = _finish_estimate(boats, legs_by_sail, finished, cix_by_sail, mode_legs, line_legs)
        fin_ci = _final_course_leg(course, legs_by_sail, finished, cix_by_sail)

        # course marks at the finishers' actual rounding points, labelled as in the course
        ref = [b for b in boats if finished[b.sail]]
        if len(ref) < 3:                              # tiny fleet / no course fit: use every boat
            ref = boats
        marks = detect_marks(ref, wind_from, legs_by_sail, course, cix_by_sail)

        start_t = float(gun_t)
        start_line = _start_line(boats, wind_from, start_t, lat0, lon0, marks)
        rounds_by_sail, mark_xy = _roundings(boats, course, marks, legs_by_sail, cix_by_sail, gun_t, lat0, lon0)
        gps_finish, marks = _convergence_finish(boats, course, marks, mark_xy, legs_by_sail, cix_by_sail,
                                                finished, finish_t, rounds_by_sail, fin_ci, lat0, lon0)

        # ---- tactical inputs: wind-shadow exposure (all boats) + penalty-turn candidates ----
        shadow = dirty_air(boats, grid, wind_series)
        penalties = {b.sail: detect_penalty_turns(b, gun_t + 20.0, finish_t.get(b.sail, float(b.t[-1])))
                     for b in boats}

        fleet = _fleet_stats(boats, finished, grid)
        out = [_boat(b, wind_from, gun_t, grid, legs_by_sail[b.sail], cix_by_sail[b.sail], finished[b.sail],
                     b.sail in gps_finish, finish_t, rounds_by_sail, shadow, penalties, progress)
               for b in sorted(boats, key=lambda x: x.sail)]

        return RaceAnalysis(
            name=req.name, key=req.key or req.name, course=course, gun_manual=bool(req.gun),
            start_t=start_t, gun_t=float(gun_t), wind_from=wind_from, lat0=lat0, lon0=lon0,
            marks=marks, start_line=start_line, grid=grid, wind_series=wind_series, fleet=fleet, boats=out)


# --------------------------------------------------------------------------- #
# Phases
# --------------------------------------------------------------------------- #
def _kinematics(boats):
    """The local origin (fleet mean position); each boat's x/y, speed and course."""
    lat0 = float(np.mean([b.lat.mean() for b in boats]))
    lon0 = float(np.mean([b.lon.mean() for b in boats]))
    for b in boats:
        derive_kinematics(b, lat0, lon0)
    return lat0, lon0


def _gun(boats, wind_from, gun):
    """The gun time: the given clock time, else detected.

    The gun is a round clock-minute ~5 min after logging starts (logging begins
    at the warning signal; the first minutes are the pre-start). Leg 1 is
    anchored there so the pre-start is excluded from the race legs."""
    gun_t = (gun_from_clock(gun, float(np.median([b.t[0] for b in boats]))) if gun
             else detect_gun(boats, wind_from))
    if gun:
        first, last = float(np.median([b.t[0] for b in boats])), max(float(b.t[-1]) for b in boats)
        if not first - 1800 <= gun_t <= last - 300:
            hm = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%H:%M")  # noqa: E731
            raise AnalysisError(f"Gun {gun} UTC is outside the tracks ({hm(first)}–{hm(last)} UTC).")
    return gun_t


def _legs(boats, wind_from, course, gun_t):
    """Each boat's legs fitted to the course, which boats finished, and each leg's course leg.
    Returns (legs_by_sail, finished, cix_by_sail, mode_legs, line_legs)."""
    template = tuple(leg["type"] for leg in course)
    seg = {b.sail: segment_legs(b, wind_from, template=template, gun_t=gun_t, with_index=True)
           for b in boats}
    legs_by_sail = {s: lg for s, (lg, _) in seg.items()}
    # a boat "finished" if it sailed the full course (>= the fleet's modal leg count).
    # Boats with fewer legs are retirees/DNF and are excluded from fleet metrics.
    _lc = {}
    for lg, ks in seg.values():
        if ks is not None:                        # fallback up/down splits say nothing about the course
            _lc[len(lg)] = _lc.get(len(lg), 0) + 1
    # The course length the fleet sailed: the largest leg count held by >= 25% of the
    # boats (so retirees can't outvote finishers); if the count one below is that common
    # too, use it: one leg apart is a merged leg or post-finish sailing, not a retirement.
    common = sorted((c for c in _lc if c > 0 and _lc[c] >= 0.25 * sum(_lc.values())), reverse=True)
    mode_legs = common[0] if common else max((c for c in _lc if c > 0), key=_lc.get, default=0)
    line_legs = mode_legs                         # finish line from boats that sailed the most legs
    if mode_legs - 1 in common:
        mode_legs -= 1
    finished = {s: bool(lg) and ks is not None and len(lg) >= mode_legs   # fallback split: no course fit
                for s, (lg, ks) in seg.items()}
    # course leg of each boat leg: a finisher's DP template index; for a retiree (whose
    # few legs the DP can stretch onto the last template legs) and for the fallback
    # split, the n-th leg of a type = the course's n-th leg of that type
    cix_by_sail = {s: (ks if ks is not None and finished[s] else course_index(course, lg))
                   for s, (lg, ks) in seg.items()}
    return legs_by_sail, finished, cix_by_sail, mode_legs, line_legs


def _finish_estimate(boats, legs_by_sail, finished, cix_by_sail, mode_legs, line_legs):
    """Finish times by LINE CROSSING (not leg end, which drifts minutes). Updates the
    finishers' extra legs in legs_by_sail / cix_by_sail; returns finish_t by sail.

    Finish line: through the fleet-median last-turn apex, perpendicular to the
    fleet-median final-leg direction. A boat finishes the first time its
    projection along that direction passes the apex. Same reference for all
    boats -> reliable finish ORDER (validated against official results)."""
    apexes, fdirs = [], []
    for b in boats:
        lg = legs_by_sail[b.sail]
        if not finished[b.sail] or len(lg) != line_legs:
            continue
        a, c, _ = lg[-1]
        j = _refine_turn(b, c)                      # sharpest turn ~ the line
        apexes.append((b.x[j], b.y[j]))
        dx, dy = b.x[c] - b.x[a], b.y[c] - b.y[a]
        nn = float(np.hypot(dx, dy))
        if nn > 1:
            fdirs.append((dx / nn, dy / nn))
    finish_t = {}
    if len(apexes) >= 5 and fdirs:
        Fx = float(np.median([q[0] for q in apexes])); Fy = float(np.median([q[1] for q in apexes]))
        fux = float(np.median([v[0] for v in fdirs])); fuy = float(np.median([v[1] for v in fdirs]))
        nn = float(np.hypot(fux, fuy)); fux /= nn; fuy /= nn
    else:
        Fx = None
    for b in boats:
        lg = legs_by_sail[b.sail]
        if not lg:
            continue
        ft = float(b.t[lg[-1][1]])                 # no line crossing seen: end of its last leg
        crossed = False
        if Fx is not None and finished[b.sail]:
            # search from the start of the leg where the fleet finishes (a boat can have
            # extra legs) to the END OF TRACK: the leg-end trim can stop short of the line
            a, c, k = lg[mode_legs - 1]
            proj = (b.x[a:] - Fx) * fux + (b.y[a:] - Fy) * fuy
            hit = np.where(proj >= 0)[0]
            if len(hit):
                ft, crossed = float(b.t[a + int(hit[0])]), True
        finish_t[b.sail] = ft
        # a finisher's legs beyond the fleet's count (a final leg the fleet's own fits
        # merged, or post-finish sailing) merge into the fleet's last leg, which ends at the
        # line when the crossing is seen, so every finisher's legs compare like for like
        if finished[b.sail] and len(lg) > mode_legs:
            a, c, k = lg[mode_legs - 1]
            end = int(np.searchsorted(b.t, ft)) if crossed else lg[-1][1]
            legs_by_sail[b.sail] = lg[:mode_legs - 1] + [(a, max(a + 1, min(end, len(b.t) - 1)), k)]
            cix_by_sail[b.sail] = cix_by_sail[b.sail][:mode_legs]
    return finish_t


def _final_course_leg(course, legs_by_sail, finished, cix_by_sail):
    """The fleet's final course leg, which every finisher's last leg becomes (the fit can label
    the same physical final leg with different template legs, e.g. in a shortened race).
    Updates legs_by_sail / cix_by_sail; returns that course leg's index (None if no finisher)."""
    lasts = [cix_by_sail[s][-1] for s in legs_by_sail if finished[s] and cix_by_sail[s]]
    fin_ci = max(set(lasts), key=lasts.count) if lasts else None
    kind_of = {"U": "upwind", "R": "reach", "D": "downwind"}
    for s in legs_by_sail:
        cx = cix_by_sail[s]
        if fin_ci is None or not finished[s] or not cx or cx[-1] == fin_ci:
            continue
        if len(cx) < 2 or (cx[-2] is not None and cx[-2] < fin_ci):
            a, c, _ = legs_by_sail[s][-1]
            legs_by_sail[s] = legs_by_sail[s][:-1] + [(a, c, kind_of[course[fin_ci]["type"]])]
            cix_by_sail[s] = cx[:-1] + [fin_ci]
    return fin_ci


def _start_line(boats, wind_from, start_t, lat0, lon0, marks):
    """The start line at the gun, with its ends added to marks: RC (committee boat,
    starboard end) and Pin (port end). Starboard = +along-line (wind_from + 90 deg).
    Returns [pin, rc] or None."""
    startinfo = infer_start(boats, wind_from, start_t)
    start_line = None
    if startinfo:
        s = startinfo
        def xy_to_ll(px, py):
            mlat = 111320.0; mlon = 111320.0 * np.cos(np.radians(lat0))
            return [lat0 + py / mlat, lon0 + px / mlon]
        pe1 = s["line_perp"]
        pin = xy_to_ll(pe1 * s["ux"] + s["along_min"] * s["lx"],
                       pe1 * s["uy"] + s["along_min"] * s["ly"])
        rc = xy_to_ll(pe1 * s["ux"] + s["along_max"] * s["lx"],
                      pe1 * s["uy"] + s["along_max"] * s["ly"])
        start_line = [pin, rc]
        marks.append({"label": "RC", "ll": rc})       # committee boat (starboard)
        marks.append({"label": "Pin", "ll": pin})     # pin end (port)
    return start_line


def _roundings(boats, course, marks, legs_by_sail, cix_by_sail, gun_t, lat0, lon0):
    """Mark-rounding times by PROXIMITY (first close approach to each buoy).
    Returns (rounds_by_sail, the marks' x/y by label).

    Leg boundaries come from ~60s-smoothed VMG, so their times are far too noisy
    to order the fleet at a mark; the actual closest pass is sharp and reliable."""
    cosLat = float(np.cos(np.radians(lat0)))
    def _ll_xy(ll):
        return ((ll[1] - lon0) * 111320.0 * cosLat, (ll[0] - lat0) * 111320.0)
    _mk = {}
    for m in marks:
        _mk.setdefault(m["label"], []).append(_ll_xy(m["ll"]))
    seq_xy = [_mk.get(leg["mark"], []) for leg in course]           # targets per leg end
    rounds_by_sail = {}
    for b in boats:
        lg = legs_by_sail[b.sail]
        gi0 = int(np.argmin(np.abs(b.t - gun_t)))
        rounds, k0 = [], gi0
        for li in range(len(lg)):
            ci = cix_by_sail[b.sail][li]
            tgts = seq_xy[ci] if ci is not None else []
            if not tgts or k0 >= len(b.t) - 2:
                rounds.append(None); continue
            dist = np.min(np.stack([np.hypot(b.x[k0:] - tx, b.y[k0:] - ty)
                                    for tx, ty in tgts]), axis=0)
            near = np.where(dist < 100.0)[0]
            if len(near):
                j = int(near[0])
                while j + 1 < len(dist) and dist[j + 1] <= dist[j]:
                    j += 1                                          # walk to the local apex
            else:
                j = int(np.argmin(dist))                            # never close: best pass
            rounds.append(float(b.t[k0 + j])); k0 = k0 + j + 1
        rounds_by_sail[b.sail] = rounds
    return rounds_by_sail, _mk


def _convergence_finish(boats, course, marks, _mk, legs_by_sail, cix_by_sail, finished, finish_t,
                        rounds_by_sail, fin_ci, lat0, lon0):
    """The finish line where the finishers' tracks converge (between the committee boat and
    the pin; they spread out again as they keep sailing). Boats crossing it get that finish
    time (a finisher passing just outside its ends: the line extended); without such a line
    (e.g. tracking stopped first) the estimate stays. Updates legs_by_sail, rounds_by_sail,
    finish_t and finished; returns (the sails that finished by crossing it, the marks with
    the finish line's ends)."""
    cosLat = float(np.cos(np.radians(lat0)))
    gps_finish = set()
    pci = (len(course) - 2 if fin_ci >= len(course) - 2 else fin_ci - 1) if fin_ci is not None else None
    if pci is not None and pci >= 0 and _mk.get(course[pci]["mark"]):
        starts = {}
        for b in boats:
            li = next((i for i, c in enumerate(cix_by_sail[b.sail]) if c == pci), None)
            if li is not None and rounds_by_sail[b.sail][li] is not None:
                starts[b.sail] = rounds_by_sail[b.sail][li]
            elif finished[b.sail] and legs_by_sail[b.sail] and cix_by_sail[b.sail] \
                    and cix_by_sail[b.sail][-1] == fin_ci:      # no rounding pass seen: its final leg's start
                starts[b.sail] = float(b.t[legs_by_sail[b.sail][-1][0]])
        fins = [b for b in boats if finished[b.sail]]
        if fins:
            target = np.median([(b.x[legs_by_sail[b.sail][-1][1]], b.y[legs_by_sail[b.sail][-1][1]])
                                for b in fins], axis=0)
            conv = finish_by_convergence(boats, starts, np.mean(_mk[course[pci]["mark"]], axis=0),
                                         target, {b.sail for b in fins})
            # sanity: a real line is crossed no later than the previous estimate (which runs late,
            # to the leg end); a course shortened at its last mark would put a "line" in
            # post-finish sailing, i.e. clearly later
            dt = [t - finish_t[s] for s, t in (conv or {}).get("times", {}).items()
                  if s in finish_t and cix_by_sail[s] and cix_by_sail[s][-1] == fin_ci]
            if conv and dt and float(np.median(dt)) <= 60.0:
                for b in boats:
                    lg, ft = legs_by_sail[b.sail], conv["times"].get(b.sail)
                    if ft is None and finished[b.sail]:
                        ft = conv["near"].get(b.sail)
                    # only a boat that sailed the final leg finishes by crossing (a retiree
                    # sailing home through the finish area doesn't)
                    if ft is None or not lg or not cix_by_sail[b.sail] or cix_by_sail[b.sail][-1] != fin_ci:
                        continue
                    a, _c, k = lg[-1]                 # the last leg (and race window) ends at the line
                    legs_by_sail[b.sail] = lg[:-1] + [(a, max(a + 1, min(int(np.searchsorted(b.t, ft)), len(b.t) - 1)), k)]
                    rounds_by_sail[b.sail][-1] = ft
                    finish_t[b.sail] = ft
                    finished[b.sail] = True
                    gps_finish.add(b.sail)
                fl = course[-1]["mark"]
                to_ll = lambda px, py: [lat0 + py / 111320.0, lon0 + px / (111320.0 * cosLat)]  # noqa: E731
                marks = [m for m in marks if m["label"] != fl] + [{"label": fl, "ll": to_ll(*e)} for e in conv["ends"]]
    return gps_finish, marks


def _fleet_stats(boats, finished, grid):
    """Fleet aggregate stats on the grid (FINISHERS only — exclude retirees/DNF)."""
    ref = [b for b in boats if finished[b.sail]]  # incl. boats seen crossing the finish line
    if len(ref) < 3:
        ref = boats
    sog_grid, vmg_grid = [], []
    for b in ref:                                 # finishers (every boat if fewer than 3)
        sog_grid.append(np.interp(grid, b.t, b.sog, left=np.nan, right=np.nan))
        vmg_grid.append(np.interp(grid, b.t, b.vmg, left=np.nan, right=np.nan))
    sog_grid = np.array(sog_grid); vmg_grid = np.array(vmg_grid)
    with np.errstate(all="ignore"):
        return {
            "sog_p50": np.nanmedian(sog_grid, axis=0),
            "sog_p25": np.nanpercentile(sog_grid, 25, axis=0),
            "sog_p75": np.nanpercentile(sog_grid, 75, axis=0),
            "vmg_p50": np.nanmedian(vmg_grid, axis=0),
        }


def _boat(b, wind_from, gun_t, grid, legs, cix, finished, gps_finish, finish_t, rounds_by_sail, shadow,
          penalties, progress):
    """One boat's race: its maneuvers, unrounded summary and position ladder."""
    mans = detect_maneuvers(b, wind_from)
    # RACE window only: gun -> finish (exclude pre-start and post-finish)
    gi = int(np.argmin(np.abs(b.t - gun_t)))
    fi = legs[-1][1] if legs else len(b.t) - 1
    sl = slice(gi, fi + 1)
    sog_s, twa_s, vmg_s = b.sog[sl], b.twa[sl], b.vmg[sl]
    up = twa_s < 70; dn = twa_s > 110
    dist_m = float(np.sum(np.hypot(np.diff(b.x[sl]), np.diff(b.y[sl]))))
    mans = [m for m in mans if b.t[gi] <= m.t <= b.t[fi]]   # race maneuvers only
    tacks = [m for m in mans if m.kind == "tack"]
    gybes = [m for m in mans if m.kind == "gybe"]
    summary = {
        "n": int(fi - gi + 1),
        "n_legs": len(legs), "finished": finished, "gps_finish": gps_finish,
        "race_min": float(finish_t.get(b.sail, b.t[fi]) - gun_t) / 60.0,
        "dist_nm": dist_m / 1852.0,
        "sog_mean": float(sog_s.mean()),
        "sog_up": float(sog_s[up].mean()) if up.any() else None,
        "sog_dn": float(sog_s[dn].mean()) if dn.any() else None,
        "twa_up": float(np.median(twa_s[up])) if up.any() else None,
        "twa_dn": float(np.median(twa_s[dn])) if dn.any() else None,
        "vmg_up": float(vmg_s[up].mean()) if up.any() else None,
        "vmg_dn": float(-vmg_s[dn].mean()) if dn.any() else None,
        "n_tacks": len(tacks), "n_gybes": len(gybes),
        "tack_loss": float(np.mean([m.bl_lost for m in tacks])) if tacks else None,
        "gybe_loss": float(np.mean([m.bl_lost for m in gybes])) if gybes else None,
    }
    # cumulative made-good distance along each leg's OWN axis (handles
    # upwind/reach/downwind uniformly) -> continuous position ladder
    made = np.zeros(len(b.t)); acc = 0.0
    for (a, c, kind) in legs:
        axx, ayy = b.x[c] - b.x[a], b.y[c] - b.y[a]
        nrm = float(np.hypot(axx, ayy))
        if c <= a or nrm < 1.0:
            made[a:] = acc; continue
        axx, ayy = axx / nrm, ayy / nrm
        seg = (b.x[a:c + 1] - b.x[a]) * axx + (b.y[a:c + 1] - b.y[a]) * ayy
        made[a:c + 1] = acc + seg
        acc += float(seg[-1])
        made[c + 1:] = acc
    course_grid = np.interp(grid, b.t, made, left=np.nan, right=np.nan)
    return BoatAnalysis(boat=b, legs=legs, course_ix=cix, rounds=rounds_by_sail.get(b.sail, []),
                        maneuvers=mans, penalties=penalties.get(b.sail, []), shadow=shadow.get(b.sail, ""),
                        course_grid=course_grid, progress=progress.get(b.sail), summary=summary)
