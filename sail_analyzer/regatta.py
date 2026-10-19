"""
regatta.py — compatibility module during the Clean Architecture refactor (#6).

The sailing rules live in sail_analyzer/entities/; this module re-exports their old names
and keeps the KML track reader (an I/O gateway, moved out in step 4).

Input: TracTrac native KML exports (position + UTC timestamp at ~3s).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import numpy as np

from sail_analyzer.entities.constants import KN, ILCA_LOA  # noqa: F401
from sail_analyzer.entities.kinematics import to_xy, _circ_smooth, _movavg, wrap180, _med_xy, derive_kinematics  # noqa: F401
from sail_analyzer.entities.track import Boat  # noqa: F401
from sail_analyzer.entities.wind import estimate_wind_dir, _circ_mean, _nogo_center, wind_timeseries, apply_wind  # noqa: F401
from sail_analyzer.entities.fleet import dirty_air, fleet_progress  # noqa: F401
from sail_analyzer.entities.maneuvers import detect_penalty_turns, Maneuver, detect_maneuvers  # noqa: F401
from sail_analyzer.entities.legs import _MODE, _KIND, _mode_array, segment_legs, _block_splits, course_index, _segment_legs_vmg  # noqa: F401
from sail_analyzer.entities.start import detect_gun, infer_start  # noqa: F401
from sail_analyzer.entities.finish import finish_by_convergence, detect_finish  # noqa: F401
from sail_analyzer.entities.marks import _refine_turn, _split_gate, detect_marks  # noqa: F401


KML_NS = "{http://www.opengis.net/kml/2.2}"


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _parse_ts(s: str) -> float:
    # "2025-09-19T10:45:01Z" -> epoch seconds (UTC)
    dt = datetime.strptime(s.strip(), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return dt.timestamp()


def parse_kml(path: str):
    """Return (t[N] epoch s, lat[N], lon[N]) sorted by time, deduped."""
    t, lat, lon = [], [], []
    # Stream-parse: files are flat lists of <Placemark><TimeStamp><when>..</when>
    # <Point><coordinates>lon,lat,alt</coordinates>.
    for _, elem in ET.iterparse(path, events=("end",)):
        if elem.tag == KML_NS + "Placemark":
            when = elem.find(f"{KML_NS}TimeStamp/{KML_NS}when")
            coord = elem.find(f"{KML_NS}Point/{KML_NS}coordinates")
            if when is not None and coord is not None and when.text and coord.text:
                try:
                    parts = coord.text.strip().split(",")
                    lo, la = float(parts[0]), float(parts[1])
                    t.append(_parse_ts(when.text))
                    lat.append(la)
                    lon.append(lo)
                except (ValueError, IndexError):
                    pass
            elem.clear()
    if not t:
        return np.array([]), np.array([]), np.array([])
    t = np.array(t)
    order = np.argsort(t)
    t, lat, lon = t[order], np.array(lat)[order], np.array(lon)[order]
    # dedupe identical timestamps
    keep = np.concatenate(([True], np.diff(t) > 0))
    return t[keep], lat[keep], lon[keep]
