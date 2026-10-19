"""Golden snapshots of the analysis of the 9 Copa 2025 races (the KML tracks in the repo).

Each race's analysis must reproduce the reference bit for bit: every section of the output
(metadata, marks, start line, wind series, fleet stats, each boat, the regatta-index entry)
is pinned by a hash, both from the analysis itself and through the path the API serves
(fresh registry -> ensure_cache: analyse, cache, meta entry -> regatta index). A readable summary (marks, legs, course indices, roundings, summary
metrics) is compared too, with a small float tolerance, so a failure shows what moved.

Runs with the rest of the suite (~10 s); SAIL_GOLDEN=0 skips it. Only this test:
    python -m unittest discover -s tests -p test_golden_analysis.py
Regenerate the references after an intended change (say why in the PR):
    python tests/test_golden_analysis.py --update
"""

import glob
import hashlib
import json
import math
import os
import shutil
import sys
import tempfile
import unittest

try:
    from tests import support
except ImportError:          # run as a script: python tests/test_golden_analysis.py --update
    import support
from sail_analyzer import app as A

DIR = os.path.join(support.GOLDEN, "analysis")


def races():
    """The repo's own races, registered exactly as on a first run."""
    return A.seed_registry()["regattas"][0]["races"]


def build(race):
    return A.build_race(A.race_dir_abs(race), race["name"], race["course"], race.get("gun"), race.get("key"))


def served():
    """The path the app serves: a fresh registry (seeded from the repo's race folders) and
    ensure_cache (analyse, cache + meta entry, index) on temporary folders. Returns
    (index, {race id: cached race})."""
    tmp = tempfile.mkdtemp(prefix="sa_golden_")
    saved = {k: getattr(A, k) for k in ("REG_PATH", "CACHE_DIR", "UPLOAD_DIR")}
    try:
        A.REG_PATH = os.path.join(tmp, "regattas.json")
        A.CACHE_DIR = os.path.join(tmp, "cache")
        A.UPLOAD_DIR = os.path.join(tmp, "data")
        index = A.ensure_cache("copa2025")
        return index, {r["id"]: A._read_json(A.race_cache_path("copa2025", r["id"])) for r in races()}
    finally:
        for k, v in saved.items():
            setattr(A, k, v)
        shutil.rmtree(tmp, ignore_errors=True)


def index_snapshot(index):
    return {"races": [[e["i"], e["id"], e["race"], e["n_boats"]] for e in index], "sha": sha(index)}


def sha(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:20]


def snapshot(i, race, d):
    meta = {k: d[k] for k in ("race", "key", "course", "gun_manual", "start_t", "gun_t", "start_iso",
                              "wind_from", "lat0", "lon0")}
    boats = {b["sail"]: {"name": b["name"], "legs": b["legs"], "course_ix": b["course_ix"], "rounds": b["rounds"],
                         "summary": b["summary"], "n_maneuvers": len(b["maneuvers"]),
                         "n_penalties": len(b["penalties"])} for b in d["boats"]}
    sections = {"meta": sha(meta), "marks": sha(d["marks"]), "start_line": sha(d["start_line"]),
                "wind_series": sha(d["wind_series"]), "fleet_stats": sha(d["fleet_stats"]),
                "index_entry": sha(A.index_entry(i, race["id"], d))}
    sections.update({"boat " + b["sail"]: sha(b) for b in d["boats"]})
    return {"summary": {"meta": meta, "marks": d["marks"], "start_line": d["start_line"], "boats": boats,
                        "order": [b["sail"] for b in d["boats"]]},
            "sections": sections}


def diffs(a, b, path="", out=None, tol=1e-6):
    """Differences between two JSON values; floats equal within a relative tolerance."""
    out = [] if out is None else out
    if len(out) >= 20:
        return out
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                out.append(f"{path}/{k}: {'missing' if k not in b else 'unexpected'}")
            else:
                diffs(a[k], b[k], f"{path}/{k}", out, tol)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append(f"{path}: length {len(a)} -> {len(b)}")
        for k, (x, y) in enumerate(zip(a, b)):
            diffs(x, y, f"{path}[{k}]", out, tol)
    elif isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) \
            and not isinstance(b, bool):
        if not math.isclose(a, b, rel_tol=tol, abs_tol=tol):
            out.append(f"{path}: {a} -> {b}")
    elif a != b:
        out.append(f"{path}: {a!r} -> {b!r}")
    return out


def golden_path(race):
    return os.path.join(DIR, f"{race['id']}.json")


@unittest.skipIf(os.environ.get("SAIL_GOLDEN") == "0", "SAIL_GOLDEN=0")
class GoldenAnalysis(unittest.TestCase):
    def test_same_races(self):
        """Every reference is checked: the races found are exactly those with a reference."""
        found = {r["id"]: r["name"] for r in races()}
        refs = {}
        for path in sorted(glob.glob(os.path.join(DIR, "r*.json"))):
            with open(path, encoding="utf-8") as fh:
                refs[os.path.basename(path)[:-5]] = json.load(fh)["summary"]["meta"]["race"]
        self.assertEqual(found, refs)

    def test_copa_races(self):
        for i, race in enumerate(races()):
            with self.subTest(race=race["name"]):
                with open(golden_path(race), encoding="utf-8") as fh:
                    ref = json.load(fh)
                got = snapshot(i, race, build(race))
                d = diffs(ref["summary"], got["summary"])
                self.assertFalse(d, "analysis summary changed:\n  " + "\n  ".join(d))
                changed = sorted(k for k in set(ref["sections"]) | set(got["sections"])
                                 if ref["sections"].get(k) != got["sections"].get(k))
                self.assertFalse(changed, f"output sections changed (not bit-identical): {changed}")

    def test_served_path(self):
        """Through the cache and index the API serves, the races are the same, bit for bit."""
        index, cached = served()
        with open(os.path.join(DIR, "index.json"), encoding="utf-8") as fh:
            ref = json.load(fh)
        got = index_snapshot(index)
        self.assertEqual(got["races"], ref["races"])
        self.assertEqual(got["sha"], ref["sha"], "regatta index changed (not bit-identical)")
        for i, race in enumerate(races()):
            with self.subTest(race=race["name"]):
                with open(golden_path(race), encoding="utf-8") as fh:
                    want = json.load(fh)["sections"]
                self.assertIsNotNone(cached[race["id"]], "race not cached")
                self.assertEqual(snapshot(i, race, cached[race["id"]])["sections"], want)


def update():
    os.makedirs(DIR, exist_ok=True)
    snaps = {race["id"]: snapshot(i, race, build(race)) for i, race in enumerate(races())}
    index, _cached = served()
    files = {f"{rid}.json": snap for rid, snap in snaps.items()}
    files["index.json"] = index_snapshot(index)
    for path in glob.glob(os.path.join(DIR, "*.json")):          # no stale references left
        os.remove(path)
    for name, obj in files.items():
        with open(os.path.join(DIR, name), "w", encoding="utf-8", newline="\n") as fh:
            json.dump(obj, fh, indent=1, sort_keys=True)
            fh.write("\n")
        print("wrote", name)


if __name__ == "__main__":
    if "--update" in sys.argv:
        update()
    else:
        unittest.main()
