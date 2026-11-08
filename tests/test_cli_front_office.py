import random
import tempfile
import unittest
from pathlib import Path

from mlb_manager import contracts, freeagency, frontoffice, trades
from mlb_manager.cli import App
from mlb_manager.league import PHASE_REGULAR, League

from tests.helpers import league_at_offseason
from tests.test_cli import ScriptedIO


def by_value(league, team, reverse=True):
    return sorted(trades.tradable_players(league, team),
                  key=lambda p: frontoffice.player_value(league, p, team), reverse=reverse)


class FrontOfficeCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_app(self, league: League, answers: list[str]) -> str:
        io_ = ScriptedIO(answers)
        App(io_.input, io_.output, save_dir=self.dir).run(league)
        return io_.text

    def test_front_office_screens(self) -> None:
        league = League.create(seed=101, user_team="NYY")
        text = self.run_app(league, [
            "14",
            "1", "0",                       # contracts & payroll
            "2", "4", "3", "BOS", "0",      # trade center: my picks, scout BOS
            "3", "2", "SP", "3", "0",       # free agency: filter SP, show more
            "4",                            # draft (not in draft phase: info only)
            "5", "0",                       # prospects
            "6",                            # transactions
            "0", "0", "n", "0",
        ])
        for expected in ("FRONT OFFICE", "contracts & payroll", "Future commitments",
                         "Pick code", "Boston Red Sox (BOS) - strategy", "Free agents (",
                         "amateur draft is held during the offseason", "minor league system"):
            self.assertIn(expected, text)

    def test_propose_and_complete_trade(self) -> None:
        league = League.create(seed=21, user_team="NYY")
        bos = league.teams["BOS"]
        target = by_value(league, bos, reverse=False)[5]
        offer = by_value(league, league.user)[0]
        text = self.run_app(league, [
            "14", "2", "1", "BOS", str(offer.id), "", str(target.id), "", "y",
            "0", "0", "0", "n", "0",
        ])
        self.assertIn("BOS accepts the trade.", text)
        self.assertIn("TRADE:", text)
        self.assertEqual(league.players[target.id].team, "NYY")
        self.assertEqual(league.players[offer.id].team, "BOS")

    def test_counter_offer_flow(self) -> None:
        league = League.create(seed=21, user_team="NYY")
        star = by_value(league, league.teams["BOS"])[0]
        text = self.run_app(league, [
            "14", "2", "1", "BOS", "", f"{league.draft_year}-R2-NYY", str(star.id), "", "y",
            "0", "0", "0", "n", "0",
        ])
        self.assertIn("BOS counters with:", text)
        self.assertEqual(league.players[star.id].team, "NYY")

    def test_invalid_trade_reported(self) -> None:
        league = League.create(seed=21, user_team="NYY")
        text = self.run_app(league, [
            "14", "2", "1", "BOS", "abc", "1", "", "", "",
            "0", "0", "0", "n", "0",
        ])
        self.assertIn("numeric player IDs", text)

    def test_respond_to_offers(self) -> None:
        league = League.create(seed=21, user_team="NYY")
        offers = [o for o in (trades.generate_offer_for_user(league, random.Random(i)) for i in range(12)) if o]
        league.trade_offers = offers[:2]
        first, second = offers[0], offers[1]
        text = self.run_app(league, [
            "14", "2",
            "2", str(second.offer_id), "2",   # reject the second
            "2", str(first.offer_id), "1",    # accept the first
            "0", "0", "0", "n", "0",
        ])
        self.assertIn("Offer rejected.", text)
        self.assertIn("TRADE:", text)
        self.assertEqual(league.trade_offers, [])

    def test_sign_free_agent_in_season(self) -> None:
        league = League.create(seed=102, user_team="SEA")
        fa = freeagency.free_agents(league)[0]
        text = self.run_app(league, ["14", "3", "1", str(fa.id), "", "", "0", "0", "0", "n", "0"])
        self.assertIn("signed free agent", text)
        self.assertEqual(fa.team, "SEA")

    def test_prospect_moves(self) -> None:
        league = League.create(seed=103, user_team="PIT")
        team = league.user
        hitter = next(p for p in team.active if not league.players[p].is_pitcher)
        text = self.run_app(league, ["14", "5", "2", str(hitter), "1", str(hitter), "5",
                                     "0", "0", "0", "n", "0"])
        self.assertIn("sent to the minors", text)
        self.assertIn("promoted", text)
        self.assertIn("Active roster, lineup and pitching staff rebuilt", text)
        self.assertEqual(len(team.active), 26)

    def test_full_offseason_through_menus(self) -> None:
        # Replay the same decisions on a twin league to learn deterministic ids.
        twin = league_at_offseason(seed=104, user="DET")
        twin.begin_offseason()
        keep = max(twin.pending_free_agents(twin.user), key=lambda p: p.overall)
        twin.extend_contract(twin.user, keep.id, 2)
        twin.advance_offseason()
        twin.draft_until_user()
        prospect = twin.draft.pool[0]
        twin.draft_select(prospect)
        twin.advance_offseason()
        space = contracts.cap_space(twin.user, twin.players, twin.salary_cap)
        fa = next(p for p in freeagency.free_agents(twin) if freeagency.asking(twin, p)[0] <= space)

        league = league_at_offseason(seed=104, user="DET")
        text = self.run_app(league, [
            "12", "y",                                   # begin offseason
            "12", "1", "1", str(keep.id), "2", "y", "0",  # re-sign best expiring player
            "12", "2", "y",                              # finish re-signing -> draft
            "12", "1", "1", "2", str(prospect), "3",     # draft room: sim, pick, auto rest
            "12", "2",                                   # close draft -> free agency
            "12", "1", "1", str(fa.id), "", "", "0",     # sign a free agent
            "12", "2",                                   # finish free agency -> new season
            "0", "n", "0",
        ])
        self.assertIn("re-signed", text)
        self.assertIn("you select", text)
        self.assertIn("signed free agent", text)
        self.assertIn("Welcome to 2027!", text)
        self.assertEqual(league.phase, PHASE_REGULAR)
        self.assertEqual(league.players[prospect].team, "DET")
        self.assertEqual(league.players[keep.id].team, "DET")
        self.assertEqual(league.players[fa.id].team, "DET")
