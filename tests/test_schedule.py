import unittest
from collections import Counter, defaultdict

from mlb_manager.franchises import FRANCHISES, get_franchise, natural_rival
from mlb_manager.schedule import GAMES_PER_TEAM, generate_schedule, matchup_counts


class ScheduleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.games = generate_schedule(11)

    def test_matchup_counts_sum_to_162(self) -> None:
        totals: Counter[str] = Counter()
        for (a, b), n in matchup_counts().items():
            totals[a] += n
            totals[b] += n
        self.assertEqual(set(totals.values()), {GAMES_PER_TEAM})

    def test_matchup_format(self) -> None:
        counts = matchup_counts()
        self.assertEqual(counts[("BOS", "NYY")], 13)
        self.assertEqual(counts[("NYM", "NYY")], 4)
        for f in FRANCHISES:
            other_league = [
                n for (a, b), n in counts.items()
                if f.abbr in (a, b)
                and get_franchise(a).league != get_franchise(b).league
            ]
            self.assertEqual(sum(other_league), 46)
            self.assertEqual(
                counts[tuple(sorted((f.abbr, natural_rival(f.abbr))))], 4
            )

    def test_every_team_plays_162_with_balanced_home_games(self) -> None:
        self.assertEqual(len(self.games), 30 * GAMES_PER_TEAM // 2)
        total: Counter[str] = Counter()
        home: Counter[str] = Counter()
        for g in self.games:
            total[g.away] += 1
            total[g.home] += 1
            home[g.home] += 1
        self.assertEqual(set(total.values()), {GAMES_PER_TEAM})
        self.assertTrue(all(78 <= n <= 84 for n in home.values()), home)

    def test_no_team_plays_twice_in_a_day(self) -> None:
        by_day: dict[int, list[str]] = defaultdict(list)
        for g in self.games:
            by_day[g.day].extend((g.away, g.home))
        for day, clubs in by_day.items():
            self.assertEqual(len(clubs), len(set(clubs)), day)

    def test_ids_unique_and_sorted_by_day(self) -> None:
        self.assertEqual([g.game_id for g in self.games], list(range(1, len(self.games) + 1)))
        days = [g.day for g in self.games]
        self.assertEqual(days, sorted(days))

    def test_deterministic(self) -> None:
        again = generate_schedule(11)
        self.assertEqual([g.to_dict() for g in again], [g.to_dict() for g in self.games])
        self.assertNotEqual(
            [g.to_dict() for g in generate_schedule(12)], [g.to_dict() for g in self.games]
        )
