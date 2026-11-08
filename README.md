# MLB Manager

A terminal-based baseball franchise management game. Pick one of the 30 MLB
franchises, manage a roster of fictional generated players, set lineups and
pitching staffs, simulate a 162-game season, and chase a championship through
a 12-team postseason. As general manager you also draft prospects, make
trades, sign free agents and manage contracts under a salary cap, building a
contender over many seasons. Franchises are saved as JSON files.

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
.venv/bin/mlb-manager --new --team KC --salary-cap 110  # tighter salary cap
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
| 14 | Front Office | Contracts & payroll, Trade Center, Free Agency, Draft, Minor League / Prospects, league transactions |

### Front office

- **Contracts & cap** – every player has a salary and years remaining. Free-agent
  signings and trades must fit under the cap (default $125M) unless they reduce
  payroll; re-signing your own players may exceed it. Released players' salary
  stays on this season's books as dead money.
- **Trades** – combine up to five players and/or draft picks per side (pick codes
  look like `2027-R1-NYY`). Clubs value ability, potential, age, position, need,
  contract and whether they are contending or rebuilding. If they say no they
  may counter. CPU clubs also send you offers you can accept, reject or counter.
  The trade deadline falls about two-thirds through the season.
- **Draft** – five rounds after each season, worst record picks first. Scouting
  grades are estimates: your own organisation is scouted more accurately.
- **Free agency** – players with expiring deals hit the market in the offseason;
  unsigned players remain available (cheaper, one-year deals) during the season.
- **Minor leagues** – reserves are your farm system (organisation limit 45).
  Promote, demote, swap, release, or auto-organise the best 26.

### Offseason

After the World Series choose **SIMULATE → Begin the offseason**. Players age
and develop, contracts tick down, veterans retire. Then step through:
**re-signing period → draft → free agency → next season**. Each step can be
handled manually or auto-run by your staff.

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
