"""Centralised valuation, negotiation, counteroffers, CPU trading and safeguards."""

import random
import tempfile
import unittest
from pathlib import Path

from mlb_manager import contracts, frontoffice, roster, trades, valuation
from mlb_manager.league import League
from mlb_manager.persistence import load_league, save_league
from mlb_manager.trades import TradeProposal


def _value(lg, player, team):  # noqa: ANN001
    return valuation.Valuator(lg).player(player, team)


def _by_value(lg, team, reverse=True):  # noqa: ANN001
    val = valuation.Valuator(lg)
    return sorted(trades.tradable_players(lg, team), key=lambda p: (val.player(p, team).total, p.id), reverse=reverse)


class ValuationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lg = League.create(seed=31, user_team="NYY")
        self.contender = next(t for t in self.lg.teams.values() if t.strategy == "contending" and t.abbr != "NYY")
        self.rebuilder = next(t for t in self.lg.teams.values() if t.strategy == "rebuilding")

    def test_components_sum_to_total(self) -> None:
        p = next(iter(self.lg.players.values()))
        v = _value(self.lg, p, self.contender)
        self.assertAlmostEqual(v.total, v.asset + v.contract + v.strategic + v.fit)

    def test_strategy_changes_what_clubs_want(self) -> None:
        lg = self.lg
        players = [p for p in lg.players.values() if p.team and p.team not in (self.contender.abbr, self.rebuilder.abbr)]
        prospect = max((p for p in players if p.age <= 21), key=lambda p: p.potential)
        veteran = max((p for p in players if p.age >= 32), key=lambda p: p.overall)
        vet_c, vet_r = _value(lg, veteran, self.contender).total, _value(lg, veteran, self.rebuilder).total
        self.assertGreater(vet_c, vet_r)
        pro_c, pro_r = _value(lg, prospect, self.contender).total, _value(lg, prospect, self.rebuilder).total
        # Priorities: relative to a veteran, the rebuilder prizes the prospect far more.
        self.assertGreater(pro_r / max(vet_r, 1.0), pro_c / max(vet_c, 1.0))
        # Raw prospects (averaged: each club's scouts see individual players differently).
        raw = [p for p in players if p.age <= 22 and p.overall < 50 and p.potential >= 58]
        val = valuation.Valuator(lg)
        self.assertGreater(sum(val.player(p, self.rebuilder).total for p in raw),
                           sum(val.player(p, self.contender).total for p in raw))
        pick = next(k for k in lg.draft_picks if k.round == 1 and k.owner not in (self.contender.abbr, self.rebuilder.abbr))
        val = valuation.Valuator(lg)
        self.assertGreater(val.pick(pick, self.rebuilder).total, val.pick(pick, self.contender).total)

    def test_contract_matters(self) -> None:
        lg = self.lg
        p = max((x for x in lg.players.values() if x.team and x.team != self.rebuilder.abbr and 27 <= x.age <= 30),
                key=lambda x: x.overall)
        p.salary, p.contract_years = contracts.MIN_SALARY, 3
        cheap = _value(lg, p, self.rebuilder).total
        p.salary = 30.0
        expensive = _value(lg, p, self.rebuilder)
        self.assertGreater(cheap, expensive.total + 20)
        self.assertLess(expensive.contract, -50)

    def test_age_and_projection(self) -> None:
        p = next(x for x in self.lg.players.values() if x.age <= 22 and x.potential >= x.overall + 8)
        proj = valuation.project(p, p.potential)
        self.assertGreater(proj[3], proj[0])
        old = next(x for x in self.lg.players.values() if x.age >= 34)
        proj = valuation.project(old, old.potential)
        self.assertLess(proj[2], proj[0])

    def test_injuries_and_expiring_deadline_rentals_lower_value(self) -> None:
        lg = self.lg
        p = max((x for x in lg.players.values() if x.team and x.team != self.contender.abbr), key=lambda x: x.overall)
        healthy = _value(lg, p, self.contender).total
        p.injury_days, p.injury = 90, "torn hamstring"
        self.assertLess(_value(lg, p, self.contender).total, healthy)
        p.injury_days, p.injury = 0, None
        p.contract_years = 1
        early = _value(lg, p, self.contender).asset
        lg.day = trades.deadline_day(lg)
        self.assertLess(_value(lg, p, self.contender).asset, early * 0.7)

    def test_replacement_level_scrubs_have_no_trade_value(self) -> None:
        scrub = min((x for x in self.lg.players.values() if x.team and x.age >= 32), key=lambda x: x.overall)
        self.assertLessEqual(_value(self.lg, scrub, self.contender).total, 0.0)

    def test_frontoffice_entry_point_matches(self) -> None:
        p = next(iter(self.lg.players.values()))
        self.assertAlmostEqual(frontoffice.player_value(self.lg, p, self.contender),
                               _value(self.lg, p, self.contender).total)


class NegotiationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lg = League.create(seed=21, user_team="NYY")
        self.bos = self.lg.teams["BOS"]

    def test_identical_trades_identical_evaluations_even_after_reload(self) -> None:
        lg = self.lg
        prop = TradeProposal("NYY", "BOS", a_players=[_by_value(lg, lg.user)[4].id],
                             b_players=[_by_value(lg, self.bos)[3].id])
        a, b = trades.evaluate(lg, prop), trades.evaluate(lg, prop.copy())
        self.assertEqual((a.value_in, a.value_out, a.verdict), (b.value_in, b.value_out, b.verdict))
        with tempfile.TemporaryDirectory() as tmp:
            loaded = load_league(save_league(lg, Path(tmp) / "t.json"))
        c = trades.evaluate(loaded, prop)
        self.assertAlmostEqual(a.value_in, c.value_in)
        self.assertAlmostEqual(a.value_out, c.value_out)

    def test_many_low_value_players_never_buy_a_star(self) -> None:
        lg = self.lg
        star = _by_value(lg, self.bos)[0]
        pile = [p.id for p in _by_value(lg, lg.user)[8:13]]
        verdict = trades.evaluate(lg, TradeProposal("NYY", "BOS", a_players=pile, b_players=[star.id]))
        self.assertFalse(verdict.accepted)
        self.assertLess(trades.incoming_total([10] * 5), 40)

    def test_salary_dump_onto_cpu_rejected(self) -> None:
        lg = self.lg
        dump = max(trades.tradable_players(lg, lg.user), key=lambda p: p.salary - contracts.market_salary(p))
        dump.salary, dump.contract_years = 25.0, 4
        fringe = _by_value(lg, self.bos, reverse=False)[0]
        prop = TradeProposal("NYY", "BOS", a_players=[dump.id], b_players=[fringe.id])
        verdict = trades.evaluate(lg, prop)
        self.assertFalse(verdict.accepted)
        self.assertIn("salary", verdict.reasons)

    def test_salary_relief_alone_is_not_enough(self) -> None:
        lg = self.lg
        team = self.bos
        team.strategy = "rebuilding"
        vet = max((p for p in trades.tradable_players(lg, team) if p.age >= 29), key=lambda p: p.overall)
        vet.salary, vet.contract_years = 18.0, 1
        junk = [p.id for p in _by_value(lg, lg.user, reverse=False)[:2]]
        prop = TradeProposal("NYY", "BOS", a_players=junk, b_players=[vet.id])
        if trades.validate(lg, prop):
            self.skipTest("cap-illegal for this seed")
        result = trades.negotiate(lg, prop)
        self.assertFalse(result.accepted)
        if result.counter is not None:
            self.assertFalse(set(junk) & set(result.counter.a_players))  # junk dropped from the counter

    def test_counter_is_proportional_and_avoids_core_players(self) -> None:
        lg = self.lg
        from tests.test_trades import near_miss

        prop = near_miss(lg, "BOS")
        result = trades.negotiate(lg, prop)
        self.assertIsNotNone(result.counter)
        self.assertTrue(result.counter_note)
        val = valuation.Valuator(lg)
        core = {p.id for p in _by_value(lg, lg.user)[:3]}
        added = set(result.counter.a_players) - set(prop.a_players)
        self.assertFalse(added & core)
        ev = trades.evaluate(lg, prop)
        for pid in added:
            self.assertLessEqual(val.player(lg.players[pid], self.bos).total,
                                 ev.shortfall * trades.DEFAULT_TOLERANCE.counter_overshoot + 4.5)
        self.assertTrue(trades.is_acceptable(lg, result.counter, "BOS"))

    def test_repeated_offers_are_refused_and_fatigue_builds(self) -> None:
        lg = self.lg
        from tests.test_trades import near_miss

        prop = near_miss(lg, "BOS")
        first = trades.negotiate(lg, prop)
        self.assertFalse(first.repeated)
        second = trades.negotiate(lg, prop)
        self.assertTrue(second.repeated)
        self.assertIn("already turned", second.evaluation.message)
        self.assertIsNone(second.counter)
        self.assertGreater(trades.fatigue_margin(lg, "BOS"), 0)
        lg.day += trades.FATIGUE_WINDOW_DAYS * 2
        self.assertEqual(trades.fatigue_margin(lg, "BOS"), 0)
        self.assertFalse(trades.negotiate(lg, prop).repeated)

    def test_cpu_keeps_catching_and_rotation_depth(self) -> None:
        lg = self.lg
        catchers = [p for p in trades.tradable_players(lg, self.bos) if p.can_play("C")]
        keep = 2
        surplus = [p.id for p in catchers[: max(1, len(catchers) - keep + 1)]]
        best = next(p for p in _by_value(lg, lg.user) if not p.can_play("C"))  # brings no catching back
        prop = TradeProposal("NYY", "BOS", a_players=[best.id], b_players=surplus)
        verdict = trades.evaluate(lg, prop)
        self.assertFalse(verdict.accepted)
        self.assertIn("depth", verdict.reasons)

    def test_contract_impact(self) -> None:
        lg = self.lg
        a, b = _by_value(lg, lg.user)[5], _by_value(lg, self.bos)[5]
        impact = {i.team: i for i in trades.contract_impact(lg, TradeProposal("NYY", "BOS", [a.id], [], [b.id], []))}
        self.assertAlmostEqual(impact["NYY"].change, round(b.salary - a.salary, 2))
        self.assertAlmostEqual(impact["BOS"].change, round(a.salary - b.salary, 2))


class CpuTradingTests(unittest.TestCase):
    def test_needs_detected(self) -> None:
        lg = League.create(seed=31, user_team="NYY")
        team = lg.teams["BOS"]
        starter = lg.players[team.lineup[0].player_id]
        position = team.lineup[0].position
        starter.injury_days, starter.injury = 80, "torn ACL"
        needs = trades.team_needs(lg, team)
        self.assertIn(position, [n.position for n in needs])

    def test_cpu_offers_to_user_have_reasons_and_are_cpu_approved(self) -> None:
        lg = League.create(seed=21, user_team="NYY")
        offers = [o for o in (trades.generate_offer_for_user(lg, random.Random(i)) for i in range(15)) if o]
        self.assertTrue(offers)
        for offer in offers:
            self.assertTrue(offer.reason)
            self.assertFalse(trades.validate(lg, offer.proposal))
            self.assertTrue(trades.evaluate(lg, offer.proposal, evaluator=offer.proposal.team_a).accepted)

    def test_season_of_cpu_trades_is_sensible(self) -> None:
        lg = League.create(seed=17, user_team="NYY")
        lg.sim_regular_season()
        cpu_trades = [t for t in lg.trade_log if lg.user_team not in t["teams"]]
        self.assertTrue(cpu_trades)
        self.assertLessEqual(len(cpu_trades), 30)  # no frenzy
        counts: dict[str, int] = {}
        for t in lg.trade_log:
            for abbr in t["teams"]:
                counts[abbr] = counts.get(abbr, 0) + 1
        self.assertLessEqual(max(counts.values()), trades.CPU_TRADES_PER_SEASON + 1)
        seen: dict[int, int] = {}
        for t in lg.trade_log:
            for pid in t["players"]:
                if pid in seen:
                    self.assertGreaterEqual(t["day"] - seen[pid], trades.FLIP_PROTECTION_DAYS)
                seen[pid] = t["day"]
        notes = [x for x in lg.transactions if x.startswith(f"{lg.year}") and "TRADE" in x]
        self.assertTrue(all("(" in n for n in notes))  # every CPU trade carries its reason
        for team in lg.teams.values():
            self.assertEqual(roster.validate(team, lg.players), [], team.abbr)
            catchers = sum(lg.players[p].can_play("C") for p in team.all_players)
            self.assertGreaterEqual(catchers, 2, team.abbr)


if __name__ == "__main__":
    unittest.main()
