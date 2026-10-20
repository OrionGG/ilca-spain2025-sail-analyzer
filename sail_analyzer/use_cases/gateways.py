"""Gateway interfaces: what the use cases need from the outside world, implemented by the
interface adapters (and, until step 4 of #6, by app.py)."""

from __future__ import annotations

from typing import Protocol

from sail_analyzer.entities.track import Boat


class TrackGateway(Protocol):
    def boats(self, tracks_dir: str) -> list[Boat]:
        """The usable boat tracks of a race's tracks folder, each with a unique sail id,
        in the folder's order (empty when there are none)."""
        ...
