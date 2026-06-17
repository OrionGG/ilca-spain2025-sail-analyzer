"""
app.py — local web app for ILCA GPS race analysis.

Run:  python app.py        (parses all races once, caches, opens browser)
      python app.py --rebuild   (force re-parse)
      python app.py --port 8000

Stdlib only (+ numpy via regatta). No internet required; the UI is fully
offline (no CDN, no map tiles).
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

import regatta as R

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.dirname(HERE)               # the folder with 01_Prueba 1 ...
CACHE_DIR = os.path.join(HERE, "cache")
STATIC_DIR = os.path.join(HERE, "static")

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
def build_race(race_dir: str, race_name: str):
    files = sorted(glob.glob(os.path.join(race_dir, "*.kml")))
    boats = []
    for f in files:
        base = os.path.basename(f)[:-4]
        sail = base.split("-")[0]
        disp = base.split("_", 1)[-1] if "_" in base else base
        t, la, lo = R.parse_kml(f)
        if len(t) < 30:
            continue
        b = R.Boat(sail=sail, name=disp, file=f, t=t, lat=la, lon=lo)
        boats.append(b)
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
    gun_t = R.detect_gun(boats, wind_from)
    legs_by_sail = {b.sail: R.segment_legs(b, wind_from, gun_t=gun_t) for b in boats}

    # course marks at the fleet's actual rounding points (windward / wing /
    # leeward gate / finish), located from the per-boat legs
    marks = R.detect_marks(boats, wind_from, legs_by_sail)

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

    # fleet aggregate stats on the grid (resample each boat)
    sog_grid, vmg_grid = [], []
    for b in boats:
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
        up = b.twa < 70; dn = b.twa > 110
        dist_m = float(np.sum(np.hypot(np.diff(b.x), np.diff(b.y))))
        tacks = [m for m in mans if m.kind == "tack"]
        gybes = [m for m in mans if m.kind == "gybe"]
        summary = {
            "n": len(b.t),
            "dist_nm": round(dist_m / 1852.0, 2),
            "sog_mean": round(float(b.sog.mean()), 2),
            "sog_up": round(float(b.sog[up].mean()), 2) if up.any() else None,
            "sog_dn": round(float(b.sog[dn].mean()), 2) if dn.any() else None,
            "twa_up": round(float(np.median(b.twa[up])), 0) if up.any() else None,
            "twa_dn": round(float(np.median(b.twa[dn])), 0) if dn.any() else None,
            "vmg_up": round(float(b.vmg[up].mean()), 2) if up.any() else None,
            "vmg_dn": round(float(-b.vmg[dn].mean()), 2) if dn.any() else None,
            "n_tacks": len(tacks), "n_gybes": len(gybes),
            "tack_loss": round(float(np.mean([m.bl_lost for m in tacks])), 2) if tacks else None,
            "gybe_loss": round(float(np.mean([m.bl_lost for m in gybes])), 2) if gybes else None,
        }
        # cumulative made-good distance along each leg's OWN axis (handles
        # upwind/reach/downwind uniformly) -> continuous position ladder
        course = np.zeros(len(b.t)); acc = 0.0
        for (a, c, kind) in legs:
            axx, ayy = b.x[c] - b.x[a], b.y[c] - b.y[a]
            nrm = float(np.hypot(axx, ayy))
            if c <= a or nrm < 1.0:
                course[a:] = acc; continue
            axx, ayy = axx / nrm, ayy / nrm
            seg = (b.x[a:c + 1] - b.x[a]) * axx + (b.y[a:c + 1] - b.y[a]) * ayy
            course[a:c + 1] = acc + seg
            acc += float(seg[-1])
            course[c + 1:] = acc
        course_grid = np.interp(grid, b.t, course, left=np.nan, right=np.nan)

        prog = progress.get(b.sail)
        boats_out.append({
            "course": rnan(course_grid, 0),
            "sail": b.sail, "name": b.name, "color": PALETTE[i % len(PALETTE)],
            "t": r0(b.t), "lat": r6(b.lat), "lon": r6(b.lon),
            "sog": r2(b.sog), "cog": r1(b.cog), "twa": r1(b.twa),
            "vmg": r2(b.vmg), "tack": [int(x) for x in b.tack],
            "legs": [[int(a), int(c), kind] for (a, c, kind) in legs],
            "maneuvers": [vars(m) for m in mans],
            "progress": (rnan(prog, 1) if prog is not None else None),
            "summary": summary,
        })

    return {
        "race": race_name,
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


def discover_races():
    dirs = sorted(d for d in glob.glob(os.path.join(DATA_DIR, "*"))
                  if os.path.isdir(d) and glob.glob(os.path.join(d, "*.kml")))
    races = []
    for d in dirs:
        name = os.path.basename(d)
        name = re.sub(r"^\d+_", "", name)        # strip "01_"
        races.append((d, name))
    return races


def ensure_cache(rebuild=False):
    os.makedirs(CACHE_DIR, exist_ok=True)
    races = discover_races()
    index = []
    for i, (d, name) in enumerate(races):
        cpath = os.path.join(CACHE_DIR, f"race_{i}.json")
        if rebuild or not os.path.exists(cpath):
            print(f"  parsing {name} ...", flush=True)
            data = build_race(d, name)
            if data is None:
                continue
            with open(cpath, "w", encoding="utf-8") as fh:
                json.dump(data, fh, separators=(",", ":"))
        # read minimal header for the index
        with open(cpath, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        index.append({"i": i, "race": data["race"], "start_iso": data["start_iso"],
                      "wind_from": data["wind_from"], "n_boats": len(data["boats"])})
    with open(os.path.join(CACHE_DIR, "index.json"), "w", encoding="utf-8") as fh:
        json.dump(index, fh)
    return index


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
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/" or path == "/index.html":
            return self._file(os.path.join(STATIC_DIR, "index.html"), "text/html; charset=utf-8")
        if path.startswith("/static/"):
            fp = os.path.join(STATIC_DIR, path[len("/static/"):])
            ctype = ("text/css" if fp.endswith(".css")
                     else "application/javascript" if fp.endswith(".js")
                     else "application/octet-stream")
            return self._file(fp, ctype)
        if path == "/api/index":
            return self._file(os.path.join(CACHE_DIR, "index.json"), "application/json")
        m = re.match(r"/api/race/(\d+)$", path)
        if m:
            return self._file(os.path.join(CACHE_DIR, f"race_{int(m.group(1))}.json"),
                              "application/json")
        self._send(404, "not found", "text/plain")

    def _file(self, fp, ctype):
        if not os.path.exists(fp):
            return self._send(404, "not found", "text/plain")
        with open(fp, "rb") as fh:
            self._send(200, fh.read(), ctype)


def main():
    rebuild = "--rebuild" in sys.argv
    port = 8765
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])

    print(f"Data folder : {DATA_DIR}")
    print("Building analysis cache (first run parses 441 KML files; ~10-20 s)...")
    idx = ensure_cache(rebuild=rebuild)
    print(f"Ready: {len(idx)} races cached.")

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
