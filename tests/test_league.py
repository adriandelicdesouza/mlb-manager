import unittest

from mlb_manager.league import (
    PHASE_COMPLETE,
    PHASE_POSTSEASON,
    PHASE_REGULAR,
    STARTER_READY_FATIGUE,
    League,
)
from mlb_manager.schedule import GAMES_PER_TEAM


class LeagueSeasonTests(unittest.TestCase):
    """Runs one full season (regular + postseason) shared by all tests."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.league = League.create(seed=2024, user_team="CHC")
        cls.first_day = cls.league.advance_day()
        cls.league.sim_regular_season()
        cls.after_regular = cls.league.records()
        cls.league.sim_to_completion()

    def test_first_day_plays_scheduled_games(self) -> None:
        self.assertEqual(self.first_day.day, 0)
        self.assertGreater(len(self.first_day.games), 0)

    def test_regular_season_complete(self) -> None:
        for abbr, rec in self.after_regular.items():
            self.assertEqual(rec.games, GAMES_PER_TEAM, abbr)
        total_w = sum(r.wins for r in self.after_regular.values())
        self.assertEqual(total_w, 30 * GAMES_PER_TEAM // 2)

    def test_postseason_and_champion(self) -> None:
        lg = self.league
        self.assertEqual(lg.phase, PHASE_COMPLETE)
        self.assertIsNotNone(lg.playoffs)
        self.assertEqual(len(lg.playoffs.rounds), 4)
        champion = lg.playoffs.champion
        self.assertIn(champion, lg.playoffs.seeds["AL"] + lg.playoffs.seeds["NL"])
        self.assertEqual(lg.history[-1]["champion"], champion)
        self.assertEqual(lg.postseason_finish(champion), "Won World Series")

    def test_postseason_does_not_change_regular_season_stats(self) -> None:
        total_games = sum(p.pitching.gs for p in self.league.players.values())
        self.assertEqual(total_games, 30 * GAMES_PER_TEAM)

    def test_season_stats_accumulated(self) -> None:
        hr_leader, hr = self.league.batting_leaders("hr", 1)[0]
        self.assertGreater(hr, 15)
        era_leaders = self.league.pitching_leaders("era", 5)
        self.assertEqual(len(era_leaders), 5)
        self.assertLessEqual(era_leaders[0][1], era_leaders[-1][1])
        total_w = sum(p.pitching.w for p in self.league.players.values())
        self.assertEqual(total_w, 30 * GAMES_PER_TEAM // 2)

    def test_user_last_game_recorded(self) -> None:
        g = self.league.last_user_game
        self.assertIsNotNone(g)
        self.assertIn("CHC", (g.away, g.home))

    def test_advance_after_completion_is_noop(self) -> None:
        report = self.league.advance_day()
        self.assertEqual(report.games, [])


class LeagueMechanicsTests(unittest.TestCase):
    def test_deterministic_replay(self) -> None:
        a = League.create(seed=5, user_team="LAD")
        b = League.create(seed=5, user_team="LAD")
        a.sim_days(20)
        b.sim_days(20)
        self.assertEqual(a.to_dict(), b.to_dict())

    def test_rotation_cycles_and_rests_starters(self) -> None:
        lg = League.create(seed=8, user_team="SEA")
        starters = []
        for _ in range(15):
            report = lg.advance_day()
            for played in report.games:
                box = played.result.box_for("SEA") if "SEA" in (
                    played.result.away, played.result.home) else None
                if box:
                    starters.append(next(pid for pid, s in box.pitching.items() if s.gs))
        self.assertGreaterEqual(len(set(starters)), 5)
        for a, b in zip(starters, starters[1:]):
            self.assertNotEqual(a, b)
        self.assertGreater(STARTER_READY_FATIGUE, 0)

    def test_phase_transition_to_postseason(self) -> None:
        lg = League.create(seed=3, user_team="ATL")
        self.assertEqual(lg.phase, PHASE_REGULAR)
        lg.sim_regular_season()
        self.assertEqual(lg.phase, PHASE_POSTSEASON)
        self.assertEqual(len(lg.playoffs.current_round), 4)
        self.assertTrue(any("playoff seeds" in n for n in lg.news))

    def test_new_season(self) -> None:
        lg = League.create(seed=4, user_team="MIN")
        with self.assertRaises(RuntimeError):
            lg.start_new_season()
        lg.sim_to_completion()
        player = lg.players[1]
        age = player.age
        lg.start_new_season()
        self.assertEqual(lg.year, 2027)
        self.assertEqual(lg.phase, PHASE_REGULAR)
        self.assertEqual(player.age, age + 1)
        self.assertEqual(player.batting.pa + player.pitching.g, 0)
        self.assertEqual(len(lg.history), 1)
        lg.advance_day()
        self.assertTrue(any(g.played for g in lg.schedule))

    def test_invalid_user_lineup_repaired_before_game(self) -> None:
        lg = League.create(seed=6, user_team="TEX")
        lg.user.lineup = lg.user.lineup[:5]
        lg.sim_days(3)
        self.assertEqual(len(lg.user.lineup), 9)
        self.assertTrue(any("rebuilt automatically" in n for n in lg.news))
