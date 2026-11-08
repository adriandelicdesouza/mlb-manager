import random
import unittest
from collections import Counter

from mlb_manager import frontoffice, injuries, roster, trades
from mlb_manager.league import League

from tests.helpers import league_at_offseason


class InjuryMechanicsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.league = League.create(seed=200, user_team="NYY")
        self.team = self.league.user
        self.players = self.league.players

    def test_injury_roll_is_seeded_and_severity_distributed(self) -> None:
        player = self.players[self.team.active[0]]
        rolls = [injuries.roll_injury(player, random.Random(i)) for i in range(2000)]
        self.assertEqual(rolls[:5], [injuries.roll_injury(player, random.Random(i)) for i in range(5)])
        days = [d for _, d in rolls]
        short = sum(d <= 5 for d in days) / len(days)
        long_ = sum(d >= 60 for d in days) / len(days)
        self.assertTrue(0.35 < short < 0.55, short)
        self.assertTrue(0.02 < long_ < 0.10, long_)

    def test_older_and_overworked_players_riskier(self) -> None:
        p = self.players[self.team.active[0]]
        p.age = 25
        young = injuries.injury_risk(p)
        p.age = 36
        self.assertGreater(injuries.injury_risk(p), young)
        self.assertGreater(injuries.injury_risk(p, starter_pitches=120), injuries.injury_risk(p, starter_pitches=90))

    def test_heal(self) -> None:
        p = self.players[self.team.active[0]]
        injuries.apply_injury(p, "sprained ankle", 2)
        self.assertTrue(p.is_injured)
        self.assertFalse(injuries.heal_one_day(p))
        self.assertTrue(injuries.heal_one_day(p))
        self.assertFalse(p.is_injured)
        self.assertIsNone(p.injury)

    def test_game_day_substitution_leaves_saved_lineup_alone(self) -> None:
        starter = self.team.lineup[0]
        injuries.apply_injury(self.players[starter.player_id], "bruised knee", 3)
        closer = self.team.closer
        injuries.apply_injury(self.players[closer], "blister", 2)
        day_team, notes = injuries.game_day_team(self.team, self.players)
        self.assertEqual(self.team.lineup[0], starter)  # saved lineup untouched
        self.assertNotEqual(day_team.lineup[0].player_id, starter.player_id)
        self.assertEqual(day_team.lineup[0].position, starter.position)
        self.assertNotIn(starter.player_id, day_team.active)
        self.assertIsNone(day_team.closer)
        self.assertTrue(notes)
        for slot in day_team.lineup:
            self.assertFalse(self.players[slot.player_id].is_injured)

    def test_il_rules(self) -> None:
        healthy = self.team.active[0]
        with self.assertRaises(roster.RosterError):
            injuries.place_on_il(self.team, self.players, healthy)
        hurt = self.team.lineup[2].player_id
        injuries.apply_injury(self.players[hurt], "oblique strain", 30)
        injuries.place_on_il(self.team, self.players, hurt)
        self.assertIn(hurt, self.team.injured_list)
        self.assertNotIn(hurt, self.team.active)
        self.assertNotIn(hurt, [s.player_id for s in self.team.lineup])
        self.assertIn(hurt, self.team.all_players)
        with self.assertRaises(roster.RosterError):
            injuries.activate_from_il(self.team, self.players, hurt)
        injuries.heal_fully(self.players[hurt])
        message = injuries.activate_from_il(self.team, self.players, hurt)
        self.assertIn("activated", message)
        self.assertNotIn(hurt, self.team.injured_list)


class LeagueInjuryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.league = League.create(seed=201, user_team="LAD")
        cls.violations: list[str] = []
        cls.lineup_injured_played: list[int] = []
        lg = cls.league
        for _ in range(90):
            hurt_before = {pid for pid, p in lg.players.items() if p.is_injured}
            games_before = {pid: lg.players[pid].batting.g for pid in hurt_before}
            lg.advance_day()
            for pid in hurt_before:
                if lg.players[pid].batting.g != games_before[pid]:
                    cls.lineup_injured_played.append(pid)
            for team in lg.teams.values():
                problems = roster.validate(team, lg.players)
                if problems:
                    cls.violations.append(f"{team.abbr}: {problems}")
                if team.abbr != lg.user_team:
                    for pid in team.active:
                        if lg.players[pid].injury_days >= injuries.IL_THRESHOLD_DAYS:
                            cls.violations.append(f"{team.abbr} kept {pid} active while injured")

    def test_injuries_happen(self) -> None:
        il_moves = [t for t in self.league.transactions if "injured list" in t]
        self.assertGreater(len(il_moves), 20)

    def test_injured_players_never_play(self) -> None:
        self.assertEqual(self.lineup_injured_played, [])

    def test_rosters_stay_valid_and_cpu_uses_il(self) -> None:
        self.assertEqual(self.violations, [])
        for team in self.league.teams.values():
            hitters, pitchers = injuries.healthy_counts(team, self.league.players)
            self.assertGreaterEqual(hitters, 9, team.abbr)
            self.assertGreaterEqual(pitchers, 6, team.abbr)
            self.assertEqual(len(team.active), roster.MAX_ACTIVE)

    def test_injury_determinism(self) -> None:
        a = League.create(seed=202, user_team="SF")
        b = League.create(seed=202, user_team="SF")
        a.sim_days(25)
        b.sim_days(25)
        self.assertEqual(a.to_dict(), b.to_dict())
        self.assertTrue(any(p.is_injured or p.id in t.injured_list
                            for t in a.teams.values() for p in (a.players[x] for x in t.all_players)))


class UserInjuryFlowTests(unittest.TestCase):
    def test_user_il_frees_spot_and_call_up_follows(self) -> None:
        lg = League.create(seed=203, user_team="ATL")
        team = lg.user
        hurt = team.lineup[0].player_id
        injuries.apply_injury(lg.players[hurt], "torn hamstring", 70)
        notes = lg.place_on_il(team, hurt)
        self.assertIn(hurt, team.injured_list)
        self.assertEqual(len(team.active), roster.MAX_ACTIVE)  # replacement called up
        self.assertTrue(any("called up" in n for n in notes))
        self.assertEqual(roster.validate(team, lg.players), [])

    def test_too_many_active_injuries_forces_il(self) -> None:
        lg = League.create(seed=204, user_team="BOS")
        team = lg.user
        hitters = [p for p in team.active if not lg.players[p].is_pitcher]
        for pid in hitters[:5]:
            injuries.apply_injury(lg.players[pid], "hamstring strain", 15)
        lg.advance_day()
        healthy_hitters, _ = injuries.healthy_counts(team, lg.players)
        self.assertGreaterEqual(healthy_hitters, frontoffice.HEALTHY_HITTERS_NEEDED)
        self.assertTrue(team.injured_list)

    def test_offseason_heals_and_clears_il(self) -> None:
        lg = league_at_offseason(seed=205, user="MIN")
        team = lg.user
        hurt = team.active[3]
        injuries.apply_injury(lg.players[hurt], "torn UCL", 120)
        lg.place_on_il(team, hurt)
        lg.begin_offseason()
        self.assertEqual(team.injured_list, [])
        if hurt in lg.players:
            self.assertFalse(lg.players[hurt].is_injured)
            self.assertIn(hurt, team.all_players)

    def test_traded_injured_player_lands_on_il(self) -> None:
        lg = League.create(seed=206, user_team="NYY")
        bos = lg.teams["BOS"]
        hurt = lg.user.active[0]
        injuries.apply_injury(lg.players[hurt], "broken wrist", 40)
        lg.place_on_il(lg.user, hurt)
        cheap = min(trades.tradable_players(lg, bos), key=lambda p: frontoffice.player_value(lg, p, bos))
        prop = trades.TradeProposal("NYY", "BOS", a_players=[hurt], b_players=[cheap.id])
        if trades.validate(lg, prop):
            self.skipTest("trade not legal for this seed")
        trades.execute(lg, prop)
        self.assertIn(hurt, bos.injured_list)
        self.assertNotIn(hurt, lg.user.all_players)

    def test_injured_value_discount(self) -> None:
        lg = League.create(seed=207, user_team="NYY")
        bos = lg.teams["BOS"]
        p = lg.players[lg.user.active[0]]
        healthy = frontoffice.player_value(lg, p, bos)
        injuries.apply_injury(p, "torn UCL", 100)
        self.assertLess(frontoffice.player_value(lg, p, bos), healthy)


class InjuryVolumeTests(unittest.TestCase):
    def test_injury_counts_per_team_reasonable(self) -> None:
        lg = League.create(seed=208, user_team="CHC")
        counts: Counter[str] = Counter()
        for _ in range(60):
            before = {pid for pid, p in lg.players.items() if p.is_injured}
            lg.advance_day()
            for pid, p in lg.players.items():
                if p.is_injured and pid not in before and p.team:
                    counts[p.team] += 1
        per_team_per_60 = sum(counts.values()) / 30
        self.assertTrue(1.0 < per_team_per_60 < 8.0, per_team_per_60)
