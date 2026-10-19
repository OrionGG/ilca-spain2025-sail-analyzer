"""Gate buoys: one or two, from the roundings' cross-wind positions."""

import unittest

import numpy as np

from tests import support  # noqa: F401
import regatta as R


def buoys(cw):
    return len(R._split_gate([(38.59 + c * 1e-6, -0.03, float(c)) for c in cw]))


class GateSplit(unittest.TestCase):
    def setUp(self):
        self.rng = np.random.default_rng(3)

    def normal(self, mu, sd, n):
        return list(self.rng.normal(mu, sd, n))

    def test_two_buoys_40_m_apart(self):
        self.assertEqual(buoys(self.normal(0, 6, 25) + self.normal(40, 6, 22)), 2)

    def test_two_buoys_uneven_groups(self):
        self.assertEqual(buoys(self.normal(0, 5, 10) + self.normal(42, 5, 28)), 2)

    def test_two_buoys_with_strays(self):
        self.assertEqual(buoys(self.normal(0, 6, 25) + self.normal(40, 6, 22) + [250, 260]), 2)

    def test_wide_gate(self):
        self.assertEqual(buoys(self.normal(0, 8, 20) + self.normal(100, 8, 20)), 2)

    def test_one_buoy(self):
        self.assertEqual(buoys(self.normal(0, 5, 30)), 1)

    def test_strays_do_not_make_a_second_buoy(self):
        self.assertEqual(buoys(self.normal(0, 5, 18) + [200, 210]), 1)
        self.assertEqual(buoys(self.normal(0, 5, 17) + [500, 505, 510]), 1)
        self.assertEqual(buoys(self.normal(0, 5, 20) + [500, 505, 510, 515]), 1)
        self.assertEqual(buoys([0, 2, 4, 6, 8, 300, 320]), 1)

    def test_small_fleets(self):
        self.assertEqual(buoys([0, 5, 10, 15, 20, 25]), 1)
        self.assertEqual(buoys([0, 35, 70, 71, 110, 150]), 1)     # never trimmed away
        self.assertEqual(buoys([3, 4]), 0)                        # too few roundings for a mark

    def test_empty(self):
        self.assertEqual(R._split_gate([]), [])


if __name__ == "__main__":
    unittest.main()
