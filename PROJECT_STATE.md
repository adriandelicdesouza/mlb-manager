# Project State

_Last updated: 2026-11-08 · Version 0.6.0 (interactive terminal GUI)_

## Status
**v0.6.0 adds an interactive, mouse-enabled terminal GUI** built with Textual
(`src/mlb_manager/tui/`). It runs on the unchanged `League`/domain APIs; the
classic numbered-menu CLI remains (`--cli`, and automatically without a TTY).
The engine stays standard-library only and gained opt-in play-by-play event
recording (no RNG use) for the batter-by-batter game view.

### v0.5.0 recap
**v0.5.0 replaces the CPU trade logic** with a centralised, team-specific
valuation model (`valuation.py`) and a negotiation layer (`trades.negotiate`):
per-side breakdowns, acceptable/borderline/unacceptable verdicts with
configurable tolerances, proportional counteroffers, need-driven CPU trading
and anti-exploit safeguards.

### v0.4.0 recap
**v0.4.0 adds player careers and franchise depth**: career statistics that
follow players through trades and survive retirement, separate postseason
statistics (per round, career and World Series totals) with LCS/WS MVPs,
injury tendencies, injury history, rehab assignments and post-injury rust, a
lightweight deterministic morale system, and an expanded Season Center
(career leaderboards, franchise records, retired players, career milestones,
postseason and championship history).

### v0.3.0 recap
**v0.3.0 adds in-season management** on top of the GM layer: injuries and an
injured list, in-season development, end-of-season awards, season milestones,
season summaries and a franchise history. Smarter CPU roster management keeps
every active roster valid and healthy enough to play.

### v0.2.0 recap
**Playable multi-season franchise game with a GM layer.** On top of the v0.1
MVP (simulation, schedule, playoffs, saves), managers now run a front office:
contracts and a salary cap, trades (players and picks) with a strategy-driven
CPU, an annual amateur draft, free agency, prospect development and a minor
league system. All 30 clubs participate in every system.

## Implemented (all verified)
| Area | Module | Notes |
| --- | --- | --- |
| Simulation core | `game.py`, `schedule.py`, `standings.py`, `playoffs.py` | Unchanged from v0.1 (engine preserved) |
| Contracts & cap | `contracts.py` | Market salary curve; asking contracts; hard cap for FA/trades; own re-signs may exceed it; dead money on release; commitments |
| Development | `development.py` | Potential, fast/normal/slow growth, breakouts/busts, decline after 31, retirements, noisy scouting (own org more accurate) |
| Draft | `draft.py` | 5 rounds × 30, 200-prospect classes (~48% pitchers, ages 18/21/22), worst-first order with playoff clubs last, tradable picks 3 years out, CPU boards by strategy |
| Front office | `frontoffice.py` | Strategy assignment, player/pick valuation, best-26 optimisation, depth fill, cut-down to 45, initialisation/migration |
| Trades | `trades.py` | Validation (ownership, pending FAs, deadline, cap, depth, roster size), evaluation with diminishing returns, counters, CPU offers to user, CPU-CPU trades |
| Free agency | `freeagency.py` | Asking prices (cheaper in-season), negotiation band, user signings, CPU signing rounds |
| League flow | `league.py` | Weekly CPU front-office activity in season; offseason phases resign → draft → free agency → new season |
| Saves | `persistence.py` | Format v2; v1 saves migrated automatically and deterministically |
| Injuries | `injuries.py` | Seeded risk per appearance (age, pitch count), 4 severities, IL place/activate, game-day substitution, healthy counts |
| Awards | `awards.py` | Value formulas from stats: MVP, Cy Young, ROY (service 0), Gold Glove (≥81 games at position; P), Silver Slugger (main position) |
| Milestones | `seasonlog.py` | 30/40/50/60 HR, 100/125/150 RBI, 200 H, 40/60 SB, 15/20 W, 200/250/300 K, 40/50 SV, no-hitters, clinches, eliminations, 10-game streaks |
| UI | `cli.py`, `views.py`, `main.py` | Franchise menu item 14 "Front Office": Contracts & Payroll, Trade Center, Free Agency, Draft, Minor League / Prospects, Transactions; offseason steps under SIMULATE; `--salary-cap` |

## Verified results (v0.6.0)
- **Test suite: `Ran 244 tests in ~227s — OK`**. New `test_tui.py` (13, Textual pilot):
  title screen new franchise (Select + typed seed) and load, save (Ctrl+S) with file
  check, quit dialog (cancel/quit), keyboard and mouse navigation of every pane, Tab
  focus, resizing to 80×24 / 100×30 / 200×60, sim buttons and keys (day, week),
  roster profile/demote/promote/release-with-confirm, lineup and staff edits, trade
  center selection → submit → counter → accept → players moved, incoming offer accept
  and reject, full offseason through the sidebar (begin → re-sign → draft room pick
  → finish draft → free agent signing → new season), Season Center tabs, game view
  stepping (next play, space, inning, final, replay) with final score matching the
  result, actions refused while a simulation runs; `--cli`/non-TTY use the classic UI.
- Engine: identical game results with and without event recording (checked).
- Real pseudo-terminal launch: `mlb-manager --new --team NYY` shows the GUI and the
  roster pane via keys; `--cli` shows the classic franchise menu.
- Visual check of rendered screenshots (dashboard, roster, lineup, trade, game,
  season, title, 80×24) for layout and the restrained theme.
- `scripts/league_health.py 6 11` reproduces the v0.5.0 run exactly (same champions,
  OVR 53.3 → 55.4, R/G 4.12–4.32, every roster valid), confirming event recording
  does not affect results; headless season ≈ 6.8 s.

## Verified results (v0.5.0)
- **Test suite: `Ran 231 tests in ~143s — OK`**. New `test_trade_intelligence.py` (18):
  component sums, strategy priorities (relative and averaged over scouting noise),
  contract/age/injury/deadline-rental effects, replacement-level scrubs worth ≤ 0,
  identical evaluations incl. after save/load, quantity-for-quality, salary dumps,
  talent floor, proportional counters that avoid core players, repeated-offer refusal
  and fatigue, C/SP depth floors, contract impact, needs, CPU offers with motives, a full
  season of CPU trades (volume, per-club limit, flip protection, reasons, valid rosters).
- The reported TOR/LAD case (two 33/36-year-old fringe players for a 32-year-old
  $16.55M one-year SP): a contending LAD refuses outright with no counter; a rebuilding
  LAD (for whom the contract is a burden) asks only for a 4th-round pick and drops the
  fringe players; a fair prospect offer to a contender draws a 2nd-round-pick counter.
- `scripts/league_health.py 6 11`: active OVR 53.3 → 55.4, R/G 4.12–4.32, 4–12 CPU
  trades per season, every roster valid; headless season ≈ 6.7–7.1 s.

## Verified results (v0.4.0)
- **Test suite: `Ran 212 tests in ~124s — OK`**. New: `test_careers.py` (14: derived
  stats incl. zero AB/IP, stints across trades, archive idempotence, regular vs
  postseason separation checked against schedule/series runs, WS ⊂ postseason,
  series MVPs, multi-season persistence incl. retirement and save/load, v3→v4
  migration, Season Center/Injuries CLI), `test_morale_injuries.py` (12:
  durability distribution and risk, injury history, rust vs rehab, CPU rehab
  with valid rosters, morale factors/determinism/bounds/effects).
- `scripts/league_health.py 6 11`: active OVR 53.3 → 55.5 and R/G 4.09–4.24
  (unchanged from v0.3.0), every roster valid; ~300–320 injuries per season,
  CPU IL placements 142 per season (v0.3.0: 143), ~90 rehab assignments;
  mean morale 51 with no club in revolt; save size ~1.6 MB growing ~0.2 MB/season.
- A real v3 save (`tor_2028`, offseason) loads, migrates to v4 and plays on.
- Headless season ≈ 6.2 s (v0.3.0: 5.8 s); an O(n²) roster-selection scan was
  replaced by id sets to absorb the extra bookkeeping.

## Verified results (v0.3.0)
- **Test suite: `Ran 186 tests in ~85s — OK`** (`.venv/bin/python -m unittest discover -s tests`).
  New: `test_injuries.py` (15), `test_season_experience.py` (21), in-season CLI tests (2).
  Covered: seeded injuries and severity mix, injured players never play (90-day
  check), CPU IL usage and roster validity every day, user IL flow with call-up,
  forced IL when healthy depth is short, offseason healing, traded injured
  players land on the IL, award eligibility/correctness, milestones, in-season
  development caps and playing-time effect, performance-aware lineups and
  rotations, CPU promotions, v2→v3 migration, franchise log, Season Center and
  Injuries menus.
- Real console session: injuries reported in news, injured starter auto-replaced,
  injury report and IL screens, milestones (10-game streak, combined no-hitters),
  full season → awards announced → season summary → franchise history.
- Over 3 seeded seasons: 2–4 no-hitters per season, ~135–147 CPU IL placements.
- `scripts/league_health.py 6 11`: active OVR 53.3 → 55.5 (≈+0.4/yr, same slope
  as v0.2), R/G 4.09–4.18, every roster valid every season.
- Headless season: ~5.8 s (was ~4 s; injuries, CPU roster reviews, milestones).

## Verified results (v0.2.0)
- **Test suite: `Ran 148 tests in ~54s — OK`** (`.venv/bin/python -m unittest discover -s tests`).
  New coverage: contracts, development, draft, trades (incl. exploit cases),
  free agency, front office, user-driven offseason, 3-season progression,
  v1→v2 migration, mid-draft save/load determinism, front-office CLI flows.
- Real console-script sessions (`.venv/bin/mlb-manager`) exercised: Front Office
  screens, a rejected trade followed by an accepted CPU counter-offer, full
  season → begin offseason → finish re-signing → draft room (sim to pick,
  auto-draft) → free agency → 2027 season → save.
- `scripts/league_health.py 6 11` (six seasons, automated offseasons):
  active-roster OVR 52.2 → 54.2 (stable after year 2), R/G 4.09–4.21,
  wins range ~52–112, median payroll $82–105M under a $125M cap, player
  population bounded (~1,260–1,410), 3–10 CPU trades per season, six different
  champions; every roster valid every season.
- `mlb-manager --simulate-season --seed 7` still completes in ~4 s.

## Known limitations
- The GUI game view replays the user's last game after it has been simulated
  (no in-game decisions yet); only the user's games record play-by-play.
- The GUI does not yet expose every classic-CLI screen (e.g. full schedule list,
  league transactions log, morale table); they remain available with `--cli`.
- Career statistics start with the league's first season (generated veterans
  have no synthetic past); no Hall of Fame or no-trade clauses.
- Saves made mid-postseason with v0.3.0 have no per-round stats for games
  already played (v3→v4 migration).
- Mid-season transactions credit a player's games to the club he was with when
  they were played; the lineup "games by position" is still per season.
- Milestones list is capped at 250 per season; news feed shows the latest 60 items.
- Luxury tax not implemented (hard cap only; re-signing exception).
- User contract negotiation is take-it-or-leave-it for re-signings; free-agent
  offers allow 85–100 % of the ask with a deterministic chance.
- Average active age drifts down slightly over many seasons.
- Earlier MVP limitations still apply (≈216-day schedule, ER simplification,
  low stolen-base volume, no in-game decisions).
