import unittest
from collections import Counter

from mlb_manager import franchises as fr


class FranchiseDataTests(unittest.TestCase):
    def test_thirty_unique_franchises(self) -> None:
        self.assertEqual(len(fr.FRANCHISES), 30)
        self.assertEqual(len({f.abbr for f in fr.FRANCHISES}), 30)

    def test_two_leagues_three_divisions_of_five(self) -> None:
        counts = Counter((f.league, f.division) for f in fr.FRANCHISES)
        self.assertEqual(len(counts), 6)
        self.assertTrue(all(n == 5 for n in counts.values()))

    def test_known_division_membership(self) -> None:
        self.assertEqual(
            sorted(fr.teams_in_division("AL", "East")), ["BAL", "BOS", "NYY", "TB", "TOR"]
        )
        self.assertEqual(
            sorted(fr.teams_in_division("NL", "West")), ["ARI", "COL", "LAD", "SD", "SF"]
        )
        self.assertEqual(fr.get_franchise("chc").division_key, "NL Central")

    def test_natural_rivals_pair_every_club_once_across_leagues(self) -> None:
        seen = [abbr for pair in fr.NATURAL_RIVALS for abbr in pair]
        self.assertEqual(sorted(seen), sorted(f.abbr for f in fr.FRANCHISES))
        for al, nl in fr.NATURAL_RIVALS:
            self.assertEqual(fr.get_franchise(al).league, "AL")
            self.assertEqual(fr.get_franchise(nl).league, "NL")
        self.assertEqual(fr.natural_rival("NYM"), "NYY")

    def test_unknown_franchise(self) -> None:
        with self.assertRaises(KeyError):
            fr.get_franchise("XXX")
