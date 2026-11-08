import unittest

from mlb_manager import contracts, development, freeagency, frontoffice, roster
from mlb_manager.draft import DRAFT_ROUNDS, FUTURE_DRAFT_YEARS
from mlb_manager.league import (
    PHASE_DRAFT,
    PHASE_FREE_AGENCY,
    PHASE_REGULAR,
    PHASE_RESIGN,
    ContractError,
    League,
)

from tests.helpers import league_at_offseason


class OffseasonFlowTests(unittest.TestCase):
    """Walks one offseason making user decisions at every step."""

    def test_user_driven_offseason(self) -> None:
        lg = league_at_offseason(seed=90, user="CLE")
        team = lg.user
        ages = {pid: lg.players[pid].age for pid in team.all_players}
        lg.begin_offseason()
        self.assertEqual(lg.phase, PHASE_RESIGN)
        for pid, age in ages.items():
            if pid in lg.players:
                self.assertEqual(lg.players[pid].age, age + 1)

        pending = lg.pending_free_agents(team)
        self.assertTrue(pending, "expected expiring contracts after one season")
        keep, *leave = pending
        lg.extend_contract(team, keep.id, 2)
        self.assertEqual(keep.contract_years, 2)
        with self.assertRaises(ContractError):
            lg.extend_contract(team, keep.id, 2)  # now has 2 years left

        lg.advance_offseason()
        self.assertEqual(lg.phase, PHASE_DRAFT)
        self.assertIn(keep.id, team.all_players)
        for p in leave:
            self.assertIsNone(p.team)
            self.assertIn(p, freeagency.free_agents(lg))

        lg.draft_until_user()
        prospect = lg.draft.pool[0]
        lg.draft_select(prospect)
        self.assertEqual(lg.players[prospect].team, "CLE")
        lg.advance_offseason()
        self.assertEqual(lg.phase, PHASE_FREE_AGENCY)

        fa = freeagency.free_agents(lg)[0]
        salary, years = freeagency.asking(lg, fa)
        self.assertGreaterEqual(years, 1)
        if contracts.cap_space(team, lg.players, lg.salary_cap) >= salary and len(team.all_players) < frontoffice.MAX_ORG:
            freeagency.sign_free_agent(lg, team, fa.id, salary, years)
            self.assertEqual(fa.team, "CLE")

        year = lg.year
        lg.advance_offseason()
        self.assertEqual(lg.phase, PHASE_REGULAR)
        self.assertEqual(lg.year, year + 1)
        self.assertEqual(lg.day, 0)
        self.assertIn(prospect, team.all_players)
        for t in lg.teams.values():
            self.assertLessEqual(len(t.all_players), frontoffice.MAX_ORG)
            self.assertEqual(roster.validate(t, lg.players), [], t.abbr)
        self.assertEqual({p.year for p in lg.draft_picks},
                         set(range(lg.year, lg.year + FUTURE_DRAFT_YEARS)))
        lg.sim_days(3)
        self.assertTrue(any(g.played for g in lg.schedule))

    def test_extension_rules_in_season(self) -> None:
        lg = League.create(seed=91, user_team="TB")
        team = lg.user
        final_year = next(lg.players[p] for p in team.all_players if lg.players[p].contract_years == 1)
        lg.extend_contract(team, final_year.id, 3)
        self.assertEqual(final_year.contract_years, 4)
        long_deal = next(lg.players[p] for p in team.all_players if lg.players[p].contract_years >= 3)
        with self.assertRaises(ContractError):
            lg.extend_contract(team, long_deal.id, 2)

    def test_cannot_begin_offseason_mid_season(self) -> None:
        lg = League.create(seed=92, user_team="TB")
        with self.assertRaises(RuntimeError):
            lg.begin_offseason()
        with self.assertRaises(RuntimeError):
            lg.advance_offseason()


class MultiSeasonTests(unittest.TestCase):
    """Plays three full seasons with automated offseasons (slow: ~15 s)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.league = League.create(seed=93, user_team="HOU")
        cls.snapshots = []
        for _ in range(3):
            cls.league.sim_to_completion()
            lg = cls.league
            cls.snapshots.append({
                "year": lg.year,
                "champion": lg.playoffs.champion,
                "trades": sum("TRADE" in t and t.startswith(str(lg.year)) for t in lg.transactions),
                "avg_ovr": sum(lg.players[p].overall for t in lg.teams.values() for p in t.active) / 780,
            })
            lg.start_new_season()

    def test_years_advance_and_history_recorded(self) -> None:
        self.assertEqual([s["year"] for s in self.snapshots], [2026, 2027, 2028])
        self.assertEqual(self.league.year, 2029)
        self.assertEqual(len(self.league.history), 3)

    def test_rosters_contracts_and_picks_stay_consistent(self) -> None:
        lg = self.league
        seen: set[int] = set()
        for team in lg.teams.values():
            self.assertEqual(roster.validate(team, lg.players), [], team.abbr)
            self.assertLessEqual(len(team.all_players), frontoffice.MAX_ORG)
            for pid in team.all_players:
                self.assertNotIn(pid, seen)
                seen.add(pid)
                p = lg.players[pid]
                self.assertEqual(p.team, team.abbr)
                self.assertGreaterEqual(p.contract_years, 1)
            if team.abbr != lg.user_team:
                self.assertLessEqual(contracts.payroll(team, lg.players), lg.salary_cap + 1e-6)
        self.assertEqual(len(lg.draft_picks), 30 * DRAFT_ROUNDS * FUTURE_DRAFT_YEARS)

    def test_drafted_players_join_organisations(self) -> None:
        drafted = [p for p in self.league.players.values() if p.draft_year is not None and p.team]
        self.assertGreater(len(drafted), 200)

    def test_league_talent_is_stable(self) -> None:
        first, last = self.snapshots[0]["avg_ovr"], self.snapshots[-1]["avg_ovr"]
        self.assertLess(abs(last - first), 3.0)

    def test_cpu_clubs_trade(self) -> None:
        self.assertGreater(sum(s["trades"] for s in self.snapshots), 3)

    def test_player_population_bounded(self) -> None:
        self.assertLess(len(self.league.players), 30 * frontoffice.MAX_ORG + 250)

    def test_aging_and_development_happened(self) -> None:
        drafted_2026 = [p for p in self.league.players.values() if p.draft_year == 2026]
        self.assertTrue(drafted_2026)
        for p in drafted_2026:
            self.assertGreaterEqual(p.age, 20)
        self.assertTrue(all(p.potential >= p.overall or p.age > 31 for p in drafted_2026))
        self.assertTrue(development.DEVELOPMENT_TYPES)
