# In-game management (v0.8.0)

## Engine: one state machine, two resolutions

`game.GameSimulator` holds the full state of a game (inning, half, outs,
bases with responsible pitchers, count, current lineups, pitchers used,
pitch loads, box scores) and the event list. Events are appended and never
rewritten.

| Resolution | Used for | How a plate appearance is resolved |
| --- | --- | --- |
| Classic | every CPU-vs-CPU game, simulated user games | one draw from the plate-appearance (PA) model; built-in steal and pitching-change rules |
| Pitch-by-pitch | the user's live games | pitch by pitch, with a `SideManager` per club supplying calls |

The classic path consumes random numbers exactly like the v0.6 engine, so
automated seasons are unchanged (verified: `scripts/league_health.py 6 11`
output is identical to v0.6 apart from save size and timings; 400 classic
games compared field by field).

Live decision points (`game.phase`):

| Phase | Meaning | Typical moves |
| --- | --- | --- |
| `half` | before a half-inning | pitching change, defensive replacement, game plans |
| `pa` | before a plate appearance | pinch hitter/runner, pitching change, intentional walk, alignment, approaches |
| `pitch` | between pitches | pitch type/location, take, bunt, hit-and-run, steals, pitchout |
| `final` | game over | — |

`step()` resolves one unit: start the half (managers' half-inning hooks,
extra-inning runner), start the plate appearance (managers' pre-PA hooks,
which may end it, e.g. an intentional walk), or throw one pitch (pickoff
check, pitch selection, execution, zone, swing, contact, foul/fair, batted
ball, runner advancement). `LiveGame.advance(unit)` loops `step()` until the
unit boundary (pitch, play, batter, half, inning, game), an assisted-mode
prompt or the final.

## Pitch model and calibration (`tactics.py`)

For each pitch: target location (heart / edge / up / down / chase; a
count-and-approach mix when the catcher calls it) → execution roll (control,
pitch grade, effort, fatigue; a miss becomes a *mistake* over the plate or a
*wild* pitch) → in zone? → swing? (count, location, hitter instructions) →
contact? → foul or fair → batted-ball outcome. Pitch types come from a
deterministic repertoire per pitcher (fastball or sinker plus 1–4
secondaries with 20–80 grades and usage shares).

Calibration: for each plate appearance the PA model gives K, BB, HBP and
batted-ball shares. Two knobs (zone and contact offsets) and an HBP rate are
solved by Newton iteration over a count-state dynamic programme so that
**neutral** play reproduces K/BB/HBP exactly in expectation (cached).
Pitch-type and location batted-ball effects are centred on the neutral mix
(weighted by how often each location is put in play), so neutral play keeps
the PA model's batted-ball shares. Instructions are deviations from that
baseline and are scaled by ability (e.g. a power approach helps power
hitters more; strict discipline depends on the batter's eye).

Matched-game check (600 games, same matchups and seeds, live games fully
automated): classic R/G 4.26, HR/G 1.10, AVG .245, K% 22.8, BB% 7.9 vs live
4.12, 1.14, .243, 22.9, 7.7; 3.83 pitches per PA.

## Dugout (`dugout.py`)

* `Instructions` – team batting/baserunning plan, per-batter overrides,
  pitching plan (effort, approach, sequencing, hold runners, workload hook),
  approach per opposing hitter, defensive alignment. `NextPitch` – one-shot
  calls cleared after use (bunt and send last for the plate appearance).
* Automation per category: `manual` (the CPU makes no decisions; a pitcher
  35+ pitches past his limit is replaced as a forced safety move),
  `assisted` (routine work automatic; prompts for pitching changes,
  intentional walks, infield in, pinch hitters, steals, sacrifice bunts),
  `auto` (`CpuBrain`, the same logic that manages the opponent).
* `controls()` lists only relevant controls for the phase and side; disabled
  entries carry a reason (automated category, three-batter minimum, fixed
  batting order...). `apply()` validates and logs.
* Every intervention becomes a `Decision` (situation, category, action,
  player, previous/new, source, ex-ante assessment, resulting event index).
  Assessments use pre-decision information only: expected linear-weight runs
  under old vs new instructions (count-state DP), steal success vs run
  expectancy break-even, run expectancy for walks and bunts, upcoming-hitter
  matchups for pitching changes.
* `summary()` builds the post-game report stored on the `GameResult`.

## League integration

`League.start_live_game()` performs the normal pre-game work (roster checks,
starters, injury replacements) and stores `league.live_game`;
`play_game` uses its result for that game key (finishing it automatically if
needed), so the rest of the day, stats, injuries (effort-weighted workload)
and fatigue flow as usual. The live game is part of the save
(`live_game` key; format version unchanged because older readers ignore it).

## Limitations

* Only the user's games can be played live; CPU games use classic resolution.
* No wild pitches, passed balls, balks, mound visits or double switches;
  "send/hold" is an instruction for the next ball in play rather than a
  mid-play decision.
* Live automated games run about 3 % below classic scoring because the CPU
  uses situational tactics (intentional walks, infield in, bunts); within
  sampling noise of the calibration check.
* Assessments are approximations (the intentional-walk grade ignores the next
  hitter; defensive substitutions use team defense rating).
