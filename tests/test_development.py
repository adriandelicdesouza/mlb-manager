import random
import statistics
import unittest

from mlb_manager import development
from mlb_manager.generation import PlayerFactory


def players(age: int, n: int = 300, development_type: str = "normal"):
    factory = PlayerFactory(random.Random(age))
    out = []
    for _ in range(n):
        p = factory.create("SS", talent=-5, age_mu=age, age_sigma=0)
        p.age = age
        p.potential = p.overall + 15 if age < 25 else p.overall
        p.development = development_type
        out.append(p)
    return out


class DevelopmentTests(unittest.TestCase):
    def test_young_players_improve_toward_potential(self) -> None:
        rng = random.Random(1)
        group = players(20)
        deltas = [development.develop(p, rng) for p in group]
        self.assertGreater(statistics.mean(deltas), 2)
        for p in group:
            self.assertGreaterEqual(p.potential, p.overall)

    def test_fast_developers_grow_faster(self) -> None:
        fast = statistics.mean(development.develop(p, random.Random(p.id)) for p in players(20, development_type="fast"))
        slow = statistics.mean(development.develop(p, random.Random(p.id)) for p in players(20, development_type="slow"))
        self.assertGreater(fast, slow)

    def test_older_players_decline(self) -> None:
        rng = random.Random(2)
        deltas = [development.develop(p, rng) for p in players(35)]
        self.assertLess(statistics.mean(deltas), -2)

    def test_retirement(self) -> None:
        rng = random.Random(3)
        old = players(41, n=10)
        self.assertTrue(all(development.should_retire(p, rng) for p in old))
        young = players(24, n=50)
        self.assertFalse(any(development.should_retire(p, rng) for p in young))

    def test_scouting_is_noisy_but_better_for_own_players(self) -> None:
        group = players(19, n=400)
        own_err, other_err = [], []
        for p in group:
            p.team = "NYY"
            own_err.append(abs(development.scouted_potential(p, "NYY", 1, 2026) - p.potential))
            other_err.append(abs(development.scouted_potential(p, "BOS", 1, 2026) - p.potential))
        self.assertGreater(statistics.mean(other_err), statistics.mean(own_err))
        self.assertGreater(statistics.mean(other_err), 1.0)
        p = group[0]
        self.assertEqual(development.scouted_potential(p, "BOS", 1, 2026),
                         development.scouted_potential(p, "BOS", 1, 2026))

    def test_veterans_are_scouted_exactly(self) -> None:
        for p in players(30, n=20):
            self.assertEqual(development.scouted_potential(p, "BOS", 1, 2026), max(p.potential, p.overall))

    def test_grades(self) -> None:
        self.assertEqual(development.potential_grade(80), "A+")
        self.assertEqual(development.potential_grade(61), "B")
        self.assertEqual(development.potential_grade(30), "F")
