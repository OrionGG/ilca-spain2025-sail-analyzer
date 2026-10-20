"""OfficialResults: a regatta's official results, downloaded from its results page and used
as the reference for finish places; each results column is mapped to one of its races."""

from __future__ import annotations

from .errors import NotFound
from .gateways import Clock, RegattaRepository, ResultsSource, ResultsStore
from .registry import find_regatta, regatta_of


class OfficialResults:
    def __init__(self, repo: RegattaRepository, source: ResultsSource, store: ResultsStore, clock: Clock):
        self.repo, self.source, self.store, self.clock = repo, source, store, clock

    def get(self, rid):
        """The results with their column mapping (None if not downloaded); NotFound: no such regatta."""
        def read(reg):                                # written together (download / delete)
            rg = find_regatta(reg, rid)
            return rg, (self.store.load(rid) if rg and rg.get("results") else None)
        rg, data = self.repo.view(read)
        if rg is None:
            raise NotFound("unknown regatta")
        return {**rg["results"], **data} if data else None

    def download(self, rid, url):
        """Fetch and keep the results; a column keeps its race when the page is fetched again,
        new columns map to the race in their position when it's free. Returns (rows, columns)."""
        regatta_of(self.repo.load(), rid)             # NotFound before going to the network
        res = self.source.fetch(url)                  # network: outside any lock

        def save(reg):                                # the file and its registry entry together
            g = regatta_of(reg, rid)                  # NotFound: deleted while fetching
            self.store.save(rid, {"races": res["races"], "rows": res["rows"]})
            default_map = [g["races"][i]["id"] if i < len(g["races"]) else None for i in range(len(res["races"]))]
            old = g.get("results") or {}
            by_name = dict(zip(old.get("columns") or [], old.get("map") or []))
            mp = [by_name[c] if c in by_name else None for c in res["races"]]
            used = {m for m in mp if m}
            for i, c in enumerate(res["races"]):          # new columns: positional default if free
                if c not in by_name and default_map[i] and default_map[i] not in used:
                    mp[i] = default_map[i]
                    used.add(mp[i])
            g["results"] = {"url": url, "fetched": self.clock.now_iso(),
                            "columns": res["races"], "n": len(res["rows"]), "map": mp}
        self.repo.edit(save)
        return len(res["rows"]), res["races"]

    def set_map(self, rid, mp):
        """Which race each results column is (None = not a tracked race)."""
        def set_map(reg):
            g = regatta_of(reg, rid)
            if not g.get("results"):
                raise ValueError("No official results for this regatta.")
            ids = {r["id"] for r in g["races"]}
            if not isinstance(mp, list) or len(mp) != len(g["results"]["columns"]):
                raise ValueError("Bad results mapping.")
            g["results"]["map"] = [m if m in ids else None for m in mp]
        self.repo.edit(set_map)

    def remove(self, rid):
        def drop(reg):
            g = regatta_of(reg, rid)
            g.pop("results", None)
            self.store.remove(rid)
        self.repo.edit(drop)
