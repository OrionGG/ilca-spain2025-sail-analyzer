"""Shared test helpers: import the app's modules the way the app does (its folder on sys.path)."""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "sail_analyzer")
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden")

if PKG not in sys.path:
    sys.path.insert(0, PKG)


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return fh.read()
