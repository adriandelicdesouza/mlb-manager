# Project State

_Last updated: 2026-11-08 · Version 0.1.0 (MVP)_

## Status
**Playable MVP complete.** A manager can pick any of the 30 clubs, manage a
generated roster, simulate a full 162-game season and 12-team postseason,
start subsequent seasons, and save/load franchises as JSON.

## Implemented (all verified)
| Area | Module | Notes |
| --- | --- | --- |
| Franchises | `franchises.py` | 30 clubs, AL/NL × East/Central/West, natural rivals |
| Players | `models.py`, `ratings.py`, `names.py`, `generation.py` | Fictional names; 1–99 ratings; position-specific profiles (catcher framing, pitcher stuff/control/movement/stamina, fielding/arm/range weighted per position); secondary positions; out-of-position penalty |
| Rosters | `roster.py` | 26 active (max 13 pitchers) + 14 reserves per club; auto lineup/rotation/closer; validation + auto-repair; promote/demote swaps; lineup, position, rotation and closer edits |
| Game sim | `game.py` | Plate-appearance model with platoon splits, framing, team defense, baserunning, steals, DPs, errors, sac flies, pitch counts, fatigue, bullpen roles, extra-inning runner, W/L/SV, box score + scoring log |
| Schedule | `schedule.py` | 2,430 games; 162 per club (13×div, 6–7×league, 46 interleague incl. 4 vs rival); 80–82 home games; no doubleheaders |
| Standings | `standings.py` | W/L/PCT/GB, home/away, L10, streak, run diff; ties: H2H → division record → run diff → abbreviation |
| Playoffs | `playoffs.py` | 6 per league; WC bo3, DS bo5 (2-2-1), CS bo7 (2-3-2), WS bo7 (home field to better record) |
| Season | `league.py` | Day-by-day advancement, starter rest/rotation, fatigue recovery, stats, leaders, phases, aging/development into new seasons |
| Saves | `persistence.py` | Versioned JSON (`format_version: 1`), atomic writes, clear errors |
| UI | `cli.py`, `views.py`, `main.py` | Interactive menus; views are pure functions; scripted-input tests |

## Verified results
- `.venv/bin/pip install -e .` installs cleanly (no warnings).
- **Test suite: `Ran 79 tests in ~27s — OK`** (`python -m unittest discover -s tests`).
- Launched the real console script with piped input and exercised: new
  franchise, dashboard, roster, lineup edits, pitching staff, player cards,
  roster move (with automatic lineup fix), 7-day and full-season sims, box
  score, standings, leaders, bracket, save, load, new season, and EOF autosave.
- `mlb-manager --simulate-season --seed 7` completes a full season with
  postseason in ~4 s.
- Calibration across seeds 7/8/42 (regular season): 4.15–4.32 R/G,
  1.10–1.16 HR/G, .242–.245 AVG; team wins range roughly 52–105; HR leaders
  43–52; ERA leaders ~1.9–2.2.
- Determinism: same seed + same decisions ⇒ identical league state, also
  after a save/load round trip (covered by tests).

## Known limitations
- No injuries, trades, free agency, draft or contracts.
- AI clubs never make roster moves; the user's club is the only one managed.
- Earned runs: only runs from batters reaching on errors (and extra-inning
  runners) are unearned; no full reconstruction of innings without errors.
- Season spans ~216 schedule days (vs. ~186 in MLB) because series are packed
  in fixed blocks; there are no doubleheaders or rainouts.
- No in-game (pitch-by-pitch) manager decisions; the user manages between games.
- Postseason stats are not tracked separately (regular-season stats only).
- Stolen-base volume is below modern MLB levels.
