"""Rules for the regatta / race registry: ids, lookups and edits of its plain data (storing it
is the RegattaRepository's job)."""

from __future__ import annotations

import re

from .errors import NotFound


def slug(name):
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s[:40] or "regatta"


def race_num(race_id):
    m = re.fullmatch(r"r(\d+)", race_id or "")
    return int(m.group(1)) if m else 0


def find_regatta(reg, rid):
    return next((g for g in reg["regattas"] if g["id"] == rid), None)


def find_race(rg, race_id):
    return next((r for r in rg["races"] if r["id"] == race_id), None)


def regatta_of(reg, rid):
    """The regatta (NotFound if there is none)."""
    rg = find_regatta(reg, rid)
    if rg is None:
        raise NotFound("unknown regatta")
    return rg


def race_of(reg, rid, race_id):
    """The race (NotFound if its regatta or the race is unknown)."""
    race = find_race(regatta_of(reg, rid), race_id)
    if race is None:
        raise NotFound("unknown race")
    return race


def put_race(reg, rid, race_id, new):
    """Replace one race's entry (new dict) or remove it (None), in a registry being edited."""
    rg = regatta_of(reg, rid)
    if new is None:
        rg["races"] = [r for r in rg["races"] if r["id"] != race_id]
        rg["seq"] = max(rg.get("seq", 0), race_num(race_id))   # keep the id retired
    else:
        rg["races"] = [new if r["id"] == race_id else r for r in rg["races"]]
