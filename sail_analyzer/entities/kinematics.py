"""Local projection, smoothing, medians and per-boat speed / course from position + time."""

from __future__ import annotations

import math

import numpy as np

from .constants import KN
from .track import Boat


# --------------------------------------------------------------------------- #
# Geometry helpers (local ENU tangent plane, good for a few-km course)
# --------------------------------------------------------------------------- #
def to_xy(lat, lon, lat0, lon0):
    """Equirectangular projection to local meters: x=East, y=North."""
    mlat = 111320.0
    mlon = 111320.0 * math.cos(math.radians(lat0))
    x = (lon - lon0) * mlon
    y = (lat - lat0) * mlat
    return x, y, mlat, mlon


def _circ_smooth(deg, win):
    """Smooth an angle series (degrees) with a centered moving average."""
    if win <= 1 or len(deg) < 3:
        return deg
    r = np.radians(deg)
    cs = _movavg(np.cos(r), win)
    sn = _movavg(np.sin(r), win)
    return (np.degrees(np.arctan2(sn, cs))) % 360.0


def _movavg(a, win):
    if win <= 1 or len(a) < 3:
        return a
    win = int(win) | 1                       # force odd
    pad = win // 2
    ap = np.pad(a, pad, mode="edge")
    k = np.ones(win) / win
    return np.convolve(ap, k, mode="valid")


def wrap180(deg):
    """Wrap to (-180, 180]."""
    return (np.asarray(deg) + 180.0) % 360.0 - 180.0


def _med_xy(pts, need=3):
    if len(pts) < need or not pts:
        return None
    P = np.array(pts, float)
    return [float(np.median(P[:, 0])), float(np.median(P[:, 1]))]


def derive_kinematics(b: Boat, lat0, lon0, smooth_win=3):
    x, y, _, _ = to_xy(b.lat, b.lon, lat0, lon0)
    b.x, b.y = x, y
    t = b.t
    n = len(t)
    if n < 2:
        b.sog = np.zeros(n); b.cog = np.zeros(n)
        return b
    # centered differences for velocity
    vx = np.gradient(x, t)        # m/s East
    vy = np.gradient(y, t)        # m/s North
    sog = np.hypot(vx, vy) * KN
    cog = (np.degrees(np.arctan2(vx, vy))) % 360.0     # 0=N,90=E
    b.sog = _movavg(sog, smooth_win)
    b.cog = _circ_smooth(cog, smooth_win)
    return b
