import unittest

from mlb_manager import ratings as rt
from mlb_manager import roster
from mlb_manager.generation import generate_rosters


class RosterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.teams, self.players, _ = generate_rosters(seed=9)
        self.team = self.teams["SEA"]

    def test_auto_lineup_covers_all_positions(self) -> None:
        positions = sorted(slot.position for slot in self.team.lineup)
        self.assertEqual(positions, sorted(rt.LINEUP_POSITIONS))
        for slot in self.team.lineup:
            self.assertFalse(self.players[slot.player_id].is_pitcher)

    def test_rotation_prefers_starters(self) -> None:
        self.assertEqual(len(self.team.rotation), roster.ROTATION_SIZE)
        starters = sum(self.players[pid].position == "SP" for pid in self.team.rotation)
        self.assertGreaterEqual(starters, 4)
        self.assertNotIn(self.team.closer, self.team.rotation)

    def test_swap_active_reserve_keeps_roster_valid(self) -> None:
        starter_id = self.team.lineup[0].player_id
        reserve_hitter = next(
            pid for pid in self.team.reserves if not self.players[pid].is_pitcher
        )
        notes = roster.swap_active_reserve(self.team, self.players, starter_id, reserve_hitter)
        self.assertIn(reserve_hitter, self.team.active)
        self.assertIn(starter_id, self.team.reserves)
        self.assertEqual(len(self.team.active), roster.MAX_ACTIVE)
        self.assertTrue(notes)
        self.assertEqual(roster.validate(self.team, self.players), [])

    def test_swap_rotation_pitcher(self) -> None:
        sp = self.team.rotation[0]
        reserve_sp = next(pid for pid in self.team.reserves if self.players[pid].is_pitcher)
        roster.swap_active_reserve(self.team, self.players, sp, reserve_sp)
        self.assertNotIn(sp, self.team.rotation)
        self.assertEqual(roster.validate(self.team, self.players), [])

    def test_pitcher_limit_enforced(self) -> None:
        hitter = roster.bench(self.team, self.players)[0].id
        reserve_pitcher = next(pid for pid in self.team.reserves if self.players[pid].is_pitcher)
        with self.assertRaises(roster.RosterError):
            roster.swap_active_reserve(self.team, self.players, hitter, reserve_pitcher)

    def test_invalid_ids_rejected(self) -> None:
        with self.assertRaises(roster.RosterError):
            roster.swap_active_reserve(self.team, self.players, self.team.reserves[0], self.team.reserves[1])

    def test_lineup_edits(self) -> None:
        first, second = self.team.lineup[0], self.team.lineup[1]
        roster.swap_batting_order(self.team, 0, 1)
        self.assertEqual(self.team.lineup[0], second)
        roster.swap_positions(self.team, 0, 1)
        self.assertEqual(self.team.lineup[0].position, first.position)
        bench_id = roster.bench(self.team, self.players)[0].id
        roster.substitute_starter(self.team, self.players, 4, bench_id)
        self.assertEqual(self.team.lineup[4].player_id, bench_id)
        self.assertEqual(roster.validate(self.team, self.players), [])

    def test_validate_and_repair_detect_broken_lineup(self) -> None:
        pitcher = self.team.rotation[0]
        self.team.lineup[0].player_id = pitcher
        self.assertTrue(roster.validate(self.team, self.players))
        notes = roster.repair(self.team, self.players)
        self.assertTrue(notes)
        self.assertEqual(roster.validate(self.team, self.players), [])

    def test_rotation_and_closer_changes(self) -> None:
        pen = roster.bullpen(self.team, self.players)
        new_closer = next(p.id for p in pen if p.id != self.team.closer)
        roster.set_closer(self.team, self.players, new_closer)
        self.assertEqual(self.team.closer, new_closer)
        roster.set_rotation_spot(self.team, self.players, 0, new_closer)
        self.assertEqual(self.team.rotation[0], new_closer)
        self.assertNotEqual(self.team.closer, new_closer)
        self.assertEqual(roster.validate(self.team, self.players), [])
        with self.assertRaises(roster.RosterError):
            roster.set_closer(self.team, self.players, self.team.lineup[0].player_id)

    def test_out_of_position_penalty(self) -> None:
        catcher = next(self.players[s.player_id] for s in self.team.lineup if s.position == "C")
        catcher.secondary_positions = []
        self.assertFalse(catcher.can_play("SS"))
        self.assertLess(catcher.overall_at("SS"), catcher.overall_at("C") + 1)
        self.assertEqual(
            rt.defense_rating(catcher.ratings, "SS", eligible=True)
            - rt.defense_rating(catcher.ratings, "SS", eligible=False),
            rt.OUT_OF_POSITION_PENALTY,
        )
