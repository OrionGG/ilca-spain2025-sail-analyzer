"""Shared test helpers. The app's modules are imported through the package
(`from sail_analyzer import app`), the way the app itself imports them."""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "sail_analyzer")
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden")

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return fh.read()
