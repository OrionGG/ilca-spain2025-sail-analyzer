"""A race's config as a request gives it: its course and gun time, validated (ValueError with
the message shown to the user)."""

from __future__ import annotations

import re

from sail_analyzer.entities.course import normalize_course


def normalize_gun(gun):
    if gun is not None and not isinstance(gun, str):
        raise ValueError("Gun time must be HH:MM or HH:MM:SS (UTC).")
    gun = (gun or "").strip()
    if not gun:
        return None
    if not re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", gun):
        raise ValueError("Gun time must be HH:MM or HH:MM:SS (UTC).")
    hms = [int(x) for x in gun.split(":")] + [0]
    h, m, sec = hms[0], hms[1], hms[2]
    if h > 23 or m > 59 or sec > 59:
        raise ValueError("Gun time out of range.")
    return gun


def race_config(course, gun):
    """(course, gun) validated; a course that doesn't start upwind needs a gun time."""
    course, gun = normalize_course(course), normalize_gun(gun)
    if course[0]["type"] != "U" and not gun:
        raise ValueError("This course doesn't start upwind: enter the gun time "
                         "(auto-detection looks for the fleet starting to beat).")
    return course, gun
