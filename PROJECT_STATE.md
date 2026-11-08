# Project State

_Last updated: 2026-11-08 · Version 0.2.0 (GM expansion)_

## Status
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
| UI | `cli.py`, `views.py`, `main.py` | Franchise menu item 14 "Front Office": Contracts & Payroll, Trade Center, Free Agency, Draft, Minor League / Prospects, Transactions; offseason steps under SIMULATE; `--salary-cap` |

## Verified results
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
- No injuries, awards, morale or no-trade clauses.
- Luxury tax not implemented (hard cap only; re-signing exception).
- User contract negotiation is take-it-or-leave-it for re-signings; free-agent
  offers allow 85–100 % of the ask with a deterministic chance.
- In-season development does not occur (ratings change each offseason).
- Average active age drifts down slightly over many seasons.
- Earlier MVP limitations still apply (≈216-day schedule, ER simplification,
  low stolen-base volume, no in-game decisions).
