"""Use-case interactors, driven through their interfaces with fake gateways."""

import glob
import os
import unittest

from tests import support
from sail_analyzer import regatta as R
from sail_analyzer.entities.track import Boat
from sail_analyzer.use_cases.analyse_race import SUMMARY_DIGITS, AnalyseRace, BoatAnalysis, RaceAnalysis, RaceRequest
from sail_analyzer.use_cases.errors import AnalysisError

RACE1 = os.path.join(support.ROOT, "01_Prueba 1")
TRAP = [{"type": t, "mark": m, "gate": g} for t, m, g in
        (("U", "1", False), ("R", "2", False), ("D", "G", True), ("U", "2", False), ("D", "G", True), ("R", "F", False))]
TRACKS = {}


def race1_tracks(n=8):
    """(sail, name, t, lat, lon) of the first n boats of Copa race 1 (parsed once)."""
    if n not in TRACKS:
        files = sorted(glob.glob(os.path.join(glob.escape(RACE1), "*.kml")))[:n]
        TRACKS[n] = [(os.path.basename(f).split("-")[0], os.path.basename(f)[:-4], *R.parse_kml(f)) for f in files]
    return TRACKS[n]


class FakeTracks:
    """TrackGateway double: fresh Boat entities on every call (the interactor derives into them)."""

    def __init__(self, tracks):
        self.tracks, self.asked = tracks, []

    def boats(self, tracks_dir):
        self.asked.append(tracks_dir)
        return [Boat(sail=s, name=n, file=n, t=t.copy(), lat=la.copy(), lon=lo.copy()) for s, n, t, la, lo in self.tracks]


class AnalyseRaceTest(unittest.TestCase):
    def run_it(self, tracks, **req):
        gw = FakeTracks(tracks)
        out = AnalyseRace(gw).execute(RaceRequest(**{"tracks_dir": "race-folder", "name": "Race A",
                                                     "course": TRAP, **req}))
        return gw, out

    def test_no_usable_tracks_no_analysis(self):
        gw, out = self.run_it([])
        self.assertIsNone(out)
        self.assertEqual(gw.asked, ["race-folder"])

    def test_analysis_response_model(self):
        gw, a = self.run_it(race1_tracks())
        self.assertEqual(gw.asked, ["race-folder"])
        self.assertIsInstance(a, RaceAnalysis)
        self.assertEqual((a.name, a.key, a.course, a.gun_manual), ("Race A", "Race A", TRAP, False))
        self.assertEqual(a.start_t, a.gun_t)
        self.assertEqual([b.boat.sail for b in a.boats], sorted(s for s, *_ in race1_tracks()))
        self.assertTrue({"RC", "Pin", "1", "G"} <= {m["label"] for m in a.marks})
        self.assertEqual(len(a.wind_series), len(a.grid))
        self.assertEqual(set(a.fleet), {"sog_p50", "sog_p25", "sog_p75", "vmg_p50"})
        for ba in a.boats:
            self.assertIsInstance(ba, BoatAnalysis)
            self.assertEqual(len(ba.course_ix), len(ba.legs))
            self.assertEqual(len(ba.rounds), len(ba.legs))
            self.assertEqual(len(ba.course_grid), len(a.grid))
            self.assertTrue(set(SUMMARY_DIGITS) <= set(ba.summary))
        # unrounded: rounding is the presenter's job
        self.assertTrue(any(round(ba.summary["race_min"], 2) != ba.summary["race_min"] for ba in a.boats))

    def test_key_and_manual_gun(self):
        _gw, auto = self.run_it(race1_tracks())
        import datetime
        hhmm = datetime.datetime.fromtimestamp(auto.gun_t, datetime.timezone.utc).strftime("%H:%M")
        _gw, a = self.run_it(race1_tracks(), gun=hhmm, key="regatta/r01/abc")
        self.assertEqual((a.key, a.gun_manual, a.gun_t), ("regatta/r01/abc", True, auto.gun_t))

    def test_gun_outside_the_tracks(self):
        with self.assertRaisesRegex(AnalysisError, "outside the tracks"):
            self.run_it(race1_tracks(), gun="03:00")


if __name__ == "__main__":
    unittest.main()
