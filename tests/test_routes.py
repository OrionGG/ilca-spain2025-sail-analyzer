"""HTTP API end to end: a real server on a temporary registry / data / cache folder.

Uploads 8 real tracks of Copa race 1 (a full analysis in a few seconds); the
official-results download is stubbed with the local fixture.
"""

import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from tests import support
import app as A

RACE1 = os.path.join(support.ROOT, "01_Prueba 1")


def read(path):
    with open(path, "rb") as fh:
        return fh.read()
TRAP = A.template_course("trap_outer")


class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="sa_routes_")
        cls.saved = {k: getattr(A, k) for k in ("REG_PATH", "UPLOAD_DIR", "CACHE_DIR", "DATA_DIR")}
        A.REG_PATH = os.path.join(cls.tmp, "regattas.json")
        A.UPLOAD_DIR = os.path.join(cls.tmp, "data")
        A.CACHE_DIR = os.path.join(cls.tmp, "cache")
        A.DATA_DIR = os.path.join(cls.tmp, "none")                 # no race folders to seed
        os.makedirs(A.DATA_DIR)
        cls.fetch = A.RS.fetch_results
        A.RS.fetch_results = lambda url: A.RS.parse_results(support.fixture("sailti_like.html"))
        cls.srv = A.ThreadingHTTPServer(("127.0.0.1", 0), A.Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        A.RS.fetch_results = cls.fetch
        for k, v in cls.saved.items():
            setattr(A, k, v)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # -- helpers -------------------------------------------------------------
    def call(self, path, body=None, raw=None, headers=None, method=None):
        h = {"X-Sail-Analyzer": "1", "Content-Type": "application/json"}
        h.update(headers or {})
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, headers=h,
                                     method=method or ("POST" if data is not None else "GET"))
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.headers.get("Content-Type", ""), r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type", ""), e.read()

    def json(self, path, body=None, raw=None, status=200, **kw):
        code, _ct, data = self.call(path, body, raw, **kw)
        out = json.loads(data or b"{}")
        self.assertEqual(code, status, f"{path}: {out}")
        return out

    def upload(self, rid, race_id, name, raw, status=200):
        q = urllib.request.quote(name)
        return self.json(f"/api/regatta/{rid}/race/{race_id}/file?name={q}", raw=raw, status=status)

    # -- guard and static files ---------------------------------------------
    def test_guard_refuses_cross_site_requests(self):
        for headers in ({"X-Sail-Analyzer": ""}, {"Origin": "https://evil.example"}, {"Host": "evil.example"}):
            code, _ct, _d = self.call("/api/regattas", {"name": "x"}, headers=headers)
            self.assertEqual(code, 403, headers)
        code, _ct, _d = self.call("/api/regattas", headers={"Host": "evil.example"})
        self.assertEqual(code, 403)

    def test_static_files(self):
        code, ct, body = self.call("/")
        self.assertEqual((code, ct.split(";")[0]), (200, "text/html"))
        self.assertIn(b"<html", body.lower())
        for name, ctype in (("app.js", "application/javascript"), ("manage.js", "application/javascript"),
                            ("style.css", "text/css")):
            code, ct, _ = self.call(f"/static/{name}")
            self.assertEqual((code, ct), (200, ctype), name)
        self.assertEqual(self.call("/static/nope.js")[0], 404)

    def test_unknown_routes(self):
        self.assertEqual(self.json("/api/index/nope", status=404)["error"], "unknown regatta")
        self.assertEqual(self.json("/api/results/nope", status=404)["error"], "unknown regatta")
        self.json("/api/nope", {}, status=404)

    # -- the whole life of a regatta -----------------------------------------
    def test_regatta_lifecycle(self):
        view = self.json("/api/regattas")
        self.assertEqual(view["regattas"], [])
        self.assertEqual([t["id"] for t in view["templates"]],
                         ["trap_outer", "trap_outer_wing", "trap_inner", "wl2_dn", "wl2_up", "wl3_dn"])

        # regattas: create (unique slug), rename, delete
        self.assertEqual(self.json("/api/regattas", {"name": "Test Regatta"})["id"], "test-regatta")
        self.assertEqual(self.json("/api/regattas", {"name": "Test Regatta"})["id"], "test-regatta-2")
        self.json("/api/regattas", {"name": "  "}, status=400)
        self.json("/api/regatta/test-regatta-2/delete", {})
        self.json("/api/regatta/test-regatta/rename", {"name": "Renamed"})
        view = self.json("/api/regattas")
        self.assertEqual([(g["id"], g["name"]) for g in view["regattas"]], [("test-regatta", "Renamed")])
        rid = "test-regatta"

        # races: validated course, staged upload, analysed commit
        self.json(f"/api/regatta/{rid}/races", {"name": "Bad", "course": [{"type": "U", "mark": "1"}]}, status=400)
        race_id = self.json(f"/api/regatta/{rid}/races", {"name": "Race A", "course": TRAP, "gun": None})["id"]
        self.assertEqual(race_id, "r01")
        kml = sorted(f for f in os.listdir(RACE1) if f.endswith(".kml"))[:8]
        raw = read(os.path.join(RACE1, kml[0]))
        self.assertIn("Start an upload", self.upload(rid, race_id, kml[0], raw, status=400)["error"])
        self.json(f"/api/regatta/{rid}/race/{race_id}/stage", {})
        for f in kml:
            self.upload(rid, race_id, f, read(os.path.join(RACE1, f)))
        self.assertIn("Not a .kml", self.upload(rid, race_id, "x.txt", raw, status=400)["error"])
        self.assertIn("does not look like", self.upload(rid, race_id, "y.kml", b"hello", status=400)["error"])
        self.assertIn("Two tracks named", self.upload(rid, race_id, kml[0], raw, status=400)["error"])
        self.json(f"/api/regatta/{rid}/race/{race_id}/commit", {"name": "Race A", "course": TRAP, "gun": None})

        race = self.json("/api/regattas")["regattas"][0]["races"][0]
        self.assertEqual({k: race[k] for k in ("id", "name", "n_files", "uploaded", "analysed", "error")},
                         {"id": "r01", "name": "Race A", "n_files": 8, "uploaded": True, "analysed": True, "error": None})

        idx = self.json(f"/api/index/{rid}")
        self.assertEqual(len(idx), 1)
        self.assertEqual(set(idx[0]), {"i", "id", "race", "start_iso", "wind_from", "n_boats", "fleet", "boats"})
        self.assertEqual(idx[0]["n_boats"], 8)
        mini = next(iter(idx[0]["boats"].values()))
        self.assertTrue({"rank", "n_fin", "finished", "name", "race_min", "sog_up"} <= set(mini))

        data = self.json(f"/api/race/{rid}/{race_id}")
        self.assertEqual(set(data), {"race", "key", "course", "gun_manual", "start_t", "gun_t", "start_iso",
                                     "wind_from", "lat0", "lon0", "marks", "start_line", "wind_series",
                                     "fleet_stats", "boats"})
        self.assertEqual(len(data["boats"]), 8)
        self.assertTrue({"RC", "Pin", "1", "G"} <= {m["label"] for m in data["marks"]})
        boat = data["boats"][0]
        self.assertTrue({"sail", "name", "color", "t", "lat", "lon", "sog", "cog", "twa", "vmg", "tack", "legs",
                         "course_ix", "rounds", "maneuvers", "penalties", "shadow", "course", "progress",
                         "summary"} <= set(boat))
        key = data["key"]

        # update: a name only keeps the analysis; a new course re-analyses with a new marks key
        self.json(f"/api/regatta/{rid}/race/{race_id}/update", {"name": "Race B", "course": TRAP, "gun": None})
        renamed = self.json(f"/api/race/{rid}/{race_id}")
        self.assertEqual((renamed["race"], renamed["key"]), ("Race B", key))      # hand-placed marks still apply
        self.assertEqual(renamed["boats"], data["boats"])                          # not re-analysed
        wing = A.template_course("trap_outer_wing")
        self.json(f"/api/regatta/{rid}/race/{race_id}/update", {"name": "Race B", "course": wing, "gun": None})
        data = self.json(f"/api/race/{rid}/{race_id}")
        self.assertEqual(data["course"], wing)
        self.assertNotEqual(data["key"], key)

        # official results: download, column map, remove
        out = self.json(f"/api/regatta/{rid}/results", {"url": "https://example.org/results"})
        self.assertEqual(out, {"ok": True, "n": 4, "columns": ["1", "2", "3"]})
        res = self.json(f"/api/results/{rid}")["results"]
        self.assertEqual((len(res["rows"]), res["columns"], res["map"]), (4, ["1", "2", "3"], ["r01", None, None]))
        self.json(f"/api/regatta/{rid}/results/map", {"map": [None, "r01", "nope"]})
        self.assertEqual(self.json(f"/api/results/{rid}")["results"]["map"], [None, "r01", None])
        self.json(f"/api/regatta/{rid}/results/map", {"map": ["r01"]}, status=400)
        self.json(f"/api/regatta/{rid}/results", {"url": "https://example.org/results"})     # re-download
        self.assertEqual(self.json(f"/api/results/{rid}")["results"]["map"], [None, "r01", None])  # kept by name
        self.json(f"/api/regatta/{rid}/results/delete", {})
        self.assertIsNone(self.json(f"/api/results/{rid}")["results"])

        # unstage / delete race / delete regatta
        self.json(f"/api/regatta/{rid}/race/{race_id}/unstage", {})
        self.json(f"/api/regatta/{rid}/race/{race_id}/delete", {})
        self.assertEqual(self.json("/api/regattas")["regattas"][0]["races"], [])
        self.assertFalse(os.path.exists(A.upload_dir_for(rid, race_id)))
        self.json(f"/api/regatta/{rid}/delete", {})
        self.assertEqual(self.json("/api/regattas")["regattas"], [])
        self.json(f"/api/index/{rid}", status=404)
        self.json(f"/api/regatta/{rid}/results", {"url": "https://example.org/results"}, status=404)
        self.assertFalse(os.path.exists(os.path.join(A.UPLOAD_DIR, rid)))


if __name__ == "__main__":
    unittest.main()
