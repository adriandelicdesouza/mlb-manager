# CLAUDE.md – MLB Manager engineering guide

## Commands
- Install (editable, inside the project venv only): `.venv/bin/pip install -e .`
- Run the game: `.venv/bin/mlb-manager` (Textual GUI on a terminal; `--cli` for
  the classic numbered menus, also used automatically when stdin/stdout is not a TTY)
- Headless season smoke test: `.venv/bin/mlb-manager --simulate-season --seed 7`
- Tests: `.venv/bin/python -m unittest discover -s tests -v`
  - Always confirm the reported test count is non-zero ("Ran N tests").

## Constraints
- Python 3.12+. The simulation engine (everything outside `tui/`) is standard
  library only; the interactive GUI uses Textual (`textual>=8.2,<9`). No
  external APIs, no database. Domain code must never import `mlb_manager.tui`.
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
| `game.py` | Game state machine: classic plate-appearance resolution (CPU games) and pitch-by-pitch live resolution with manager hooks, substitutions, snapshot/restore; box scores |
| `tactics.py` | Instructions, pitcher repertoires, pitch-level model calibrated to the PA model, run expectancy / linear weights |
| `dugout.py` | `LiveGame`: automation levels, CPU in-game brain, context-sensitive controls, prompts, progression, decision log + ex-ante assessments, post-game report |
| `schedule.py` | 162-game balanced schedule generation |
| `standings.py` | Standings, tiebreakers, games back, playoff seeding |
| `playoffs.py` | 12-team bracket (WC bo3, DS bo5, CS bo7, WS bo7) |
| `contracts.py` | Market salaries (MLB scale), contract structures (AAV, back-loaded cash), arbitration salaries, payroll, commitments |
| `finance.py` | League financial rules engine: season-aware rule eras (Standard MLB CBA table / custom), cap & floor, Competitive Balance Tax, revenue sharing, transaction checks, CPU budgets, daily CBT accrual, season settlement |
| `development.py` | Potential, growth/decline, retirement, imperfect scouting |
| `draft.py` | Draft picks, class generation, draft order, CPU draft boards |
| `frontoffice.py` | Club strategy, best-26 org management, init/migration; `player_value` entry point |
| `valuation.py` | Centralised team-specific asset valuation (projections, contract, window, fit, picks) |
| `trades.py` | Proposals, validation, per-side evaluation, negotiation/counters, CPU needs, offers and CPU-CPU trades |
| `freeagency.py` | Free-agent market, offers, CPU signings |
| `injuries.py` | Injury rolls, durability traits, injury history, IL moves, rehab assignments, rust, game-day substitutions (never mutate the saved lineup) |
| `careers.py` | Career/postseason stats (per-club stints, archived history), leaderboards, franchise records, series MVPs |
| `morale.py` | Deterministic morale: explainable factors, weekly drift, modest game/contract effects |
| `awards.py` | End-of-season award formulas |
| `seasonlog.py` | Milestone detection (stats, no-hitters, clinches, streaks) |
| `league.py` | `League` aggregate: day advancement, phases (regular → postseason → complete → resign → draft → free_agency), results, leaders |
| `persistence.py` | Versioned JSON save/load (atomic writes) |
| `views.py` | Pure text rendering (tables, box scores, standings, cards) |
| `cli.py` | Classic numbered-menu terminal UI (injectable input/output for tests) |
| `tui/` | Textual GUI: `app.py` (screens, sim worker, files), `setup.py` (new-league setup, load review, rules editor), `base.py` (pane base), `panes.py` (dashboard, roster, lineup, trades, draft, FA, season), `dugout.py` (Game tab / live dugout), `modals.py`, `theme.py` |
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
- Valuation lives in `valuation.py` (`Valuator` caches club outlooks for one
  decision pass; `frontoffice.player_value` delegates to it). It is the single
  source of truth for trades, free agency and cut-downs; tune it rather than
  adding ad hoc checks. Build a new `Valuator` after rosters change.
- User trade proposals go through `trades.negotiate()` (validation, repeat
  detection, evaluation, counter); UIs must not re-implement any of it.
  Negotiation tolerances live in `trades.NegotiationTolerance`.
- Keep CPU decisions deterministic: use `league._rng(<purpose>, ...)`.
- Injured players must never reach `simulate_game`: always pass the team through
  `injuries.game_day_team()`; roster lists are active / reserves / injured_list
  and must be mutated via `Team.remove_player()` so no list is missed.
- When a player leaves a club call `league.note_departure(player)` *before*
  changing `player.team` (closes his stat stint, resets morale).
- `Player.batting/pitching` are the current regular season only; postseason
  lines live in `post_batting/post_pitching` (by round). Never merge them.
  Use `careers.*` helpers for career totals (they add the live season once).
- Game-day form (morale, rust) is applied through `League.game_players()`
  (adjusted copies); never mutate stored ratings for temporary effects.
- Financial rules live only in `finance.py` (see `docs/financial-rules.md`).
  Never compare payroll to a cap, floor or threshold elsewhere: call
  `finance.check_transaction(league, team, kind, added, ...)` for every move that
  changes payroll (raise its `reason` when not `allowed`, surface `warnings`),
  `finance.fits_budget`/`cpu_can_add` for CPU spending, and
  `finance.min_salary(league)` / `finance.rules(league)` instead of constants.
  Rules are per league and per season (`FinanceConfig` eras); in the offseason
  transactions count toward `finance.contract_season(league)` (= next season).
  Proposed CBA terms (`PROPOSED_TERMS`) are informational and must never be
  applied as rules. Changing a running league's rules goes through
  `finance.change_rules()` and must never touch contracts or history.
- Salaries are MLB scale (`contracts.MARKET_SCALE`); valuation stays in its
  pre-v0.9 value units by dividing salaries by `MARKET_SCALE`, so trade and FA
  tolerances are unchanged. `Player.salary` is current-season cash;
  `contracts.aav()` is the CBT measure; use `contracts.sign()` (structure) and
  `contracts.salary_in()` for future years.
- `scripts/league_health.py [seasons] [seed]` checks long-run balance
  (talent drift, payrolls, population, roster validity). Run it after
  touching development, contracts or CPU behaviour.

## Live games (dugout)
- `simulate_game` (classic resolution) must keep its exact random-number
  sequence: automated seasons are regression-checked against earlier versions
  (`scripts/league_health.py 6 11`). Put new live-only behaviour behind
  `pitch_by_pitch` / a `_RunContext`, never in the classic path.
- Live games are pitch-by-pitch `GameSimulator`s driven through `LiveGame`
  (`league.start_live_game()`); `play_game` consumes `league.live_game` for its
  key. Decisions go through `LiveGame.apply(key, value)` / `answer_prompt`;
  UIs render `LiveGame.controls()` and never call engine mutators directly.
- Neutral instructions must reproduce the PA model: tactical effects are
  deviations (centred pitch effects, centred location multipliers). After
  touching `tactics.py`, compare live-auto vs classic rates over a few hundred
  matched games (R/G, HR/G, AVG, K%, BB%).
- Engine moves raise `RuleError` (a `ValueError`) with a user-facing reason.
- Season settlement (`finance.settle_season`) runs once, when the World Series
  ends; CBT accrual runs once per regular-season day (`finance.accrue_day`).
- Prompt checks must not consume randomness; CPU decisions use the session's
  own `dugout` RNG stream so games replay identically given the same decisions.

## Testing notes
- `tests/test_cli.py` drives the UI with a `ScriptedIO` helper (scripted
  answers; EOF ends the session) – extend it when adding menus.
- `tests/helpers.league_at_offseason()` skips straight to a completed season
  for fast offseason tests; replay steps on a twin league to learn the
  deterministic ids a CLI script needs.
- Full-season tests take ~6 s each; the whole suite runs in ~300 s.
- `tests/test_dugout.py` builds live games straight from `generate_rosters`
  (`live_game()` helper) and steps them with `advance_to(...)`.
- `tests/test_finance.py` covers caps, floors, CBT, revenue sharing, arbitration,
  persistence and legacy migration; `tests/helpers.set_rules()/hard_cap()` give
  a league custom rules. New-league CLI scripts need a setup answer (`"1"` =
  start) after the seed, and loads a review answer (`"1"` = continue).
- `tests/test_tui.py` drives the GUI with Textual's `run_test()` pilot (clicks,
  keys, resize) and asserts on league state; use `await app.workers.wait_for_complete()`
  after simulation actions (they run in a worker thread).
- GUI panes only call domain APIs; anything that decides game outcomes belongs in
  the domain modules (and is then shared by CLI and GUI). Pure text helpers for
  the GUI live in `views.py`.
- Sim calibration targets (MLB-like): ~4.2–4.5 R/G, ~1.1 HR/G, ~.245 AVG.
  Re-check with `--simulate-season` after touching `game.py` constants.

## Conventions
- Ratings use a 1–99 scale (50 = MLB average).
- Player ids are ints, team ids are 2–3 letter abbreviations (`NYY`).
- Domain objects serialise via `to_dict()` / `from_dict()`; bump
  `SAVE_FORMAT_VERSION` in `persistence.py` on incompatible changes.
- Keep `PROJECT_STATE.md` and `ROADMAP.md` updated with *verified* results only.
