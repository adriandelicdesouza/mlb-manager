import unittest
from collections import Counter

from mlb_manager import contracts, frontoffice, roster
from mlb_manager.league import League


class FrontOfficeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.league = League.create(seed=80, user_team="SD")

    def test_strategies_cover_all_three_types(self) -> None:
        counts = Counter(t.strategy for t in self.league.teams.values())
        self.assertEqual(counts["contending"], frontoffice.CONTENDERS)
        self.assertEqual(counts["rebuilding"], frontoffice.REBUILDERS)
        self.assertEqual(sum(counts.values()), 30)

    def test_strategy_changes_valuation(self) -> None:
        lg = self.league
        team = lg.teams["BOS"]
        vet = max((lg.players[p] for p in lg.teams["ATL"].all_players if lg.players[p].age >= 31),
                  key=lambda p: p.overall)
        team.strategy = "contending"
        contender = frontoffice.player_value(lg, vet, team)
        team.strategy = "rebuilding"
        rebuilder = frontoffice.player_value(lg, vet, team)
        self.assertGreater(contender, rebuilder)

    def test_young_potential_valued(self) -> None:
        lg = self.league
        team = lg.teams["BOS"]
        team.strategy = "rebuilding"
        prospect = max((p for p in lg.players.values() if p.team and p.age <= 21), key=lambda p: p.potential)
        self.assertGreater(frontoffice.player_value(lg, prospect, team), 0)

    def test_draft_picks_initialised(self) -> None:
        lg = self.league
        self.assertEqual(len(lg.draft_picks), 30 * 5 * 3)
        years = {p.year for p in lg.draft_picks}
        self.assertEqual(years, {lg.year, lg.year + 1, lg.year + 2})
        self.assertEqual(len({p.pick_id for p in lg.draft_picks}), len(lg.draft_picks))

    def test_optimize_roster_is_valid(self) -> None:
        lg = self.league
        team = lg.teams["COL"]
        team.active, team.reserves = team.reserves[:10] + team.active[:16], team.active[16:] + team.reserves[10:]
        frontoffice.optimize_roster(lg, team)
        self.assertEqual(len(team.active), roster.MAX_ACTIVE)
        self.assertEqual(roster.validate(team, lg.players), [])

    def test_release_creates_dead_money(self) -> None:
        lg = self.league
        team = lg.user
        pid = team.active[0]
        salary = lg.players[pid].salary
        lg.release(team, pid)
        self.assertIsNone(lg.players[pid].team)
        self.assertEqual(team.dead_money, salary)
        self.assertEqual(len(team.active), roster.MAX_ACTIVE)  # vacancy filled from reserves
        self.assertEqual(roster.validate(team, lg.players), [])

    def test_fill_minimums_and_cutdown(self) -> None:
        lg = self.league
        team = lg.teams["MIA"]
        for pid in [p for p in team.reserves if lg.players[p].is_pitcher]:
            frontoffice.release_player(lg, team, pid)
        for pid in [p for p in team.active if lg.players[p].is_pitcher][:3]:
            frontoffice.release_player(lg, team, pid)
        frontoffice.fill_minimums(lg, team)
        pitchers = sum(lg.players[p].is_pitcher for p in team.all_players)
        self.assertGreaterEqual(pitchers, frontoffice.MIN_ORG_PITCHERS)
        for p in list(lg.players.values()):
            if p.team is None and len(team.all_players) <= frontoffice.MAX_ORG + 2:
                contracts.sign(p, team.abbr, contracts.MIN_SALARY, 1)
                team.reserves.append(p.id)
        frontoffice.cutdown(lg, team)
        self.assertLessEqual(len(team.all_players), frontoffice.MAX_ORG)

    def test_initialize_is_idempotent(self) -> None:
        lg = self.league
        before = lg.to_dict()
        frontoffice.initialize(lg)
        after = lg.to_dict()
        self.assertEqual(before["players"], after["players"])
        self.assertEqual(before["draft_picks"], after["draft_picks"])
