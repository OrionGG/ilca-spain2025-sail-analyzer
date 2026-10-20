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
from sail_analyzer.use_cases.errors import NotFound  # noqa: E402
from sail_analyzer.use_cases.manage_races import ManageRaces  # noqa: E402
from sail_analyzer.use_cases.manage_regattas import ManageRegattas  # noqa: E402
from sail_analyzer.use_cases.official_results import OfficialResults  # noqa: E402
from sail_analyzer.use_cases.race_analyses import RaceAnalyses, index_entry  # noqa: E402,F401
from sail_analyzer.use_cases.registry import find_regatta  # noqa: E402
from sail_analyzer.use_cases.registry import race_num as _race_num, slug as _slug  # noqa: E402,F401

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

    def count(self, tracks_dir):
        return len(list_kml(tracks_dir))


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


def rel_dir(path):
    return os.path.relpath(path, HERE).replace("\\", "/")


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
def _package_sources(root, skip=(CACHE_DIR, UPLOAD_DIR, STATIC_DIR)):
    """The package's Python files in every circle. The package's own cache/, data/ and static/
    folders (generated analysis, uploaded tracks, the UI) aren't walked; dot-files (editor lock
    files) and __pycache__ are left out."""
    skip = {os.path.normcase(os.path.abspath(p)) for p in skip}
    out = []
    for d, sub, files in os.walk(root):
        sub[:] = [x for x in sub if x != "__pycache__" and not x.startswith(".")
                  and os.path.normcase(os.path.join(d, x)) not in skip]
        out += [os.path.join(d, f) for f in files if f.endswith(".py") and not f.startswith(".")]
    return out


def _newest(paths):
    """The newest modification time of the files that still exist."""
    times = []
    for f in paths:
        try:
            times.append(os.path.getmtime(f))
        except OSError:                           # removed or a dangling link: not code we run
            pass
    return max(times)


ANALYSIS_SOURCES = _package_sources(HERE)       # the code a cached race comes from
SRC_MTIME = _newest(ANALYSIS_SOURCES)


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


def store_race_cache(rid, race, data, cfg, entry):
    """cfg = race_cfg_hash of the tracks as they were when the analysis read them; entry = the
    race's regatta-index entry. The meta is invalidated first: data without a matching meta is
    stale, never "fresh"."""
    _write_json(_meta_path(rid, race["id"]), {"cfg": None})   # atomic + retried, unlike a delete
    _write_json(race_cache_path(rid, race["id"]), data, compact=True)
    _write_json(_meta_path(rid, race["id"]), {"cfg": cfg, "name": race["name"],
                                              "key": race.get("key"), "entry": entry})


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


# --------------------------------------------------------------------------- #
# Gateways for the use cases, and the wiring (transitional: they move to the
# interface adapters / frameworks circles in steps 4-5). Module globals are read
# at call time, so tests can point the app at temporary folders.
# --------------------------------------------------------------------------- #
class JsonRegattaRepository:
    def load(self):
        return load_registry()

    def edit(self, fn):
        return edit_registry(fn)

    def view(self, fn):
        with REG_RMW:
            return fn(load_registry())


class FileAnalysisCache:
    def state(self, rid, race):
        return cache_state(rid, race)

    def config_hash(self, race, tracks_dir):
        return race_cfg_hash({**race, "dir": tracks_dir})

    def read(self, rid, race_id):
        return _read_json(race_cache_path(rid, race_id))

    def meta(self, rid, race_id):
        return _read_json(_meta_path(rid, race_id))

    def exists(self, rid, race_id):
        return os.path.exists(race_cache_path(rid, race_id))

    def store(self, rid, race, data, cfg, entry):
        store_race_cache(rid, race, data, cfg, entry)

    def store_error(self, rid, race_id, cfg, error):
        _write_json(_meta_path(rid, race_id), {"cfg": cfg, "error": error})

    def drop(self, rid, race_id):
        drop_race_cache(rid, race_id)

    def regatta_exists(self, rid):
        return os.path.exists(os.path.join(CACHE_DIR, rid))

    def drop_regatta(self, rid):
        shutil.rmtree(os.path.join(CACHE_DIR, rid), ignore_errors=True)


class UploadFolders:
    def race_dir(self, race):
        return race_dir_abs(race)

    def is_upload(self, race):
        return is_upload_dir(race_dir_abs(race))

    def new_race_dir(self, rid, race_id):
        d = upload_dir_for(rid, race_id)
        os.makedirs(d, exist_ok=True)
        return rel_dir(d)

    def staging_dir(self, rid, race_id):
        return staging_dir_for(rid, race_id)

    def stage(self, rid, race_id):
        st = staging_dir_for(rid, race_id)
        shutil.rmtree(st, ignore_errors=True)
        if os.path.isdir(st) and os.listdir(st):
            raise ValueError("Could not clear the previous upload (a file is in use) — try again.")
        os.makedirs(st, exist_ok=True)

    def unstage(self, rid, race_id):
        shutil.rmtree(staging_dir_for(rid, race_id), ignore_errors=True)

    def put_staged(self, rid, race_id, name, raw):
        st = staging_dir_for(rid, race_id)
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

    def swap_in(self, rid, race_id):
        st, d = staging_dir_for(rid, race_id), upload_dir_for(rid, race_id)
        old = f"{d}.old-{uuid.uuid4().hex[:6]}"   # fresh name: a stuck leftover can't collide
        if os.path.isdir(d):
            os.replace(d, old)                    # whole-folder swap: never half-replaced
        try:
            os.replace(st, d)
        except OSError:
            if os.path.isdir(old):
                os.replace(old, d)
            raise

        def undo():                               # new set back to staging, old set back
            os.replace(d, st)
            if os.path.isdir(old):
                os.replace(old, d)
        return rel_dir(d), undo, lambda: shutil.rmtree(old, ignore_errors=True)

    def delete_race(self, rid, race):
        if is_upload_dir(race_dir_abs(race)):
            shutil.rmtree(race_dir_abs(race), ignore_errors=True)
        shutil.rmtree(staging_dir_for(rid, race["id"]), ignore_errors=True)

    def regatta_exists(self, rid):
        return os.path.exists(os.path.join(UPLOAD_DIR, rid))

    def delete_regatta(self, rid):
        shutil.rmtree(os.path.join(UPLOAD_DIR, rid), ignore_errors=True)


class ResultsPage:
    def fetch(self, url):
        return RS.fetch_results(url)


class ResultsFiles:
    def save(self, rid, data):
        _write_json(results_path(rid), data)

    def load(self, rid):
        return _read_json(results_path(rid))

    def remove(self, rid):
        remove_file(results_path(rid))


class RegattaLocksInProcess:
    def regatta(self, rid):
        return regatta_lock(rid)


class UtcClock:
    def now_iso(self):
        return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RandomMarksKeys:
    def new(self, rid, race_id):
        return new_key(rid, race_id)


class JsonRacePresenter:
    def present(self, analysis):
        return present_race(analysis)


def _console(line):
    print(line, flush=True)


_TRACKS, _REPO, _CACHE, _FOLDERS = KmlTrackGateway(), JsonRegattaRepository(), FileAnalysisCache(), UploadFolders()
_LOCKS = RegattaLocksInProcess()
ANALYSES = RaceAnalyses(_TRACKS, JsonRacePresenter(), _REPO, _CACHE, _FOLDERS, _LOCKS, _console)
REGATTAS = ManageRegattas(_REPO, _CACHE, _FOLDERS, _TRACKS, _LOCKS)
RACES = ManageRaces(_REPO, ANALYSES, _FOLDERS, _CACHE, _TRACKS, RandomMarksKeys(), _LOCKS)
RESULTS = OfficialResults(_REPO, ResultsPage(), ResultsFiles(), UtcClock())


def ensure_cache(rid, rebuild=False):
    """Build missing/stale races of one regatta and return its index."""
    return ANALYSES.index(rid, rebuild)


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
        except NotFound as e:
            return self._err(str(e), 404)
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
            return self._json(REGATTAS.list())
        m = re.fullmatch(r"/api/index/([a-z0-9-]+)", path)
        if m:
            idx = ANALYSES.index(m.group(1))      # stat-only when nothing changed
            return self._json(idx) if idx is not None else self._err("unknown regatta", 404)
        m = re.fullmatch(r"/api/results/([a-z0-9-]+)", path)
        if m:
            return self._json({"ok": True, "results": RESULTS.get(m.group(1))})
        m = re.fullmatch(r"/api/race/([a-z0-9-]+)/([a-z0-9-]+)", path)
        if m:
            rid, race_id = m.group(1), m.group(2)
            err = ANALYSES.race(rid, race_id)
            if err:
                return self._err(err)
            return self._file(race_cache_path(rid, race_id), "application/json")
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
        except NotFound as e:
            return self._err(str(e), 404)
        except ValueError as e:
            return self._err(str(e))
        except Exception as e:                    # never leave the client without a response
            return self._err(f"Server error: {type(e).__name__}: {e}", 500)

    def _route(self, parts, body):
        if parts == ["api", "regattas"]:                                   # create regatta
            return self._json({"ok": True, "id": REGATTAS.create(clean_text(body.get("name"), 80))})
        rid = parts[2] if len(parts) > 2 else ""
        if not _safe_id(rid) or find_regatta(load_registry(), rid) is None:
            return self._err("unknown regatta", 404)
        if parts[3:] == ["results"]:                                      # download official results
            n, columns = RESULTS.download(rid, str(body.get("url", "")).strip())
            return self._json({"ok": True, "n": n, "columns": columns})
        if parts[3:] == ["results", "map"]:                               # result column -> race
            RESULTS.set_map(rid, body.get("map"))
            return self._json({"ok": True})
        if parts[3:] == ["results", "delete"]:
            RESULTS.remove(rid)
            return self._json({"ok": True})
        if parts[3:] == ["rename"]:
            REGATTAS.rename(rid, clean_text(body.get("name"), 80))
            return self._json({"ok": True})
        if parts[3:] == ["delete"]:
            REGATTAS.delete(rid)
            return self._json({"ok": True})
        if parts[3:] == ["races"]:                                         # create race
            course, gun = course_and_gun(body)
            return self._json({"ok": True, "id": RACES.create(rid, clean_text(body.get("name"), 60), course, gun)})
        if len(parts) <= 5 or parts[3] != "race":
            return self._err("unknown race", 404)
        race_id, action = parts[4], parts[5]
        if action == "update":                    # name / course / gun; analysed before saving
            err = RACES.update(rid, race_id, self._race_name(body), *course_and_gun(body))
            return self._json({"ok": True, **({"warning": err} if err else {})})
        if action == "stage":                     # empty staging folder for a new set of tracks
            RACES.stage(rid, race_id)
            return self._json({"ok": True})
        if action == "unstage":                   # discard the staged copies of a failed save
            RACES.unstage(rid, race_id)
            return self._json({"ok": True})
        if action == "commit":                    # analyse the staged tracks, then swap them in
            config = (self._race_name(body), *course_and_gun(body)) if "course" in body else None
            RACES.commit(rid, race_id, config)
            return self._json({"ok": True})
        if action == "delete":
            RACES.delete(rid, race_id)
            return self._json({"ok": True})
        return self._err("not found", 404)

    @staticmethod
    def _race_name(body):
        """The new name of a race ('' = keep its name)."""
        return clean_text(body["name"], 60) if "name" in body else ""

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
        if not _safe_id(parts[2]):
            return self._err("unknown race", 404)
        RACES.upload(parts[2], parts[4], name, raw)
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
