"""RaceAnalyses: a regatta's race analyses — made, cached and indexed.

Analyses are cached as the presenter shows them (the race view the API serves), each with a
meta: the config it was made for, the race's name and marks key, and the regatta-index entry.
A cache that no longer matches its race's config (or the code) is rebuilt; a rename or a new
marks key only patches it.
"""

from __future__ import annotations

import statistics

from .analyse_race import AnalyseRace, RaceRequest
from .errors import AnalysisError, NotFound, TransientError
from .gateways import AnalysisCache, Log, RacePresenter, RegattaLocks, RegattaRepository, TrackFolders, TrackGateway
from .registry import find_race, find_regatta, put_race


class RaceAnalyses:
    def __init__(self, tracks: TrackGateway, presenter: RacePresenter, repo: RegattaRepository,
                 cache: AnalysisCache, folders: TrackFolders, locks: RegattaLocks, log: Log = lambda line: None):
        self.analyse_race = AnalyseRace(tracks)
        self.tracks, self.presenter, self.repo, self.cache = tracks, presenter, repo, cache
        self.folders, self.locks, self.log = folders, locks, log

    # -- one analysis ------------------------------------------------------------
    def analyse(self, race, tracks_dir):
        """Run the analysis for a race config on a folder of tracks; AnalysisError if it fails."""
        if not self.tracks.count(tracks_dir):
            raise AnalysisError("No .kml files in this race's folder — choose its tracks folder.")
        self.log(f"  parsing {race['name']} ...")
        try:
            a = self.analyse_race.execute(RaceRequest(tracks_dir=tracks_dir, name=race["name"], course=race["course"],
                                                      gun=race.get("gun"), key=race.get("key")))
            data = self.presenter.present(a) if a is not None else None
        except AnalysisError:
            raise
        except (OSError, MemoryError) as e:
            raise TransientError(f"Could not read the tracks right now ({type(e).__name__}: {e}) — try again.")
        except Exception as e:
            raise AnalysisError(f"Analysis failed: {type(e).__name__}: {e}")
        if data is None:
            raise AnalysisError("No usable tracks: expected TracTrac KML files (timestamped points, 30+ fixes per boat).")
        return data

    def analysed(self, race, tracks_dir):
        """(data, cfg): analysis of a race config on a folder of tracks + the hash of those tracks."""
        cfg = self.cache.config_hash(race, tracks_dir)
        return self.analyse(race, tracks_dir), cfg

    def save(self, rid, race, data, cfg):
        """Cache first, registry last: if either write fails the registry still has the old
        config, and a cache made for a config the registry doesn't have is just stale."""
        self._store(rid, race, data, cfg)
        self.repo.edit(lambda reg: put_race(reg, rid, race["id"], race))

    def cached(self, rid, race, force=False):
        """(Re)build one race's cache when missing/stale. Returns an error string or
        None. A failed build removes the old cache so stale results are never served."""
        state, meta, h = self.cache.state(rid, race)
        if not force:
            if state == "fresh":
                return None
            if state == "error":
                return meta["error"]
            if state == "patch":                      # rename / new marks key: no re-parse
                data = self.cache.read(rid, race["id"])
                if data is not None:
                    data["race"], data["key"] = race["name"], race.get("key") or race["name"]
                    self._store(rid, race, data, h)
                    return None
        try:
            data = self.analyse(race, self.folders.race_dir(race))
        except AnalysisError as e:
            self.cache.drop(rid, race["id"])
            if e.permanent:                           # same inputs will fail again: remember it
                self.cache.store_error(rid, race["id"], h, str(e))
            return str(e)
        self._store(rid, race, data, h)
        return None

    def _store(self, rid, race, data, cfg):
        self.cache.store(rid, race, data, cfg, index_entry(0, race["id"], data))

    # -- a regatta -----------------------------------------------------------------
    def index(self, rid, rebuild=False):
        """Build missing/stale races of one regatta and return its index (NotFound: unknown regatta)."""
        rg = find_regatta(self.repo.load(), rid)
        if rg is None:
            raise NotFound("unknown regatta")
        todo = {r["id"] for r in rg["races"]
                if rebuild or self.cache.state(rid, r)[0] not in ("fresh", "error")}
        if not todo:
            return self._index(rid, rg)               # nothing to build: don't wait for the lock
        with self.locks.regatta(rid):
            rg = find_regatta(self.repo.load(), rid)
            if rg is None:                            # deleted meanwhile
                raise NotFound("unknown regatta")
            for race in rg["races"]:
                if race["id"] not in todo:
                    continue
                try:
                    err = self.cached(rid, race, force=rebuild)
                except Exception as e:                # one race's failure must not sink the regatta
                    err = f"{type(e).__name__}: {e}"
                if err:
                    self.log(f"  ! {race['name']}: {err}")
            return self._index(rid, rg)

    def _index(self, rid, rg):
        """The regatta's race index, assembled from the per-race meta entries (no track data)."""
        index = []
        for race in rg["races"]:
            meta = self.cache.meta(rid, race["id"])
            if meta and meta.get("entry") and self.cache.exists(rid, race["id"]):
                index.append({**meta["entry"], "i": len(index)})
        return index

    def race(self, rid, race_id):
        """Make sure a race's analysis is cached. NotFound for an unknown race; returns the
        analysis error, or None when the cached analysis is ready."""
        rg = find_regatta(self.repo.load(), rid)
        race = find_race(rg, race_id) if rg else None
        if race is None:
            raise NotFound("unknown race")
        if self.cache.state(rid, race)[0] != "fresh":
            with self.locks.regatta(rid):             # re-read: the race may have changed meanwhile
                rg = find_regatta(self.repo.load(), rid)
                race = find_race(rg, race["id"]) if rg else None
                if race is None:
                    raise NotFound("unknown race")
                err = self.cached(rid, race)
            if err:
                return err
        return None


def index_entry(i, race_id, data):
    """Regatta trends: per-boat mini-summary + finisher medians + finish rank."""
    fin = [b for b in data["boats"] if b["summary"].get("finished")]
    def _med(k):
        vals = [b["summary"][k] for b in fin if b["summary"].get(k) is not None]
        return round(statistics.median(vals), 2) if vals else None
    keys = ("race_min", "sog_up", "sog_dn", "vmg_up", "vmg_dn", "twa_up",
            "tack_loss", "gybe_loss", "dist_nm", "n_tacks", "n_gybes")
    fleet = {k: _med(k) for k in keys}
    order = sorted([b for b in fin if b["summary"].get("race_min") is not None],
                   key=lambda b: b["summary"]["race_min"])
    rank = {b["sail"]: j + 1 for j, b in enumerate(order)}
    mini = {}
    for b in data["boats"]:
        s = b["summary"]
        mini[b["sail"]] = {**{k: s.get(k) for k in keys}, "name": b.get("name"),
                           "finished": s.get("finished"),
                           "rank": rank.get(b["sail"]), "n_fin": len(order)}
    return {"i": i, "id": race_id, "race": data["race"], "start_iso": data["start_iso"],
            "wind_from": data["wind_from"], "n_boats": len(data["boats"]),
            "fleet": fleet, "boats": mini}
