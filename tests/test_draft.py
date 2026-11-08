import random
import unittest
from collections import Counter

from mlb_manager import contracts
from mlb_manager import draft as draft_mod
from mlb_manager.generation import PlayerFactory
from mlb_manager.league import PHASE_DRAFT, PHASE_FREE_AGENCY

from tests.helpers import league_at_offseason


class DraftClassTests(unittest.TestCase):
    def test_class_positional_and_age_distribution(self) -> None:
        prospects = draft_mod.generate_class(random.Random(4), PlayerFactory(random.Random(5)))
        self.assertEqual(len(prospects), draft_mod.CLASS_SIZE)
        pitchers = sum(p.is_pitcher for p in prospects) / len(prospects)
        self.assertTrue(0.35 <= pitchers <= 0.62, pitchers)
        positions = Counter(p.position for p in prospects)
        for pos in ("C", "SS", "CF"):
            self.assertGreater(positions[pos], 5)
        self.assertEqual({p.age for p in prospects} - {18, 21, 22}, set())
        for p in prospects:
            self.assertGreaterEqual(p.potential, p.overall)
            self.assertIsNone(p.team)
        self.assertGreater(max(p.potential for p in prospects), 65)

    def test_draft_order_worst_first_playoff_clubs_last(self) -> None:
        teams = {a: None for a in ("A", "B", "C", "D")}
        order = draft_mod.draft_order(teams, ["A", "B", "C", "D"], {"A": 2, "C": 3})
        self.assertEqual(order, ["D", "B", "A", "C"])


class DraftFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.league = league_at_offseason(seed=51, user="KC")
        self.league.begin_offseason()
        # Trade KC's first-rounder to SEA before the draft.
        pick = draft_mod.find_pick(self.league.draft_picks, self.league.draft_year, 1, "KC")
        pick.owner = "SEA"
        self.league.advance_offseason()  # re-signing -> draft

    def test_draft_created_with_order_and_pool(self) -> None:
        lg = self.league
        self.assertEqual(lg.phase, PHASE_DRAFT)
        self.assertEqual(len(lg.draft.order), 30)
        self.assertEqual(len(lg.draft.pool), draft_mod.CLASS_SIZE)
        self.assertIsNotNone(lg.draft_on_clock())

    def test_user_picks_and_traded_pick_respected(self) -> None:
        lg = self.league
        made = lg.draft_until_user()
        clock = lg.draft_on_clock()
        self.assertEqual(clock[2], "KC")
        self.assertEqual(clock[1], 2)  # KC's first-rounder belongs to SEA
        self.assertTrue(any(s.team == "SEA" and s.original == "KC" for s in made))
        with self.assertRaises(ValueError):
            lg.draft_select(999999)
        choice = lg.draft.pool[0]
        sel = lg.draft_select(choice)
        player = lg.players[choice]
        self.assertEqual(player.team, "KC")
        self.assertIn(choice, lg.user.reserves)
        self.assertEqual(player.contract_years, contracts.ROOKIE_CONTRACT_YEARS)
        self.assertEqual((player.draft_round, player.draft_pick), (2, sel.overall_pick))

    def test_complete_draft(self) -> None:
        lg = self.league
        year = lg.draft.year
        lg.advance_offseason()  # auto-completes the draft
        self.assertEqual(lg.phase, PHASE_FREE_AGENCY)
        self.assertEqual(len(lg.draft.selections), 30 * draft_mod.DRAFT_ROUNDS)
        per_team = Counter(s.team for s in lg.draft.selections)
        self.assertEqual(per_team["SEA"], 6)
        self.assertEqual(per_team["KC"], 4)
        self.assertFalse(any(p.year == year for p in lg.draft_picks))
        self.assertEqual(lg.draft.pool, [])
        ids = [s.player_id for s in lg.draft.selections]
        self.assertEqual(len(ids), len(set(ids)))
        # Top of the draft goes to better prospects than the end of it.
        top = [lg.players[s.player_id].potential for s in lg.draft.selections[:30]]
        late = [lg.players[s.player_id].potential for s in lg.draft.selections[-30:]]
        self.assertGreater(sum(top), sum(late))
