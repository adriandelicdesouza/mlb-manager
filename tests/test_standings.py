import unittest

from mlb_manager.generation import generate_rosters
from mlb_manager.schedule import ScheduledGame
from mlb_manager.standings import (
    compute_records,
    division_standings,
    games_back,
    playoff_seeds,
    rank,
)


def game(gid, away, home, ar, hr):
    return ScheduledGame(gid, gid, away, home, ar, hr, 9)


class StandingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.teams, _, _ = generate_rosters(seed=1)

    def test_records_and_derived_values(self) -> None:
        games = [
            game(1, "NYY", "BOS", 5, 3),
            game(2, "NYY", "BOS", 1, 2),
            game(3, "BOS", "NYY", 0, 4),
            ScheduledGame(4, 4, "BOS", "NYY"),  # unplayed
        ]
        rec = compute_records(games, self.teams)
        nyy, bos = rec["NYY"], rec["BOS"]
        self.assertEqual((nyy.wins, nyy.losses), (2, 1))
        self.assertEqual((bos.wins, bos.losses), (1, 2))
        self.assertEqual((nyy.away_wins, nyy.away_losses, nyy.home_wins), (1, 1, 1))
        self.assertEqual(nyy.run_diff, 5)  # 10 scored, 5 allowed
        self.assertEqual(nyy.streak, "W1")
        self.assertEqual(nyy.last10, "2-1")
        self.assertEqual((nyy.division_wins, nyy.division_losses), (2, 1))
        self.assertEqual(games_back(nyy, bos), 1.0)

    def test_head_to_head_breaks_ties(self) -> None:
        games = [
            game(1, "TB", "TOR", 3, 2),  # TB beats TOR
            game(2, "TOR", "BAL", 9, 0),  # TOR wins elsewhere
            game(3, "BAL", "TB", 9, 0),  # TB loses elsewhere
        ]
        rec = compute_records(games, self.teams)
        self.assertEqual(rec["TB"].pct, rec["TOR"].pct)
        self.assertLess(rank(["TOR", "TB"], rec).index("TB"), 1)

    def test_playoff_seeds_division_winners_first(self) -> None:
        # Give each AL club a distinct number of wins over NL opponents.
        al = [t for t in self.teams.values() if t.league == "AL"]
        nl = [t.abbr for t in self.teams.values() if t.league == "NL"]
        games, gid = [], 0
        for i, team in enumerate(al):
            for _ in range(i + 1):
                gid += 1
                games.append(game(gid, nl[0], team.abbr, 0, 1))
            for _ in range(15 - i):
                gid += 1
                games.append(game(gid, nl[1], team.abbr, 1, 0))
        rec = compute_records(games, self.teams)
        seeds = playoff_seeds(self.teams, rec, "AL")
        self.assertEqual(len(seeds), 6)
        leaders = {division_standings(self.teams, rec, "AL", d)[0] for d in ("East", "Central", "West")}
        self.assertEqual(set(seeds[:3]), leaders)
        # Division winners are seeded by record among themselves.
        self.assertEqual(seeds[:3], rank(seeds[:3], rec))
        # Wild cards are the best remaining records.
        others = [t.abbr for t in al if t.abbr not in leaders]
        self.assertEqual(seeds[3:], rank(others, rec)[:3])
