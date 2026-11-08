"""Developer tool: simulate several seasons and print league-health metrics.

Usage: .venv/bin/python scripts/league_health.py [seasons] [seed]
"""

import json
import statistics
import sys
import time

from mlb_manager import contracts, finance, roster
from mlb_manager.league import League


def main() -> None:
    seasons = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 11
    lg = League.create(seed, "NYY")
    for _ in range(seasons):
        start = time.time()
        lg.sim_to_completion()
        active = [lg.players[pid] for t in lg.teams.values() for pid in t.active]
        wins = sorted(r.wins for r in lg.records().values())
        pays = sorted(contracts.payroll(t, lg.players) for t in lg.teams.values())
        runs = sum(p.batting.r for p in lg.players.values()) / 4860
        trades = sum(x.startswith(str(lg.year)) and "TRADE" in x for x in lg.transactions)
        injuries = sum(1 for p in lg.players.values() for h in p.injury_history if h["y"] == lg.year)
        moods = [lg.players[pid].morale for t in lg.teams.values() for pid in t.all_players]
        save_mb = len(json.dumps(lg.to_dict(), separators=(",", ":"))) / 1e6
        print(
            f"{lg.year} activeOVR {statistics.mean(p.overall for p in active):.1f} "
            f"age {statistics.mean(p.age for p in active):.1f} R/G {runs:.2f} "
            f"wins {wins[0]}-{wins[-1]} payroll {pays[0]:.0f}/{pays[15]:.0f}/{pays[-1]:.0f} "
            f"players {len(lg.players)} trades {trades} champion {lg.playoffs.champion} "
            f"injuries {injuries} morale {statistics.mean(moods):.0f} (<30: {sum(m < 30 for m in moods)}) "
            f"retired {len(lg.retired)} save {save_mb:.1f}MB ({time.time() - start:.1f}s)"
        )
        rows = [r for r in lg.finance.state.ledger if r["year"] == lg.year]
        payors = sorted((r["team"], round(r["cbt_tax"], 1), r["cbt_offence"]) for r in rows if r["cbt_tax"] > 0)
        top = sorted(rows, key=lambda r: -r["payroll"])[:3]
        print(f"     CBT payors {payors}  top payrolls {[(r['team'], round(r['payroll'])) for r in top]}  "
              f"rules {finance.rules(lg, lg.year).cbt_threshold:.0f}M threshold")
        for team in lg.teams.values():
            problems = roster.validate(team, lg.players)
            assert not problems, (team.abbr, problems)
        lg.start_new_season()


if __name__ == "__main__":
    main()
