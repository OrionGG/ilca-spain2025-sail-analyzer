"""The regatta / race / results interactors, driven through their gateways with in-memory fakes
(no files, no network, no tracks parsed)."""

import contextlib
import copy
import unittest

from sail_analyzer.use_cases.errors import AnalysisError, NotFound, TransientError
from sail_analyzer.use_cases.manage_races import ManageRaces
from sail_analyzer.use_cases.manage_regattas import ManageRegattas
from sail_analyzer.use_cases.official_results import OfficialResults
from sail_analyzer.use_cases.race_analyses import RaceAnalyses

UP = [{"type": "U", "mark": "1", "gate": False}, {"type": "D", "mark": "F", "gate": False}]
REACH = [{"type": "R", "mark": "1", "gate": False}] + UP[1:]


def race(race_id, name=None, **kw):
    return {"id": race_id, "name": name or race_id.upper(), "dir": f"tracks/{race_id}", "course": UP,
            "gun": None, "key": f"k-{race_id}", **kw}


class Repo:
    """RegattaRepository: snapshots are copies, so only edit() changes what is stored."""

    def __init__(self, *regattas):
        self.reg, self.edits = {"version": 1, "regattas": list(regattas)}, 0

    def load(self):
        return copy.deepcopy(self.reg)

    def edit(self, fn):
        reg = copy.deepcopy(self.reg)
        out = fn(reg)
        self.reg, self.edits = reg, self.edits + 1
        return out

    def view(self, fn):
        return fn(copy.deepcopy(self.reg))

    def regatta(self, rid):
        return next(g for g in self.reg["regattas"] if g["id"] == rid)


class Tracks:
    """TrackGateway: how many track files each folder holds."""

    def __init__(self, counts=None):
        self.counts = counts or {}

    def count(self, tracks_dir):
        return self.counts.get(tracks_dir, 0)

    def boats(self, tracks_dir):
        raise AssertionError("the fake analysis never reads tracks")


class Analysis:
    """Stands in for AnalyseRace: an analysis object per request, or the given failure."""

    def __init__(self):
        self.requests, self.fail = [], None

    def execute(self, req):
        self.requests.append(req)
        if self.fail:
            raise self.fail
        return req


class Presenter:
    def present(self, a):
        return {"race": a.name, "key": a.key or a.name, "course": a.course, "gun": a.gun, "dir": a.tracks_dir,
                "start_iso": "2025-04-12T11:00:00+00:00", "wind_from": 270.0,
                "boats": [{"sail": "206341", "name": "GARCIA GALL",
                           "summary": {"finished": True, "race_min": 52.5, "sog_up": 5.1}}]}


class Cache:
    """AnalysisCache: data / meta per race; state is what the test sets (default stale)."""

    def __init__(self):
        self.data, self.metas, self.states, self.log = {}, {}, {}, []

    def state(self, rid, r):
        st = self.states.get((rid, r["id"]), "stale")
        return st, self.metas.get((rid, r["id"])), f"cfg-{r['dir']}"

    def config_hash(self, r, tracks_dir):
        return f"cfg-{tracks_dir}"

    def read(self, rid, race_id):
        return copy.deepcopy(self.data.get((rid, race_id)))

    def meta(self, rid, race_id):
        return self.metas.get((rid, race_id))

    def exists(self, rid, race_id):
        return (rid, race_id) in self.data

    def store(self, rid, r, data, cfg, entry):
        self.log.append(("store", r["id"], cfg))
        self.data[(rid, r["id"])] = data
        self.metas[(rid, r["id"])] = {"cfg": cfg, "name": r["name"], "key": r.get("key"), "entry": entry}

    def store_error(self, rid, race_id, cfg, error):
        self.metas[(rid, race_id)] = {"cfg": cfg, "error": error}

    def drop(self, rid, race_id):
        self.log.append(("drop", race_id))
        self.data.pop((rid, race_id), None)
        self.metas.pop((rid, race_id), None)

    def regatta_exists(self, rid):
        return any(k[0] == rid for k in self.data)

    def drop_regatta(self, rid):
        self.log.append(("drop_regatta", rid))
        for k in [k for k in self.data if k[0] == rid]:
            del self.data[k]


class Folders:
    """TrackFolders: race folders are their registry "dir"; uploads live under up/."""

    def __init__(self, tracks):
        self.tracks, self.log, self.leftovers = tracks, [], set()

    def race_dir(self, r):
        return r["dir"]

    def is_upload(self, r):
        return r["dir"].startswith("up/")

    def new_race_dir(self, rid, race_id):
        return f"up/{rid}/{race_id}"

    def staging_dir(self, rid, race_id):
        return f"up/{rid}/{race_id}.new"

    def stage(self, rid, race_id):
        self.log.append(("stage", race_id))

    def unstage(self, rid, race_id):
        self.log.append(("unstage", race_id))

    def put_staged(self, rid, race_id, name, raw):
        self.log.append(("put", race_id, name))

    def swap_in(self, rid, race_id):
        self.log.append(("swap", race_id))
        return f"up/{rid}/{race_id}", lambda: self.log.append(("undo", race_id)), \
            lambda: self.log.append(("done", race_id))

    def delete_race(self, rid, r):
        self.log.append(("delete_race", r["id"]))

    def regatta_exists(self, rid):
        return rid in self.leftovers

    def delete_regatta(self, rid):
        self.log.append(("delete_regatta", rid))


class Locks:
    def __init__(self):
        self.held, self.taken = [], []

    @contextlib.contextmanager
    def regatta(self, rid):
        self.taken.append(rid)
        self.held.append(rid)
        try:
            yield
        finally:
            self.held.pop()


class Keys:
    def __init__(self):
        self.n = 0

    def new(self, rid, race_id):
        self.n += 1
        return f"{rid}/{race_id}/new{self.n}"


class World:
    """One regatta "copa" with races r01 (5 tracks) and r02 (no tracks), wired to the interactors."""

    def __init__(self):
        self.repo = Repo({"id": "copa", "name": "Copa", "races": [race("r01"), race("r02")]})
        self.tracks = Tracks({"tracks/r01": 5, "up/copa/r01.new": 3})
        self.cache, self.folders, self.locks, self.keys = Cache(), Folders(self.tracks), Locks(), Keys()
        self.lines = []
        self.analyses = RaceAnalyses(self.tracks, Presenter(), self.repo, self.cache, self.folders, self.locks,
                                     self.lines.append)
        self.analysis = self.analyses.analyse_race = Analysis()
        self.regattas = ManageRegattas(self.repo, self.cache, self.folders, self.tracks, self.locks)
        self.races = ManageRaces(self.repo, self.analyses, self.folders, self.cache, self.tracks, self.keys,
                                 self.locks)

    def stored(self, race_id):
        return next(r for r in self.repo.regatta("copa")["races"] if r["id"] == race_id)


class RaceAnalysesTest(unittest.TestCase):
    def setUp(self):
        self.w = World()

    def test_analyse_errors(self):
        w = self.w
        with self.assertRaisesRegex(AnalysisError, "No .kml files"):
            w.analyses.analyse(race("r02"), "tracks/r02")
        w.analysis.fail = KeyError("x")
        with self.assertRaisesRegex(AnalysisError, "Analysis failed: KeyError") as e:
            w.analyses.analyse(race("r01"), "tracks/r01")
        self.assertTrue(e.exception.permanent)
        w.analysis.fail = OSError("busy")
        with self.assertRaises(TransientError) as e:
            w.analyses.analyse(race("r01"), "tracks/r01")
        self.assertFalse(e.exception.permanent)
        w.analysis.fail = None
        w.analyses.analyse_race.execute = lambda req: None          # no usable tracks
        with self.assertRaisesRegex(AnalysisError, "No usable tracks"):
            w.analyses.analyse(race("r01"), "tracks/r01")
        self.assertEqual(w.lines, ["  parsing R01 ..."] * 3)

    def test_cached_by_state(self):
        w, r = self.w, race("r01")
        w.cache.states[("copa", "r01")] = "fresh"
        self.assertIsNone(w.analyses.cached("copa", r))
        self.assertEqual(w.analysis.requests, [])
        w.cache.states[("copa", "r01")] = "error"
        w.cache.metas[("copa", "r01")] = {"error": "Analysis failed: boom"}
        self.assertEqual(w.analyses.cached("copa", r), "Analysis failed: boom")
        self.assertEqual(w.analysis.requests, [])
        w.cache.states[("copa", "r01")] = "stale"
        self.assertIsNone(w.analyses.cached("copa", r))
        self.assertEqual([q.tracks_dir for q in w.analysis.requests], ["tracks/r01"])
        meta = w.cache.metas[("copa", "r01")]
        self.assertEqual((meta["cfg"], meta["name"], meta["entry"]["race"]), ("cfg-tracks/r01", "R01", "R01"))
        self.assertEqual(meta["entry"]["boats"]["206341"]["rank"], 1)
        # a rename only patches the cached analysis
        w.cache.states[("copa", "r01")] = "patch"
        self.assertIsNone(w.analyses.cached("copa", {**r, "name": "Race one", "key": "k2"}))
        self.assertEqual(len(w.analysis.requests), 1)
        data = w.cache.data[("copa", "r01")]
        self.assertEqual((data["race"], data["key"]), ("Race one", "k2"))
        self.assertEqual(w.cache.metas[("copa", "r01")]["entry"]["race"], "Race one")

    def test_failed_build_drops_the_cache(self):
        w, r = self.w, race("r01")
        w.cache.data[("copa", "r01")] = {"old": True}
        w.analysis.fail = TransientError("busy")
        self.assertEqual(w.analyses.cached("copa", r, force=True), "busy")
        self.assertFalse(w.cache.exists("copa", "r01"))
        self.assertNotIn(("copa", "r01"), w.cache.metas)            # transient: not remembered
        w.analysis.fail = AnalysisError("bad course")
        self.assertEqual(w.analyses.cached("copa", r), "bad course")
        self.assertEqual(w.cache.metas[("copa", "r01")], {"cfg": "cfg-tracks/r01", "error": "bad course"})

    def test_index(self):
        w = self.w
        self.assertIsNone(w.analyses.index("nope"))
        idx = w.analyses.index("copa")
        self.assertEqual([(e["i"], e["id"]) for e in idx], [(0, "r01")])  # r02 has no tracks
        self.assertEqual(w.locks.taken, ["copa"])
        self.assertEqual(w.lines[-1], "  ! R02: No .kml files in this race's folder — choose its tracks folder.")
        for k in ("r01", "r02"):
            w.cache.states[("copa", k)] = "fresh" if k == "r01" else "error"
        self.assertEqual([e["id"] for e in w.analyses.index("copa")], ["r01"])
        self.assertEqual(w.locks.taken, ["copa"])                  # nothing to build: no lock
        w.analyses.index("copa", rebuild=True)
        self.assertEqual(w.locks.taken, ["copa", "copa"])
        self.assertEqual(len(w.analysis.requests), 2)

    def test_one_race_failing_does_not_sink_the_regatta(self):
        w = self.w
        w.repo.regatta("copa")["races"].insert(0, race("r00"))
        w.tracks.counts["tracks/r00"] = 2
        real = w.analyses.cached
        w.analyses.cached = lambda rid, r, force=False: 1 / 0 if r["id"] == "r00" else real(rid, r, force)
        self.assertEqual([e["id"] for e in w.analyses.index("copa")], ["r01"])
        self.assertIn("  ! R00: ZeroDivisionError: division by zero", w.lines)

    def test_race(self):
        w = self.w
        with self.assertRaises(NotFound):
            w.analyses.race("copa", "r09")
        with self.assertRaises(NotFound):
            w.analyses.race("nope", "r01")
        self.assertIsNone(w.analyses.race("copa", "r01"))
        self.assertTrue(w.cache.exists("copa", "r01"))
        self.assertIn("No .kml files", w.analyses.race("copa", "r02"))
        w.cache.states[("copa", "r01")] = "fresh"
        w.locks.taken.clear()
        self.assertIsNone(w.analyses.race("copa", "r01"))
        self.assertEqual(w.locks.taken, [])

    def test_save_writes_the_cache_before_the_registry(self):
        w = self.w
        order = []
        store, edit = w.cache.store, w.repo.edit
        w.cache.store = lambda *a: (order.append("cache"), store(*a))
        w.repo.edit = lambda fn: (order.append("registry"), edit(fn))[1]
        new = {**race("r01"), "name": "New"}
        w.analyses.save("copa", new, {"race": "New", "start_iso": "", "wind_from": 0, "boats": []}, "c")
        self.assertEqual(order, ["cache", "registry"])
        self.assertEqual(w.stored("r01")["name"], "New")


class ManageRegattasTest(unittest.TestCase):
    def setUp(self):
        self.w = World()

    def test_create(self):
        w = self.w
        with self.assertRaisesRegex(ValueError, "name"):
            w.regattas.create("")
        self.assertEqual(w.regattas.create("Copa"), "copa-2")
        w.folders.leftovers.add("trofeo")                   # tracks of a deleted regatta remain
        w.cache.data[("trofeo-2", "r01")] = {}              # and an old cache
        self.assertEqual(w.regattas.create("Trofeo!"), "trofeo-3")
        self.assertEqual(w.regattas.create("***"), "regatta")
        self.assertEqual([g["id"] for g in w.repo.reg["regattas"]], ["copa", "copa-2", "trofeo-3", "regatta"])
        self.assertEqual(w.repo.regatta("trofeo-3"), {"id": "trofeo-3", "name": "Trofeo!", "races": []})

    def test_rename_and_delete(self):
        w = self.w
        w.regattas.rename("copa", "Copa 2025")
        self.assertEqual(w.repo.regatta("copa")["name"], "Copa 2025")
        with self.assertRaisesRegex(ValueError, "name"):
            w.regattas.rename("copa", "")
        with self.assertRaisesRegex(ValueError, "unknown regatta"):
            w.regattas.rename("nope", "X")
        with self.assertRaises(NotFound):
            w.regattas.delete("nope")
        w.regattas.delete("copa")
        self.assertEqual(w.repo.reg["regattas"], [])
        self.assertEqual(w.locks.taken, ["nope", "copa"])
        self.assertIn(("delete_regatta", "copa"), w.folders.log)
        self.assertIn(("drop_regatta", "copa"), w.cache.log)

    def test_list(self):
        w = self.w
        w.cache.data[("copa", "r01")] = {}
        w.cache.metas[("copa", "r02")] = {"error": "boom"}
        w.repo.regatta("copa")["results"] = {"url": "u", "fetched": "t", "columns": ["R1"], "n": 3, "map": ["r01"],
                                             "extra": 1}
        out = w.regattas.list()
        g = out["regattas"][0]
        self.assertEqual(g["results"], {"url": "u", "fetched": "t", "columns": ["R1"], "n": 3, "map": ["r01"]})
        self.assertEqual([(r["id"], r["n_files"], r["analysed"], r["error"], r["uploaded"]) for r in g["races"]],
                         [("r01", 5, True, None, False), ("r02", 0, False, "boom", False)])
        self.assertTrue(out["templates"] and all(t["course"] for t in out["templates"]))


class ManageRacesTest(unittest.TestCase):
    def setUp(self):
        self.w = World()

    def test_create_never_reuses_an_id(self):
        w = self.w
        with self.assertRaises(NotFound):
            w.races.create("nope", "", UP, None)
        self.assertEqual(w.races.create("copa", "", UP, None), "r03")
        self.assertEqual(w.stored("r03"), {"id": "r03", "name": "Race 3", "dir": "up/copa/r03", "course": UP,
                                           "gun": None, "key": "copa/r03/new1"})
        w.races.delete("copa", "r03")
        self.assertEqual(w.races.create("copa", "Final", REACH, "12:05"), "r04")
        self.assertEqual((w.stored("r04")["name"], w.stored("r04")["gun"]), ("Final", "12:05"))
        self.assertEqual(w.locks.taken, ["nope", "copa", "copa", "copa"])

    def test_update_name_only(self):
        w = self.w
        self.assertIsNone(w.races.update("copa", "r01", "Renamed", UP, None))
        self.assertEqual((w.stored("r01")["name"], w.stored("r01")["key"]), ("Renamed", "k-r01"))
        self.assertEqual(w.analysis.requests, [])                   # no analysis cached: nothing patched
        w.analyses.cached("copa", w.stored("r01"))
        w.cache.states[("copa", "r01")] = "patch"
        self.assertIsNone(w.races.update("copa", "r01", "", UP, None))   # '' keeps the name
        self.assertEqual(w.stored("r01")["name"], "Renamed")
        self.assertEqual(len(w.analysis.requests), 1)
        w.cache.states[("copa", "r01")] = "error"
        w.cache.metas[("copa", "r01")]["error"] = "boom"
        self.assertEqual(w.races.update("copa", "r01", "Again", UP, None), "boom")
        self.assertEqual(w.stored("r01")["name"], "Again")

    def test_update_course_is_analysed_before_saving(self):
        w = self.w
        w.analysis.fail = AnalysisError("no start")
        with self.assertRaisesRegex(AnalysisError, "no start"):
            w.races.update("copa", "r01", "", REACH, "12:00")
        self.assertEqual(w.stored("r01"), race("r01"))             # nothing saved
        w.analysis.fail = None
        self.assertIsNone(w.races.update("copa", "r01", "", REACH, "12:00"))
        r = w.stored("r01")
        self.assertEqual((r["course"], r["gun"], r["key"]), (REACH, "12:00", "copa/r01/new2"))
        self.assertEqual(w.cache.metas[("copa", "r01")]["key"], r["key"])
        self.assertIsNone(w.races.update("copa", "r01", "", REACH, "12:01"))   # a new gun keeps the key
        self.assertEqual(w.stored("r01")["key"], "copa/r01/new2")
        self.assertEqual(len(w.analysis.requests), 3)
        self.assertIsNone(w.races.update("copa", "r02", "", REACH, "12:00"))   # no tracks: just saved
        self.assertEqual(len(w.analysis.requests), 3)
        self.assertEqual(w.stored("r02")["course"], REACH)
        with self.assertRaises(NotFound):
            w.races.update("copa", "r09", "", UP, None)

    def test_course_and_gun_are_validated_once_the_race_is_found(self):
        w = self.w
        bad = [{"type": "R", "mark": "1"}, {"type": "D", "mark": "F"}]     # no gun for a reaching start
        with self.assertRaises(NotFound):
            w.races.create("nope", "", bad, None)
        with self.assertRaises(NotFound):
            w.races.update("copa", "r09", "", bad, None)
        with self.assertRaisesRegex(ValueError, "No uploaded"):        # staged tracks checked first
            w.races.commit("copa", "r02", ("", bad, None))
        for action in (lambda: w.races.create("copa", "", bad, None), lambda: w.races.update("copa", "r01", "", bad, None),
                       lambda: w.races.commit("copa", "r01", ("", bad, None)),
                       lambda: w.races.update("copa", "r01", "", UP, "25:00")):
            with self.assertRaisesRegex(ValueError, "gun time|Gun time"):
                action()
        self.assertEqual(w.repo.edits, 0)
        self.assertEqual(w.analysis.requests, [])
        w.races.update("copa", "r02", "", [{"type": "upwind", "mark": " 1 "}, {"type": "d", "mark": "F"}], " 12:00 ")
        self.assertEqual((w.stored("r02")["course"], w.stored("r02")["gun"]), (UP, "12:00"))   # normalized

    def test_upload_and_staging(self):
        w = self.w
        w.races.stage("copa", "r01")
        w.races.upload("copa", "r01", "206341-GARCIA.kml", b"<kml/>")
        w.races.unstage("copa", "r01")
        self.assertEqual(w.folders.log, [("stage", "r01"), ("put", "r01", "206341-GARCIA.kml"), ("unstage", "r01")])
        for action in (lambda: w.races.stage("copa", "r09"), lambda: w.races.unstage("nope", "r01"),
                       lambda: w.races.upload("copa", "r09", "a.kml", b""),
                       lambda: w.races.upload("nope", "r01", "a.kml", b"")):
            with self.assertRaises(NotFound):
                action()

    def test_commit(self):
        w = self.w
        with self.assertRaisesRegex(ValueError, "No uploaded"):
            w.races.commit("copa", "r02")
        w.folders.log.clear()
        w.analysis.fail = AnalysisError("bad tracks")
        with self.assertRaisesRegex(AnalysisError, "bad tracks"):
            w.races.commit("copa", "r01")
        self.assertEqual(w.folders.log, [])                         # the current tracks stay
        self.assertEqual(w.stored("r01"), race("r01"))
        w.analysis.fail = None
        w.races.commit("copa", "r01", ("", REACH, "12:00"))
        self.assertEqual(w.analysis.requests[-1].tracks_dir, "up/copa/r01.new")   # analysed before the swap
        r = w.stored("r01")
        self.assertEqual((r["dir"], r["name"], r["course"], r["gun"]), ("up/copa/r01", "R01", REACH, "12:00"))
        self.assertTrue(r["key"].startswith("copa/r01/new"))
        self.assertEqual(w.cache.metas[("copa", "r01")]["cfg"], "cfg-up/copa/r01.new")
        self.assertEqual(w.folders.log, [("swap", "r01"), ("done", "r01")])

    def test_commit_undoes_the_swap_when_saving_fails(self):
        w = self.w
        w.folders.log.clear()

        def broken(fn):
            raise OSError("disk full")
        w.repo.edit = broken
        with self.assertRaisesRegex(OSError, "disk full"):
            w.races.commit("copa", "r01")
        self.assertEqual(w.folders.log, [("swap", "r01"), ("undo", "r01")])
        self.assertEqual(w.stored("r01"), race("r01"))

    def test_delete(self):
        w = self.w
        w.analyses.cached("copa", w.stored("r01"))
        w.races.delete("copa", "r01")
        self.assertEqual([r["id"] for r in w.repo.regatta("copa")["races"]], ["r02"])
        self.assertEqual(w.repo.regatta("copa")["seq"], 1)          # r01 stays retired
        self.assertIn(("delete_race", "r01"), w.folders.log)
        self.assertFalse(w.cache.exists("copa", "r01"))
        with self.assertRaises(NotFound):
            w.races.delete("copa", "r01")


class Results:
    def __init__(self, *pages):
        self.pages, self.files = list(pages), {}

    def fetch(self, url):
        return self.pages.pop(0)

    def save(self, rid, data):
        self.files[rid] = data

    def load(self, rid):
        return self.files.get(rid)

    def remove(self, rid):
        self.files.pop(rid, None)


class Clock:
    def now_iso(self):
        return "2025-04-13T18:00:00+00:00"


class OfficialResultsTest(unittest.TestCase):
    def setUp(self):
        self.repo = Repo({"id": "copa", "name": "Copa", "races": [race("r01"), race("r02"), race("r03")]})
        page1 = {"races": ["R1", "R2", "R3", "R4"], "rows": [{"sail": "206341"}, {"sail": "1"}]}
        page2 = {"races": ["R0", "R2", "R1"], "rows": [{"sail": "206341"}]}
        self.store = Results(page1, page2)
        self.results = OfficialResults(self.repo, self.store, self.store, Clock())

    def test_download_and_map(self):
        rs, repo = self.results, self.repo
        with self.assertRaises(NotFound):
            rs.get("nope")
        self.assertIsNone(rs.get("copa"))
        self.assertEqual(rs.download("copa", "https://results"), (2, ["R1", "R2", "R3", "R4"]))
        got = rs.get("copa")
        self.assertEqual(got["map"], ["r01", "r02", "r03", None])   # by position
        self.assertEqual((got["n"], got["fetched"], got["rows"]), (2, Clock().now_iso(), self.store.files["copa"]["rows"]))
        rs.set_map("copa", ["r02", "r01", "r09", None])            # unknown races map to nothing
        self.assertEqual(repo.regatta("copa")["results"]["map"], ["r02", "r01", None, None])
        # fetched again: known columns keep their race, a new one takes its position's race if free
        self.assertEqual(rs.download("copa", "https://results"), (1, ["R0", "R2", "R1"]))
        self.assertEqual(repo.regatta("copa")["results"]["map"], [None, "r01", "r02"])
        with self.assertRaisesRegex(ValueError, "Bad results mapping"):
            rs.set_map("copa", ["r01"])
        rs.remove("copa")
        self.assertIsNone(rs.get("copa"))
        self.assertNotIn("copa", self.store.files)
        with self.assertRaisesRegex(ValueError, "No official results"):
            rs.set_map("copa", [None, None, None])

    def test_download_for_a_regatta_deleted_meanwhile(self):
        with self.assertRaisesRegex(ValueError, "unknown regatta"):
            self.results.download("nope", "https://results")
        self.assertEqual(self.store.files, {})


if __name__ == "__main__":
    unittest.main()
