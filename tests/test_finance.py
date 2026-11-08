"""League financial rules: caps, floors, the Competitive Balance Tax, revenue sharing, saves."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from mlb_manager import contracts, finance, freeagency, trades
from mlb_manager.league import PHASE_RESIGN, ContractError, League
from mlb_manager.persistence import load_league, save_league
from tests.helpers import league_at_offseason


def custom(season: int = 2026, **changes: object) -> finance.FinancialRules:
    return replace(finance.default_custom_rules(season), **changes)  # type: ignore[arg-type]


def league_with(rules: finance.FinancialRules | None = None, seed: int = 71, user: str = "BOS") -> League:
    config = finance.new_config(2026, finance.PRESET_CUSTOM, rules) if rules else None
    return League.create(seed=seed, user_team=user, finance_config=config)


def cheapest_free_agent(league: League, minimum_only: bool = False):  # noqa: ANN201
    pool = freeagency.free_agents(league)
    if minimum_only:
        pool = [p for p in pool if freeagency.asking(league, p)[0] <= finance.min_salary(league) + 1e-9]
    else:
        pool = [p for p in pool if freeagency.asking(league, p)[0] > finance.min_salary(league) + 1]
    return pool[0]


def make_room(league: League, team) -> None:  # noqa: ANN001
    while len(team.all_players) >= 44:
        league.release(team, team.reserves[-1])


def cheap_expiring(league: League, team, skip: tuple[int, ...] = ()):  # noqa: ANN001, ANN201
    """The best final-year player, re-priced at the minimum so an extension adds payroll."""
    player = max((league.players[p] for p in team.all_players
                  if league.players[p].contract_years == 1 and p not in skip and league.players[p].age < 33),
                 key=lambda p: p.overall)
    contracts.set_flat_salary(player, finance.min_salary(league))
    return player


def fill_to(league: League, team, total: float) -> None:  # noqa: ANN001
    team.dead_money = 0.0
    team.dead_money = max(0.0, total - finance.payroll(league, team))


class MLBRulesTests(unittest.TestCase):
    def test_default_is_standard_mlb_without_cap(self) -> None:
        r = finance.mlb_rules(2026)
        self.assertEqual(r.cap_type, finance.CAP_NONE)
        self.assertFalse(r.has_floor)
        self.assertTrue(r.cbt_enabled)
        self.assertEqual(r.cbt_threshold, 244.0)
        self.assertTrue(r.revenue_sharing)
        self.assertAlmostEqual(r.min_salary, 0.78)
        self.assertEqual(r.cbt_rates, (0.20, 0.30, 0.50))
        league = League.create(seed=3, user_team="NYY")
        self.assertEqual(league.finance.config.preset_for(2026), finance.PRESET_MLB)
        self.assertEqual(finance.rules(league), r)
        self.assertEqual(len(league.teams), 30)

    def test_season_specific_terms(self) -> None:
        self.assertEqual(finance.mlb_rules(2017).cbt_threshold, 195.0)
        self.assertAlmostEqual(finance.mlb_rules(2017).min_salary, 0.535)
        self.assertEqual(finance.mlb_rules(2022).cbt_threshold, 230.0)
        self.assertAlmostEqual(finance.mlb_rules(2024).min_salary, 0.74)
        self.assertEqual(finance.mlb_rules(2025).cbt_threshold, 241.0)
        self.assertEqual(len(finance.mlb_rules(2021).cbt_surcharges), 2)
        self.assertEqual(len(finance.mlb_rules(2022).cbt_surcharges), 3)
        with self.assertRaises(ValueError):
            finance.mlb_rules(2010)

    def test_expired_cba_carried_forward_and_proposals_not_applied(self) -> None:
        for season in (2027, 2028, 2030):
            r = finance.mlb_rules(season)
            self.assertTrue(r.provisional)
            self.assertEqual(r.cap_type, finance.CAP_NONE)  # the proposed cap is NOT a rule
            self.assertFalse(r.has_floor)  # nor the proposed floor
            self.assertEqual(r.cbt_threshold, 244.0)
            self.assertIn("December 1, 2026", r.notes[0])
        self.assertFalse(finance.mlb_rules(2026).provisional)
        self.assertTrue(finance.proposed_terms(2028))
        self.assertTrue(all("not adopted" in t.status for t in finance.proposed_terms()))

    def test_future_agreement_is_a_table_entry(self) -> None:
        future = finance.Agreement("2027-2031 CBA", 2027, 2031, {y: 250.0 for y in range(2027, 2032)},
                                   {y: 0.80 for y in range(2027, 2032)}, finance.SURCHARGES_2022,
                                   cap_type=finance.CAP_HARD, caps={2029: 260.0}, floors={2029: 120.0})
        saved = finance.AGREEMENTS
        finance.AGREEMENTS = (*saved, future)
        try:
            self.assertFalse(finance.mlb_rules(2027).provisional)
            self.assertEqual(finance.mlb_rules(2027).cbt_threshold, 250.0)
            self.assertFalse(finance.mlb_rules(2028).has_cap)
            r = finance.mlb_rules(2029)
            self.assertEqual((r.cap_type, r.cap_amount, r.floor_amount), (finance.CAP_HARD, 260.0, 120.0))
            self.assertTrue(finance.mlb_rules(2032).provisional)
        finally:
            finance.AGREEMENTS = saved

    def test_eras_make_rules_season_aware(self) -> None:
        config = finance.new_config(2026)
        config.set_rules(2028, finance.PRESET_CUSTOM, custom(cap_type=finance.CAP_HARD, cap_amount=210.0))
        self.assertEqual(config.rules_for(2026).cbt_threshold, 244.0)
        self.assertFalse(config.rules_for(2027).has_cap)
        self.assertTrue(config.rules_for(2028).hard_cap)
        self.assertTrue(config.rules_for(2031).hard_cap)
        config.set_rules(2030, finance.PRESET_MLB)
        self.assertTrue(config.rules_for(2029).hard_cap)
        self.assertFalse(config.rules_for(2030).has_cap)

    def test_starting_season_and_minimum_salary_by_season(self) -> None:
        league = League.create(seed=4, user_team="KC", year=2024)
        self.assertEqual(league.year, 2024)
        self.assertAlmostEqual(finance.min_salary(league), 0.74)
        self.assertEqual(finance.rules(league).cbt_threshold, 237.0)
        self.assertTrue(all(p.salary >= 0.74 - 1e-9 for p in league.players.values() if p.team))
        league.year = 2026  # the next seasons' minimum rises: contracts below it are lifted
        notes = finance.start_season(league)
        self.assertTrue(notes)
        self.assertTrue(all(p.salary >= 0.78 - 1e-9 for p in league.players.values()
                            if p.team and p.contract_years > 0))
        with self.assertRaises(ValueError):
            League.create(seed=4, user_team="KC", year=2001)

    def test_draft_signings_use_the_season_minimum(self) -> None:
        self.assertEqual(contracts.rookie_salary(3, 0.78), 0.78)
        self.assertEqual(contracts.rookie_salary(1, 0.78), contracts.FIRST_ROUND_ROOKIE_SALARY)


class ValidationAndFormattingTests(unittest.TestCase):
    def test_parse_amounts(self) -> None:
        for text, value in (("244", 244.0), ("$244M", 244.0), ("244m", 244.0), ("244,000,000", 244.0),
                            ("$780,000", 0.78), ("780k", 0.78), ("0.78", 0.78), ("1.2b", 1200.0)):
            self.assertAlmostEqual(finance.parse_amount(text), value, msg=text)
        for bad in ("", "abc", "-5", "12$"):
            with self.assertRaises(ValueError, msg=bad):
                finance.parse_amount(bad)
        self.assertAlmostEqual(finance.parse_rate("50%"), 0.5)
        self.assertAlmostEqual(finance.parse_rate("0.48"), 0.48)
        self.assertAlmostEqual(finance.parse_rate("125"), 1.25)

    def test_format_amounts(self) -> None:
        self.assertEqual(finance.format_amount(244.0), "$244M")
        self.assertEqual(finance.format_amount(17.5), "$17.5M")
        self.assertEqual(finance.format_amount(0.78), "$780,000")
        self.assertEqual(finance.format_amount(1.25), "$1.25M")

    def test_validation_messages(self) -> None:
        self.assertEqual(finance.validate_rules(finance.mlb_rules(2026)), [])
        self.assertTrue(finance.validate_rules(custom(cap_type=finance.CAP_HARD, cap_amount=5.0)))
        self.assertTrue(finance.validate_rules(custom(cap_type="salary")))
        self.assertTrue(finance.validate_rules(custom(cap_type=finance.CAP_HARD, cap_amount=150.0,
                                                      floor_enabled=True, floor_amount=160.0)))
        self.assertTrue(finance.validate_rules(custom(cbt_rates=(0.5, 0.3, 0.2))))
        self.assertTrue(finance.validate_rules(custom(min_salary=0.0)))
        self.assertTrue(finance.validate_rules(custom(revenue_sharing_rate=1.5)))
        self.assertTrue(finance.validate_rules(custom(cap_type=finance.CAP_SOFT, cap_amount=200.0,
                                                      soft_cap_exceptions=("bogus",))))
        self.assertEqual(finance.validate_rules(custom(cap_type=finance.CAP_NONE, cap_amount=0.0)), [])
        with self.assertRaises(ValueError):
            finance.new_config(2026, finance.PRESET_CUSTOM, custom(cbt_threshold=-1.0))


class CompetitiveBalanceTaxTests(unittest.TestCase):
    def test_tax_rates_and_surcharges(self) -> None:
        r = finance.mlb_rules(2026)
        under = finance.cbt_bill(r, 240.0, 0)
        self.assertEqual(under.total, 0.0)
        self.assertEqual(under.offence, 0)
        first = finance.cbt_bill(r, 274.0, 0)  # $30M over, first time
        self.assertEqual(first.offence, 1)
        self.assertAlmostEqual(first.base_tax, 6.0)
        self.assertAlmostEqual(first.surcharge, 1.2)  # 12% on the $10M beyond +$20M
        self.assertFalse(first.draft_penalty)
        second = finance.cbt_bill(r, 274.0, 1)
        self.assertAlmostEqual(second.base_rate, 0.30)
        self.assertAlmostEqual(second.total, 9.0 + 1.2)
        third = finance.cbt_bill(r, 314.0, 4)  # $70M over, fifth straight year -> 50%
        self.assertAlmostEqual(third.base_rate, 0.50)
        self.assertAlmostEqual(third.surcharge, 0.12 * 20 + 0.42 * 20 + 0.60 * 10)
        self.assertAlmostEqual(third.total, 35.0 + 16.8)
        self.assertTrue(third.draft_penalty)
        old = finance.mlb_rules(2019)  # 2017 CBA: 42.5% first time, 45% for repeat payors
        self.assertAlmostEqual(finance.cbt_bill(old, 256.0, 0).surcharge, 0.12 * 20 + 0.425 * 10)
        self.assertAlmostEqual(finance.cbt_bill(old, 256.0, 2).surcharge, 0.12 * 20 + 0.45 * 10)

    def test_disabled_cbt_charges_nothing(self) -> None:
        self.assertEqual(finance.cbt_bill(custom(cbt_enabled=False), 400.0, 3).total, 0.0)

    def _settle_with(self, league: League, abbr: str, cbt_aav: float) -> tuple[dict, list[str]]:
        state = league.finance.state
        state.accrued_days = 1
        for a, team in league.teams.items():
            state.accrued_cash[a] = finance.payroll(league, team)
            state.accrued_aav[a] = 100.0
        state.accrued_aav[abbr] = cbt_aav
        news = finance.settle_season(league)
        return next(r for r in reversed(state.ledger) if r["team"] == abbr), news

    def test_consecutive_violations_and_reset(self) -> None:
        league = league_at_offseason(seed=72, user="NYY")
        r = finance.rules(league, league.year)
        over = r.cbt_threshold - r.player_benefits + 10.0  # CBT payroll $10M over
        rates, news = [], []
        for aav in (over, over, over, over, 150.0, over):
            row, notes = self._settle_with(league, "NYY", aav)
            rates.append(row["cbt_rate"])
            news += notes
            league.year += 1
        self.assertEqual(rates, [0.20, 0.30, 0.50, 0.50, 0.0, 0.20])  # back under -> reset
        self.assertEqual(league.finance.state.streaks["NYY"], 1)
        self.assertTrue(any("Competitive Balance Tax" in n for n in news))
        self.assertTrue(any("resets" in n for n in news))

    def test_cbt_payroll_uses_aav_proration_and_benefits(self) -> None:
        league = league_with(None, seed=73)
        team = league.user
        player = cheapest_free_agent(league)
        make_room(league, team)
        salary, years = 30.0, 5
        league.finance.state.accrued_days = 0
        contracts.sign(player, team.abbr, salary, years)
        team.reserves.append(player.id)
        self.assertLess(player.salary, contracts.aav(player))  # back-loaded: cash below AAV in year 1
        self.assertAlmostEqual(sum(contracts.cash_schedule(30.0, 5, contracts.ESCALATOR)), 150.0)
        r = finance.rules(league)
        self.assertAlmostEqual(finance.cbt_payroll(league, team),
                               finance.aav_payroll(league, team) + r.player_benefits, places=3)
        self.assertNotAlmostEqual(finance.aav_payroll(league, team), finance.payroll(league, team), places=1)
        # Half a season without the player, half with him: he counts for half his AAV.
        days = finance.season_days(league)
        team.remove_player(player.id)
        for _ in range(days // 2):
            finance.accrue_day(league)
        without = finance.aav_payroll(league, team)
        team.reserves.append(player.id)
        expected = (without * (days // 2) + (without + 30.0) * (days - days // 2)) / days + r.player_benefits
        self.assertAlmostEqual(finance.cbt_payroll(league, team), expected, places=3)

    def test_draft_penalty_moves_top_pick_back_ten(self) -> None:
        order = [f"T{i:02d}" for i in range(30)]
        moved = finance.penalised_first_round(order, ["T12", "T02"])
        self.assertEqual(moved.index("T12"), 22)
        self.assertEqual(moved.index("T02"), 2)  # top-six picks are protected
        self.assertIsNone(finance.penalised_first_round(order, []))

    def test_draft_penalty_applies_in_the_draft(self) -> None:
        league = league_at_offseason(seed=74, user="NYY")
        league.begin_offseason()
        victim = league.pending_draft_order[12]
        league.finance.state.draft_penalties[league.draft_year] = [victim]
        news = league.advance_offseason()
        self.assertEqual(league.draft.slot(22), (1, victim))
        self.assertEqual(league.draft.slot(30 + 12), (2, victim))  # later rounds unchanged
        self.assertTrue(any("CBT draft penalty" in n for n in news))


class CapTests(unittest.TestCase):
    def test_no_cap_allows_unlimited_payroll(self) -> None:
        league = league_with(None, seed=75)
        team = league.user
        team.dead_money = 600.0  # an absurd payroll is still legal without a cap
        player = cheapest_free_agent(league)
        make_room(league, team)
        salary, years = freeagency.asking(league, player)
        note = freeagency.sign_free_agent(league, team, player.id, salary, years)
        self.assertIn("signed free agent", note)
        check = finance.check_transaction(league, team, finance.KIND_FREE_AGENT, 50.0, salary=50.0)
        self.assertTrue(check.allowed)
        self.assertTrue(any("Competitive Balance Tax" in w for w in check.warnings))
        self.assertIsNone(finance.cap_room(league, team))

    def test_hard_cap_blocks_signings_extensions_and_trades(self) -> None:
        league = league_with(custom(cap_type=finance.CAP_HARD, cap_amount=150.0), seed=76)
        team = league.user
        make_room(league, team)
        expiring = cheap_expiring(league, team)
        fill_to(league, team, 149.5)  # $0.5M of room
        player = cheapest_free_agent(league)
        salary, years = freeagency.asking(league, player)
        with self.assertRaisesRegex(freeagency.FreeAgencyError, "Hard cap"):
            freeagency.sign_free_agent(league, team, player.id, salary, years)
        with self.assertRaisesRegex(ContractError, "Hard cap"):
            league.extend_contract(team, expiring.id, 3)
        # Trades that add payroll are illegal; trades that shed payroll are fine.
        partner = league.teams["TB"]
        mine = min((league.players[p] for p in team.all_players if league.players[p].contract_years > 0),
                   key=lambda p: p.salary)
        theirs = max((league.players[p] for p in partner.all_players if league.players[p].contract_years > 0),
                     key=lambda p: p.salary)
        add = trades.TradeProposal(team.abbr, partner.abbr, a_players=[mine.id], b_players=[theirs.id])
        self.assertTrue(any("Hard cap" in e for e in trades.validate(league, add)))
        shed = trades.TradeProposal(
            team.abbr, partner.abbr,
            a_players=[max((p for p in team.all_players if league.players[p].contract_years > 0),
                           key=lambda p: league.players[p].salary)],
            b_players=[min((p for p in partner.all_players if league.players[p].contract_years > 0),
                           key=lambda p: league.players[p].salary)])
        self.assertFalse([e for e in trades.validate(league, shed) if f"{team.abbr}'s payroll" in e])
        impact = trades.contract_impact(league, add)[0]
        self.assertIsNotNone(impact.cap_space_after)
        self.assertTrue(any("Hard cap" in m for m in impact.messages))
        # A league-mandated draft signing is exempt but flagged.
        check = finance.check_transaction(league, team, finance.KIND_DRAFT, 1.5, salary=1.5)
        self.assertTrue(check.allowed)
        self.assertTrue(check.warnings)

    def test_hard_cap_blocks_resigning_and_arbitration(self) -> None:
        league = league_at_offseason(seed=77, user="SEA")
        league.finance.config = finance.new_config(2026, finance.PRESET_CUSTOM,
                                                   custom(cap_type=finance.CAP_HARD, cap_amount=150.0))
        league.begin_offseason()
        self.assertEqual(league.phase, PHASE_RESIGN)
        team = league.user
        pending = league.pending_free_agents(team)
        self.assertTrue(pending)
        team.dead_money = 150.0 - finance.payroll(league, team) + team.dead_money  # exactly at the cap
        for player in pending:
            with self.assertRaisesRegex(ContractError, "Hard cap"):
                league.extend_contract(team, player.id, 1)

    def test_soft_cap_exceptions_and_penalty(self) -> None:
        rules = custom(cap_type=finance.CAP_SOFT, cap_amount=150.0, soft_cap_tax_rate=1.5)
        league = league_with(rules, seed=78)
        team = league.user
        make_room(league, team)
        expiring = cheap_expiring(league, team)
        other = cheap_expiring(league, team, skip=(expiring.id,))
        fill_to(league, team, 155.0)  # $5M over the soft cap
        star = cheapest_free_agent(league)
        salary, years = freeagency.asking(league, star)
        with self.assertRaisesRegex(freeagency.FreeAgencyError, "Soft cap"):
            freeagency.sign_free_agent(league, team, star.id, salary, years)
        minimum = finance.min_salary(league)
        check = finance.check_transaction(league, team, finance.KIND_FREE_AGENT, minimum, salary=minimum)
        self.assertTrue(check.allowed)  # minimum-salary exception
        self.assertEqual(check.exception, finance.EXC_MINIMUM)
        self.assertTrue(any("exception" in w.lower() for w in check.warnings))
        self.assertIn("extended", league.extend_contract(team, expiring.id, 2))  # own-player exception
        # Trade matching: taking back up to 125% of outgoing salary is allowed over the cap.
        partner = league.teams["TB"]
        out_p = max((league.players[p] for p in team.all_players if league.players[p].contract_years > 0
                     and 4 < league.players[p].salary < 20), key=lambda p: p.salary)
        fits = [league.players[p] for p in partner.all_players if league.players[p].contract_years > 0
                and out_p.salary < league.players[p].salary <= out_p.salary * 1.25]
        too_much = [league.players[p] for p in partner.all_players if league.players[p].contract_years > 0
                    and league.players[p].salary > out_p.salary * 1.25 + 0.2]
        if fits:
            ok = trades.TradeProposal(team.abbr, partner.abbr, a_players=[out_p.id], b_players=[fits[0].id])
            self.assertFalse([e for e in trades.validate(league, ok) if "cap" in e.lower()])
        if too_much:
            bad = trades.TradeProposal(team.abbr, partner.abbr, a_players=[out_p.id], b_players=[too_much[0].id])
            self.assertTrue(any("Soft cap" in e for e in trades.validate(league, bad)))
        # Without the own-player exception, extensions over the cap are refused.
        league.finance.config = finance.new_config(2026, finance.PRESET_CUSTOM,
                                                   replace(rules, soft_cap_exceptions=(finance.EXC_MINIMUM,)))
        with self.assertRaisesRegex(ContractError, "Soft cap"):
            league.extend_contract(team, other.id, 2)
        # Penalty at season end on the season-average payroll above the cap.
        self.assertAlmostEqual(finance.soft_cap_penalty(rules, 160.0), 15.0)
        self.assertEqual(finance.soft_cap_penalty(rules, 140.0), 0.0)

    def test_salary_floor_shortfall_and_warnings(self) -> None:
        rules = custom(floor_enabled=True, floor_amount=150.0)
        league = league_with(rules, seed=79)
        r = finance.rules(league)
        self.assertAlmostEqual(finance.floor_penalty(r, 120.0), 30.0)
        self.assertEqual(finance.floor_penalty(r, 151.0), 0.0)
        for team in league.teams.values():  # new leagues are shaped to the floor
            self.assertGreaterEqual(finance.payroll(league, team), 150.0 - 1.0)
            self.assertGreaterEqual(finance.payroll_budget(league, team), 150.0)
        team = league.user
        check = finance.check_transaction(league, team, finance.KIND_TRADE,
                                          -(finance.payroll(league, team) - 140.0), outgoing=20.0)
        self.assertTrue(check.allowed)
        self.assertTrue(any("floor" in w.lower() for w in check.warnings))
        state = league.finance.state
        state.accrued_days = 1
        for abbr, t in league.teams.items():
            state.accrued_cash[abbr] = finance.payroll(league, t)
            state.accrued_aav[abbr] = finance.aav_payroll(league, t)
        state.accrued_cash[team.abbr] = 130.0
        news = finance.settle_season(league)
        row = next(x for x in state.ledger if x["team"] == team.abbr)
        self.assertAlmostEqual(row["floor_penalty"], 20.0)
        self.assertTrue(any("floor" in n.lower() for n in news))

    def test_cpu_free_agency_respects_hard_cap(self) -> None:
        league = league_at_offseason(seed=80, user="NYY")
        league.finance.config = finance.new_config(2026, finance.PRESET_CUSTOM,
                                                   custom(cap_type=finance.CAP_HARD, cap_amount=160.0))
        league.begin_offseason()
        league.advance_offseason(auto_user=True)  # re-signing
        league.advance_offseason()  # draft
        before = {a: finance.payroll(league, t) for a, t in league.teams.items()}
        freeagency.cpu_free_agency(league)
        for abbr, team in league.teams.items():
            after = finance.payroll(league, team)
            if after > before[abbr] + 1e-6:
                self.assertLessEqual(after, 160.0 + 1e-6, abbr)

    def test_cpu_budgets_follow_markets_and_revenue_sharing(self) -> None:
        league = league_with(None, seed=81)
        big, small = league.teams["NYY"], league.teams["ATH"]
        big.strategy = small.strategy = "balanced"
        self.assertGreater(finance.payroll_budget(league, big), finance.payroll_budget(league, small) * 2)
        shared = finance.payroll_budget(league, small)
        league.finance.config = finance.new_config(2026, finance.PRESET_CUSTOM, custom(revenue_sharing=False))
        league.finance._cache.clear()
        self.assertLess(finance.payroll_budget(league, small), shared)
        rev = finance.projected_revenue(league, big)
        self.assertEqual(rev["shared_in"], 0.0)

    def test_revenue_sharing_settlement(self) -> None:
        league = league_with(None, seed=82)
        league.finance.state.accrued_days = 0
        finance.settle_season(league)
        rows = [r for r in league.finance.state.ledger if r["year"] == league.year]
        self.assertEqual(len(rows), 30)
        self.assertAlmostEqual(sum(r["shared_out"] for r in rows), sum(r["shared_in"] for r in rows), places=3)
        nyy = next(r for r in rows if r["team"] == "NYY")
        ath = next(r for r in rows if r["team"] == "ATH")
        self.assertGreater(nyy["shared_out"], nyy["shared_in"])
        self.assertLess(ath["shared_out"], ath["shared_in"])


class ArbitrationTests(unittest.TestCase):
    def test_club_controlled_players_get_one_year_arbitration_terms(self) -> None:
        league = league_at_offseason(seed=83, user="TB")
        league.begin_offseason()
        team = league.user
        young = next((p for p in league.pending_free_agents(team) if p.service_years < 6), None)
        if young is None:
            young = league.pending_free_agents(team)[0]
            young.service_years = 4
        salary, years = league.resign_ask(young)
        self.assertEqual(years, 1)
        self.assertEqual(contracts.contract_status(young), "Arb-eligible")
        self.assertLessEqual(salary, contracts.market_salary(young, finance.min_salary(league)))
        note = league.extend_contract(team, young.id, 1)
        self.assertIn("arbitration", note)
        self.assertEqual(young.contract_years, 1)
        self.assertAlmostEqual(young.salary, salary)

    def test_arbitration_salary_scales_with_service(self) -> None:
        league = League.create(seed=84, user_team="TB")
        player = max(league.players.values(), key=lambda p: p.overall)
        shares = []
        for service in (2, 3, 4, 5):
            player.service_years = service
            shares.append(contracts.arbitration_salary(player, 0.78))
        self.assertEqual(shares[0], 0.78)
        self.assertLess(shares[1], shares[2])
        self.assertLess(shares[2], shares[3])


class PersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_configuration_and_state_round_trip(self) -> None:
        rules = custom(cap_type=finance.CAP_SOFT, cap_amount=190.0, floor_enabled=True, floor_amount=90.0,
                       soft_cap_exceptions=(finance.EXC_OWN,), cbt_threshold=230.0, revenue_sharing_rate=0.3)
        league = league_with(rules, seed=85)
        league.finance.config.set_rules(2028, finance.PRESET_MLB)
        league.finance.state.streaks["NYY"] = 2
        league.finance.state.draft_penalties[2026] = ["LAD"]
        league.sim_days(3)
        path = save_league(league, self.dir / "fin.json")
        loaded = load_league(path)
        self.assertEqual(loaded.finance.config.to_dict(), league.finance.config.to_dict())
        self.assertEqual(loaded.finance.state.to_dict(), league.finance.state.to_dict())
        self.assertEqual(finance.rules(loaded), finance.rules(league))
        self.assertEqual(finance.rules(loaded, 2029), finance.mlb_rules(2029))
        self.assertEqual(loaded.finance.state.accrued_days, 3)
        loaded.sim_days(2)
        league.sim_days(2)
        self.assertEqual(loaded.finance.state.to_dict(), league.finance.state.to_dict())

    def test_load_and_modify_rules_preserves_data(self) -> None:
        league = League.create(seed=86, user_team="LAD")
        league.sim_days(10)
        path = save_league(league, self.dir / "lad.json")
        loaded = load_league(path)
        snapshot = {pid: (p.salary, p.contract_years, contracts.aav(p), p.team) for pid, p in loaded.players.items()}
        history = (list(loaded.transactions), [t.dead_money for t in loaded.teams.values()], loaded.day)
        payroll = finance.payroll(loaded, loaded.user)
        cap = max(100.0, payroll - 20.0)
        preview = finance.preview_change(loaded, finance.PRESET_CUSTOM, custom(cap_type=finance.CAP_HARD,
                                                                               cap_amount=cap))
        lines = finance.change_rules(loaded, finance.PRESET_CUSTOM, custom(cap_type=finance.CAP_HARD, cap_amount=cap))
        self.assertEqual(preview, lines)
        self.assertTrue(any("above the new cap" in line for line in lines))
        self.assertTrue(any("preserved" in line for line in lines))
        self.assertEqual(snapshot, {pid: (p.salary, p.contract_years, contracts.aav(p), p.team)
                                    for pid, p in loaded.players.items()})
        self.assertEqual(history[1:], ([t.dead_money for t in loaded.teams.values()], loaded.day))
        self.assertTrue(finance.rules(loaded).hard_cap)
        self.assertEqual(len(loaded.finance.state.changes), 1)
        saved = load_league(save_league(loaded, self.dir / "lad2.json"))
        self.assertTrue(finance.rules(saved).hard_cap)
        self.assertEqual(finance.change_rules(saved, finance.PRESET_CUSTOM,
                                              custom(cap_type=finance.CAP_HARD, cap_amount=cap)),
                         ["No rule changes."])

    def test_rule_change_in_offseason_starts_next_season(self) -> None:
        league = league_at_offseason(seed=87, user="CHC")
        league.begin_offseason()
        finance.change_rules(league, finance.PRESET_CUSTOM, custom(cap_type=finance.CAP_HARD, cap_amount=250.0))
        self.assertFalse(finance.rules(league, league.year).has_cap)  # the finished season keeps its rules
        self.assertTrue(finance.rules(league).hard_cap)  # contract season = next year

    def test_legacy_save_migration(self) -> None:
        league = League.create(seed=88, user_team="MIN")
        league.sim_days(5)
        data = league.to_dict()
        data.pop("finance")
        data["salary_cap"] = 125.0
        for player in data["players"]:
            player.pop("contract_aav", None)
            player.pop("contract_raise", None)
        salaries = {p["id"]: p["salary"] for p in data["players"]}
        path = self.dir / "old.json"
        path.write_text(json.dumps({"format_version": 4, "app_version": "0.8.0", "league": data}))
        loaded = load_league(path)
        self.assertTrue(loaded.finance.migrated)
        self.assertEqual(loaded.finance.config.legacy_salary_cap, 125.0)
        r = finance.rules(loaded)
        self.assertEqual((r.cap_type, r.cbt_threshold, r.revenue_sharing), (finance.CAP_NONE, 244.0, True))
        self.assertEqual({pid: p.salary for pid, p in loaded.players.items()}, salaries)
        self.assertTrue(all(p.contract_aav == p.salary for p in loaded.players.values() if p.salary > 0))
        self.assertEqual(loaded.finance.state.accrued_days, 5)
        self.assertTrue(any("Save upgraded" in n and "$125M hard cap" in n for n in loaded.news))
        restore = finance.restore_legacy_cap_rules(loaded)
        self.assertEqual((restore.cap_type, restore.cap_amount), (finance.CAP_HARD, 125.0))
        loaded.sim_days(2)  # plays on
        self.assertEqual(loaded.finance.state.accrued_days, 7)


if __name__ == "__main__":
    unittest.main()
