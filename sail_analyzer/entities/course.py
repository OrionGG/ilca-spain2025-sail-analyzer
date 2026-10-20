"""Courses: templates, leg specs and the course invariants."""

from __future__ import annotations

import re


# Course templates offered in the UI. Legs: "<type>:<mark>[*]" — type U(pwind) /
# R(each) / D(ownwind), the mark the leg ends at, * = that mark is a gate.
# The last leg always ends at the finish line.
TEMPLATES = [
    {"id": "trap_outer", "name": "Trapezoid outer — reach finish", "legs": "U:1 R:2 D:G* U:2 D:G* R:F"},
    {"id": "trap_outer_wing", "name": "Trapezoid outer, wing mark moved (2a/2b)", "legs": "U:1 R:2a D:G* U:2b D:G* R:F"},
    {"id": "trap_inner", "name": "Trapezoid inner — reach finish", "legs": "U:1 D:4* U:1 R:2 D:3 R:F"},
    {"id": "wl2_dn", "name": "Windward-leeward, 2 laps, downwind finish", "legs": "U:1 D:G* U:1 D:F"},
    {"id": "wl2_up", "name": "Windward-leeward, 2 laps, upwind finish", "legs": "U:1 D:G* U:1 D:G* U:F"},
    {"id": "wl3_dn", "name": "Windward-leeward, 3 laps, downwind finish", "legs": "U:1 D:G* U:1 D:G* U:1 D:F"},
]


def parse_legs(spec: str):
    out = []
    for tok in spec.split():
        typ, mark = tok.split(":")
        out.append({"type": typ, "mark": mark.rstrip("*"), "gate": mark.endswith("*")})
    return out


def template_course(tid):
    return parse_legs(next(t["legs"] for t in TEMPLATES if t["id"] == tid))


def normalize_course(course):
    """Validate a course from the UI: >= 2 legs, types U/R/D, mark labels 1-6
    chars (RC/Pin reserved); the finish label must not be a rounding mark."""
    if not isinstance(course, list) or len(course) < 2:
        raise ValueError("A course needs at least 2 legs.")
    out = []
    for i, leg in enumerate(course):
        if not isinstance(leg, dict):
            raise ValueError(f"Leg {i + 1}: bad format.")
        typ = str(leg.get("type", "")).upper()[:1]
        mark = str(leg.get("mark", "")).strip()[:6]
        if mark and not re.fullmatch(r"[A-Za-z0-9._-]+", mark):
            raise ValueError(f"Leg {i + 1}: mark labels use letters, digits, '.', '_' or '-'.")
        if typ not in ("U", "R", "D"):
            raise ValueError(f"Leg {i + 1}: type must be upwind, reach or downwind.")
        if not mark:
            raise ValueError(f"Leg {i + 1}: give the mark it ends at.")
        if mark in ("RC", "Pin"):
            raise ValueError(f"Leg {i + 1}: '{mark}' is reserved for the start line.")
        out.append({"type": typ, "mark": mark, "gate": bool(leg.get("gate")) and i < len(course) - 1})
    if out[-1]["mark"] in [leg["mark"] for leg in out[:-1]]:
        raise ValueError("The last leg ends at the finish: give it its own label (e.g. F).")
    return out
