"""Course, gun and name validation of regatta/race input."""

import unittest

from tests import support  # noqa: F401
from sail_analyzer import app as A


def leg(t, m, gate=False):
    return {"type": t, "mark": m, "gate": gate}


class Course(unittest.TestCase):
    def test_templates_parse(self):
        self.assertEqual(A.template_course("trap_outer"),
                         [leg("U", "1"), leg("R", "2"), leg("D", "G", True), leg("U", "2"),
                          leg("D", "G", True), leg("R", "F")])
        for t in A.TEMPLATES:
            self.assertEqual(A.normalize_course(A.parse_legs(t["legs"])), A.parse_legs(t["legs"]), t["id"])

    def test_normalizes(self):
        self.assertEqual(A.normalize_course([{"type": "upwind", "mark": " 1 "}, {"type": "d", "mark": "F", "gate": True}]),
                         [leg("U", "1"), leg("D", "F")])          # the finish can't be a gate

    def test_rejects(self):
        bad = {
            "at least 2 legs": [leg("U", "1")],
            "bad format": [leg("U", "1"), "x"],
            "type must be": [leg("X", "1"), leg("D", "F")],
            "give the mark": [leg("U", ""), leg("D", "F")],
            "mark labels use": [leg("U", "a b"), leg("D", "F")],
            "reserved": [leg("U", "RC"), leg("D", "F")],
            "own label": [leg("U", "1"), leg("D", "G", True), leg("U", "1")],
        }
        for msg, course in bad.items():
            with self.assertRaisesRegex(ValueError, msg):
                A.normalize_course(course)
        with self.assertRaises(ValueError):
            A.normalize_course("U:1 D:F")

    def test_label_is_cut_to_6_chars(self):
        self.assertEqual(A.normalize_course([leg("U", "1234567"), leg("D", "F")])[0]["mark"], "123456")


class Gun(unittest.TestCase):
    def test_formats(self):
        self.assertIsNone(A.normalize_gun(None))
        self.assertIsNone(A.normalize_gun("  "))
        self.assertEqual(A.normalize_gun("12:05"), "12:05")
        self.assertEqual(A.normalize_gun("9:05:30"), "9:05:30")
        for bad in ("12", "12:5", "24:00", "12:60", "12:00:61", "noon", 1205):
            with self.assertRaises(ValueError, msg=bad):
                A.normalize_gun(bad)

    def test_course_not_starting_upwind_needs_a_gun(self):
        body = {"course": [leg("D", "1"), leg("U", "F")]}
        with self.assertRaisesRegex(ValueError, "gun time"):
            A.course_and_gun(body)
        self.assertEqual(A.course_and_gun({**body, "gun": "12:00"})[1], "12:00")


class Names(unittest.TestCase):
    def test_clean_text(self):        # markup and control characters dropped, ends trimmed
        self.assertEqual(A.clean_text('  <b>Copa</b>\n de "España"  '), "bCopa/b de España")
        self.assertEqual(A.clean_text("x" * 100, 10), "x" * 10)
        self.assertEqual(A.clean_text(None), "")

    def test_slug_and_ids(self):
        self.assertEqual(A._slug("Copa de España ILCA 7 2025"), "copa-de-espa-a-ilca-7-2025")
        self.assertEqual(A._slug("!!!"), "regatta")
        self.assertTrue(A._safe_id("copa2025"))
        self.assertFalse(A._safe_id("../etc"))
        self.assertEqual(A._race_num("r07"), 7)
        self.assertEqual(A._race_num("x"), 0)


if __name__ == "__main__":
    unittest.main()
