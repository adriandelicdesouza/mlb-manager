# MLB Manager

A terminal-based baseball franchise management game. Pick one of the 30 MLB
franchises, manage a roster of fictional generated players, set lineups and
pitching staffs, simulate a 162-game season, and chase a championship through
a 12-team postseason. As general manager you also draft prospects, make
trades, sign free agents and manage contracts under a salary cap, building a
contender over many seasons. Franchises are saved as JSON files.

Python 3.12+. The simulation engine uses only the standard library; the
interactive interface is built with [Textual](https://textual.textualize.io/).
No network access, no database.

## Quick start

```bash
python3 -m venv .venv          # if .venv does not exist yet
.venv/bin/pip install -e .     # editable src-layout install
.venv/bin/mlb-manager          # launch the interactive game
```

`mlb-manager` opens the interactive GUI: a dark, compact terminal app you can
drive with the mouse or the keyboard. The classic numbered menus are still
there with `--cli` (and are used automatically when the program is not
attached to a terminal).

### GUI at a glance

| Key | Screen | What you can do |
| --- | --- | --- |
| `d` | Dashboard | Record, standing, recent results, upcoming games, payroll, injuries, offers, milestones, news |
| `r` | Roster | Filter active / minors / injured / all / pending FAs; open a player profile (click or Enter); promote, demote, IL, activate, rehab, re-sign/extend, release |
| `l` | Lineup & staff | Reorder the batting order, start bench players, set rotation slots and the closer, auto-build |
| `t` | Trade center | Pick a partner, click assets on both sides, see contract implications, submit, take counteroffers, review incoming offers |
| `p` | Draft room | Prospect board with scouting grades and position filter; draft, sim to your pick, auto-draft |
| `f` | Free agents | Market with asking prices; sign at the ask or make an offer |
| `s` | Season center | Standings, statistics, career leaders, awards, milestones, franchise records, postseason, titles, retired players |
| `g` | Game | The dugout: play your next game live, pitch by pitch, or simulate it; last game's feed, box score and managerial report |

Sidebar buttons (or `n` / `w`) simulate a day, week, month or the rest of the
season; in the offseason the same area advances re-signing → draft → free
agency → next season, always with a confirmation. `Ctrl+S` saves, `q` quits
(offering to save first). Tab moves focus between controls.

Other ways to launch:

```bash
.venv/bin/python -m mlb_manager
.venv/bin/mlb-manager --new --team NYY --seed 42     # skip the title menu
.venv/bin/mlb-manager --load saves/nyy_2026.json     # resume a franchise
.venv/bin/mlb-manager --save-dir ~/mlb-saves         # custom save folder
.venv/bin/mlb-manager --simulate-season --seed 7     # headless full season
.venv/bin/mlb-manager --new --team KC --salary-cap 110  # tighter salary cap
.venv/bin/mlb-manager --cli                          # classic numbered-menu interface
```

Saves go to `./saves/` by default (override with `--save-dir` or the
`MLB_MANAGER_SAVE_DIR` environment variable).

## How to play

In the classic CLI (`--cli`), the franchise menu offers:

| # | Screen | What you can do |
| --- | --- | --- |
| 1 | Dashboard | Record, standing, next game, last result, news |
| 2 | Roster | Active 26 and reserves with ratings and season stats |
| 3 | Lineup | Reorder the batting order, swap positions, start bench players, auto-build |
| 4 | Pitching staff | Set the 5-man rotation and closer, see fatigue |
| 5 | Roster moves | Swap an active player with a reserve (max 13 active pitchers) |
| 6 | Player details | Ratings, defensive value, season/postseason/career stats, awards, health, morale |
| 7–11 | Standings, schedule, leaders, box score, bracket | Follow the league |
| 12 | Simulate | 1 / 7 / 30 days, to end of regular season, or through the World Series; start the next season |
| 13 | Save | Write a JSON save |
| 14 | Front Office | Contracts & payroll, Trade Center, Free Agency, Draft, Minor League / Prospects, league transactions |
| 15 | Injuries & injured list | Injury report; IL placement/activation, rehab assignments, clubhouse morale |
| 16 | Season center | Milestones, awards, season summary, franchise history, career leaderboards, franchise records, postseason leaders, retired players, career milestones, postseason and championship history |

### During the season

- **Injuries** happen in games. An injured starter sits automatically and the
  best healthy bench player takes his spot until he returns. Put longer
  injuries on the IL to call up a replacement; activate players once healthy.
- **Development** continues all year: young players with playing time improve
  monthly; veterans may slip.
- **Awards** are announced when the regular season ends; milestones (40-homer
  seasons, no-hitters, clinches...) appear in the news and the Season center.
  The ALCS, NLCS and World Series each name an MVP.
- **Careers** – every player's statistics are kept season by season and club
  by club (they follow him through trades) and survive retirement. Postseason
  statistics are tracked separately and never mixed with the regular season.
- **Injury management** – some players are durable, some injury-prone, and a
  recent serious injury raises the risk. About ten days before an IL player
  returns you can send him on a rehab assignment in the minors; players
  activated straight off the IL after a long injury are rusty for a few days.
- **Morale** – players react to playing time, their role, their contract and
  how the team is doing. Very happy players perform slightly better and give a
  hometown discount; unhappy ones slump a little and may refuse to re-sign.

### Managing a game from the dugout

Open the **Game** tab (`g`) and choose **Play live** (classic CLI:
SIMULATE → *Manage your next game live*). Pick how much you want to manage:
**Assisted** (the CPU handles routine calls and stops at key moments with a
recommendation and its reasoning), **Full manual** or **Automatic**. Each
category (pitching, hitting, baserunning, defense, substitutions) can be set
separately at any time, e.g. manual pitching with automatic hitting.

The game is simulated as you go, never in advance: nothing you decide can
change a pitch that has already been thrown.

- **Progress** by pitch `1`, play `2`, batter `3`, half-inning `4`, inning `5`
  or to the final `6`; `a` starts/pauses auto-play (choose its unit); `space`
  throws one pitch. `y` / `x` accept or decline an assisted-mode suggestion.
- **Controls** (right-hand table, Enter or click) only show what applies now:
  - *between pitches* – pitch type (from the pitcher's real repertoire) and
    location, pitching effort, sequencing and approach, intentional ball,
    pitchout; take, bunt, hit-and-run, steal / delayed steal, leads,
    send or hold the runners on the next ball in play;
  - *between batters* – batter approach, swing aggression, discipline and
    contact effort (remembered per hitter), pitcher approach vs this hitter,
    pinch hitter / runner, pitching change (three-batter minimum applies),
    intentional walk, infield / outfield depth, legal shading, defensive
    aggressiveness;
  - *between innings* – team hitting plan, workload preference (quick hook /
    long leash), defensive replacements; the batting order itself is fixed by
    rule and the screen says so.
- **Reviews**: Lineups & defense, Bullpen (fatigue, role, availability),
  Scouting (opponent hitters' pull tendencies and speed, their pitcher's
  repertoire, who is due up with expected OBP/K/HR, your players' fatigue,
  morale and health), Decisions, Plan, Box score.
- Instructions change probabilities, never guarantee results: a slider is
  thrown with the pitcher's own slider grade and can still hang; max effort
  adds velocity and bite but tires him faster; a power approach trades
  strikeouts for extra-base hits according to the hitter's power.
- Every decision is logged with the situation, the old and new instruction,
  who made it (you, the CPU, an accepted suggestion) and the play that
  followed. The post-game **managerial report** grades decisions *ex ante*
  (break-even steal rates, run expectancy, matchup expectations), not by
  whether they worked.
- You can save mid-game; loading resumes exactly where you left off.
  Simulating days with a game in progress lets the CPU finish it with your
  current instructions. Roster moves wait until the game is over.

### Front office

- **Contracts & cap** – every player has a salary and years remaining. Free-agent
  signings and trades must fit under the cap (default $125M) unless they reduce
  payroll; re-signing your own players may exceed it. Released players' salary
  stays on this season's books as dead money.
- **Trades** – combine up to five players and/or draft picks per side (pick codes
  look like `2027-R1-NYY`). Each club judges a deal by its own needs: projected
  ability and growth (as *its* scouts see it), age, position, contract and years
  of control, injuries, and whether it is contending or rebuilding. You see the
  contract implications of every proposal and the club's reaction (accept,
  close, far off) with hints, but not its internal numbers. Near misses get a
  proportional counteroffer; lowball offers are simply refused, and repeating a
  rejected offer gets you nowhere. CPU clubs trade among themselves to fix real
  needs and send you offers with their motive. The trade deadline falls about
  two-thirds through the season.
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
- `docs/in-game-management.md` – live game architecture, pitch model and calibration
