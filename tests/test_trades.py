import random
import unittest

from mlb_manager import contracts, frontoffice, roster, trades
from mlb_manager.league import League
from mlb_manager.trades import TradeProposal


def by_value(league, team, reverse=True):
    return sorted(
        trades.tradable_players(league, team),
        key=lambda p: frontoffice.player_value(league, p, team),
        reverse=reverse,
    )


def near_miss(league, cpu_abbr, user=None):  # noqa: ANN001
    """A user offer that falls short but is close enough to draw a counter."""
    user = user or league.user_team
    cpu = league.teams[cpu_abbr]
    targets = sorted(trades.tradable_players(league, cpu),
                     key=lambda p: -frontoffice.player_value(league, p, cpu))[2:12]
    offers = by_value(league, league.teams[user])[3:]
    for target in targets:
        for offer in offers:
            prop = TradeProposal(user, cpu_abbr, a_players=[offer.id], b_players=[target.id])
            if trades.validate(league, prop):
                continue
            verdict = trades.evaluate(league, prop)
            if not verdict.accepted and not verdict.hopeless and trades.counter(league, prop):
                return prop
    raise AssertionError("no near-miss offer found")


class TradeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.league = League.create(seed=21, user_team="NYY")
        self.user = self.league.user
        self.bos = self.league.teams["BOS"]

    def test_validation_errors(self) -> None:
        lg = self.league
        bos_player = self.bos.active[0]
        nyy_player = self.user.active[0]
        cases = [
            TradeProposal("NYY", "BOS", a_players=[bos_player], b_players=[nyy_player]),
            TradeProposal("NYY", "BOS", a_picks=["2026-R1-BOS"], b_players=[bos_player]),
            TradeProposal("NYY", "BOS", b_players=[bos_player]),
            TradeProposal("NYY", "NYY", a_players=[nyy_player], b_players=[nyy_player]),
        ]
        for prop in cases:
            self.assertTrue(trades.validate(lg, prop), prop)

    def test_deadline_closes_trading(self) -> None:
        lg = self.league
        lg.day = trades.deadline_day(lg) + 1
        prop = TradeProposal("NYY", "BOS", a_players=[self.user.active[0]], b_players=[self.bos.active[0]])
        self.assertTrue(any("deadline" in e for e in trades.validate(lg, prop)))

    def test_pending_free_agent_cannot_be_traded(self) -> None:
        pid = self.user.active[0]
        self.league.players[pid].contract_years = 0
        prop = TradeProposal("NYY", "BOS", a_players=[pid], b_players=[self.bos.active[0]])
        self.assertTrue(any("pending free agent" in e for e in trades.validate(self.league, prop)))

    def test_junk_for_star_rejected(self) -> None:
        star = by_value(self.league, self.bos)[0]
        junk = [p.id for p in by_value(self.league, self.user, reverse=False)[:3]]
        verdict = trades.evaluate(self.league, TradeProposal("NYY", "BOS", a_players=junk, b_players=[star.id]))
        self.assertFalse(verdict.accepted)

    def test_quantity_has_diminishing_returns(self) -> None:
        self.assertLess(trades.incoming_total([100, 100, 100]), 300)
        self.assertEqual(trades.incoming_total([100]), 100)
        self.assertEqual(trades.incoming_total([100, -50]), 50)

    def test_overpay_accepted_and_executed(self) -> None:
        lg = self.league
        target = by_value(lg, self.bos, reverse=False)[5]
        offer = by_value(lg, self.user)[0]
        prop = TradeProposal("NYY", "BOS", a_players=[offer.id], b_players=[target.id])
        if trades.validate(lg, prop):
            self.skipTest("cap-illegal combination for this seed")
        self.assertTrue(trades.evaluate(lg, prop).accepted)
        trades.execute(lg, prop)
        self.assertEqual(lg.players[offer.id].team, "BOS")
        self.assertEqual(lg.players[target.id].team, "NYY")
        self.assertIn(offer.id, self.bos.all_players)
        self.assertIn(target.id, self.user.all_players)
        for team in (self.user, self.bos):
            self.assertEqual(roster.validate(team, lg.players), [])
        self.assertTrue(any("TRADE" in t for t in lg.transactions))

    def test_picks_change_hands(self) -> None:
        lg = self.league
        pick_id = f"{lg.draft_year}-R1-NYY"
        target = by_value(lg, self.bos, reverse=False)[3]
        prop = TradeProposal("NYY", "BOS", a_picks=[pick_id], b_players=[target.id])
        self.assertTrue(trades.evaluate(lg, prop).accepted)
        trades.execute(lg, prop)
        self.assertEqual(trades.get_pick(lg, pick_id).owner, "BOS")

    def test_evaluation_is_deterministic(self) -> None:
        star = by_value(self.league, self.bos)[0]
        prop = TradeProposal("NYY", "BOS", a_players=[self.user.active[3]], b_players=[star.id])
        first = trades.evaluate(self.league, prop)
        self.assertEqual(first, trades.evaluate(self.league, prop))

    def test_counter_offer_is_acceptable(self) -> None:
        lg = self.league
        prop = near_miss(lg, "BOS")
        self.assertFalse(trades.evaluate(lg, prop).accepted)
        counter = trades.counter(lg, prop)
        self.assertIsNotNone(counter)
        self.assertTrue(trades.is_acceptable(lg, counter, "BOS"))

    def test_lowball_for_star_gets_no_counter(self) -> None:
        lg = self.league
        star = by_value(lg, self.bos)[0]
        prop = TradeProposal("NYY", "BOS", a_picks=[f"{lg.draft_year}-R5-NYY"], b_players=[star.id])
        verdict = trades.evaluate(lg, prop)
        self.assertEqual(verdict.verdict, trades.UNACCEPTABLE)
        self.assertTrue(verdict.hopeless)
        self.assertIsNone(trades.counter(lg, prop))

    def test_cap_blocks_taking_on_salary(self) -> None:
        lg = self.league
        lg.salary_cap = contracts.payroll(self.user, lg.players) + 0.5
        expensive = max(trades.tradable_players(lg, self.bos), key=lambda p: p.salary)
        cheap = min(trades.tradable_players(lg, self.user), key=lambda p: p.salary)
        prop = TradeProposal("NYY", "BOS", a_players=[cheap.id], b_players=[expensive.id])
        self.assertTrue(any("salary cap" in e for e in trades.validate(lg, prop)))

    def test_cpu_offer_to_user_is_valid_and_cpu_approved(self) -> None:
        lg = self.league
        offers = [trades.generate_offer_for_user(lg, random.Random(i)) for i in range(12)]
        offers = [o for o in offers if o]
        self.assertTrue(offers)
        for offer in offers:
            self.assertEqual(offer.proposal.team_b, "NYY")
            self.assertEqual(trades.validate(lg, offer.proposal), [])
            self.assertTrue(trades.evaluate(lg, offer.proposal, offer.proposal.team_a).accepted)

    def test_accept_and_reject_offers(self) -> None:
        lg = self.league
        offers = [o for o in (trades.generate_offer_for_user(lg, random.Random(i)) for i in range(12)) if o]
        lg.trade_offers = offers[:2]
        first, second = offers[0], offers[1]
        target = first.proposal.b_players[0]
        lg.accept_offer(first.offer_id)
        self.assertEqual(lg.players[target].team, first.proposal.team_a)
        lg.reject_offer(second.offer_id)
        self.assertEqual(lg.trade_offers, [])
        with self.assertRaises(trades.TradeError):
            lg.accept_offer(12345)

    def test_cpu_trades_between_contenders_and_rebuilders(self) -> None:
        lg = self.league
        notes = []
        for i in range(6):
            notes += trades.cpu_trade_round(lg, random.Random(i), attempts=3)
        self.assertTrue(notes, "expected at least one CPU-CPU trade")
        for team in lg.teams.values():
            self.assertEqual(roster.validate(team, lg.players), [], team.abbr)
            self.assertLessEqual(len(team.all_players), frontoffice.MAX_ORG + 0)
