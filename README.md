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
.venv/bin/mlb-manager --load saves/my_franchise.json
.venv/bin/mlb-manager --simulate-season --seed 7     # headless full season
```

## Running tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```

## Documentation

- `CLAUDE.md` – engineering guide and conventions for contributors/agents
- `PROJECT_STATE.md` – what is implemented and verified right now
- `ROADMAP.md` – milestones, completed and planned
