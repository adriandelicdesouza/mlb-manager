import copy
import random
import unittest

from mlb_manager.game import simulate_game
from mlb_manager.generation import generate_rosters


def play(teams, players, seed, away="NYY", home="BOS", spot=0):
    return simulate_game(
        teams[away], teams[home], players, random.Random(seed),
        teams[away].rotation[spot], teams[home].rotation[spot],
    )


class GameSimulationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.teams, cls.players, _ = generate_rosters(seed=77)
        cls.results = [play(cls.teams, cls.players, s, spot=s % 5) for s in range(150)]

    def test_deterministic_with_same_seed(self) -> None:
        a = play(self.teams, self.players, 42)
        b = play(self.teams, self.players, 42)
        self.assertEqual((a.away_runs, a.home_runs, a.log), (b.away_runs, b.home_runs, b.log))

    def test_games_never_end_tied_and_reach_nine_innings(self) -> None:
        for r in self.results:
            self.assertNotEqual(r.away_runs, r.home_runs)
            self.assertGreaterEqual(r.innings, 9)
            self.assertEqual(len(r.away_box.line), r.innings)

    def test_box_score_consistency(self) -> None:
        for r in self.results:
            for batting, pitching in ((r.away_box, r.home_box), (r.home_box, r.away_box)):
                runs = sum(s.r for s in batting.batting.values())
                self.assertEqual(runs, batting.runs)
                self.assertEqual(sum(s.r for s in pitching.pitching.values()), batting.runs)
                hits = sum(s.h for s in batting.batting.values())
                self.assertEqual(hits, batting.hits)
                self.assertEqual(sum(s.h for s in pitching.pitching.values()), hits)
                self.assertLessEqual(sum(s.rbi for s in batting.batting.values()), runs)
                for s in pitching.pitching.values():
                    self.assertLessEqual(s.er, s.r)
            # Home pitchers always record every out of every top half.
            self.assertEqual(sum(s.outs for s in r.home_box.pitching.values()), 3 * r.innings)

    def test_decisions_assigned_to_correct_teams(self) -> None:
        for r in self.results:
            win_box, lose_box = r.box_for(r.winner), r.box_for(r.loser)
            self.assertIn(r.winning_pitcher, win_box.pitching)
            self.assertIn(r.losing_pitcher, lose_box.pitching)
            self.assertEqual(sum(s.w for s in win_box.pitching.values()), 1)
            self.assertEqual(sum(s.l for s in lose_box.pitching.values()), 1)
            if r.save_pitcher is not None:
                self.assertIn(r.save_pitcher, win_box.pitching)
                self.assertNotEqual(r.save_pitcher, r.winning_pitcher)

    def test_starters_and_relievers_used(self) -> None:
        for r in self.results:
            for box in (r.away_box, r.home_box):
                starters = [pid for pid, s in box.pitching.items() if s.gs]
                self.assertEqual(len(starters), 1)
        self.assertTrue(any(len(r.home_box.pitching) > 1 for r in self.results))

    def test_simulation_does_not_mutate_players(self) -> None:
        before = {pid: p.to_dict() for pid, p in self.players.items()}
        play(self.teams, self.players, 5)
        self.assertEqual(before, {pid: p.to_dict() for pid, p in self.players.items()})

    def test_better_team_wins_more(self) -> None:
        players = copy.deepcopy(self.players)
        for pid in self.teams["NYY"].active:
            ratings = players[pid].ratings
            for attr in ratings:
                if attr != "stamina":
                    ratings[attr] = min(99, ratings[attr] + 20)
        wins = sum(
            play(self.teams, players, s, spot=s % 5).winner == "NYY" for s in range(200)
        )
        self.assertGreater(wins, 140)

    def test_league_like_scoring(self) -> None:
        runs = [r.away_runs + r.home_runs for r in self.results]
        mean = sum(runs) / len(runs) / 2
        self.assertTrue(3.0 <= mean <= 6.0, mean)
