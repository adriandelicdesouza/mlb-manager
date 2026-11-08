# CLAUDE.md – MLB Manager engineering guide

## Commands
- Install (editable, inside the project venv only): `.venv/bin/pip install -e .`
- Run the game: `.venv/bin/mlb-manager` (or `.venv/bin/python -m mlb_manager`)
- Headless season smoke test: `.venv/bin/mlb-manager --simulate-season --seed 7`
- Tests: `.venv/bin/python -m unittest discover -s tests -v`
  - Always confirm the reported test count is non-zero ("Ran N tests").

## Constraints
- Python 3.12+, standard library only. No external APIs, no database.
- Never modify the global Python environment; use `.venv/`.
- No remote git pushes. Commit locally in logical commits.

## Architecture (src layout: `src/mlb_manager/`)
Simulation/domain code never prints or reads input; the CLI is the only
presentation layer.

| Module | Responsibility |
| --- | --- |
| `franchises.py` | Static data: 30 franchises, leagues, divisions, natural rivals |
| `models.py` | Dataclasses (`Player`, `Team`, stats) + JSON (de)serialisation |
| `ratings.py` | Attribute definitions, position-specific weights, overall/defense math |
| `names.py` | Fictional name generator |
| `generation.py` | Seeded creation of players, rosters and a full league |
| `roster.py` | Roster rules, auto lineup/rotation, validation and repair, moves |
| `game.py` | Plate-appearance based single-game simulation and box scores |
| `schedule.py` | 162-game balanced schedule generation |
| `standings.py` | Standings, tiebreakers, games back, playoff seeding |
| `playoffs.py` | 12-team bracket (WC bo3, DS bo5, CS bo7, WS bo7) |
| `league.py` | `League` aggregate: day advancement, phases, results, leaders |
| `persistence.py` | Versioned JSON save/load (atomic writes) |
| `cli.py` | Interactive terminal UI (injectable input/output for tests) |
| `main.py` | argparse entry point (`mlb-manager` console script) |

## Determinism
- A league has one integer `seed`. Every random stream is derived from it with
  string seeds, e.g. `random.Random(f"{seed}:game:{game_id}")`. String seeding
  is stable across processes (unaffected by `PYTHONHASHSEED`).
- Never use the global `random` module state in simulation code.
- Same seed + same manager decisions => identical results, including after a
  save/load round trip.

## Conventions
- Ratings use a 1–99 scale (50 = MLB average).
- Player ids are ints, team ids are 2–3 letter abbreviations (`NYY`).
- Domain objects serialise via `to_dict()` / `from_dict()`; bump
  `SAVE_FORMAT_VERSION` in `persistence.py` on incompatible changes.
- Keep `PROJECT_STATE.md` and `ROADMAP.md` updated with *verified* results only.
