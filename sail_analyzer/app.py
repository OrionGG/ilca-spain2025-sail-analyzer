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
    # a boat "finished" if it sailed the full course (>= the fleet's modal leg count);
    # boats with fewer legs are retirees/DNF and are excluded from fleet metrics
    _lc = {}
    for lg in legs_by_sail.values():
        _lc[len(lg)] = _lc.get(len(lg), 0) + 1
    mode_legs = max(_lc, key=_lc.get) if _lc else 0

    # course marks at the fleet's actual rounding points (windward / wing /
    # leeward gate / finish). Prueba 2 & 5 had a wing-mark change between the two
    # roundings -> report 2a (1st) and 2b (2nd) separately.
    split_wing = race_name.strip()[-2:] in (" 2", " 5")
    marks = R.detect_marks(boats, wind_from, legs_by_sail, split_wing=split_wing)

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
    _w2 = _mk.get("2a", _mk.get("2", [])); _w2b = _mk.get("2b", _mk.get("2", []))
    _gts = _mk.get("G", []); _fin = _mk.get("F", [])
    seq_xy = [_mk.get("1", []), _w2, _gts, _w2b, _gts, _fin]      # targets per leg end
    rounds_by_sail = {}
    for b in boats:
        lg = legs_by_sail[b.sail]
        gi0 = int(np.argmin(np.abs(b.t - gun_t)))
        rounds, k0 = [], gi0
        for li in range(len(lg)):
            tgts = seq_xy[li] if li < len(seq_xy) else []
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
            "rounds": [None if v is None else int(round(v)) for v in rounds_by_sail.get(b.sail, [])],
            "shadow": shadow.get(b.sail, ""),
            "penalties": penalties.get(b.sail, []),
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
    # auto-rebuild when the analysis code changed (so a plain restart picks up
    # new logic instead of serving a stale cache)
    src_mtime = max(os.path.getmtime(os.path.join(HERE, f)) for f in ("regatta.py", "app.py"))
    races = discover_races()
    index = []
    for i, (d, name) in enumerate(races):
        cpath = os.path.join(CACHE_DIR, f"race_{i}.json")
        stale = (not os.path.exists(cpath)) or os.path.getmtime(cpath) < src_mtime
        if rebuild or stale:
            print(f"  parsing {name} ...", flush=True)
            data = build_race(d, name)
            if data is None:
                continue
            with open(cpath, "w", encoding="utf-8") as fh:
                json.dump(data, fh, separators=(",", ":"))
        # read minimal header for the index
        with open(cpath, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        # regatta trends: per-boat mini-summary + finisher medians + finish rank
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
        index.append({"i": i, "race": data["race"], "start_iso": data["start_iso"],
                      "wind_from": data["wind_from"], "n_boats": len(data["boats"]),
                      "fleet": fleet, "boats": mini})
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
