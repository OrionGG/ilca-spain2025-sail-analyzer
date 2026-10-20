"""A boat's track: positions + times, and what is derived from them."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


# --------------------------------------------------------------------------- #
# Per-boat derived track
# --------------------------------------------------------------------------- #
@dataclass
class Boat:
    sail: str
    name: str
    file: str
    t: np.ndarray
    lat: np.ndarray
    lon: np.ndarray
    x: np.ndarray = field(default=None)
    y: np.ndarray = field(default=None)
    sog: np.ndarray = field(default=None)   # knots
    cog: np.ndarray = field(default=None)   # deg
    vmg: np.ndarray = field(default=None)   # knots, + = to windward
    twa: np.ndarray = field(default=None)   # deg, 0..180
    tack: np.ndarray = field(default=None)  # +1 stbd / -1 port (rel to wind)
