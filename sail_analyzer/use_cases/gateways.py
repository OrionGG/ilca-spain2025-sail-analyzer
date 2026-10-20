"""Gateway interfaces: what the use cases need from the outside world, implemented by the
interface adapters (and, until step 4 of #6, by app.py). Plus the output boundary through
which a race analysis is shown."""

from __future__ import annotations

from typing import Callable, ContextManager, Protocol

from sail_analyzer.entities.track import Boat


class TrackGateway(Protocol):
    def boats(self, tracks_dir: str) -> list[Boat]:
        """The usable boat tracks of a race's tracks folder, in the folder's order (empty when
        there are none). Each call returns FRESH Boat entities (positions and times only): the
        analysis derives kinematics and wind into them. Sail ids are unique within the race."""
        ...

    def count(self, tracks_dir: str) -> int:
        """How many track files the folder holds (0 when it doesn't exist)."""
        ...


class RacePresenter(Protocol):
    """Output boundary: a race analysis as it is shown and cached (the API's view of it)."""

    def present(self, analysis) -> dict:
        ...


class RegattaRepository(Protocol):
    """The regatta / race registry: {"regattas": [{"id", "name", "races": [race config], ...}]}."""

    def load(self) -> dict:
        """A snapshot of the registry (changes go through edit)."""
        ...

    def edit(self, fn: Callable[[dict], object]):
        """Apply fn(registry) and save, atomically w.r.t. other edits; returns fn's result."""
        ...

    def view(self, fn: Callable[[dict], object]):
        """fn(registry) under the same lock as edit (to read the registry together with files
        that edits write alongside it); nothing is saved."""
        ...


class AnalysisCache(Protocol):
    """Cached race analyses (the presented race) with their meta: the config they were made
    for, the race's name / marks key, the regatta-index entry, or the error they failed with."""

    def state(self, rid: str, race: dict) -> tuple:
        """("fresh" | "patch" | "error" | "stale", meta, config hash) of a race's cache."""
        ...

    def config_hash(self, race: dict, tracks_dir: str) -> str:
        """What an analysis depends on: the race's course and gun and the tracks folder's files."""
        ...

    def read(self, rid: str, race_id: str) -> dict | None: ...

    def meta(self, rid: str, race_id: str) -> dict | None: ...

    def exists(self, rid: str, race_id: str) -> bool: ...

    def store(self, rid: str, race: dict, data: dict, cfg: str, entry: dict) -> None: ...

    def store_error(self, rid: str, race_id: str, cfg: str, error: str) -> None: ...

    def drop(self, rid: str, race_id: str) -> None: ...

    def regatta_exists(self, rid: str) -> bool: ...

    def drop_regatta(self, rid: str) -> None: ...


class TrackFolders(Protocol):
    """Where races' tracks live: the app's copies of uploaded tracks (per race, with a staging
    folder for a new set) or a folder given by the registry."""

    def race_dir(self, race: dict) -> str: ...

    def is_upload(self, race: dict) -> bool: ...

    def new_race_dir(self, rid: str, race_id: str) -> str:
        """Create an (empty) upload folder for a new race; returns the registry's "dir" for it."""
        ...

    def staging_dir(self, rid: str, race_id: str) -> str: ...

    def stage(self, rid: str, race_id: str) -> None:
        """An empty staging folder for a new set of tracks (ValueError if it can't be cleared)."""
        ...

    def unstage(self, rid: str, race_id: str) -> None: ...

    def put_staged(self, rid: str, race_id: str, name: str, raw: bytes) -> None: ...

    def swap_in(self, rid: str, race_id: str) -> tuple:
        """Make the staged set the race's tracks, as a whole-folder swap. Returns (the
        registry's new "dir", undo(), done()): undo() puts both sets back, done() discards
        the old set."""
        ...

    def delete_race(self, rid: str, race: dict) -> None: ...

    def regatta_exists(self, rid: str) -> bool: ...

    def delete_regatta(self, rid: str) -> None: ...


class ResultsSource(Protocol):
    def fetch(self, url: str) -> dict:
        """The official results at a results-page link: {"races": [column labels], "rows": [...]}."""
        ...


class ResultsStore(Protocol):
    """A regatta's downloaded official results (rows and scores; the registry keeps the
    columns' mapping to races)."""

    def save(self, rid: str, data: dict) -> None: ...

    def load(self, rid: str) -> dict | None: ...

    def remove(self, rid: str) -> None: ...


class RegattaLocks(Protocol):
    def regatta(self, rid: str) -> ContextManager:
        """One regatta's analyses and track swaps at a time."""
        ...


class Clock(Protocol):
    def now_iso(self) -> str:
        """The current UTC time, ISO 8601 to the second."""
        ...


class MarksKeys(Protocol):
    def new(self, rid: str, race_id: str) -> str:
        """A fresh key for a race's hand-placed marks (unique per race and course)."""
        ...


Log = Callable[[str], None]                       # progress / problem lines for the console
