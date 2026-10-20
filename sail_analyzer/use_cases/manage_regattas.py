"""ManageRegattas: list, create, rename and delete regattas."""

from __future__ import annotations

from sail_analyzer.entities.course import TEMPLATES, parse_legs

from .gateways import AnalysisCache, RegattaLocks, RegattaRepository, TrackFolders, TrackGateway
from .registry import find_regatta, regatta_of, slug


class ManageRegattas:
    def __init__(self, repo: RegattaRepository, cache: AnalysisCache, folders: TrackFolders,
                 tracks: TrackGateway, locks: RegattaLocks):
        self.repo, self.cache, self.folders, self.tracks, self.locks = repo, cache, folders, tracks, locks

    def list(self):
        """Every regatta with its races (and whether each is analysed), plus the course templates."""
        out = []
        for rg in self.repo.load()["regattas"]:
            races = []
            for r in rg["races"]:
                meta = self.cache.meta(rg["id"], r["id"]) or {}
                races.append({"id": r["id"], "name": r["name"], "course": r["course"], "gun": r.get("gun"),
                              "n_files": self.tracks.count(self.folders.race_dir(r)),
                              "uploaded": self.folders.is_upload(r),
                              "analysed": self.cache.exists(rg["id"], r["id"]),
                              "error": meta.get("error")})
            res = rg.get("results")
            out.append({"id": rg["id"], "name": rg["name"], "races": races,
                        "results": {k: res.get(k) for k in ("url", "fetched", "columns", "n", "map")} if res else None})
        return {"regattas": out, "templates": [{**t, "course": parse_legs(t["legs"])} for t in TEMPLATES]}

    def create(self, name):
        """A new regatta; returns its id (a slug of the name not used before)."""
        if not name:
            raise ValueError("Give the regatta a name.")

        def taken(reg, rid):              # registered, or leftovers of a deleted one
            return find_regatta(reg, rid) or self.folders.regatta_exists(rid) or self.cache.regatta_exists(rid)

        def add(reg):
            rid, k = slug(name), 2
            while taken(reg, rid):
                rid, k = f"{slug(name)}-{k}", k + 1
            reg["regattas"].append({"id": rid, "name": name, "races": []})
            return rid
        return self.repo.edit(add)

    def rename(self, rid, name):
        def rename(reg):
            g = regatta_of(reg, rid)
            if not name:
                raise ValueError("Give the regatta a name.")
            g["name"] = name
        self.repo.edit(rename)

    def delete(self, rid):
        """The regatta, its races' uploaded tracks and its cached analyses."""
        regatta_of(self.repo.load(), rid)             # an unknown regatta takes no lock
        with self.locks.regatta(rid):
            regatta_of(self.repo.load(), rid)
            self.repo.edit(lambda reg: reg.update(regattas=[g for g in reg["regattas"] if g["id"] != rid]))
            self.folders.delete_regatta(rid)          # app-made copies/caches only
            self.cache.drop_regatta(rid)
