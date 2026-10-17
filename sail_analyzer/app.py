"""
app.py — local web app for ILCA GPS race analysis.

Run:  python app.py        (parses all races once, caches, opens browser)
      python app.py --rebuild   (force re-parse)
      python app.py --port 8000

Regattas and races are managed from the UI ("Regattas" button): each race is a
folder of TracTrac KML tracks (uploaded copies live in data/) plus its course
(leg types + marks) and an optional gun time. The list is kept in
regattas.json (local, git-ignored); the repo's own race folders are registered
on first run as the "Copa de España ILCA 7 2025" regatta.

Stdlib only (+ numpy via regatta). No internet required; the UI is fully
offline (no CDN, no map tiles).
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
import sys
import threading
import uuid
import webbrowser
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, urlparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

import regatta as R

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.dirname(HERE)               # the folder with 01_Prueba 1 ...
CACHE_DIR = os.path.join(HERE, "cache")
STATIC_DIR = os.path.join(HERE, "static")
UPLOAD_DIR = os.path.join(HERE, "data")         # uploaded KML copies: data/<regatta>/<race>/
REG_PATH = os.path.join(HERE, "regattas.json")  # regatta/race registry (local, git-ignored)
LOCK = threading.RLock()                        # cache builds + registry read-modify-write
REG_LOCK = threading.Lock()                     # regattas.json file reads/writes

# Course templates offered in the UI. Legs: "<type>:<mark>[*]" — type U(pwind) /
# R(each) / D(ownwind), the mark the leg ends at, * = that mark is a gate.
# The last leg always ends at the finish line.
TEMPLATES = [
    {"id": "trap_outer", "name": "Trapezoid outer — reach finish", "legs": "U:1 R:2 D:G* U:2 D:G* R:F"},
    {"id": "trap_outer_wing", "name": "Trapezoid outer, wing mark moved (2a/2b)", "legs": "U:1 R:2a D:G* U:2b D:G* R:F"},
    {"id": "trap_inner", "name": "Trapezoid inner — reach finish", "legs": "U:1 D:4* U:1 R:2 D:3 R:F"},
    {"id": "wl2_dn", "name": "Windward-leeward, 2 laps, downwind finish", "legs": "U:1 D:G* U:1 D:F"},
    {"id": "wl2_up", "name": "Windward-leeward, 2 laps, upwind finish", "legs": "U:1 D:G* U:1 D:G* U:F"},
    {"id": "wl3_dn", "name": "Windward-leeward, 3 laps, downwind finish", "legs": "U:1 D:G* U:1 D:G* U:1 D:F"},
]


def parse_legs(spec: str):
    out = []
    for tok in spec.split():
        typ, mark = tok.split(":")
        out.append({"type": typ, "mark": mark.rstrip("*"), "gate": mark.endswith("*")})
    return out


def template_course(tid):
    return parse_legs(next(t["legs"] for t in TEMPLATES if t["id"] == tid))

PALETTE = ["#e6194B", "#3cb44b", "#ffe119", "#4363d8", "#f58231", "#911eb4",
           "#42d4f4", "#f032e6", "#bfef45", "#fabed4", "#469990", "#dcbeff",
           "#9A6324", "#fffac8", "#800000", "#aaffc3", "#808000", "#ffd8b1",
           "#000075", "#a9a9a9"]


def r6(a):  return [round(float(v), 6) for v in a]
def r2(a):  return [round(float(v), 2) for v in a]
def r1(a):  return [round(float(v), 1) for v in a]
def r0(a):  return [int(round(float(v))) for v in a]
def rnan(a, nd=1):
    """Round, mapping NaN -> None (valid JSON)."""
    return [None if (v is None or (isinstance(v, float) and np.isnan(v))) else round(float(v), nd)
            for v in a]


# --------------------------------------------------------------------------- #
# Build one race into a compact dict
# --------------------------------------------------------------------------- #
def list_kml(d):
    """The folder's .kml files (any case: .kml / .KML)."""
    if not os.path.isdir(d):
        return []
    return sorted(f for f in glob.glob(os.path.join(d, "*"))
                  if f.lower().endswith(".kml") and os.path.isfile(f))


def clean_text(s, n=80):
    """Names shown in the UI come from users/filenames: drop markup characters."""
    return re.sub(r"[<>\"`\x00-\x1f]", "", str(s or "")).strip()[:n]


def gun_from_clock(clock: str, t0: float):
    """'HH:MM[:SS]' (UTC) on the day of the first fix -> epoch seconds."""
    parts = [int(x) for x in clock.strip().split(":")]
    h, m, sec = (parts + [0, 0])[:3]
    day = datetime.fromtimestamp(t0, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    g = day.timestamp() + h * 3600 + m * 60 + sec
    if g < t0 - 3 * 3600:                         # track started the evening before
        g += 86400
    return float(g)


def build_race(race_dir: str, race_name: str, course, gun=None, key=None):
    tracks = []
    for f in list_kml(race_dir):
        base = clean_text(os.path.basename(f)[:-4], 120)
        try:
            t, la, lo = R.parse_kml(f)
        except ET.ParseError as e:                # truncated / not-KML file: skip that boat
            print(f"  ! skipping {os.path.basename(f)}: {e}", flush=True)
            continue
        if len(t) >= 30:
            tracks.append((f, base, t, la, lo))
    # boat id = sail number (TracTrac "206341-O_NAME"); when that isn't unique in
    # this folder (other naming schemes) the whole file name is the id
    first = [base.split("-")[0] for _, base, *_ in tracks]
    boats, used = [], set()
    for (f, base, t, la, lo), sail in zip(tracks, first):
        sid = sail if first.count(sail) == 1 else base
        k = 2
        while sid in used:
            sid, k = f"{base}~{k}", k + 1
        used.add(sid)
        disp = base.split("_", 1)[-1] if "_" in base else base
        boats.append(R.Boat(sail=sid, name=disp, file=f, t=t, lat=la, lon=lo))
    if not boats:
        return None

    lat0 = float(np.mean([b.lat.mean() for b in boats]))
    lon0 = float(np.mean([b.lon.mean() for b in boats]))
    for b in boats:
        R.derive_kinematics(b, lat0, lon0)

    wind_from = R.estimate_wind_dir(boats)
    for b in boats:
        R.apply_wind(b, wind_from)

    # common time grid (5 s)
    t_start = min(b.t[0] for b in boats)
    t_end = max(b.t[-1] for b in boats)
    grid = np.arange(t_start, t_end + 1, 5.0)

    wind_series = R.wind_timeseries(boats, wind_from, grid)
    progress = R.fleet_progress(boats, wind_from, grid)

    # The gun is a round clock-minute ~5 min after logging starts (logging begins
    # at the warning signal; the first minutes are the pre-start). Leg 1 is
    # anchored there so the pre-start is excluded from the race legs.
    gun_t = (gun_from_clock(gun, float(np.median([b.t[0] for b in boats]))) if gun
             else R.detect_gun(boats, wind_from))
    template = tuple(leg["type"] for leg in course)
    seg = {b.sail: R.segment_legs(b, wind_from, template=template, gun_t=gun_t, with_index=True)
           for b in boats}
    legs_by_sail = {s: lg for s, (lg, _) in seg.items()}
    # course leg of each boat leg = its template index (type-order fallback otherwise)
    cix_by_sail = {s: (ks if ks is not None else R.course_index(course, lg)) for s, (lg, ks) in seg.items()}
    # a boat "finished" if it sailed the full course (>= the fleet's modal leg count);
    # boats with fewer legs are retirees/DNF and are excluded from fleet metrics
    _lc = {}
    for lg in legs_by_sail.values():
        _lc[len(lg)] = _lc.get(len(lg), 0) + 1
    mode_legs = max(_lc, key=_lc.get) if _lc else 0

    # course marks at the fleet's actual rounding points, labelled as in the course
    marks = R.detect_marks(boats, wind_from, legs_by_sail, course, cix_by_sail)

    # ---- finish times by LINE CROSSING (not leg end, which drifts minutes) ----
    # Finish line: through the fleet-median last-turn apex, perpendicular to the
    # fleet-median final-leg direction. A boat finishes the first time its
    # projection along that direction passes the apex. Same reference for all
    # boats -> reliable finish ORDER (validated against official results).
    apexes, fdirs = [], []
    for b in boats:
        lg = legs_by_sail[b.sail]
        if len(lg) != mode_legs or not lg:
            continue
        a, c, _ = lg[-1]
        j = R._refine_turn(b, c)                    # sharpest turn ~ the line
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
        a, c, _ = lg[-1]
        ft = float(b.t[c])
        if Fx is not None and len(lg) == mode_legs:
            # search from the leg start to the END OF TRACK: the leg-end trim can
            # stop short of the line, which would otherwise bias that boat late
            proj = (b.x[a:] - Fx) * fux + (b.y[a:] - Fy) * fuy
            hit = np.where(proj >= 0)[0]
            if len(hit):
                ft = float(b.t[a + int(hit[0])])
        finish_t[b.sail] = ft

    # start line at the gun, with its ends labelled: RC (committee boat, starboard
    # end) and Pin (port end). Starboard = +along-line (wind_from + 90 deg).
    start_t = float(gun_t)
    startinfo = R.infer_start(boats, wind_from, start_t)
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

    # ---- mark-rounding times by PROXIMITY (first close approach to each buoy) ----
    # Leg boundaries come from ~60s-smoothed VMG, so their times are far too noisy
    # to order the fleet at a mark; the actual closest pass is sharp and reliable.
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

    # ---- tactical inputs: wind-shadow exposure (all boats) + penalty-turn candidates ----
    shadow = R.dirty_air(boats, grid, wind_series)
    penalties = {b.sail: R.detect_penalty_turns(b, gun_t + 20.0, finish_t.get(b.sail, float(b.t[-1])))
                 for b in boats}

    # fleet aggregate stats on the grid (FINISHERS only — exclude retirees/DNF)
    sog_grid, vmg_grid = [], []
    for b in boats:
        if len(legs_by_sail.get(b.sail) or []) < mode_legs:
            continue
        sog_grid.append(np.interp(grid, b.t, b.sog, left=np.nan, right=np.nan))
        vmg_grid.append(np.interp(grid, b.t, b.vmg, left=np.nan, right=np.nan))
    sog_grid = np.array(sog_grid); vmg_grid = np.array(vmg_grid)
    with np.errstate(all="ignore"):
        fleet_stats = {
            "t": r0(grid),
            "sog_p50": rnan(np.nanmedian(sog_grid, axis=0), 2),
            "sog_p25": rnan(np.nanpercentile(sog_grid, 25, axis=0), 2),
            "sog_p75": rnan(np.nanpercentile(sog_grid, 75, axis=0), 2),
            "vmg_p50": rnan(np.nanmedian(vmg_grid, axis=0), 2),
        }

    boats_out = []
    for i, b in enumerate(sorted(boats, key=lambda x: x.sail)):
        mans = R.detect_maneuvers(b, wind_from)
        legs = legs_by_sail[b.sail]
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
            "n_legs": len(legs), "finished": bool(len(legs) >= mode_legs),
            "race_min": round(float(finish_t.get(b.sail, b.t[fi]) - gun_t) / 60.0, 2),
            "dist_nm": round(dist_m / 1852.0, 2),
            "sog_mean": round(float(sog_s.mean()), 2),
            "sog_up": round(float(sog_s[up].mean()), 2) if up.any() else None,
            "sog_dn": round(float(sog_s[dn].mean()), 2) if dn.any() else None,
            "twa_up": round(float(np.median(twa_s[up])), 0) if up.any() else None,
            "twa_dn": round(float(np.median(twa_s[dn])), 0) if dn.any() else None,
            "vmg_up": round(float(vmg_s[up].mean()), 2) if up.any() else None,
            "vmg_dn": round(float(-vmg_s[dn].mean()), 2) if dn.any() else None,
            "n_tacks": len(tacks), "n_gybes": len(gybes),
            "tack_loss": round(float(np.mean([m.bl_lost for m in tacks])), 2) if tacks else None,
            "gybe_loss": round(float(np.mean([m.bl_lost for m in gybes])), 2) if gybes else None,
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

        prog = progress.get(b.sail)
        boats_out.append({
            "rounds": [None if v is None else int(round(v)) for v in rounds_by_sail.get(b.sail, [])],
            "shadow": shadow.get(b.sail, ""),
            "penalties": penalties.get(b.sail, []),
            "course": rnan(course_grid, 0),
            "sail": b.sail, "name": b.name, "color": PALETTE[i % len(PALETTE)],
            "t": r0(b.t), "lat": r6(b.lat), "lon": r6(b.lon),
            "sog": r2(b.sog), "cog": r1(b.cog), "twa": r1(b.twa),
            "vmg": r2(b.vmg), "tack": [int(x) for x in b.tack],
            "legs": [[int(a), int(c), kind] for (a, c, kind) in legs],
            "course_ix": cix_by_sail[b.sail],
            "maneuvers": [vars(m) for m in mans],
            "progress": (rnan(prog, 1) if prog is not None else None),
            "summary": summary,
        })

    return {
        "race": race_name,
        "key": key or race_name,
        "course": course,
        "gun_manual": bool(gun),
        "start_t": start_t,
        "gun_t": float(gun_t),
        "start_iso": datetime.fromtimestamp(start_t, timezone.utc).isoformat(),
        "wind_from": round(float(wind_from), 0),
        "lat0": lat0, "lon0": lon0,
        "marks": marks,
        "start_line": start_line,
        "wind_series": {"t": r0(grid), "dir": r1(wind_series)},
        "fleet_stats": fleet_stats,
        "boats": boats_out,
    }


# --------------------------------------------------------------------------- #
# Regatta / race registry (regattas.json)
# --------------------------------------------------------------------------- #
def _slug(name):
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s[:40] or "regatta"


def _race_num(race_id):
    m = re.fullmatch(r"r(\d+)", race_id or "")
    return int(m.group(1)) if m else 0


def _safe_id(s):
    return bool(re.fullmatch(r"[a-z0-9-]{1,48}", s or ""))


def discover_races():
    dirs = sorted(d for d in glob.glob(os.path.join(DATA_DIR, "*"))
                  if os.path.isdir(d) and list_kml(d))
    races = []
    for d in dirs:
        name = os.path.basename(d)
        name = re.sub(r"^\d+_", "", name)        # strip "01_"
        races.append((d, name))
    return races


def seed_registry():
    """First run: register the repo's own race folders (Copa de España 2025).
    Prueba 2 & 5 had the wing mark moved between roundings (2a / 2b)."""
    races = []
    for k, (d, name) in enumerate(discover_races()):
        wing = name.strip()[-2:] in (" 2", " 5")
        races.append({"id": f"r{k + 1:02d}", "name": name,
                      "dir": os.path.relpath(d, HERE).replace("\\", "/"),
                      "course": template_course("trap_outer_wing" if wing else "trap_outer"),
                      "gun": None, "key": name})
    reg = {"version": 1, "regattas": []}
    if races:
        reg["regattas"].append({"id": "copa2025", "name": "Copa de España ILCA 7 2025", "races": races})
    return reg


def load_registry():
    if not os.path.exists(REG_PATH):
        seeded = seed_registry()
        with REG_LOCK:
            if not os.path.exists(REG_PATH):
                _write_registry(seeded)
    with REG_LOCK:
        with open(REG_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)


def save_registry(reg):
    with REG_LOCK:
        _write_registry(reg)


def _write_registry(reg):
    atomic_write(REG_PATH, json.dumps(reg, indent=1, ensure_ascii=False))


def atomic_write(path, text):
    """Write via a temp file + rename so readers never see a half-written file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    for attempt in range(20):                     # Windows: a reader may hold the target open
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 19:
                os.remove(tmp)
                raise
            threading.Event().wait(0.05)


def find_regatta(reg, rid):
    return next((g for g in reg["regattas"] if g["id"] == rid), None)


def find_race(rg, race_id):
    return next((r for r in rg["races"] if r["id"] == race_id), None)


def race_dir_abs(race):
    d = race.get("dir") or ""
    return d if os.path.isabs(d) else os.path.normpath(os.path.join(HERE, d))


def upload_dir_for(rid, race_id):
    return os.path.join(UPLOAD_DIR, rid, race_id)


def staging_dir_for(rid, race_id):
    return upload_dir_for(rid, race_id) + ".new"


def new_key(rid, race_id):
    """localStorage key for a race's hand-edited marks: unique per race and per
    course, so a reused id or a changed course never inherits stale marks."""
    return f"{rid}/{race_id}/{uuid.uuid4().hex[:8]}"


def is_upload_dir(path):
    return os.path.normpath(path).startswith(os.path.normpath(UPLOAD_DIR) + os.sep)


def kml_files(race):
    return list_kml(race_dir_abs(race))


def normalize_course(course):
    """Validate a course from the UI: >= 2 legs, types U/R/D, mark labels 1-6
    chars (RC/Pin reserved); the finish label must not be a rounding mark."""
    if not isinstance(course, list) or len(course) < 2:
        raise ValueError("A course needs at least 2 legs.")
    out = []
    for i, leg in enumerate(course):
        if not isinstance(leg, dict):
            raise ValueError(f"Leg {i + 1}: bad format.")
        typ = str(leg.get("type", "")).upper()[:1]
        mark = str(leg.get("mark", "")).strip()[:6]
        if mark and not re.fullmatch(r"[A-Za-z0-9._-]+", mark):
            raise ValueError(f"Leg {i + 1}: mark labels use letters, digits, '.', '_' or '-'.")
        if typ not in ("U", "R", "D"):
            raise ValueError(f"Leg {i + 1}: type must be upwind, reach or downwind.")
        if not mark:
            raise ValueError(f"Leg {i + 1}: give the mark it ends at.")
        if mark in ("RC", "Pin"):
            raise ValueError(f"Leg {i + 1}: '{mark}' is reserved for the start line.")
        out.append({"type": typ, "mark": mark, "gate": bool(leg.get("gate")) and i < len(course) - 1})
    if out[-1]["mark"] in [leg["mark"] for leg in out[:-1]]:
        raise ValueError("The last leg ends at the finish: give it its own label (e.g. F).")
    return out


def race_fields(body, race):
    """Validated name / course / gun from a request body."""
    return {"course": normalize_course(body.get("course")), "gun": normalize_gun(body.get("gun")),
            "name": clean_text(body.get("name", race["name"]), 60) or race["name"]}


def normalize_gun(gun):
    gun = (gun or "").strip()
    if not gun:
        return None
    if not re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", gun):
        raise ValueError("Gun time must be HH:MM or HH:MM:SS (UTC).")
    hms = [int(x) for x in gun.split(":")] + [0]
    h, m, sec = hms[0], hms[1], hms[2]
    if h > 23 or m > 59 or sec > 59:
        raise ValueError("Gun time out of range.")
    return gun


# --------------------------------------------------------------------------- #
# Cache: cache/<regatta>/<race>.json (+ .meta.json), cache/<regatta>/index.json
# --------------------------------------------------------------------------- #
def _src_mtime():
    return max(os.path.getmtime(os.path.join(HERE, f)) for f in ("regatta.py", "app.py"))


def race_cfg_hash(race):
    """What the analysis depends on: course, gun and the track files."""
    files = [(os.path.basename(f), os.path.getsize(f), int(os.path.getmtime(f))) for f in kml_files(race)]
    blob = json.dumps({"course": race.get("course"), "gun": race.get("gun"), "files": files}, sort_keys=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def race_cache_path(rid, race_id):
    return os.path.join(CACHE_DIR, rid, f"{race_id}.json")


def _meta_path(rid, race_id):
    return race_cache_path(rid, race_id)[:-5] + ".meta.json"


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _write_json(path, obj, compact=False):
    atomic_write(path, json.dumps(obj, separators=(",", ":") if compact else None))


def store_race_cache(rid, race, data):
    _write_json(race_cache_path(rid, race["id"]), data, compact=True)
    _write_json(_meta_path(rid, race["id"]), {"cfg": race_cfg_hash(race), "name": race["name"],
                                              "key": race.get("key"),
                                              "entry": index_entry(0, race["id"], data)})


def drop_race_cache(rid, race_id):
    for f in (race_cache_path(rid, race_id), _meta_path(rid, race_id)):
        if os.path.exists(f):
            os.remove(f)


def build_race_cached(rid, race, force=False):
    """(Re)build one race's cache when missing/stale. Returns an error string or
    None. A failed build removes the old cache so stale results are never served."""
    cpath, mpath = race_cache_path(rid, race["id"]), _meta_path(rid, race["id"])
    h = race_cfg_hash(race)
    meta = _read_json(mpath)
    if not force and meta and meta.get("cfg") == h and meta.get("error") \
            and os.path.getmtime(mpath) >= _src_mtime():
        return meta["error"]                      # same inputs failed before: don't re-parse
    if not force and meta and meta.get("cfg") == h and os.path.exists(cpath) \
            and os.path.getmtime(cpath) >= _src_mtime():
        if meta.get("name") != race["name"] or meta.get("key") != race.get("key") or "entry" not in meta:
            data = _read_json(cpath)              # rename / new marks key: patch, no re-parse
            if data is None:
                drop_race_cache(rid, race["id"])
                return build_race_cached(rid, race, force=True)
            data["race"], data["key"] = race["name"], race.get("key") or race["name"]
            _write_json(cpath, data, compact=True)
            _write_json(mpath, {"cfg": h, "name": race["name"], "key": race.get("key"),
                                "entry": index_entry(0, race["id"], data)})
        return None
    try:
        data = analyse(race_dir_abs(race), race)
    except AnalysisError as e:
        drop_race_cache(rid, race["id"])
        if e.permanent:                           # same inputs will fail again: remember it
            _write_json(mpath, {"cfg": h, "error": str(e)})
        return str(e)
    store_race_cache(rid, race, data)
    return None


class AnalysisError(ValueError):
    permanent = True                              # the same inputs would fail again


class TransientError(AnalysisError):
    permanent = False                             # e.g. a file locked by antivirus / sync


def analyse(race_dir, race):
    """Run the analysis for a race config on a folder of tracks; AnalysisError if it fails."""
    if not list_kml(race_dir):
        raise AnalysisError(f"No .kml files in {race_dir}")
    print(f"  parsing {race['name']} ...", flush=True)
    try:
        data = build_race(race_dir, race["name"], race["course"], race.get("gun"), race.get("key"))
    except (OSError, MemoryError) as e:
        raise TransientError(f"Could not read the tracks right now ({type(e).__name__}: {e}) — try again.")
    except Exception as e:
        raise AnalysisError(f"Analysis failed: {type(e).__name__}: {e}")
    if data is None:
        raise AnalysisError("No usable tracks: expected TracTrac KML files (timestamped points, 30+ fixes per boat).")
    return data


def build_index(rid):
    """The regatta's race index, assembled from the per-race meta entries (no track data)."""
    rg = find_regatta(load_registry(), rid)
    if rg is None:
        return None
    index = []
    for race in rg["races"]:
        meta = _read_json(_meta_path(rid, race["id"]))
        if meta and meta.get("entry") and os.path.exists(race_cache_path(rid, race["id"])):
            index.append({**meta["entry"], "i": len(index)})
    return index


def ensure_cache(rid, rebuild=False):
    """Build missing/stale races of one regatta and write its index."""
    with LOCK:
        rg = find_regatta(load_registry(), rid)
        if rg is None:
            return None
        for race in rg["races"]:
            err = build_race_cached(rid, race, force=rebuild)
            if err:
                print(f"  ! {race['name']}: {err}", flush=True)
        return build_index(rid)


def index_entry(i, race_id, data):
    """Regatta trends: per-boat mini-summary + finisher medians + finish rank."""
    import statistics as _st
    fin = [b for b in data["boats"] if b["summary"].get("finished")]
    def _med(k):
        vals = [b["summary"][k] for b in fin if b["summary"].get(k) is not None]
        return round(_st.median(vals), 2) if vals else None
    keys = ("race_min", "sog_up", "sog_dn", "vmg_up", "vmg_dn", "twa_up",
            "tack_loss", "gybe_loss", "dist_nm", "n_tacks", "n_gybes")
    fleet = {k: _med(k) for k in keys}
    order = sorted([b for b in fin if b["summary"].get("race_min") is not None],
                   key=lambda b: b["summary"]["race_min"])
    rank = {b["sail"]: j + 1 for j, b in enumerate(order)}
    mini = {}
    for b in data["boats"]:
        s = b["summary"]
        mini[b["sail"]] = {**{k: s.get(k) for k in keys},
                           "finished": s.get("finished"),
                           "rank": rank.get(b["sail"]), "n_fin": len(order)}
    return {"i": i, "id": race_id, "race": data["race"], "start_iso": data["start_iso"],
            "wind_from": data["wind_from"], "n_boats": len(data["boats"]),
            "fleet": fleet, "boats": mini}


def registry_view():
    reg = load_registry()
    out = []
    for rg in reg["regattas"]:
        races = []
        for r in rg["races"]:
            races.append({"id": r["id"], "name": r["name"], "course": r["course"], "gun": r.get("gun"),
                          "n_files": len(kml_files(r)), "uploaded": is_upload_dir(race_dir_abs(r)),
                          "analysed": os.path.exists(race_cache_path(rg["id"], r["id"]))})
        out.append({"id": rg["id"], "name": rg["name"], "races": races})
    return {"regattas": out, "templates": [{**t, "course": parse_legs(t["legs"])} for t in TEMPLATES]}


# --------------------------------------------------------------------------- #
# HTTP server
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body, ctype):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _err(self, msg, code=400):
        self._json({"ok": False, "error": msg}, code)

    def do_GET(self):
        if not self._local_host():
            return self._send(403, "forbidden", "text/plain")
        try:
            return self._get(urlparse(self.path).path)
        except Exception as e:
            return self._err(f"Server error: {type(e).__name__}: {e}", 500)

    def _get(self, path):
        if path == "/" or path == "/index.html":
            return self._file(os.path.join(STATIC_DIR, "index.html"), "text/html; charset=utf-8")
        if path.startswith("/static/"):
            fp = os.path.join(STATIC_DIR, os.path.basename(path))
            ctype = ("text/css" if fp.endswith(".css")
                     else "application/javascript" if fp.endswith(".js")
                     else "application/octet-stream")
            return self._file(fp, ctype)
        if path == "/api/regattas":
            return self._json(registry_view())
        m = re.fullmatch(r"/api/index/([a-z0-9-]+)", path)
        if m:
            idx = ensure_cache(m.group(1))        # stat-only when nothing changed
            return self._json(idx) if idx is not None else self._err("unknown regatta", 404)
        m = re.fullmatch(r"/api/race/([a-z0-9-]+)/([a-z0-9-]+)", path)
        if m:
            return self._file(race_cache_path(m.group(1), m.group(2)), "application/json")
        self._send(404, "not found", "text/plain")

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 64 * 1024 * 1024:
            raise ValueError("File too large (max 64 MB).")
        return self.rfile.read(n) if n else b""

    LOCAL = ("127.0.0.1", "localhost")

    def _local_host(self):
        """DNS-rebinding guard: only answer requests addressed to this machine."""
        return urlparse("//" + (self.headers.get("Host") or "")).hostname in self.LOCAL

    def _same_origin(self):
        """CSRF guard for state changes: local Host and Origin, plus a custom
        header that a foreign page can't send without a (refused) preflight."""
        origin = self.headers.get("Origin")
        if not self._local_host() or (origin and urlparse(origin).hostname not in self.LOCAL):
            return False
        return self.headers.get("X-Sail-Analyzer") == "1"

    def do_POST(self):
        u = urlparse(self.path)
        parts = [p for p in u.path.split("/") if p]
        if not self._same_origin():
            return self._err("forbidden", 403)
        try:
            raw = self._body()
            if parts[:2] != ["api", "regatta"] and parts != ["api", "regattas"]:
                return self._err("not found", 404)
            if parts[-1] == "file":                       # raw KML upload
                return self._upload(parts, parse_qs(u.query).get("name", [""])[0], raw)
            body = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(body, dict):
                raise ValueError("Bad request body.")
            with LOCK:
                return self._route(parts, body)
        except ValueError as e:
            return self._err(str(e))
        except Exception as e:                    # never leave the client without a response
            return self._err(f"Server error: {type(e).__name__}: {e}", 500)

    def _route(self, parts, body):
        reg = load_registry()
        if parts == ["api", "regattas"]:                                   # create regatta
            name = clean_text(body.get("name"), 80)
            if not name:
                raise ValueError("Give the regatta a name.")
            rid, k = _slug(name), 2
            while find_regatta(reg, rid):
                rid, k = f"{_slug(name)}-{k}", k + 1
            reg["regattas"].append({"id": rid, "name": name, "races": []})
            save_registry(reg)
            return self._json({"ok": True, "id": rid})
        rid = parts[2] if len(parts) > 2 else ""
        rg = find_regatta(reg, rid) if _safe_id(rid) else None
        if rg is None:
            return self._err("unknown regatta", 404)
        if parts[3:] == ["rename"]:
            name = clean_text(body.get("name"), 80)
            if not name:
                raise ValueError("Give the regatta a name.")
            rg["name"] = name
            save_registry(reg)
            return self._json({"ok": True})
        if parts[3:] == ["delete"]:
            reg["regattas"] = [g for g in reg["regattas"] if g["id"] != rid]
            save_registry(reg)
            for d in (os.path.join(UPLOAD_DIR, rid), os.path.join(CACHE_DIR, rid)):
                shutil.rmtree(d, ignore_errors=True)       # app-made copies/caches only
            return self._json({"ok": True})
        if parts[3:] == ["races"]:                                         # create race
            name = clean_text(body.get("name"), 60) or f"Race {len(rg['races']) + 1}"
            k = max([rg.get("seq", 0), len(rg["races"])] + [_race_num(r["id"]) for r in rg["races"]]) + 1
            while f"r{k:02d}" in {r["id"] for r in rg["races"]}:
                k += 1
            course, gun = normalize_course(body.get("course")), normalize_gun(body.get("gun"))
            rg["seq"] = k
            race_id = f"r{k:02d}"
            d = upload_dir_for(rid, race_id)
            os.makedirs(d, exist_ok=True)
            rg["races"].append({"id": race_id, "name": name,
                                "dir": os.path.relpath(d, HERE).replace("\\", "/"),
                                "course": course, "gun": gun, "key": new_key(rid, race_id)})
            save_registry(reg)
            return self._json({"ok": True, "id": race_id})
        race = find_race(rg, parts[4]) if len(parts) > 5 and parts[3] == "race" else None
        if race is None:
            return self._err("unknown race", 404)
        action = parts[5]
        if action == "update":                    # name / course / gun; analysed before saving
            new = {**race, **race_fields(body, race)}
            if new["course"] != race["course"]:
                new["key"] = new_key(rid, race["id"])    # old hand-edited marks don't fit
            data = None
            if new["course"] != race["course"] or new["gun"] != race.get("gun"):
                data = analyse(race_dir_abs(race), new)  # raises -> nothing saved
            race.clear()
            race.update(new)
            save_registry(reg)
            if data is not None:
                store_race_cache(rid, race, data)
                err = None
            else:
                err = build_race_cached(rid, race)        # rename: patches the cache
            return self._err(err) if err else self._json({"ok": True})
        if action == "stage":                     # empty staging folder for a new set of tracks
            st = staging_dir_for(rid, race["id"])
            shutil.rmtree(st, ignore_errors=True)
            os.makedirs(st, exist_ok=True)
            return self._json({"ok": True})
        if action == "commit":                    # analyse the staged tracks, then swap them in
            st = staging_dir_for(rid, race["id"])
            if not list_kml(st):
                raise ValueError("No uploaded .kml files to use.")
            new = {**race, **(race_fields(body, race) if "course" in body else {})}
            new["key"] = new_key(rid, race["id"])  # new tracks: old hand-edited marks don't apply
            data = analyse(st, new)               # raises -> the current tracks stay
            d = upload_dir_for(rid, race["id"])
            old = d + ".old"
            shutil.rmtree(old, ignore_errors=True)
            if os.path.isdir(d):
                os.replace(d, old)                # whole-folder swap: never half-replaced
            try:
                os.replace(st, d)
            except OSError:
                if os.path.isdir(old):
                    os.replace(old, d)
                raise
            shutil.rmtree(old, ignore_errors=True)
            new["dir"] = os.path.relpath(d, HERE).replace("\\", "/")
            race.clear()
            race.update(new)
            save_registry(reg)
            store_race_cache(rid, race, data)
            return self._json({"ok": True})
        if action == "build":
            err = build_race_cached(rid, race, force=False)
            idx = build_index(rid)                # a failed race drops out of the index
            if err:
                return self._err(err)
            return self._json({"ok": True, "n_races": len(idx or [])})
        if action == "delete":
            rg["races"] = [r for r in rg["races"] if r["id"] != race["id"]]
            rg["seq"] = max(rg.get("seq", 0), _race_num(race["id"]))   # keep the id retired
            save_registry(reg)
            if is_upload_dir(race_dir_abs(race)):
                shutil.rmtree(race_dir_abs(race), ignore_errors=True)
            shutil.rmtree(staging_dir_for(rid, race["id"]), ignore_errors=True)
            drop_race_cache(rid, race["id"])
            return self._json({"ok": True})
        return self._err("not found", 404)

    def _upload(self, parts, name, raw):
        if len(parts) != 6 or parts[3] != "race":
            return self._err("not found", 404)
        name = os.path.basename(name.replace("\\", "/"))
        if len(name) > 200:
            raise ValueError("File name too long (max 200 characters).")
        if re.search(r'[<>:"/\\|?*\x00-\x1f]', name) or name != name.strip(" ."):
            raise ValueError(f"Unsupported characters in file name: {clean_text(name, 60)}")
        if not name.lower().endswith(".kml") or name.startswith("."):
            raise ValueError(f"Not a .kml file: {name}")
        if b"<kml" not in raw[:2000].lower() and b"<placemark" not in raw[:20000].lower():
            raise ValueError(f"{name} does not look like a KML file.")
        with LOCK:
            reg = load_registry()
            rg = find_regatta(reg, parts[2]) if _safe_id(parts[2]) else None
            race = find_race(rg, parts[4]) if rg else None
            if race is None:
                return self._err("unknown race", 404)
            st = staging_dir_for(parts[2], race["id"])
            if not os.path.isdir(st):
                raise ValueError("Start an upload (stage) before sending files.")
            if os.path.dirname(os.path.abspath(os.path.join(st, name))) != os.path.abspath(st):
                raise ValueError("Unsupported file name.")
            if os.path.exists(os.path.join(st, name)):
                raise ValueError(f"Two tracks named {name}: choose the race's own folder.")
            with open(os.path.join(st, name), "wb") as fh:
                fh.write(raw)
        return self._json({"ok": True})

    def _file(self, fp, ctype):
        if not os.path.exists(fp):
            return self._send(404, "not found", "text/plain")
        with open(fp, "rb") as fh:
            self._send(200, fh.read(), ctype)


def main():
    try:                                          # regatta names may not fit a cp1252 console
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    rebuild = "--rebuild" in sys.argv
    port = 8765
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])

    reg = load_registry()
    print(f"Regattas    : {', '.join(g['name'] for g in reg['regattas']) or '(none yet)'}")
    if rebuild:
        print("Rebuilding the analysis cache of every regatta...")
        for rg in reg["regattas"]:
            idx = ensure_cache(rg["id"], rebuild=True)
            print(f"Ready: {rg['name']} — {len(idx or [])} races cached.")
    else:
        print("Races are analysed when a regatta is first opened (~10-20 s after an update).")

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"\n  Sail Analyzer running at  {url}")
    print("  Press Ctrl+C to stop.\n")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
