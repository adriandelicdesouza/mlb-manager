import unittest

from mlb_manager import contracts, freeagency, frontoffice
from mlb_manager.league import League


class FreeAgencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.league = League.create(seed=70, user_team="MIL")
        self.team = self.league.user
        self.fa = freeagency.free_agents(self.league)[0]

    def test_in_season_asks_are_one_year_and_discounted(self) -> None:
        salary, years = freeagency.asking(self.league, self.fa)
        self.assertEqual(years, 1)
        self.assertLessEqual(salary, contracts.market_salary(self.fa) * 1.15)

    def test_lowball_offer_refused(self) -> None:
        salary, years = freeagency.asking(self.league, self.fa)
        if salary <= contracts.MIN_SALARY:
            self.skipTest("asking price already minimal")
        accepted, message = freeagency.evaluate_offer(self.league, self.fa, salary * 0.5, years)
        self.assertFalse(accepted)
        self.assertIn("insulting", message)

    def test_sign_at_asking_price(self) -> None:
        salary, years = freeagency.asking(self.league, self.fa)
        before = contracts.payroll(self.team, self.league.players)
        freeagency.sign_free_agent(self.league, self.team, self.fa.id, salary, years)
        self.assertEqual(self.fa.team, "MIL")
        self.assertIn(self.fa.id, self.team.all_players)
        self.assertAlmostEqual(contracts.payroll(self.team, self.league.players), before + salary, places=2)
        self.assertNotIn(self.fa, freeagency.free_agents(self.league))

    def test_cap_and_roster_limits(self) -> None:
        lg = self.league
        salary, years = freeagency.asking(lg, self.fa)
        lg.salary_cap = contracts.payroll(self.team, lg.players) + salary - 0.1
        with self.assertRaises(freeagency.FreeAgencyError):
            freeagency.sign_free_agent(lg, self.team, self.fa.id, salary, years)
        lg.salary_cap = 1000
        while len(self.team.all_players) < frontoffice.MAX_ORG:
            self.team.reserves.append(self.team.reserves[0])  # pad the count
        with self.assertRaises(freeagency.FreeAgencyError):
            freeagency.sign_free_agent(lg, self.team, self.fa.id, salary, years)

    def test_non_free_agent_rejected(self) -> None:
        with self.assertRaises(freeagency.FreeAgencyError):
            freeagency.sign_free_agent(self.league, self.team, self.team.active[0], 5, 1)

    def test_cpu_signings_respect_cap_and_needs(self) -> None:
        lg = self.league
        notes = freeagency.cpu_free_agency(lg)
        self.assertTrue(notes)
        for team in lg.teams.values():
            if team.abbr != lg.user_team:
                self.assertLessEqual(contracts.payroll(team, lg.players), lg.salary_cap)
        self.assertTrue(all("signed free agent" in n for n in notes))
        self.assertFalse(any(lg.user_team + " signed" in n for n in notes))
