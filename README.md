# ⛵ ILCA Sail Analyzer

A local, offline web app that does **Vantage / SailViewer–style race analysis** on GPS
tracks — speed/VMG/pointing vs the fleet and a training partner, wind & shift analysis
estimated from the tracks, maneuver-loss in boatlengths, start and fleet-position review.

It ships with the full **Copa de España ILCA 7 2025** (Altea) dataset — 49 boats × 9 races
of TracTrac GPS tracks — built around boat **206341 · O. GARCIA GALL**, but any boat in
the fleet can be the focus.

---

## ▶️ How to run it

**Requirements:** Python 3.8+ and NumPy. Nothing else — no internet, no map tiles, no
build step. Install NumPy if you don't have it:

```bash
pip install numpy
```

**Get the code:**

```bash
git clone https://github.com/OrionGG/ilca-spain2025-sail-analyzer.git
cd ilca-spain2025-sail-analyzer
```

**Start the app:**

```bash
python sail_analyzer/app.py
```

The first launch parses the 441 KML files once (~20 s) and caches the result, then your
browser opens automatically at **http://127.0.0.1:8765**. Later launches are instant.

> **Windows shortcut:** instead of the command line, just double-click
> **`Run Sail Analyzer.bat`** in the project root.

**Options:**

| Command | What it does |
|---|---|
| `python sail_analyzer/app.py` | Run on the default port (8765) |
| `python sail_analyzer/app.py --port 9000` | Use a different port |
| `python sail_analyzer/app.py --rebuild` | Force a re-parse of the KML files |

Press **Ctrl+C** in the terminal to stop the server.

---

## What you get

Pick a **Race**, a **Focus** boat (default GARCIA GALL) and optionally a **Compare vs**
training partner, then explore:

- **Map** – every boat (faint), your track coloured by **tack / speed / VMG**, the
  inferred course marks (W/L) and start line, plus a **play button + scrubber** that
  animates the whole fleet with a live speed/VMG/TWA readout.
- **Overview** – your averages vs the **fleet median** and head-to-head vs your partner:
  up/downwind speed, VMG, pointing angle, tack & gybe loss. Green = better.
- **Speed** – SOG over time vs the fleet 25–75% band and your partner; per-leg averages.
- **VMG** – velocity-made-good and TWA over time.
- **Wind & Shifts** – wind direction reconstructed from the fleet's tacks over time,
  and the % of upwind time you spent on the **lifted** tack.
- **Maneuvers** – every tack/gybe with entry/min/exit speed, duration and
  **boatlengths lost**; filter to your best/worst 50%; click a row to jump there.
- **Start** – distance behind the line at the gun, speed vs fleet, and speed build-up.
- **Position** – a fleet-rank ladder over the race (GPS proxy for places gained/lost).

---

## How the analysis works (and its limits)

These KMLs contain **only GPS position + time** (~3 s). Everything else is derived:

- **Speed / course** from position differences.
- **Wind direction** is *estimated*, not measured: a boat can't point within ~38° of the
  wind, so the wind is the centre of the emptiest heading arc (the upwind "no-go zone"),
  oriented by where the fleet sails after the start. Close-hauled angles come out at a
  realistic ~42–50°. The absolute wind may be a few degrees off, but the **same** estimate
  is applied to every boat, so all the *vs-fleet / vs-partner comparisons are valid*.
- **VMG, tacks/gybes, legs, maneuver loss, lifted/headed, start line and position** are
  all built on top of that.

**Not possible from this data:** heel angle and fore-aft trim — those need IMU sensors
(Vakaros / Sailmon); TracTrac trackers are GPS-only. Start/finish times, marks and the
start line are *inferred* from fleet behaviour, so treat absolute distances/times as
approximate — the relative comparisons are the trustworthy part.

---

## Project layout

```
sail_analyzer/
  app.py            local HTTP server + per-race JSON cache builder (stdlib only)
  regatta.py        analytics engine (parsing, wind, VMG, maneuvers, legs, start)
  static/           offline single-page UI (no CDN)
  cache/            generated per-race JSON (gitignored; rebuilt on first run)
01_Prueba 1/ … 09_Prueba 9/   the 49 boats × 9 races of KML tracks
Run Sail Analyzer.bat         Windows one-click launcher
```

See [`sail_analyzer/README.md`](sail_analyzer/README.md) for more detail on the engine.
