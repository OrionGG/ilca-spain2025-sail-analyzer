"""ManageRaces: add a race, change its name / course / gun, give it a new set of tracks
(staged, analysed, then swapped in), delete it.

A race's tracks or config change only once the new analysis succeeded; a new course or new
tracks get a new marks key (hand-placed marks made for the old ones don't apply). Course and gun
come as the request gives them and are validated once the race is found.
"""

from __future__ import annotations

from .errors import NotFound
from .gateways import AnalysisCache, MarksKeys, RegattaLocks, RegattaRepository, TrackFolders, TrackGateway
from .race_analyses import RaceAnalyses
from .race_config import race_config
from .registry import find_race, find_regatta, put_race, race_num


class ManageRaces:
    def __init__(self, repo: RegattaRepository, analyses: RaceAnalyses, folders: TrackFolders,
                 cache: AnalysisCache, tracks: TrackGateway, keys: MarksKeys, locks: RegattaLocks):
        self.repo, self.analyses, self.folders, self.cache = repo, analyses, folders, cache
        self.tracks, self.keys, self.locks = tracks, keys, locks

    def _race(self, rid, race_id):
        rg = find_regatta(self.repo.load(), rid)   # snapshot; changes go through repo.edit
        if rg is None:
            raise NotFound("unknown regatta")
        race = find_race(rg, race_id)
        if race is None:
            raise NotFound("unknown race")
        return race

    def create(self, rid, name, course, gun):
        """A new race (empty upload folder; tracks come by stage + upload + commit). Returns its id."""
        with self.locks.regatta(rid):
            if find_regatta(self.repo.load(), rid) is None:
                raise NotFound("unknown regatta")
            course, gun = race_config(course, gun)

            def add(reg):
                g = find_regatta(reg, rid)
                nm = name or f"Race {len(g['races']) + 1}"
                k = max([g.get("seq", 0), len(g["races"])] + [race_num(r["id"]) for r in g["races"]]) + 1
                while f"r{k:02d}" in {r["id"] for r in g["races"]}:
                    k += 1
                g["seq"] = k
                race_id = f"r{k:02d}"
                g["races"].append({"id": race_id, "name": nm, "dir": self.folders.new_race_dir(rid, race_id),
                                   "course": course, "gun": gun, "key": self.keys.new(rid, race_id)})
                return race_id
            return self.repo.edit(add)

    def update(self, rid, race_id, name, course, gun):
        """New name / course / gun. A new course or gun is analysed before it is saved; a new
        name only patches the cached analysis. Returns the analysis error of that patch, or None."""
        with self.locks.regatta(rid):
            race = self._race(rid, race_id)
            course, gun = race_config(course, gun)
            new = {**race, "course": course, "gun": gun, "name": name or race["name"]}
            if new["course"] != race["course"]:
                new["key"] = self.keys.new(rid, race["id"])    # old hand-edited marks don't fit
            if (new["course"] != race["course"] or new["gun"] != race.get("gun")) \
                    and self.tracks.count(self.folders.race_dir(new)):
                data, cfg = self.analyses.analysed(new, self.folders.race_dir(new))   # raises -> nothing saved
                self.analyses.save(rid, new, data, cfg)
                return None
            put_race(self.repo, rid, race["id"], new)        # name only: saved; patch an existing analysis
            return self.analyses.cached(rid, new) if self.cache.exists(rid, race["id"]) else None

    def stage(self, rid, race_id):
        """An empty staging folder for a new set of tracks."""
        with self.locks.regatta(rid):
            self._race(rid, race_id)
            self.folders.stage(rid, race_id)

    def unstage(self, rid, race_id):
        """Discard the staged copies of a failed save."""
        with self.locks.regatta(rid):
            self._race(rid, race_id)
            self.folders.unstage(rid, race_id)

    def upload(self, rid, race_id, name, raw):
        """One staged track file (staging only: no lock needed)."""
        rg = find_regatta(self.repo.load(), rid)
        race = find_race(rg, race_id) if rg else None
        if race is None:
            raise NotFound("unknown race")
        self.folders.put_staged(rid, race["id"], name, raw)

    def commit(self, rid, race_id, config=None):
        """Analyse the staged tracks (with a new name / course / gun when given), then swap
        them in as the race's tracks. Nothing changes if the analysis fails."""
        with self.locks.regatta(rid):
            race = self._race(rid, race_id)
            st = self.folders.staging_dir(rid, race["id"])
            if not self.tracks.count(st):
                raise ValueError("No uploaded .kml files to use.")
            new = {**race}
            if config is not None:
                name, course, gun = config
                course, gun = race_config(course, gun)
                new.update(course=course, gun=gun, name=name or race["name"])
            new["key"] = self.keys.new(rid, race["id"])  # new tracks: old hand-edited marks don't apply
            data, cfg = self.analyses.analysed(new, st)   # raises -> the current tracks stay
            new["dir"], undo, done = self.folders.swap_in(rid, race["id"])
            try:
                self.analyses.save(rid, new, data, cfg)   # moved files keep name/size/mtime
            except Exception:
                undo()                                    # new set back to staging, old set back
                raise
            done()

    def delete(self, rid, race_id):
        """The race, its uploaded tracks (app-made copies only) and its cached analysis."""
        with self.locks.regatta(rid):
            race = self._race(rid, race_id)
            put_race(self.repo, rid, race["id"], None)
            self.folders.delete_race(rid, race)
            self.cache.drop(rid, race["id"])
