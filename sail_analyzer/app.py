"""
app.py — local web app for ILCA GPS race analysis.

Run:  python app.py        (parses all races once, caches, opens browser)
      python app.py --rebuild   (force re-parse)
      python app.py --port 8000

Official results can be downloaded per regatta from a results-page URL (stored in
data/<regatta>/results.json) and are used as the reference for finish places.

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

if __package__ in (None, "") and "sail_analyzer" not in sys.modules:
    # run as a script (python sail_analyzer/app.py, the .bat): load THIS checkout's package by its
    # path. Nothing goes on sys.path, so nothing in the tracks folder shadows the stdlib and no
    # other sail_analyzer on the path (another clone or worktree) can stand in for this one.
    import importlib.util
    _pkg = os.path.dirname(os.path.abspath(__file__))
    _spec = importlib.util.spec_from_file_location("sail_analyzer", os.path.join(_pkg, "__init__.py"),
                                                   submodule_search_locations=[_pkg])
    sys.modules["sail_analyzer"] = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(sys.modules["sail_analyzer"])

from sail_analyzer import regatta as R                      # noqa: E402
from sail_analyzer import results as RS                     # noqa: E402
from sail_analyzer.entities.course import TEMPLATES, normalize_course, parse_legs, template_course  # noqa: E402,F401
from sail_analyzer.use_cases.analyse_race import AnalyseRace, RaceRequest  # noqa: E402
from sail_analyzer.use_cases.errors import AnalysisError, TransientError  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.dirname(HERE)               # the folder with 01_Prueba 1 ...
CACHE_DIR = os.path.join(HERE, "cache")
STATIC_DIR = os.path.join(HERE, "static")
UPLOAD_DIR = os.path.join(HERE, "data")         # uploaded KML copies: data/<regatta>/<race>/
REG_PATH = os.path.join(HERE, "regattas.json")  # regatta/race registry (local, git-ignored)
REG_LOCK = threading.Lock()                     # regattas.json file reads/writes
REG_RMW = threading.RLock()                     # registry read-modify-write (short)
_RLOCKS, _RLOCKS_GUARD = {}, threading.Lock()   # per-regatta: builds + race actions


def regatta_lock(rid):
    with _RLOCKS_GUARD:
        return _RLOCKS.setdefault(rid, threading.RLock())


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


class KmlTrackGateway:
    """TrackGateway over a folder of TracTrac KML files (moves to the interface adapters in step 4)."""

    def boats(self, tracks_dir):
        tracks = []
        for f in list_kml(tracks_dir):
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
        return boats


# decimals each race-summary value is shown with
SUMMARY_DIGITS = {"race_min": 2, "dist_nm": 2, "sog_mean": 2, "sog_up": 2, "sog_dn": 2, "twa_up": 0,
                  "twa_dn": 0, "vmg_up": 2, "vmg_dn": 2, "tack_loss": 2, "gybe_loss": 2}


def present_race(a):
    """The race analysis as the API's JSON: rounded values, boat colours (moves to the
    interface adapters' presenters in step 4)."""
    boats_out = []
    for i, ba in enumerate(a.boats):
        b = ba.boat
        summary = {k: (round(v, SUMMARY_DIGITS[k]) if k in SUMMARY_DIGITS and v is not None else v)
                   for k, v in ba.summary.items()}
        boats_out.append({
            "rounds": [None if v is None else int(round(v)) for v in ba.rounds],
            "shadow": ba.shadow,
            "penalties": ba.penalties,
            "course": rnan(ba.course_grid, 0),
            "sail": b.sail, "name": b.name, "color": PALETTE[i % len(PALETTE)],
            "t": r0(b.t), "lat": r6(b.lat), "lon": r6(b.lon),
            "sog": r2(b.sog), "cog": r1(b.cog), "twa": r1(b.twa),
            "vmg": r2(b.vmg), "tack": [int(x) for x in b.tack],
            "legs": [[int(i0), int(i1), kind] for (i0, i1, kind) in ba.legs],
            "course_ix": ba.course_ix,
            "maneuvers": [vars(m) for m in ba.maneuvers],
            "progress": (rnan(ba.progress, 1) if ba.progress is not None else None),
            "summary": summary,
        })
    return {
        "race": a.name,
        "key": a.key,
        "course": a.course,
        "gun_manual": a.gun_manual,
        "start_t": a.start_t,
        "gun_t": a.gun_t,
        "start_iso": datetime.fromtimestamp(a.start_t, timezone.utc).isoformat(),
        "wind_from": round(float(a.wind_from), 0),
        "lat0": a.lat0, "lon0": a.lon0,
        "marks": a.marks,
        "start_line": a.start_line,
        "wind_series": {"t": r0(a.grid), "dir": r1(a.wind_series)},
        "fleet_stats": {"t": r0(a.grid), **{k: rnan(v, 2) for k, v in a.fleet.items()}},
        "boats": boats_out,
    }


def build_race(race_dir: str, race_name: str, course, gun=None, key=None):
    """A race folder's analysis as the API's JSON (None when it has no usable tracks)."""
    a = AnalyseRace(KmlTrackGateway()).execute(
        RaceRequest(tracks_dir=race_dir, name=race_name, course=course, gun=gun, key=key))
    return present_race(a) if a is not None else None


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
                      "dir": rel_dir(d),
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


def edit_registry(fn):
    """Apply fn(registry) and save, atomically w.r.t. other requests; returns fn's result."""
    with REG_RMW:
        reg = load_registry()
        out = fn(reg)
        save_registry(reg)
        return out


def put_race(rid, race_id, new):
    """Replace one race's registry entry (new dict) or remove it (None)."""
    def fn(reg):
        rg = find_regatta(reg, rid)
        if rg is None:
            raise ValueError("unknown regatta")
        if new is None:
            rg["races"] = [r for r in rg["races"] if r["id"] != race_id]
            rg["seq"] = max(rg.get("seq", 0), _race_num(race_id))   # keep the id retired
        else:
            rg["races"] = [new if r["id"] == race_id else r for r in rg["races"]]
    edit_registry(fn)


def rel_dir(path):
    return os.path.relpath(path, HERE).replace("\\", "/")


def find_regatta(reg, rid):
    return next((g for g in reg["regattas"] if g["id"] == rid), None)


def find_race(rg, race_id):
    return next((r for r in rg["races"] if r["id"] == race_id), None)


def race_dir_abs(race):
    d = race.get("dir") or ""
    return d if os.path.isabs(d) else os.path.normpath(os.path.join(HERE, d))


def results_path(rid):
    return os.path.join(UPLOAD_DIR, rid, "results.json")


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


def race_fields(body, race):
    """Validated name / course / gun from a request body."""
    course, gun = course_and_gun(body)
    return {"course": course, "gun": gun, "name": clean_text(body.get("name", race["name"]), 60) or race["name"]}


def course_and_gun(body):
    course, gun = normalize_course(body.get("course")), normalize_gun(body.get("gun"))
    if course[0]["type"] != "U" and not gun:
        raise ValueError("This course doesn't start upwind: enter the gun time "
                         "(auto-detection looks for the fleet starting to beat).")
    return course, gun


def normalize_gun(gun):
    if gun is not None and not isinstance(gun, str):
        raise ValueError("Gun time must be HH:MM or HH:MM:SS (UTC).")
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
# caches older than the code this process runs are rebuilt
def _package_sources(root):
    """The package's Python files in every circle; the cache/, data/ and static/ folders
    (generated analysis, uploaded tracks, the UI) aren't walked."""
    out = []
    for d, sub, files in os.walk(root):
        sub[:] = [x for x in sub if x not in ("cache", "data", "static", "__pycache__")]
        out += [os.path.join(d, f) for f in files if f.endswith(".py")]
    return out


ANALYSIS_SOURCES = _package_sources(HERE)       # the code a cached race comes from
SRC_MTIME = max(os.path.getmtime(f) for f in ANALYSIS_SOURCES)


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


def store_race_cache(rid, race, data, cfg):
    """cfg = race_cfg_hash of the tracks as they were when the analysis read them.
    The meta is invalidated first: data without a matching meta is stale, never "fresh"."""
    _write_json(_meta_path(rid, race["id"]), {"cfg": None})   # atomic + retried, unlike a delete
    _write_json(race_cache_path(rid, race["id"]), data, compact=True)
    _write_json(_meta_path(rid, race["id"]), {"cfg": cfg, "name": race["name"],
                                              "key": race.get("key"),
                                              "entry": index_entry(0, race["id"], data)})


def remove_file(path):
    for attempt in range(20):                     # Windows: a reader may hold the file open
        try:
            os.remove(path)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if attempt == 19:
                raise
            threading.Event().wait(0.05)


def drop_race_cache(rid, race_id):
    for f in (race_cache_path(rid, race_id), _meta_path(rid, race_id)):
        remove_file(f)


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:                               # removed meanwhile (e.g. a race deleted)
        return None


def cache_state(rid, race):
    """("fresh" | "patch" | "error" | "stale", meta, cfg) of one race's cache."""
    cpath, mpath = race_cache_path(rid, race["id"]), _meta_path(rid, race["id"])
    try:
        h = race_cfg_hash(race)
    except OSError:                               # a track vanished while hashing
        return "stale", None, None
    meta = _read_json(mpath)
    if meta and meta.get("cfg") == h:
        if meta.get("error") and (_mtime(mpath) or 0) >= SRC_MTIME:
            return "error", meta, h               # same inputs failed before: don't re-parse
        if (_mtime(cpath) or 0) >= SRC_MTIME:
            stale_label = meta.get("name") != race["name"] or meta.get("key") != race.get("key")
            return ("patch" if stale_label or "entry" not in meta else "fresh"), meta, h
    return "stale", meta, h


def build_race_cached(rid, race, force=False):
    """(Re)build one race's cache when missing/stale. Returns an error string or
    None. A failed build removes the old cache so stale results are never served."""
    state, meta, h = cache_state(rid, race)
    if not force:
        if state == "fresh":
            return None
        if state == "error":
            return meta["error"]
        if state == "patch":                      # rename / new marks key: no re-parse
            data = _read_json(race_cache_path(rid, race["id"]))
            if data is not None:
                data["race"], data["key"] = race["name"], race.get("key") or race["name"]
                store_race_cache(rid, race, data, h)
                return None
    try:
        data = analyse(race_dir_abs(race), race)
    except AnalysisError as e:
        drop_race_cache(rid, race["id"])
        if e.permanent:                           # same inputs will fail again: remember it
            _write_json(_meta_path(rid, race["id"]), {"cfg": h, "error": str(e)})
        return str(e)
    store_race_cache(rid, race, data, h)
    return None


def analysed(race, tracks_dir):
    """(data, cfg): analysis of a race config on a folder of tracks + the hash of those tracks."""
    cfg = race_cfg_hash({**race, "dir": tracks_dir})
    return analyse(tracks_dir, race), cfg


def save_analysed(rid, race, data, cfg):
    """Cache first, registry last: if either write fails the registry still has the old
    config, and a cache made for a config the registry doesn't have is just stale."""
    store_race_cache(rid, race, data, cfg)
    put_race(rid, race["id"], race)


def analyse(race_dir, race):
    """Run the analysis for a race config on a folder of tracks; AnalysisError if it fails."""
    if not list_kml(race_dir):
        raise AnalysisError("No .kml files in this race's folder — choose its tracks folder.")
    print(f"  parsing {race['name']} ...", flush=True)
    try:
        data = build_race(race_dir, race["name"], race["course"], race.get("gun"), race.get("key"))
    except AnalysisError:
        raise
    except (OSError, MemoryError) as e:
        raise TransientError(f"Could not read the tracks right now ({type(e).__name__}: {e}) — try again.")
    except Exception as e:
        raise AnalysisError(f"Analysis failed: {type(e).__name__}: {e}")
    if data is None:
        raise AnalysisError("No usable tracks: expected TracTrac KML files (timestamped points, 30+ fixes per boat).")
    return data


def build_index(rid, rg):
    """The regatta's race index, assembled from the per-race meta entries (no track data)."""
    index = []
    for race in rg["races"]:
        meta = _read_json(_meta_path(rid, race["id"]))
        if meta and meta.get("entry") and os.path.exists(race_cache_path(rid, race["id"])):
            index.append({**meta["entry"], "i": len(index)})
    return index


def ensure_cache(rid, rebuild=False):
    """Build missing/stale races of one regatta and return its index."""
    rg = find_regatta(load_registry(), rid)
    if rg is None:
        return None
    todo = {r["id"] for r in rg["races"]
            if rebuild or cache_state(rid, r)[0] not in ("fresh", "error")}
    if not todo:
        return build_index(rid, rg)               # nothing to build: don't wait for the lock
    with regatta_lock(rid):
        rg = find_regatta(load_registry(), rid)
        if rg is None:
            return None
        for race in rg["races"]:
            if race["id"] not in todo:
                continue
            try:
                err = build_race_cached(rid, race, force=rebuild)
            except Exception as e:                # one race's failure must not sink the regatta
                err = f"{type(e).__name__}: {e}"
            if err:
                print(f"  ! {race['name']}: {err}", flush=True)
        return build_index(rid, rg)


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
        mini[b["sail"]] = {**{k: s.get(k) for k in keys}, "name": b.get("name"),
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
            meta = _read_json(_meta_path(rg["id"], r["id"])) or {}
            races.append({"id": r["id"], "name": r["name"], "course": r["course"], "gun": r.get("gun"),
                          "n_files": len(kml_files(r)), "uploaded": is_upload_dir(race_dir_abs(r)),
                          "analysed": os.path.exists(race_cache_path(rg["id"], r["id"])),
                          "error": meta.get("error")})
        res = rg.get("results")
        out.append({"id": rg["id"], "name": rg["name"], "races": races,
                    "results": {k: res.get(k) for k in ("url", "fetched", "columns", "n", "map")} if res else None})
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
        m = re.fullmatch(r"/api/results/([a-z0-9-]+)", path)
        if m:
            with REG_RMW:                                 # written together (download / delete)
                rg = find_regatta(load_registry(), m.group(1))
                data = _read_json(results_path(rg["id"])) if rg and rg.get("results") else None
            if rg is None:
                return self._err("unknown regatta", 404)
            return self._json({"ok": True, "results": {**rg["results"], **data} if data else None})
        m = re.fullmatch(r"/api/race/([a-z0-9-]+)/([a-z0-9-]+)", path)
        if m:
            rid = m.group(1)
            rg = find_regatta(load_registry(), rid)
            race = find_race(rg, m.group(2)) if rg else None
            if race is None:
                return self._err("unknown race", 404)
            if cache_state(rid, race)[0] != "fresh":
                with regatta_lock(rid):           # re-read: the race may have changed meanwhile
                    rg = find_regatta(load_registry(), rid)
                    race = find_race(rg, race["id"]) if rg else None
                    if race is None:
                        return self._err("unknown race", 404)
                    err = build_race_cached(rid, race)
                if err:
                    return self._err(err)
            return self._file(race_cache_path(rid, race["id"]), "application/json")
        self._send(404, "not found", "text/plain")

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n < 0:
            raise ValueError("Bad Content-Length.")
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
            return self._route(parts, body)
        except ValueError as e:
            return self._err(str(e))
        except Exception as e:                    # never leave the client without a response
            return self._err(f"Server error: {type(e).__name__}: {e}", 500)

    def _route(self, parts, body):
        if parts == ["api", "regattas"]:                                   # create regatta
            name = clean_text(body.get("name"), 80)
            if not name:
                raise ValueError("Give the regatta a name.")

            def taken(reg, rid):              # registered, or leftovers of a deleted one
                return find_regatta(reg, rid) or os.path.exists(os.path.join(UPLOAD_DIR, rid)) \
                    or os.path.exists(os.path.join(CACHE_DIR, rid))

            def add(reg):
                rid, k = _slug(name), 2
                while taken(reg, rid):
                    rid, k = f"{_slug(name)}-{k}", k + 1
                reg["regattas"].append({"id": rid, "name": name, "races": []})
                return rid
            return self._json({"ok": True, "id": edit_registry(add)})
        rid = parts[2] if len(parts) > 2 else ""
        if not _safe_id(rid) or find_regatta(load_registry(), rid) is None:
            return self._err("unknown regatta", 404)
        if parts[3:] == ["results"]:                                      # download official results
            url = str(body.get("url", "")).strip()
            res = RS.fetch_results(url)                   # network: outside any lock

            def save(reg):                                # the file and its registry entry together
                g = find_regatta(reg, rid)
                if g is None:
                    raise ValueError("unknown regatta")
                _write_json(results_path(rid), {"races": res["races"], "rows": res["rows"]})
                default_map = [g["races"][i]["id"] if i < len(g["races"]) else None for i in range(len(res["races"]))]
                old = g.get("results") or {}
                by_name = dict(zip(old.get("columns") or [], old.get("map") or []))
                mp = [by_name[c] if c in by_name else None for c in res["races"]]
                used = {m for m in mp if m}
                for i, c in enumerate(res["races"]):          # new columns: positional default if free
                    if c not in by_name and default_map[i] and default_map[i] not in used:
                        mp[i] = default_map[i]
                        used.add(mp[i])
                g["results"] = {"url": url, "fetched": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                "columns": res["races"], "n": len(res["rows"]), "map": mp}
            edit_registry(save)
            return self._json({"ok": True, "n": len(res["rows"]), "columns": res["races"]})
        if parts[3:] == ["results", "map"]:                               # result column -> race
            def set_map(reg):
                g = find_regatta(reg, rid)
                if g is None or not g.get("results"):
                    raise ValueError("No official results for this regatta.")
                ids = {r["id"] for r in g["races"]}
                mp = body.get("map")
                if not isinstance(mp, list) or len(mp) != len(g["results"]["columns"]):
                    raise ValueError("Bad results mapping.")
                g["results"]["map"] = [m if m in ids else None for m in mp]
            edit_registry(set_map)
            return self._json({"ok": True})
        if parts[3:] == ["results", "delete"]:
            def drop(reg):
                g = find_regatta(reg, rid)
                if g is not None:
                    g.pop("results", None)
                remove_file(results_path(rid))
            edit_registry(drop)
            return self._json({"ok": True})
        if parts[3:] == ["rename"]:
            name = clean_text(body.get("name"), 80)
            if not name:
                raise ValueError("Give the regatta a name.")
            def rename(reg):
                g = find_regatta(reg, rid)
                if g is None:
                    raise ValueError("unknown regatta")
                g["name"] = name
            edit_registry(rename)
            return self._json({"ok": True})
        with regatta_lock(rid):                   # one regatta's analyses/track swaps at a time
            return self._regatta_route(rid, parts, body)

    def _regatta_route(self, rid, parts, body):
        rg = find_regatta(load_registry(), rid)   # snapshot; changes go through edit_registry
        if rg is None:
            return self._err("unknown regatta", 404)
        if parts[3:] == ["delete"]:
            edit_registry(lambda reg: reg.update(regattas=[g for g in reg["regattas"] if g["id"] != rid]))
            for d in (os.path.join(UPLOAD_DIR, rid), os.path.join(CACHE_DIR, rid)):
                shutil.rmtree(d, ignore_errors=True)       # app-made copies/caches only
            return self._json({"ok": True})
        if parts[3:] == ["races"]:                                         # create race
            course, gun = course_and_gun(body)

            def add(reg):
                g = find_regatta(reg, rid)
                name = clean_text(body.get("name"), 60) or f"Race {len(g['races']) + 1}"
                k = max([g.get("seq", 0), len(g["races"])] + [_race_num(r["id"]) for r in g["races"]]) + 1
                while f"r{k:02d}" in {r["id"] for r in g["races"]}:
                    k += 1
                g["seq"] = k
                race_id = f"r{k:02d}"
                d = upload_dir_for(rid, race_id)
                os.makedirs(d, exist_ok=True)
                g["races"].append({"id": race_id, "name": name, "dir": rel_dir(d),
                                   "course": course, "gun": gun, "key": new_key(rid, race_id)})
                return race_id
            return self._json({"ok": True, "id": edit_registry(add)})
        race = find_race(rg, parts[4]) if len(parts) > 5 and parts[3] == "race" else None
        if race is None:
            return self._err("unknown race", 404)
        action = parts[5]
        if action == "update":                    # name / course / gun; analysed before saving
            new = {**race, **race_fields(body, race)}
            if new["course"] != race["course"]:
                new["key"] = new_key(rid, race["id"])    # old hand-edited marks don't fit
            if (new["course"] != race["course"] or new["gun"] != race.get("gun")) and kml_files(new):
                data, cfg = analysed(new, race_dir_abs(new))   # raises -> nothing saved
                save_analysed(rid, new, data, cfg)
                return self._json({"ok": True})
            put_race(rid, race["id"], new)        # name only: saved; patch an existing analysis
            err = build_race_cached(rid, new) if os.path.exists(race_cache_path(rid, race["id"])) else None
            return self._json({"ok": True, **({"warning": err} if err else {})})
        if action == "stage":                     # empty staging folder for a new set of tracks
            st = staging_dir_for(rid, race["id"])
            shutil.rmtree(st, ignore_errors=True)
            if os.path.isdir(st) and os.listdir(st):
                raise ValueError("Could not clear the previous upload (a file is in use) — try again.")
            os.makedirs(st, exist_ok=True)
            return self._json({"ok": True})
        if action == "unstage":                   # discard the staged copies of a failed save
            shutil.rmtree(staging_dir_for(rid, race["id"]), ignore_errors=True)
            return self._json({"ok": True})
        if action == "commit":                    # analyse the staged tracks, then swap them in
            st = staging_dir_for(rid, race["id"])
            if not list_kml(st):
                raise ValueError("No uploaded .kml files to use.")
            new = {**race, **(race_fields(body, race) if "course" in body else {})}
            new["key"] = new_key(rid, race["id"])  # new tracks: old hand-edited marks don't apply
            data, cfg = analysed(new, st)         # raises -> the current tracks stay
            d = upload_dir_for(rid, race["id"])
            old = f"{d}.old-{uuid.uuid4().hex[:6]}"   # fresh name: a stuck leftover can't collide
            if os.path.isdir(d):
                os.replace(d, old)                # whole-folder swap: never half-replaced
            try:
                os.replace(st, d)
            except OSError:
                if os.path.isdir(old):
                    os.replace(old, d)
                raise
            new["dir"] = rel_dir(d)
            try:
                save_analysed(rid, new, data, cfg)    # moved files keep name/size/mtime
            except Exception:
                os.replace(d, st)                 # undo: new set back to staging, old set back
                if os.path.isdir(old):
                    os.replace(old, d)
                raise
            shutil.rmtree(old, ignore_errors=True)
            return self._json({"ok": True})
        if action == "delete":
            put_race(rid, race["id"], None)
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
        rg = find_regatta(load_registry(), parts[2]) if _safe_id(parts[2]) else None
        race = find_race(rg, parts[4]) if rg else None
        if race is None:
            return self._err("unknown race", 404)
        st = staging_dir_for(parts[2], race["id"])
        if not os.path.isdir(st):
            raise ValueError("Start an upload (stage) before sending files.")
        if os.path.dirname(os.path.abspath(os.path.join(st, name))) != os.path.abspath(st):
            raise ValueError("Unsupported file name.")
        try:
            with open(os.path.join(st, name), "xb") as fh:    # staging only; no lock needed
                fh.write(raw)
        except FileExistsError:
            raise ValueError(f"Two tracks named {name}: choose the race's own folder.")
        except FileNotFoundError:
            raise ValueError("The upload was restarted by another save — save again.")
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
