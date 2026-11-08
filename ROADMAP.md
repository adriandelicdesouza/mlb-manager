# Roadmap

## Milestone 0 – Scaffolding ✅
- [x] pyproject.toml, src layout, editable install, `mlb-manager` console script
- [x] unittest test framework
- [x] Project documentation (README, CLAUDE.md, PROJECT_STATE.md, ROADMAP.md)

## Milestone 1 – MVP (playable first version) ✅ v0.1.0
- [x] 30 franchises, AL/NL with real divisions
- [x] Fictional generated players with position-specific attributes
- [x] Active (26) rosters and reserves, roster moves
- [x] Single-game simulation with box scores
- [x] 162-game schedule and standings with tiebreakers
- [x] 12-team postseason
- [x] Interactive terminal interface
- [x] JSON franchise saves
- [x] Multi-season play with aging/development

## Milestone 2 – GM & franchise management ✅ v0.2.0
Revised scope (replaces the earlier "Depth" and "Front office" milestones):
lightweight, Retro Bowl-style front office adapted to MLB. Gameplay over
financial realism.

**Contracts & cap**
- [x] Salary, contract length, years remaining, expiration status for every player
- [x] Configurable hard salary cap (default $125M, `--salary-cap`) for signings and trades
- [x] Own-player re-signing/extensions allowed above the cap
- [x] Releases with current-season dead money
- [x] Payroll, cap space, future commitments, approaching free agency views

**Trades**
- [x] Player-for-player, multi-player, draft-pick and mixed trades
- [x] CPU valuation: ability, potential, age, position, roster need, depth,
      contract surplus, competitive strategy
- [x] Contending / balanced / rebuilding CPU strategies
- [x] CPU counter-offers; CPU-initiated offers to the user (accept/reject/counter)
- [x] CPU-to-CPU trades (contenders buy veterans from rebuilders)
- [x] Anti-exploit: diminishing returns on bundles, contract-aware values,
      acceptance premium, deterministic answers, cap and roster checks, trade deadline

**Draft**
- [x] Annual 5-round draft, 200-prospect generated classes with realistic positions
- [x] Order from previous standings (worst first, playoff clubs last)
- [x] CPU selections from strategy-weighted, imperfect scouting boards
- [x] Prospect potential, development trajectories, rookie contracts
- [x] Tradable picks for the next three drafts

**Free agency**
- [x] Market of released/expiring players; asking contracts; negotiation band
- [x] CPU signings driven by need, strategy and cap space

**Development & minor leagues**
- [x] Growth toward potential (fast / normal / slow), breakouts and busts
- [x] Plateau then decline with age; retirements
- [x] Reserves act as the minor league system (organisation limit 45)
- [x] Promote / demote / swap / release; CPU best-26 organisation management
- [x] Imperfect scouting (own organisation scouted more accurately)

**Offseason flow**: season complete → re-signing period → draft → free agency → next season.

**Technical**: save format v2 with automatic v1 migration; deterministic seeds preserved.

## Milestone 3 – In-season management ✅ v0.3.0
- [x] Injuries: seeded per-game risk (age, workload), four severities (1–130 days), named injuries
- [x] Injured players never take the field: game-day substitutes keep the saved lineup intact
- [x] Injured list: user placement/activation; IL frees an active spot; automatic call-ups
- [x] CPU clubs IL long injuries, call up replacements and reinstate healed players daily
- [x] Healthy-depth guarantee (≥10 healthy hitters and pitchers active) for every club
- [x] In-season development every 30 days, scaled by age, potential and playing time (capped)
- [x] Offseason development preserved (rates rebalanced for the in-season growth)
- [x] Biweekly CPU roster reviews: promotions/demotions, performance-aware lineups and rotations
- [x] Awards per league: MVP, Cy Young, Rookie of the Year, Gold Gloves (8 + P), Silver Sluggers (9)
- [x] Season milestones: statistical thresholds, no-hitters, division clinches, eliminations, 10-game streaks
- [x] End-of-season summary, awards browser and franchise history (seasons, titles, awards, major moves)
- [x] Save format v3 with automatic v2→v3 (and v1→v3) migration

## Milestone 4 – Player careers & franchise depth ✅ v0.4.0
- [x] Career statistics (batting: G, AB, H, HR, RBI, R, BB, SO, SB, AVG/OBP/SLG/OPS;
      pitching: G, GS, IP, W, L, SV, SO, BB, ER, ERA, WHIP), per season and per club
- [x] Stats follow players through trades/releases (season split into club stints)
- [x] Separate postseason statistics per round; career postseason and World Series totals
- [x] Postseason awards: ALCS/NLCS MVP and World Series MVP
- [x] Retired players keep their career records (permanent archive)
- [x] Career milestones (100+ HR, 1,000+ H/SO, 100+ W/SV, ...)
- [x] Injury-prone / durable tendencies, injury history, rehab assignments, post-injury rust
- [x] Lightweight deterministic morale (playing time, role, contract, team results, streaks)
      with modest effects on performance and re-signing
- [x] Season Center: career leaderboards, franchise records & leaders, postseason leaders,
      retired players, career milestones, postseason and championship history
- [x] Save format v4 with automatic v3→v4 migration

## Milestone 5 – Trade intelligence ✅ v0.5.0
- [x] Centralised valuation (`valuation.py`): year-by-year projections toward the club's
      *scouted* potential, aging, performance blend, position scarcity, replacement level,
      injuries/durability, salary and control years, re-signing rights
- [x] Team-specific: competitive window (age-aware for rebuilders), positional need and
      redundancy, essential starters/closer, cap pressure per season, pick inventory
- [x] Per-side breakdown (asset, contract, strategic, roster fit, net);
      acceptable / borderline / unacceptable via configurable `NegotiationTolerance`
- [x] Counteroffers: drop unwanted pieces, ask for the cheapest non-core, non-redundant asset
      that closes the gap (bounded by the shortfall), or hold back a CPU asset; hopeless
      offers get a plain refusal
- [x] Need-driven CPU trades (weak starters, injuries, rotation, bullpen; rebuilders sell
      veterans; prospect-rich clubs buy), ≤3 per club per season, no quick flips, with reasons
- [x] CPU offers to the user explain the motive; identical pending offers are never repeated
- [x] Safeguards: diminishing returns + consolidation premium, talent floor on salary dumps,
      deadline rentals priced by remaining season, C/SP depth floors, repeated-offer refusal and
      negotiation fatigue, deterministic evaluations (also after save/load)
- [x] Contract implications shown for every proposal

## Milestone 6 – Interactive terminal GUI (v0.6.0, next)
- [ ] Textual-based app (mouse + keyboard) on top of the existing `League` API; classic CLI kept
- [ ] Dashboard, roster table + player profile, lineup editing, promote/demote/release
- [ ] Trade center, draft room, Season Center screens, compact game view (play-by-play replay)

## Later
- [ ] Hall of Fame; no-trade clauses
- [ ] Optional luxury tax (soft cap) as an alternative cap mode
- [ ] Trade block and "what would it take?" asking-price feature
- [ ] Tighter schedule packing (≈186 days), higher stolen-base volume

## Milestone 7 – In-game management
- [ ] Play-by-play mode for the user's games
- [ ] Manual pitching changes, pinch hitters, steals, bunts, intentional walks

## Technical backlog
- [ ] Optional type checking (mypy) and linting configuration
- [ ] Further sim performance work (~5.8 s per season today)
- [ ] League gets slightly younger over many seasons (avg active age 28 → ~27 after 6 years); revisit aging curve
