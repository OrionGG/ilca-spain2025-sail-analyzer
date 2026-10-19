# Tests

Characterization tests that pin the app's current behaviour. They are the safety net for the
Clean Architecture refactor (#6), where every step must show **no difference**.

Stdlib `unittest`, with no packages beyond the app's own (numpy). Run from the repo root,
either way:

```
python -m unittest discover -s tests
python -m unittest
```

The whole suite takes about 1–1.5 minutes. Most of that is the golden test analysing the 9
races twice: directly, and through the cache the API serves.

| File | What it pins |
|---|---|
| `test_golden_analysis.py` | The analysis of the 9 Copa 2025 races (the KML tracks in the repo). Each output section is pinned by a hash: metadata, marks, start line, wind series, fleet stats, each boat, the regatta-index entry. So a refactor must reproduce it bit for bit. It's checked both from the analysis itself and through the path the API serves (fresh registry → `ensure_cache` → cache → regatta index, pinned in `golden/analysis/index.json`). A readable summary (marks, legs, course indices, roundings, boat order, summary metrics) is compared with a float tolerance, so a failure shows what moved. The races found must be exactly the references. `SAIL_GOLDEN=0` skips it. |
| `test_results_parser.py` | Official-results parsing. Uses `fixtures/sailti_like.html`, a synthetic page with Sailti's markup quirks and made-up sailors, plus the table layouts found in reviews. Also the download flow with the network stubbed: follow-up request, redirects, other-site links, fallbacks, errors. |
| `test_routes.py` | The HTTP API end to end. A real server runs on a temporary registry, data and cache folder: the request guard, static files, the lifecycle of regattas and races (staged upload of 8 real tracks, analysed commit, update, delete) and the official-results routes. |
| `test_gate_split.py` | Gate marks: one or two buoys, stray roundings, small fleets. |
| `test_course_validation.py` | Course, gun and name validation, and the course templates. |

## Regenerating the analysis references

Only after an **intended** change to the analysis, and say why in the PR:

```
python tests/test_golden_analysis.py --update
```

## Browser snapshot (`ui_snapshot.js`)

The browser code can't run under Node here, so its rendered output is pinned by
`golden/ui_snapshot.json`. That file holds a hash and length of the text of every tab in every
Copa race, for focus boat 206341 with no compare boat.

The script drives the page only through its DOM, so it keeps working while the scripts behind
the page are restructured. It relies on this page contract: `#regSel`, `#raceSel`,
`#focusSel`, `#partnerSel`, `#tabs button[data-tab]`, `#tabbody`, and hand-placed marks stored in
localStorage as `sa_marks_<race key>`, with each race's current key taken from
`GET /api/race/<regatta>/<race>`.

- It reloads every race, so earlier UI state such as a leg filter doesn't carry over.
- It fails if the focus boat isn't in a race.
- It masks the official results' download date, so re-downloading them doesn't change the snapshot.
- It counts hand-placed marks only for the races being snapshotted, under each race's current
  key. Marks of other regattas or left over from an old course don't count.

1. Start the verify server: `python sail_analyzer/app.py --port 8771` (or the `sail-verify`
   launch config).
2. Open `http://localhost:8771` on the **Copa de España ILCA 7 2025** regatta. Its official
   results must be downloaded, and no race may have hand-placed marks. `uiCompare` reports
   any hand-placed marks.
3. In the browser console, paste `tests/ui_snapshot.js`, then:
   ```js
   const ref = /* paste golden/ui_snapshot.json */;
   uiCompare(ref, await uiSnapshot())            // [] = nothing changed
   ```
4. To see what changed in one tab:
   `await uiSnapshot({full: true, races: ['Prueba 3'], tabs: ['debrief']})`. Run it on `main`
   and on the branch, then compare the two texts.

The snapshot covers the tabs' text. Canvas output (the map and charts) isn't text, so changes
that touch drawing are checked with screenshots in the PR.
