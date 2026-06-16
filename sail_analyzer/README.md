# ⛵ ILCA Sail Analyzer

A local, offline web app that does Vantage / SailViewer–style race analysis on the
**Copa de España ILCA 7 2025** GPS tracks (TracTrac KML). Built around boat
**206341 · O. GARCIA GALL**, but any boat in the fleet can be the focus.

## Run it

**Easiest:** double-click `Run Sail Analyzer.bat` in the parent folder.

**Or from a terminal:**
```
cd sail_analyzer
python app.py            # parses 441 KMLs once (~20s), caches, opens the browser
```
Then go to http://127.0.0.1:8765/ . Requires Python 3 + numpy (already installed).
No internet, no map tiles, no other dependencies.

Options: `python app.py --rebuild` (re-parse from KML), `python app.py --port 9000`.

## What you get

Pick a **Race**, a **Focus** boat (default GARCIA GALL) and optionally a **Compare vs**
training partner. Then:

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
- **Start** – distance behind the line at the gun, speed vs fleet, line position,
  and your speed build-up off the line.
- **Position** – a fleet-rank ladder over the race (GPS proxy for places gained/lost).

## How the analysis works (and its limits)

These KMLs contain **only GPS position + time** (~3 s). Everything else is derived:

- **Speed / course** from position differences.
- **Wind direction** is *estimated*, not measured: a boat can't point within ~38° of
  the wind, so the wind is the centre of the emptiest heading arc (the upwind "no-go
  zone"), oriented by where the fleet sails after the start. Close-hauled angles come
  out at a realistic ~42–50°. Absolute wind may be a few degrees off, but the **same**
  estimate is applied to every boat, so all the *vs-fleet / vs-partner comparisons are
  valid*.
- **VMG, tacks/gybes, legs, maneuver loss, lifted/headed, start line, position** are all
  built on top of that.

**Not possible from this data:** heel angle and fore-aft trim. Those need IMU sensors
(Vakaros / Sailmon); TracTrac trackers are GPS-only. Start/finish times, marks and the
start line are *inferred* from fleet behaviour, so treat absolute distances/times as
approximate — the relative comparisons are the trustworthy part.

## Files
- `app.py` – local HTTP server + cache builder (stdlib only).
- `regatta.py` – the analytics engine (parsing, wind, VMG, maneuvers, legs, start).
- `static/` – the offline single-page UI (no CDN).
- `cache/` – generated per-race JSON (delete to force a rebuild).
