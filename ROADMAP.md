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

Verified: 79 automated tests passing; interactive app exercised end to end.

## Milestone 2 – Depth (next)
- [ ] Injuries and injured list (IL) with automatic call-ups for AI clubs
- [ ] AI roster management (call-ups, rotation changes on fatigue/slumps)
- [ ] Separate postseason and career statistics
- [ ] Platoon/split stats and per-team season stats screens
- [ ] Tighter schedule packing (≈186 days), occasional doubleheaders
- [ ] Higher stolen-base volume and more baserunning nuance (tag-ups on 2B, holds)

## Milestone 3 – Front office
- [ ] Trades with AI valuation
- [ ] Contracts, payroll and budget
- [ ] Free agency and amateur draft
- [ ] Player potential and minor-league development
- [ ] Awards (MVP, Cy Young, Rookie of the Year)

## Milestone 4 – In-game management
- [ ] Play-by-play mode for the user's games
- [ ] Manual pitching changes, pinch hitters, steals, bunts, intentional walks

## Technical backlog
- [ ] Save-format migrations when `SAVE_FORMAT_VERSION` changes
- [ ] Optional type checking (mypy) and linting configuration
- [ ] Performance profiling of the PA loop (currently ~1.5 ms/game)
