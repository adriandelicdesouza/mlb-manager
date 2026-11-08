import unittest

from mlb_manager.playoffs import HOME_PATTERNS, Playoffs, Series, SeriesGame

SEEDS = {
    "AL": ["NYY", "HOU", "CLE", "BAL", "SEA", "TEX"],
    "NL": ["LAD", "PHI", "MIL", "ATL", "SD", "ARI"],
}


def win_series(series: Series, winner: str) -> None:
    while not series.is_over:
        away, home = series.next_matchup()
        if winner == home:
            series.record(SeriesGame(away, home, 1, 2, 9))
        else:
            series.record(SeriesGame(away, home, 2, 1, 9))


class PlayoffTests(unittest.TestCase):
    def test_wild_card_matchups(self) -> None:
        p = Playoffs.create(SEEDS)
        pairs = {(s.series_id, s.high, s.low, s.best_of) for s in p.current_round}
        self.assertIn(("AL-WC1", "CLE", "TEX", 3), pairs)
        self.assertIn(("AL-WC2", "BAL", "SEA", 3), pairs)
        self.assertIn(("NL-WC1", "MIL", "ARI", 3), pairs)

    def test_home_patterns(self) -> None:
        s = Series("X", 2, "AL", "NYY", "HOU", 7)
        homes = []
        for _ in range(7):
            away, home = s.next_matchup()
            homes.append(home)
            # alternate winners so the series goes 7 games
            winner = "NYY" if len(s.games) % 2 == 0 else "HOU"
            s.record(SeriesGame(away, home, *(1, 2) if winner == home else (2, 1), 9))
        self.assertEqual(homes, ["NYY", "NYY", "HOU", "HOU", "HOU", "NYY", "NYY"])
        self.assertEqual(s.winner, "NYY")
        self.assertEqual(len(HOME_PATTERNS[5]), 5)

    def test_full_bracket_progression(self) -> None:
        p = Playoffs.create(SEEDS)
        # Lower seeds win every wild card series.
        for s in p.current_round:
            win_series(s, s.low)
        self.assertTrue(p.advance_round(lambda a, b: a))
        ds = {s.series_id: (s.high, s.low) for s in p.current_round}
        self.assertEqual(ds["AL-DS1"], ("NYY", "SEA"))  # 1 v winner of 4/5
        self.assertEqual(ds["AL-DS2"], ("HOU", "TEX"))  # 2 v winner of 3/6
        for s in p.current_round:
            win_series(s, s.low)
        p.advance_round(lambda a, b: a)
        cs = {s.series_id: (s.high, s.low) for s in p.current_round}
        self.assertEqual(cs["AL-CS"], ("SEA", "TEX"))  # better seed hosts
        for s in p.current_round:
            win_series(s, s.high)
        p.advance_round(lambda a, b: b)  # NL club had the better record
        ws = p.current_round[0]
        self.assertEqual((ws.high, ws.low), ("SD", "SEA"))
        self.assertFalse(p.is_complete)
        win_series(ws, "SEA")
        self.assertEqual(p.champion, "SEA")
        self.assertFalse(p.advance_round(lambda a, b: a))
        self.assertIn("NYY", p.eliminated())

    def test_cannot_record_after_series_over(self) -> None:
        s = Series("X", 0, "AL", "NYY", "HOU", 3)
        win_series(s, "NYY")
        with self.assertRaises(ValueError):
            s.record(SeriesGame("HOU", "NYY", 0, 1, 9))

    def test_round_trip(self) -> None:
        p = Playoffs.create(SEEDS)
        win_series(p.current_round[0], "TEX")
        again = Playoffs.from_dict(p.to_dict())
        self.assertEqual(again.to_dict(), p.to_dict())
        self.assertEqual(again.current_round[0].winner, "TEX")
