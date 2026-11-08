"""Injury tendencies, injury history, rehab assignments, rust and morale."""

import collections
import unittest

from mlb_manager import injuries, morale, roster
from mlb_manager.league import League
from mlb_manager.models import Player


def _injure(league: League, team, pid: int, days: int) -> Player:  # noqa: ANN001
    player = league.players[pid]
    injuries.apply_injury(player, "hamstring strain", days, league.year, league.day)
    return player


class DurabilityTests(unittest.TestCase):
    def test_trait_is_stable_and_distributed(self) -> None:
        lg = League.create(seed=3, user_team="NYY")
        traits = collections.Counter(injuries.durability(p, lg.seed) for p in lg.players.values())
        total = sum(traits.values())
        self.assertAlmostEqual(traits["durable"] / total, 0.20, delta=0.05)
        self.assertAlmostEqual(traits["injury-prone"] / total, 0.15, delta=0.05)
        p = next(iter(lg.players.values()))
        self.assertEqual(injuries.durability(p, lg.seed), injuries.durability(p, lg.seed))

    def test_risk_multiplier_reflects_trait_history_and_rust(self) -> None:
        lg = League.create(seed=3, user_team="NYY")
        by_trait = {}
        for p in lg.players.values():
            by_trait.setdefault(injuries.durability(p, lg.seed), p)
        durable, prone = by_trait["durable"], by_trait["injury-prone"]
        self.assertLess(injuries.risk_multiplier(durable, lg.seed, lg.year),
                        injuries.risk_multiplier(prone, lg.seed, lg.year))
        base = injuries.risk_multiplier(durable, lg.seed, lg.year)
        durable.injury_history.append({"y": lg.year, "d": 1, "i": "torn ACL", "n": 90})
        self.assertGreater(injuries.risk_multiplier(durable, lg.seed, lg.year), base)
        old = injuries.risk_multiplier(durable, lg.seed, lg.year + 5)
        self.assertAlmostEqual(old, base)  # old injuries stop counting
        durable.rust_days = 3
        self.assertGreater(injuries.risk_multiplier(durable, lg.seed, lg.year), base * 1.2)

    def test_injury_history_recorded_in_games(self) -> None:
        lg = League.create(seed=4, user_team="NYY")
        lg.sim_days(25)
        hurt = [p for p in lg.players.values() if p.injury_history]
        self.assertTrue(hurt)
        entry = hurt[0].injury_history[0]
        self.assertEqual(entry["y"], lg.year)
        self.assertGreater(entry["n"], 0)


class RehabTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lg = League.create(seed=9, user_team="NYY")
        self.team = self.lg.user
        self.pid = next(pid for pid in self.team.active if not self.lg.players[pid].is_pitcher)

    def test_activation_without_rehab_leaves_rust(self) -> None:
        player = _injure(self.lg, self.team, self.pid, 30)
        self.lg.place_on_il(self.team, self.pid)
        for _ in range(30):
            injuries.heal_one_day(player)
        self.assertFalse(player.is_injured)
        self.assertEqual(player.rust_days, 30 // 5)
        self.lg.activate_from_il(self.team, self.pid)
        if self.pid not in self.team.active:  # the IL call-up filled the spot: swap him back in
            other = next(pid for pid in self.team.active if pid not in {s.player_id for s in self.team.lineup}
                         and not self.lg.players[pid].is_pitcher)
            roster.swap_active_reserve(self.team, self.lg.players, other, self.pid)
        self.assertEqual(self.lg.form_modifier(player), -injuries.RUST_PENALTY + morale.performance_modifier(player))
        view = self.lg.game_players(self.team)
        self.assertEqual(view[self.pid].ratings["contact"],
                         max(1, player.ratings["contact"] - injuries.RUST_PENALTY))
        self.assertEqual(self.lg.players[self.pid].ratings["contact"], player.ratings["contact"])
        for _ in range(player.rust_days):
            injuries.heal_one_day(player)
        self.assertEqual(player.rust_days, 0)

    def test_rehab_assignment_avoids_rust(self) -> None:
        player = _injure(self.lg, self.team, self.pid, 30)
        self.lg.place_on_il(self.team, self.pid)
        with self.assertRaises(roster.RosterError):
            self.lg.start_rehab(self.team, self.pid)  # too early
        for _ in range(22):
            injuries.heal_one_day(player)
        self.assertTrue(injuries.can_rehab(self.team, player))
        self.lg.start_rehab(self.team, self.pid)
        self.assertIn(self.pid, self.team.reserves)
        self.assertTrue(player.rehab)
        with self.assertRaises(roster.RosterError):
            roster.promote(self.team, self.lg.players, self.pid)  # not until healthy
        for _ in range(8):
            injuries.heal_one_day(player)
        self.assertFalse(player.is_injured)
        self.assertFalse(player.rehab)
        self.assertEqual(player.rust_days, 0)
        message = self.lg.activate_from_il(self.team, self.pid)  # finished rehab
        self.assertIn(self.pid, self.team.active if "activated" in message else self.team.reserves)
        roster.demote(self.team, self.lg.players, next(
            pid for pid in self.team.active if pid not in {s.player_id for s in self.team.lineup}
            and not self.lg.players[pid].is_pitcher and pid != self.pid))
        if self.pid in self.team.reserves:
            self.assertIn("activated", self.lg.activate_from_il(self.team, self.pid))
        self.assertIn(self.pid, self.team.active)

    def test_short_injuries_never_rust(self) -> None:
        player = _injure(self.lg, self.team, self.pid, 8)
        for _ in range(8):
            injuries.heal_one_day(player)
        self.assertEqual(player.rust_days, 0)

    def test_cpu_clubs_use_rehab_and_rosters_stay_valid(self) -> None:
        lg = League.create(seed=12, user_team="NYY")
        lg.sim_days(80)
        rehabbed = [t for t in lg.transactions if "rehab assignment" in t]
        self.assertTrue(rehabbed)
        for team in lg.teams.values():
            self.assertEqual(roster.validate(team, lg.players), [], team.abbr)
            for pid in team.injured_list:
                self.assertFalse(lg.players[pid].rehab)
            for pid in team.active:
                self.assertFalse(lg.players[pid].rehab)


class MoraleTests(unittest.TestCase):
    def test_factors_and_labels(self) -> None:
        lg = League.create(seed=2, user_team="NYY")
        team = lg.user
        starter = lg.players[team.lineup[0].player_id]
        self.assertIn("Starting role", [r for r, _ in morale.factors(lg, starter)])
        self.assertEqual(morale.label(90), "Ecstatic")
        self.assertEqual(morale.label(10), "Angry")
        # a strong player buried in the minors wants a call-up
        best = max((lg.players[pid] for pid in team.active if not lg.players[pid].is_pitcher),
                   key=lambda p: p.overall)
        team.active.remove(best.id)
        team.reserves.append(best.id)
        reasons = dict(morale.factors(lg, best))
        self.assertLess(reasons["Wants a call-up to the majors"], 0)
        self.assertLess(morale.target(lg, best), 50)

    def test_weekly_update_is_deterministic_and_bounded(self) -> None:
        a = League.create(seed=6, user_team="NYY")
        b = League.create(seed=6, user_team="NYY")
        a.sim_days(43)
        b.sim_days(43)
        self.assertEqual([p.morale for p in a.players.values()], [p.morale for p in b.players.values()])
        values = [p.morale for p in a.players.values() if p.team]
        self.assertTrue(all(morale.MIN_MORALE <= v <= morale.MAX_MORALE for v in values))
        self.assertGreater(len(set(values)), 5)

    def test_effects_are_modest(self) -> None:
        p = Player(1, "A", "B", 28, "R", "R", "SS")
        p.morale = 90
        self.assertEqual(morale.performance_modifier(p), 1)
        self.assertLess(morale.contract_multiplier(p), 1.0)
        self.assertGreaterEqual(morale.contract_multiplier(p), 0.9)
        p.morale = 10
        self.assertEqual(morale.performance_modifier(p), -2)
        self.assertLessEqual(morale.contract_multiplier(p), 1.12)
        self.assertTrue(morale.refuses_extension(p))

    def test_morale_changes_resign_ask_and_refusals(self) -> None:
        lg = League.create(seed=2, user_team="NYY")
        player = max((lg.players[pid] for pid in lg.user.active), key=lambda p: p.overall)
        player.morale = 50
        neutral = lg.resign_ask(player)[0]
        player.morale = 90
        happy = lg.resign_ask(player)[0]
        self.assertLessEqual(happy, neutral)
        player.morale = 10
        player.contract_years = 1
        with self.assertRaises(ValueError):
            lg.extend_contract(lg.user, player.id, 2)

    def test_offseason_reset_moves_toward_neutral(self) -> None:
        lg = League.create(seed=2, user_team="NYY")
        p = next(iter(lg.players.values()))
        p.morale = 90
        morale.offseason_reset(lg, None)
        self.assertEqual(p.morale, 70)


if __name__ == "__main__":
    unittest.main()
