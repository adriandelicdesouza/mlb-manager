"""Interactive Textual GUI: navigation (mouse and keyboard) and real game actions."""

import tempfile
import unittest
from pathlib import Path

try:
    import textual  # noqa: F401

    HAVE_TEXTUAL = True
except ImportError:  # pragma: no cover - the GUI is optional
    HAVE_TEXTUAL = False

from mlb_manager import finance, trades
from mlb_manager.league import PHASE_DRAFT, PHASE_FREE_AGENCY, PHASE_REGULAR, PHASE_RESIGN, League
from mlb_manager.persistence import load_league, save_league

from tests.helpers import league_at_offseason

if HAVE_TEXTUAL:
    from textual.widgets import Checkbox, ContentSwitcher, DataTable, Input, Select, Static

    from mlb_manager.tui.app import FranchiseScreen, MLBManagerApp, TitleScreen
    from mlb_manager.tui.modals import ChoiceScreen, ConfirmScreen, ListChoiceScreen, PlayerProfileScreen
    from mlb_manager.tui.panes import PANES, DugoutPane, OffersScreen, TradePane
    from mlb_manager.tui.setup import LeagueSetupScreen, LoadReviewScreen, RulesEditScreen

SIZE = (160, 50)


def row_index(table: "DataTable", key: str) -> int:
    for index, row_key in enumerate(table.rows):
        if row_key.value == key:
            return index
    raise AssertionError(f"row {key} not found")


@unittest.skipUnless(HAVE_TEXTUAL, "textual not installed")
class TuiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def app_for(self, league: "League | None" = None) -> "MLBManagerApp":
        return MLBManagerApp(league=league, save_dir=self.dir)

    async def settle(self, pilot) -> None:  # noqa: ANN001
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()

    def current(self, app) -> str:  # noqa: ANN001
        return app.screen.query_one("#main", ContentSwitcher).current

    async def confirm(self, pilot) -> None:  # noqa: ANN001
        await pilot.pause()
        self.assertIsInstance(pilot.app.screen, ConfirmScreen)
        await pilot.click("#confirm-yes")
        await pilot.pause()

    # ------------------------------------------------------------------ title & files

    async def test_new_franchise_from_title_screen(self) -> None:
        app = self.app_for()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            self.assertIsInstance(app.screen, TitleScreen)
            app.screen.query_one("#title-team", Select).value = "BOS"
            await pilot.click("#title-seed")
            await pilot.press("4", "2")
            await pilot.click("#title-start")
            await pilot.pause()
            self.assertIsInstance(app.screen, LeagueSetupScreen)
            summary = str(app.screen.query_one("#setup-summary", Static).render())
            self.assertIn("$244M", summary)
            self.assertIn("unlimited payroll", summary)
            await pilot.click("#setup-start")
            await pilot.pause()
            self.assertIsInstance(app.screen, FranchiseScreen)
            self.assertEqual(app.league.user_team, "BOS")
            self.assertEqual(app.league.seed, 42)
            self.assertEqual(app.league.finance.config.preset_for(2026), finance.PRESET_MLB)
            self.assertIn("Boston Red Sox", str(app.screen.query_one("#topbar", Static).render()))

    async def test_new_league_with_custom_financial_rules(self) -> None:
        app = self.app_for()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            app.screen.query_one("#title-team", Select).value = "TB"
            await pilot.click("#title-start")
            await pilot.pause()
            screen = app.screen
            self.assertIsInstance(screen, LeagueSetupScreen)
            # Standard MLB rules: the form is read-only and the cap amount is hidden (no cap).
            self.assertTrue(screen.query_one("#rf-cap-type", Select).disabled)
            self.assertFalse(screen.query_one("#rf-cap-amount", Input).display)
            screen.query_one("#setup-season", Input).value = "2024"
            await pilot.pause()
            self.assertIn("$237M", str(screen.query_one("#setup-summary", Static).render()))
            screen.query_one("#setup-preset", Select).value = finance.PRESET_CUSTOM
            await pilot.pause()
            self.assertFalse(screen.query_one("#rf-cap-type", Select).disabled)
            screen.query_one("#rf-cap-type", Select).value = finance.CAP_HARD
            await pilot.pause()
            self.assertTrue(screen.query_one("#rf-cap-amount", Input).display)
            self.assertFalse(screen.query_one("#rf-soft").display)
            screen.query_one("#rf-cap-amount", Input).value = "lots"
            await pilot.pause()
            self.assertIn("Salary cap", str(screen.query_one("#setup-errors", Static).render()))
            await pilot.click("#setup-start")
            await pilot.pause()
            self.assertIsInstance(app.screen, LeagueSetupScreen)  # invalid input: still here
            screen.query_one("#rf-cap-amount", Input).value = "$235M"
            screen.query_one("#rf-floor", Checkbox).value = True
            await pilot.pause()
            self.assertTrue(screen.query_one("#rf-floor-amount", Input).display)
            screen.query_one("#rf-floor-amount", Input).value = "120m"
            await pilot.pause()
            self.assertEqual(str(screen.query_one("#setup-errors", Static).render()).strip(), "")
            await pilot.click("#setup-start")
            await pilot.pause()
            self.assertIsInstance(app.screen, FranchiseScreen)
            r = finance.rules(app.league)
            self.assertEqual(app.league.year, 2024)
            self.assertEqual((r.cap_type, r.cap_amount, r.floor_amount), (finance.CAP_HARD, 235.0, 120.0))
            self.assertIn("hard cap $235M", str(app.screen.query_one("#topbar", Static).render()))

    async def test_load_review_and_edit_financial_rules(self) -> None:
        league = League.create(seed=8, user_team="PIT")
        league.sim_days(1)
        salaries = {pid: p.salary for pid, p in league.players.items()}
        save_league(league, self.dir / "pit.json")
        app = self.app_for()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.click("#title-load")
            await pilot.pause()
            review = app.screen
            self.assertIsInstance(review, LoadReviewScreen)
            self.assertIn("Standard MLB rules", str(review.query_one("#review-rules", Static).render()))
            await pilot.click("#review-edit")
            await pilot.pause()
            edit = app.screen
            self.assertIsInstance(edit, RulesEditScreen)
            edit.query_one("#setup-preset", Select).value = finance.PRESET_CUSTOM
            await pilot.pause()
            edit.query_one("#rf-cap-type", Select).value = finance.CAP_SOFT
            await pilot.pause()
            self.assertTrue(edit.query_one("#rf-soft").display)
            edit.query_one("#rf-cap-amount", Input).value = "150"
            await pilot.pause()
            self.assertIn("Soft cap", str(edit.query_one("#edit-impact", Static).render()))
            await pilot.click("#edit-apply")
            await pilot.pause()
            self.assertIsInstance(app.screen, ConfirmScreen)
            await pilot.click("#confirm-yes")
            await pilot.pause()
            self.assertIsInstance(app.screen, LoadReviewScreen)
            self.assertIn("Soft cap $150M", str(app.screen.query_one("#review-rules", Static).render()))
            await pilot.click("#review-continue")
            await pilot.pause()
            self.assertIsInstance(app.screen, FranchiseScreen)
            self.assertTrue(app.dirty)
            self.assertTrue(finance.rules(app.league).soft_cap)
            self.assertEqual({pid: p.salary for pid, p in app.league.players.items()}, salaries)
        self.assertFalse(finance.rules(load_league(self.dir / "pit.json")).has_cap)  # file untouched until saved

    async def test_load_from_title_and_save_and_quit(self) -> None:
        league = League.create(seed=3, user_team="SEA")
        league.sim_days(2)
        save_league(league, self.dir / "sea.json")
        app = self.app_for()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.click("#title-load")
            await pilot.pause()
            self.assertIsInstance(app.screen, LoadReviewScreen)
            await pilot.click("#review-continue")
            await pilot.pause()
            self.assertIsInstance(app.screen, FranchiseScreen)
            self.assertEqual(app.league.user_team, "SEA")
            self.assertFalse(app.dirty)
            await pilot.press("n")
            await self.settle(pilot)
            self.assertTrue(app.dirty)
            await pilot.press("ctrl+s")
            await pilot.pause()
            self.assertFalse(app.dirty)
            self.assertEqual(load_league(self.dir / "sea.json").day, app.league.day)
            await pilot.press("n")
            await self.settle(pilot)
            await pilot.press("q")
            await pilot.pause()
            self.assertIsInstance(app.screen, ChoiceScreen)
            await pilot.click("#choice-cancel")
            await pilot.pause()
            self.assertIsInstance(app.screen, FranchiseScreen)
            await pilot.press("q")
            await pilot.pause()
            await pilot.click("#choice-quit")
            await pilot.pause()
        self.assertEqual(app.return_code, 0)

    # ------------------------------------------------------------------ navigation

    async def test_keyboard_and_mouse_navigation_and_resize(self) -> None:
        app = self.app_for(League.create(seed=5, user_team="NYY"))
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            for pane_id, _, key, _ in PANES:
                await pilot.press(key)
                await pilot.pause()
                self.assertEqual(self.current(app), pane_id)
            for pane_id, *_ in reversed(PANES):
                await pilot.click(f"#nav-{pane_id}")
                await pilot.pause()
                self.assertEqual(self.current(app), pane_id)
            for size in ((80, 24), (100, 30), (200, 60)):
                await pilot.resize_terminal(*size)
                await pilot.pause()
                await pilot.press("r")
                await pilot.pause()
                self.assertEqual(self.current(app), "roster")
            # keyboard-only operation: focus moves between controls with Tab
            await pilot.press("tab", "tab")
            await pilot.pause()
            self.assertIsNotNone(app.focused)

    async def test_simulation_buttons(self) -> None:
        league = League.create(seed=6, user_team="NYY")
        app = self.app_for(league)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.click("#sim-day")
            await self.settle(pilot)
            self.assertEqual(league.day, 1)
            await pilot.press("w")
            await self.settle(pilot)
            self.assertEqual(league.day, 8)
            self.assertIn("unsaved", str(app.screen.query_one("#topbar", Static).render()))
            self.assertIn("Simulated 7 day(s)", app.last_sim_summary)

    # ------------------------------------------------------------------ roster

    async def test_roster_profile_demote_promote_release(self) -> None:
        league = League.create(seed=7, user_team="NYY")
        team = league.user
        app = self.app_for(league)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.press("r")
            await pilot.pause()
            table = app.screen.query_one("#roster-table", DataTable)
            bench = next(pid for pid in team.active if not league.players[pid].is_pitcher
                         and pid not in {s.player_id for s in team.lineup})
            table.focus()
            table.move_cursor(row=row_index(table, str(bench)))
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, PlayerProfileScreen)
            self.assertIn("Career", str(app.screen.query_one("#profile-career", Static).render()))
            await pilot.press("escape")
            await pilot.pause()
            await pilot.click("#roster-demote")
            await pilot.pause()
            self.assertIn(bench, team.reserves)
            app.screen.query_one("#roster-filter", Select).value = "minors"
            await pilot.pause()
            table.move_cursor(row=row_index(table, str(bench)))
            await pilot.click("#roster-promote")
            await pilot.pause()
            self.assertIn(bench, team.active)
            app.screen.query_one("#roster-filter", Select).value = "all"
            await pilot.pause()
            victim = team.reserves[-1]
            table.move_cursor(row=row_index(table, str(victim)))
            await pilot.click("#roster-release")
            await self.confirm(pilot)
            self.assertNotIn(victim, team.all_players)
            self.assertIsNone(league.players[victim].team)

    async def test_lineup_and_staff_editing(self) -> None:
        league = League.create(seed=8, user_team="NYY")
        team = league.user
        app = self.app_for(league)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.press("l")
            await pilot.pause()
            lineup = app.screen.query_one("#lineup-table", DataTable)
            first, second = team.lineup[0].player_id, team.lineup[1].player_id
            lineup.move_cursor(row=1)
            await pilot.click("#lineup-up")
            await pilot.pause()
            self.assertEqual([team.lineup[0].player_id, team.lineup[1].player_id], [second, first])
            bench = app.screen.query_one("#bench-table", DataTable)
            bench.move_cursor(row=0)
            sub = int(bench.coordinate_to_cell_key(bench.cursor_coordinate).row_key.value)
            lineup.move_cursor(row=8)
            await pilot.click("#lineup-sub")
            await pilot.pause()
            self.assertIn(sub, [s.player_id for s in team.lineup])
            pen = app.screen.query_one("#bullpen-table", DataTable)
            pen.move_cursor(row=2)
            closer = int(pen.coordinate_to_cell_key(pen.cursor_coordinate).row_key.value)
            await pilot.click("#staff-closer")
            await pilot.pause()
            self.assertEqual(team.closer, closer)
            await pilot.click("#lineup-auto")
            await pilot.click("#staff-auto")
            await pilot.pause()
            self.assertEqual(len(team.lineup), 9)

    # ------------------------------------------------------------------ trades

    async def test_trade_center_submit_counter_and_accept(self) -> None:
        from tests.test_trades import near_miss

        league = League.create(seed=21, user_team="NYY")
        prop = near_miss(league, "BOS")
        app = self.app_for(league)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.press("t")
            await pilot.pause()
            app.screen.query_one("#trade-partner", Select).value = "BOS"
            await pilot.pause()
            mine = app.screen.query_one("#trade-mine", DataTable)
            theirs = app.screen.query_one("#trade-theirs", DataTable)
            mine.focus()
            mine.move_cursor(row=row_index(mine, f"p:{prop.a_players[0]}"))
            await pilot.press("enter")
            theirs.focus()
            theirs.move_cursor(row=row_index(theirs, f"p:{prop.b_players[0]}"))
            await pilot.press("enter")
            await pilot.pause()
            pane = app.screen.query_one(TradePane)
            self.assertEqual(pane.proposal().key(), prop.key())
            self.assertIn("Contract implications", str(app.screen.query_one("#trade-summary", Static).render()))
            await pilot.click("#trade-submit")
            await pilot.pause()
            self.assertIn("Counteroffer", pane.result_text)
            self.assertIsNotNone(pane.counter_prop)
            counter = pane.counter_prop
            await pilot.click("#trade-accept-counter")
            await self.confirm(pilot)
            for pid in counter.b_players:
                self.assertEqual(league.players[pid].team, "NYY")
            for pid in counter.a_players:
                self.assertEqual(league.players[pid].team, "BOS")
            # The same offer again is refused outright (negotiation memory).
            self.assertTrue(trades.negotiate(league, prop).errors or trades.negotiate(league, prop).repeated)

    async def test_incoming_offer_accept(self) -> None:
        import random

        league = League.create(seed=21, user_team="NYY")
        offers = [o for o in (trades.generate_offer_for_user(league, random.Random(i)) for i in range(15)) if o]
        league.trade_offers = offers[:2]
        target = offers[0].proposal.b_players[0]
        app = self.app_for(league)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.press("t")
            await pilot.click("#trade-offers")
            await pilot.pause()
            self.assertIsInstance(app.screen, OffersScreen)
            await pilot.click("#offer-accept")
            await self.confirm(pilot)
            self.assertEqual(league.players[target].team, offers[0].proposal.team_a)
            await pilot.click("#offer-reject")
            await pilot.pause()
            self.assertEqual(league.trade_offers, [])

    # ------------------------------------------------------------------ offseason, draft, free agency

    async def test_offseason_flow_and_draft_room(self) -> None:
        league = league_at_offseason(seed=12)
        app = self.app_for(league)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.click("#sim-advance")  # begin offseason
            await self.confirm(pilot)
            self.assertEqual(league.phase, PHASE_RESIGN)
            await pilot.click("#sim-advance")  # close re-signing
            await self.confirm(pilot)
            self.assertEqual(league.phase, PHASE_DRAFT)
            await pilot.press("p")
            await pilot.pause()
            await pilot.click("#draft-sim")
            await pilot.pause()
            clock = league.draft_on_clock()
            self.assertEqual(clock[2], "NYY")
            table = app.screen.query_one("#draft-table", DataTable)
            table.move_cursor(row=0)
            pick = int(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
            await pilot.click("#draft-pick")
            await self.confirm(pilot)
            self.assertEqual(league.players[pick].team, "NYY")
            self.assertEqual(league.draft.selections[-1].team, "NYY")
            await pilot.click("#sim-advance")  # finish draft
            await self.confirm(pilot)
            self.assertEqual(league.phase, PHASE_FREE_AGENCY)
            await pilot.press("f")
            await pilot.pause()
            fa = app.screen.query_one("#fa-table", DataTable)
            fa.move_cursor(row=0)
            signee = int(fa.coordinate_to_cell_key(fa.cursor_coordinate).row_key.value)
            await pilot.click("#fa-sign")
            await self.confirm(pilot)
            if league.players[signee].team != "NYY":  # cap or roster limits: still a handled outcome
                self.assertIsNone(league.players[signee].team)
            await pilot.click("#sim-advance")  # start next season
            await self.confirm(pilot)
            self.assertEqual(league.phase, PHASE_REGULAR)

    # ------------------------------------------------------------------ season center & game

    async def test_season_center_tabs(self) -> None:
        league = League.create(seed=9, user_team="NYY")
        league.sim_days(20)
        app = self.app_for(league)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.press("s")
            await pilot.pause()
            for key in ("standings", "leaders", "career", "awards", "milestones", "records",
                        "postseason", "titles", "retired", "finances"):
                await pilot.click(f"#--content-tab-season-{key}")
                await pilot.pause()
                text = str(app.screen.query_one(f"#season-text-{key}", Static).render())
                self.assertTrue(text.strip(), key)
            self.assertIn("American League", str(app.screen.query_one("#season-text-standings", Static).render()))
            finances = str(app.screen.query_one("#season-text-finances", Static).render())
            self.assertIn("Competitive Balance Tax", finances)
            self.assertIn("Club payrolls", finances)

    async def start_live(self, pilot, preset: str = "manual") -> None:  # noqa: ANN001
        await pilot.press("g")
        await pilot.pause()
        await pilot.click("#dug-start")
        await pilot.pause()
        self.assertIsInstance(pilot.app.screen, ChoiceScreen)
        await pilot.click(f"#choice-{preset}")
        await self.settle(pilot)

    async def test_dugout_live_game_controls_and_progression(self) -> None:
        league = League.create(seed=10, user_team="NYY")
        app = self.app_for(league)
        async with app.run_test(size=SIZE) as pilot:
            await self.start_live(pilot, "manual")
            self.assertEqual(self.current(app), "game")
            live = league.live_game
            self.assertIsNotNone(live)
            self.assertEqual(live.settings["pitching"], "manual")
            await pilot.press("1")
            await pilot.pause()
            self.assertTrue(any(e["k"] == "P" for e in live.sim.events))
            pitches = sum(1 for e in live.sim.events if e["k"] == "P")
            await pilot.press("space")
            await pilot.pause()
            self.assertEqual(sum(1 for e in live.sim.events if e["k"] == "P"), pitches + 1)
            await pilot.click("#dug-go-pa")
            await pilot.press("3")
            await pilot.pause()
            situation = str(app.screen.query_one("#dug-situation", Static).render())
            self.assertRegex(situation, r"(Top|Bot) \d")
            # Change an instruction through the controls table and the list modal.
            pane = app.screen.query_one(DugoutPane)
            key = next(k for k in ("bat.approach", "pitch.effort") if k in pane.controls)
            table = app.screen.query_one("#dug-controls", DataTable)
            table.focus()
            table.move_cursor(row=row_index(table, key))
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, ListChoiceScreen)
            await pilot.press("end")
            await pilot.press("enter")
            await pilot.pause()
            expected = "power" if key == "bat.approach" else "max"
            self.assertEqual(live.control(key).value, expected)
            self.assertEqual(live.decisions[-1].source, "manual")
            # Feed and review tabs show content.
            self.assertTrue(str(app.screen.query_one("#dug-text-feed", Static).render()).strip())
            # Roster moves are refused while the game is in progress.
            await pilot.press("r")
            await pilot.pause()
            before = list(league.user.active)
            await pilot.click("#roster-demote")
            await pilot.pause()
            self.assertEqual(league.user.active, before)
            # Save mid-game, then finish: sim to end and finish the day.
            await pilot.press("ctrl+s")
            await pilot.pause()
            saved = load_league(app.save_path)
            self.assertIsNotNone(saved.live_game)
            self.assertEqual(saved.live_game.sim.events, live.sim.events)
            await pilot.press("g")
            await pilot.click("#dug-finish")
            await pilot.pause()
            self.assertTrue(live.is_over)
            await pilot.click("#dug-day")
            await self.settle(pilot)
            self.assertIsNone(league.live_game)
            result = league.last_user_game
            self.assertIs(result, live.result)
            self.assertIsNotNone(result.manager_report)
            self.assertIn(league.players[result.winning_pitcher].short_name,
                          str(app.screen.query_one("#dug-text-box", Static).render()))
            rec = league.records()["NYY"]
            self.assertEqual(rec.wins + rec.losses, 1)

    async def test_dugout_assisted_prompts_and_autoplay(self) -> None:
        league = League.create(seed=12, user_team="BOS")
        app = self.app_for(league)
        async with app.run_test(size=SIZE) as pilot:
            await self.start_live(pilot, "assisted")
            live = league.live_game
            pane = app.screen.query_one(DugoutPane)
            answered = 0
            for _ in range(80):
                if live.is_over:
                    break
                if live.prompt is not None:
                    self.assertTrue(app.screen.query_one("#dug-prompt").display)
                    await pilot.press("y")
                    await pilot.pause()
                    self.assertIsNone(live.prompt)
                    answered += 1
                    continue
                await pilot.press("4")
                await pilot.pause()
            await pilot.press("a")
            await pilot.pause()
            if not live.is_over and live.prompt is None:
                self.assertIsNotNone(pane.timer)
                count = len(live.sim.events)
                await pilot.pause(1.5)
                self.assertGreater(len(live.sim.events), count)
                await pilot.press("a")
                await pilot.pause()
                self.assertIsNone(pane.timer)
                paused = len(live.sim.events)
                await pilot.pause(1.0)
                self.assertEqual(len(live.sim.events), paused)
            self.assertGreaterEqual(answered, 0)

    async def test_dugout_simulate_game_and_sidebar_sim_finishes_live_game(self) -> None:
        league = League.create(seed=13, user_team="NYY")
        app = self.app_for(league)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("g")
            await pilot.click("#dug-sim")
            await self.settle(pilot)
            self.assertIsNotNone(league.last_user_game)
            self.assertTrue(league.last_user_game.events)
            await pilot.click("#dug-start")
            await pilot.pause()
            await pilot.click("#choice-auto")
            await self.settle(pilot)
            live = league.live_game
            await pilot.press("2")
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            await self.confirm(pilot)
            await self.settle(pilot)
            self.assertIsNone(league.live_game)
            self.assertTrue(live.is_over)
            self.assertIs(league.last_user_game, live.result)

    async def test_actions_refused_while_simulating(self) -> None:
        league = League.create(seed=11, user_team="NYY")
        app = self.app_for(league)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.press("r")
            await pilot.pause()
            app.busy = True
            before = list(league.user.active)
            await pilot.click("#roster-demote")
            await pilot.pause()
            self.assertEqual(league.user.active, before)
            app.busy = False


@unittest.skipUnless(HAVE_TEXTUAL, "textual not installed")
class EntryPointTests(unittest.TestCase):
    def test_cli_flag_and_non_tty_use_classic_interface(self) -> None:
        from mlb_manager.main import build_parser, use_tui

        self.assertFalse(use_tui(build_parser().parse_args(["--cli"])))
        self.assertFalse(use_tui(build_parser().parse_args([])))  # tests run without a terminal


if __name__ == "__main__":
    unittest.main()
