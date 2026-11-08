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
- Commit locally in logical commits on a feature branch. Push to GitHub (`origin`)
  and merge to `main` only when the user explicitly asks for it.

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
| `contracts.py` | Salaries, asking contracts, payroll, cap rules, commitments |
| `development.py` | Potential, growth/decline, retirement, imperfect scouting |
| `draft.py` | Draft picks, class generation, draft order, CPU draft boards |
| `frontoffice.py` | Club strategy, player/pick valuation, best-26 org management, init/migration |
| `trades.py` | Trade proposals, validation, evaluation, counters, CPU offers and CPU-CPU trades |
| `freeagency.py` | Free-agent market, offers, CPU signings |
| `injuries.py` | Injury rolls, IL moves, game-day substitutions (never mutate the saved lineup) |
| `awards.py` | End-of-season award formulas |
| `seasonlog.py` | Milestone detection (stats, no-hitters, clinches, streaks) |
| `league.py` | `League` aggregate: day advancement, phases (regular → postseason → complete → resign → draft → free_agency), results, leaders |
| `persistence.py` | Versioned JSON save/load (atomic writes) |
| `views.py` | Pure text rendering (tables, box scores, standings, cards) |
| `cli.py` | Interactive terminal UI (injectable input/output for tests) |
| `main.py` | argparse entry point (`mlb-manager` console script) |

## Determinism
- A league has one integer `seed`. Every random stream is derived from it with
  string seeds, e.g. `random.Random(f"{seed}:game:{game_id}")`. String seeding
  is stable across processes (unaffected by `PYTHONHASHSEED`).
- Never use the global `random` module state in simulation code.
- Same seed + same manager decisions => identical results, including after a
  save/load round trip.

## Front-office rules of thumb
- CPU logic lives in the domain modules; `cli.py` only calls `League`/module APIs.
- Valuation (`frontoffice.player_value`) is the single source of truth for
  trades, re-signing priorities and cut-downs; tune it rather than adding ad
  hoc checks.
- Keep CPU decisions deterministic: use `league._rng(<purpose>, ...)`.
- Injured players must never reach `simulate_game`: always pass the team through
  `injuries.game_day_team()`; roster lists are active / reserves / injured_list
  and must be mutated via `Team.remove_player()` so no list is missed.
- `scripts/league_health.py [seasons] [seed]` checks long-run balance
  (talent drift, payrolls, population, roster validity). Run it after
  touching development, contracts or CPU behaviour.

## Testing notes
- `tests/test_cli.py` drives the UI with a `ScriptedIO` helper (scripted
  answers; EOF ends the session) – extend it when adding menus.
- `tests/helpers.league_at_offseason()` skips straight to a completed season
  for fast offseason tests; replay steps on a twin league to learn the
  deterministic ids a CLI script needs.
- Full-season tests take ~6 s each; the whole suite runs in ~85 s.
- Sim calibration targets (MLB-like): ~4.2–4.5 R/G, ~1.1 HR/G, ~.245 AVG.
  Re-check with `--simulate-season` after touching `game.py` constants.

## Conventions
- Ratings use a 1–99 scale (50 = MLB average).
- Player ids are ints, team ids are 2–3 letter abbreviations (`NYY`).
- Domain objects serialise via `to_dict()` / `from_dict()`; bump
  `SAVE_FORMAT_VERSION` in `persistence.py` on incompatible changes.
- Keep `PROJECT_STATE.md` and `ROADMAP.md` updated with *verified* results only.
