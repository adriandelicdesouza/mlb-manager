import random
import statistics
import unittest

from mlb_manager import ratings as rt
from mlb_manager import roster
from mlb_manager.generation import (
    ACTIVE_TEMPLATE,
    RESERVE_TEMPLATE,
    generate_ratings,
    generate_rosters,
)


class GenerationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.teams, cls.players, cls.next_id = generate_rosters(seed=123)

    def test_league_size(self) -> None:
        self.assertEqual(len(self.teams), 30)
        per_team = len(ACTIVE_TEMPLATE) + len(RESERVE_TEMPLATE)
        self.assertEqual(len(self.players), 30 * per_team)
        self.assertEqual(self.next_id, max(self.players) + 1)

    def test_rosters_have_26_active_and_reserves(self) -> None:
        for team in self.teams.values():
            self.assertEqual(len(team.active), roster.MAX_ACTIVE)
            self.assertEqual(len(team.reserves), len(RESERVE_TEMPLATE))
            self.assertFalse(set(team.active) & set(team.reserves))
            for pid in team.all_players:
                self.assertEqual(self.players[pid].team, team.abbr)

    def test_generated_teams_are_valid(self) -> None:
        for team in self.teams.values():
            self.assertEqual(roster.validate(team, self.players), [], team.abbr)

    def test_deterministic_for_same_seed(self) -> None:
        _, again, _ = generate_rosters(seed=123)
        self.assertEqual(
            [p.to_dict() for p in self.players.values()], [p.to_dict() for p in again.values()]
        )
        _, other, _ = generate_rosters(seed=124)
        self.assertNotEqual(self.players[1].to_dict(), other[1].to_dict())

    def test_ratings_in_range(self) -> None:
        for player in self.players.values():
            for value in player.ratings.values():
                self.assertTrue(rt.RATING_MIN <= value <= rt.RATING_MAX)

    def test_position_specific_attributes(self) -> None:
        for player in self.players.values():
            if player.is_pitcher:
                for attr in rt.PITCHING_ATTRS:
                    self.assertIn(attr, player.ratings)
            else:
                self.assertNotIn("stuff", player.ratings)
            if player.position == "C":
                self.assertIn("framing", player.ratings)
            elif not player.is_pitcher:
                self.assertNotIn("framing", player.ratings)

    def test_position_profiles_shape_attributes(self) -> None:
        rng = random.Random(5)
        sp = [generate_ratings(rng, "SP", 0)["stamina"] for _ in range(200)]
        rp = [generate_ratings(rng, "RP", 0)["stamina"] for _ in range(200)]
        self.assertGreater(statistics.mean(sp), statistics.mean(rp) + 25)
        cf = [generate_ratings(rng, "CF", 0)["speed"] for _ in range(200)]
        c = [generate_ratings(rng, "C", 0)["speed"] for _ in range(200)]
        self.assertGreater(statistics.mean(cf), statistics.mean(c) + 15)
        pitcher_contact = [generate_ratings(rng, "SP", 0)["contact"] for _ in range(200)]
        self.assertLess(statistics.mean(pitcher_contact), 25)

    def test_league_average_near_fifty(self) -> None:
        mean = statistics.mean(p.overall for p in self.players.values())
        self.assertTrue(44 <= mean <= 54, mean)
