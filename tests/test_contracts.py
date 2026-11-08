import unittest

from mlb_manager import contracts, finance
from mlb_manager.league import League
from mlb_manager.models import Player


def make_player(position="SS", age=28, level=50) -> Player:
    attrs = ("contact", "power", "eye", "speed", "fielding", "arm", "range")
    if position in ("SP", "RP"):
        attrs = ("stuff", "control", "movement", "stamina")
    return Player(1, "Test", "Player", age, "R", "R", position, ratings={a: level for a in attrs})


class ContractRuleTests(unittest.TestCase):
    def test_market_salary_rises_with_ability(self) -> None:
        salaries = [contracts.market_salary(make_player(level=lvl)) for lvl in (40, 55, 70, 85)]
        self.assertEqual(salaries, sorted(salaries))
        self.assertEqual(salaries[0], contracts.MIN_SALARY)
        self.assertEqual(contracts.market_salary(make_player(level=40), 0.5), 0.5)
        self.assertLessEqual(salaries[-1], contracts.MAX_SALARY)

    def test_relievers_and_veterans_cost_less(self) -> None:
        sp = contracts.market_salary(make_player("SP", level=70))
        rp = contracts.market_salary(make_player("RP", level=70))
        self.assertLess(rp, sp)
        young = contracts.market_salary(make_player(age=28, level=70))
        old = contracts.market_salary(make_player(age=35, level=70))
        self.assertLess(old, young)

    def test_asking_contract_is_stable_and_bounded(self) -> None:
        p = make_player(level=65)
        self.assertEqual(contracts.asking_contract(p, 1), contracts.asking_contract(p, 1))
        salary, years = contracts.asking_contract(p, 1)
        self.assertGreaterEqual(salary, contracts.MIN_SALARY)
        self.assertTrue(1 <= years <= contracts.max_years(p))
        self.assertEqual(contracts.max_years(make_player(age=37)), 1)

    def test_back_loaded_contracts(self) -> None:
        p = make_player()
        contracts.sign(p, "NYY", 20.0, 5)
        self.assertEqual(p.contract_aav, 20.0)
        self.assertLess(p.salary, 20.0)
        cash = [contracts.salary_in(p, t) for t in range(5)]
        self.assertEqual(cash, sorted(cash))
        self.assertAlmostEqual(sum(cash), 100.0)
        contracts.advance_contract_year(p)
        self.assertEqual(p.contract_years, 4)
        self.assertAlmostEqual(p.salary, cash[1])
        contracts.sign(p, "NYY", 20.0, 2)  # short deals are flat
        self.assertEqual(p.salary, 20.0)

    def test_status_labels(self) -> None:
        p = make_player()
        self.assertEqual(contracts.contract_status(p), "Free agent")
        contracts.sign(p, "NYY", 5, 1)
        self.assertEqual(contracts.contract_status(p), "Expiring")
        p.contract_years = 0
        p.service_years = 7
        self.assertEqual(contracts.contract_status(p), "Pending FA")
        p.service_years = 3
        self.assertEqual(contracts.contract_status(p), "Arb-eligible")
        p.contract_years = 3
        self.assertEqual(contracts.contract_status(p), "Signed")


class LeagueContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.league = League.create(seed=60, user_team="BOS")

    def test_every_rostered_player_has_a_contract(self) -> None:
        for team in self.league.teams.values():
            for pid in team.all_players:
                p = self.league.players[pid]
                self.assertGreaterEqual(p.salary, contracts.MIN_SALARY)
                self.assertGreaterEqual(p.contract_years, 1)
                self.assertGreater(p.potential, 0)

    def test_initial_payrolls_follow_budgets(self) -> None:
        lg = self.league
        for team in lg.teams.values():
            budget = finance.payroll_budget(lg, team)
            self.assertLessEqual(contracts.payroll(team, lg.players), budget * 1.06)
            self.assertGreaterEqual(contracts.payroll(team, lg.players), budget * 0.69)

    def test_commitments_decline_over_time(self) -> None:
        team = self.league.user
        commits = contracts.commitments(team, self.league.players)
        self.assertEqual(commits[0], contracts.payroll(team, self.league.players))
        self.assertEqual(commits, sorted(commits, reverse=True))

    def test_configurable_cap(self) -> None:
        league = League.create(seed=60, user_team="BOS", salary_cap=120)
        self.assertTrue(finance.rules(league).hard_cap)
        self.assertEqual(finance.rules(league).cap_amount, 120)
        for team in league.teams.values():
            self.assertLessEqual(contracts.payroll(team, league.players), 120)

    def test_initial_free_agent_pool(self) -> None:
        free = [p for p in self.league.players.values() if p.team is None]
        self.assertGreaterEqual(len(free), 50)
