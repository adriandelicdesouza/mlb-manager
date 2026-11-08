# MLB Manager

A terminal-based baseball franchise management game. Pick one of the 30 MLB
franchises, manage a roster of fictional generated players, set lineups and
pitching staffs, simulate a 162-game season, and chase a championship through
a 12-team postseason. Franchises are saved as JSON files.

Pure Python 3.12+, standard library only. No network access, no database.

## Quick start

```bash
python3 -m venv .venv          # if .venv does not exist yet
.venv/bin/pip install -e .     # editable src-layout install
.venv/bin/mlb-manager          # launch the interactive game
```

Other ways to launch:

```bash
.venv/bin/python -m mlb_manager
.venv/bin/mlb-manager --new --team NYY --seed 42     # skip the title menu
.venv/bin/mlb-manager --load saves/nyy_2026.json     # resume a franchise
.venv/bin/mlb-manager --save-dir ~/mlb-saves         # custom save folder
.venv/bin/mlb-manager --simulate-season --seed 7     # headless full season
```

Saves go to `./saves/` by default (override with `--save-dir` or the
`MLB_MANAGER_SAVE_DIR` environment variable).

## How to play

From the franchise menu you can:

| # | Screen | What you can do |
| --- | --- | --- |
| 1 | Dashboard | Record, standing, next game, last result, news |
| 2 | Roster | Active 26 and reserves with ratings and season stats |
| 3 | Lineup | Reorder the batting order, swap positions, start bench players, auto-build |
| 4 | Pitching staff | Set the 5-man rotation and closer, see fatigue |
| 5 | Roster moves | Swap an active player with a reserve (max 13 active pitchers) |
| 6 | Player details | Full ratings card, defensive value at every position, stats |
| 7–11 | Standings, schedule, leaders, box score, bracket | Follow the league |
| 12 | Simulate | 1 / 7 / 30 days, to end of regular season, or through the World Series; start the next season |
| 13 | Save | Write a JSON save |

Ratings use a 1–99 scale (50 = MLB average). Hitters have contact, power,
eye and speed; fielders have fielding, arm and range (weighted differently
per position); catchers also have framing; pitchers have stuff, control,
movement and stamina. Players playing outside their listed positions take a
defensive penalty, marked `(!)` in the lineup screen.

The league seed makes everything reproducible: the same seed and the same
decisions produce the same season.

## Running tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```

## Documentation

- `CLAUDE.md` – engineering guide and conventions for contributors/agents
- `PROJECT_STATE.md` – what is implemented and verified right now
- `ROADMAP.md` – milestones, completed and planned
