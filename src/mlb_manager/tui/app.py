"""Textual application: title screen and the franchise screen.

The app owns the ``League`` and talks to it only through its public API and
the domain modules. Long simulations run in a worker thread; while one runs,
``busy`` is set and every pane refuses to act.
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, ContentSwitcher, DataTable, Footer, Input, Select, Static

from mlb_manager import __version__, contracts, finance, frontoffice, views
from mlb_manager.franchises import FRANCHISES
from mlb_manager.league import (
    DEFAULT_YEAR,
    PHASE_COMPLETE,
    PHASE_DRAFT,
    PHASE_FREE_AGENCY,
    PHASE_POSTSEASON,
    PHASE_REGULAR,
    PHASE_RESIGN,
    League,
)
from mlb_manager.persistence import (
    SaveError,
    default_save_dir,
    default_save_name,
    list_saves,
    load_league,
    resolve_save_path,
    save_league,
)
from mlb_manager.tui import theme
from mlb_manager.tui.modals import ChoiceScreen, ConfirmScreen, InputScreen
from mlb_manager.tui.panes import PANES, DugoutPane, Pane, fill_table, selected_key

# ----------------------------------------------------------------------------- title


class TitleScreen(Screen[None]):
    BINDINGS = [Binding("q", "app.quit", "Quit")]

    def compose(self) -> ComposeResult:
        with Vertical(id="title-box"):
            yield Static(f"MLB MANAGER {__version__}", classes="pane-title")
            yield Static("Run a big-league front office: roster, trades, draft and a 162-game season.",
                         classes="hint")
            yield Static("New franchise", classes="section-label")
            yield Select([(f"{f.abbr} - {f.full_name}", f.abbr) for f in FRANCHISES],
                         prompt="Choose your club", id="title-team")
            with Horizontal(classes="toolbar"):
                yield Input(placeholder="Seed (blank = random)", id="title-seed", type="integer")
            with Horizontal(classes="actions"):
                yield Button("Start new franchise", id="title-start", classes="act")
            yield Static("Load franchise", classes="section-label")
            yield DataTable(id="title-saves", cursor_type="row")
            with Horizontal(classes="actions"):
                yield Button("Load selected", id="title-load", classes="act")
                yield Button("Quit", id="title-quit", classes="act")

    def on_mount(self) -> None:
        app = self.mlb
        saves = list_saves(app.save_dir)
        rows = [(str(path), [path.name]) for path in saves[:20]]
        table = self.query_one("#title-saves", DataTable)
        if not rows:
            table.display = False
            self.query_one("#title-load", Button).disabled = True
            self.mount(Static(f"No saves in {app.save_dir} yet.", classes="hint"),
                       after=self.query_one("#title-saves"))
            return
        table.styles.height = min(10, len(rows) + 2)
        fill_table(table, [f"Saves in {app.save_dir}"], rows)

    @property
    def mlb(self) -> MLBManagerApp:
        assert isinstance(self.app, MLBManagerApp)
        return self.app

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "title-quit":
            self.app.exit()
        elif event.button.id == "title-start":
            team = self.query_one("#title-team", Select).value
            if not isinstance(team, str):
                self.app.notify("Choose a club first.", severity="warning")
                return
            raw = self.query_one("#title-seed", Input).value.strip()
            seed = int(raw) if raw.lstrip("-").isdigit() else None
            self.mlb.start_new(team, seed)
        elif event.button.id == "title-load":
            key = selected_key(self.query_one("#title-saves", DataTable))
            if key is None:
                self.app.notify("No save selected.", severity="warning")
                return
            self.mlb.load(Path(key))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        if event.row_key.value:
            self.mlb.load(Path(str(event.row_key.value)))


# ----------------------------------------------------------------------------- franchise


SIM_LABELS: dict[str, tuple[str, str, str, str]] = {
    PHASE_REGULAR: ("Next day  n", "Next week  w", "Next month", "To end of season"),
    PHASE_POSTSEASON: ("Next day  n", "Finish round", "", "Finish postseason"),
}
ADVANCE_LABELS: dict[str, str] = {
    PHASE_COMPLETE: "Begin offseason",
    PHASE_RESIGN: "Finish re-signing",
    PHASE_DRAFT: "Finish draft",
    PHASE_FREE_AGENCY: "Start next season",
}


class FranchiseScreen(Screen[None]):
    BINDINGS = [
        *[Binding(key, f"show('{pane_id}')", label, show=pane_id in ("dashboard", "roster", "trades", "season"))
          for pane_id, label, key, _ in PANES],
        Binding("n", "sim('day')", "Next day"),
        Binding("w", "sim('week')", "Next week", show=False),
        Binding("space", "next_play", "Next pitch", show=False),
        *[Binding(key, f"dugout('{action}')", label, show=key in "123a") for key, action, label in (
            ("1", "go:pitch", "Pitch"), ("2", "go:play", "Play"), ("3", "go:pa", "Batter"), ("4", "go:half", "Half"),
            ("5", "go:inning", "Inning"), ("6", "go:game", "Final"), ("a", "auto", "Auto-play"),
            ("y", "answer:0", "Accept"), ("x", "answer:1", "Decline"))],
        Binding("ctrl+s", "save", "Save"),
        Binding("q", "quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Static("", id="topbar")
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield Static("VIEWS", classes="section-label")
                for pane_id, label, key, _ in PANES:
                    yield Button(f"{label:<15}{key}", id=f"nav-{pane_id}", classes="nav")
                yield Static("SIMULATE", classes="section-label")
                for kind in ("day", "week", "month", "season"):
                    yield Button("", id=f"sim-{kind}", classes="sim")
                yield Button("", id="sim-advance", classes="sim")
                yield Static("FILE", classes="section-label")
                yield Button("Save          ^s", id="file-save", classes="nav")
                yield Button("Save as", id="file-saveas", classes="nav")
                yield Button("Quit           q", id="file-quit", classes="nav")
            with ContentSwitcher(id="main", initial="dashboard"):
                for pane_id, _, _, cls in PANES:
                    yield cls(id=pane_id)
        yield Footer()

    @property
    def mlb(self) -> MLBManagerApp:
        assert isinstance(self.app, MLBManagerApp)
        return self.app

    def on_mount(self) -> None:
        self.refresh_all()

    def on_resize(self, event: Any) -> None:
        """Narrow terminals get a slimmer sidebar; Textual reflows everything else."""
        self.set_class(event.size.width < 110, "-narrow")

    # -- rendering -------------------------------------------------------------------

    @property
    def current_pane(self) -> Pane:
        switcher = self.query_one("#main", ContentSwitcher)
        return self.query_one(f"#{switcher.current}", Pane)

    def refresh_all(self) -> None:
        lg = self.mlb.league
        if lg is None:
            return
        team = lg.user
        rec = lg.records()[team.abbr]
        acc = theme.accent(team.abbr)
        payroll = contracts.payroll(team, lg.players)
        r = finance.rules(lg)
        cap = (f"{r.cap_type} cap {finance.format_amount(r.cap_amount)}" if r.has_cap else "no cap")
        cbt = f" · CBT {finance.format_amount(r.cbt_threshold)}" if r.cbt_enabled else ""
        busy = "   · simulating…" if self.mlb.busy else ""
        dirty = "  ·  unsaved" if self.mlb.dirty else ""
        self.query_one("#topbar", Static).update(
            f"[b {acc}]{team.abbr}[/]  {team.full_name}   │   {lg.year} · {views.phase_label(lg)}   │   "
            f"{rec.wins}-{rec.losses} · {views.division_position(lg, team.abbr)}   │   "
            f"Payroll ${payroll:.1f}M · {cap}{cbt}{dirty}{busy}")
        current = self.query_one("#main", ContentSwitcher).current
        for pane_id, *_ in PANES:
            button = self.query_one(f"#nav-{pane_id}", Button)
            button.set_class(pane_id == current, "-current")
            button.styles.color = acc if pane_id == current else None
        labels = SIM_LABELS.get(lg.phase, ("", "", "", ""))
        for kind, label in zip(("day", "week", "month", "season"), labels, strict=True):
            button = self.query_one(f"#sim-{kind}", Button)
            button.label = label
            button.display = bool(label)
            button.disabled = self.mlb.busy
        advance = self.query_one("#sim-advance", Button)
        label = ADVANCE_LABELS.get(lg.phase, "")
        if lg.phase == PHASE_FREE_AGENCY:
            label = f"Start {lg.year + 1} season"
        advance.label = label
        advance.display = bool(label)
        advance.disabled = self.mlb.busy
        self.current_pane.refresh_view()
        self.refresh_bindings()

    # -- actions ---------------------------------------------------------------------

    def action_show(self, pane_id: str) -> None:
        self.query_one("#main", ContentSwitcher).current = pane_id
        self.refresh_all()

    def action_sim(self, kind: str) -> None:
        self.mlb.simulate(kind)

    def action_next_play(self) -> None:
        if self.query_one("#main", ContentSwitcher).current == "game":
            self.query_one(DugoutPane).action_go("pitch")

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Dugout keys only exist on the Game tab while a live game is in progress."""
        if action == "dugout":
            lg = self.mlb.league
            try:
                current = self.query_one("#main", ContentSwitcher).current
            except Exception:  # noqa: BLE001 - not composed yet
                return None
            return True if current == "game" and lg is not None and lg.live_game is not None else None
        return True

    def action_dugout(self, command: str) -> None:
        """Dugout keys work anywhere on the Game tab (the pane handles them when focused)."""
        if self.query_one("#main", ContentSwitcher).current != "game":
            return
        pane = self.query_one(DugoutPane)
        name, _, arg = command.partition(":")
        if name == "go":
            pane.action_go(arg)
        elif name == "auto":
            pane.action_autoplay()
        elif name == "answer":
            pane.action_answer(int(arg))

    def action_save(self) -> None:
        self.mlb.save()

    def action_quit(self) -> None:
        self.mlb.request_quit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid.startswith("nav-"):
            event.stop()
            self.action_show(bid[4:])
        elif bid == "sim-advance":
            event.stop()
            self.mlb.advance_phase()
        elif bid.startswith("sim-"):
            event.stop()
            self.mlb.simulate(bid[4:])
        elif bid == "file-save":
            event.stop()
            self.mlb.save()
        elif bid == "file-saveas":
            event.stop()
            self.mlb.save_as()
        elif bid == "file-quit":
            event.stop()
            self.mlb.request_quit()


# ----------------------------------------------------------------------------- app


class MLBManagerApp(App[None]):
    CSS = theme.CSS
    TITLE = "MLB Manager"

    def __init__(
        self,
        league: League | None = None,
        save_path: Path | None = None,
        save_dir: Path | None = None,
        salary_cap: float | None = None,
        new_team: str | None = None,
        new_seed: int | None = None,
        new_year: int | None = None,
    ) -> None:
        super().__init__()
        self.league = league
        self.save_path = save_path
        self.save_dir = save_dir or default_save_dir()
        self.salary_cap = salary_cap
        self.dirty = league is not None and save_path is None
        self.busy = False
        self._new_team = new_team
        self._new_seed = new_seed
        self._new_year = new_year
        self.last_sim_summary = ""

    def on_mount(self) -> None:
        if self.league is None and self._new_team:
            self.league = League.create(seed=self._new_seed if self._new_seed is not None
                                        else random.SystemRandom().randrange(1, 10**6),
                                        user_team=self._new_team, salary_cap=self.salary_cap,
                                        year=self._new_year or DEFAULT_YEAR)
            self.dirty = True
        if self.league is not None:
            self.push_screen(FranchiseScreen())
        else:
            self.push_screen(TitleScreen())

    # -- league lifecycle ------------------------------------------------------------

    def start_new(self, team: str, seed: int | None) -> None:
        """Open the league setup screen (season and financial rules) for a new franchise."""
        from mlb_manager.tui.setup import LeagueSetupScreen

        seed = seed if seed is not None else random.SystemRandom().randrange(1, 10**6)
        self.switch_screen(LeagueSetupScreen(team, seed, self.salary_cap))

    def create_league(self, team: str, seed: int, year: int, config: finance.FinanceConfig) -> None:
        self.league = League.create(seed=seed, user_team=team, year=year, finance_config=config)
        self.save_path = None
        self.dirty = True
        self.switch_screen(FranchiseScreen())
        self.notify(f"You are now the manager of the {self.league.user.full_name} (seed {seed}).")

    def back_to_title(self) -> None:
        self.switch_screen(TitleScreen())

    def load(self, path: Path) -> None:
        """Read a save and show its financial rules for review before continuing."""
        from mlb_manager.tui.setup import LoadReviewScreen

        try:
            league = load_league(path)
        except SaveError as exc:
            self.notify(str(exc), severity="error", timeout=8)
            return
        self.switch_screen(LoadReviewScreen(league, path))

    def finish_load(self, league: League, path: Path, changed: bool) -> None:
        self.league = league
        self.save_path = path
        self.dirty = changed
        self.switch_screen(FranchiseScreen())
        self.notify(f"Loaded {path.name}." + (" Financial rules updated (unsaved)." if changed else ""))

    def save(self, path: Path | None = None) -> Path | None:
        if self.league is None:
            return None
        target = path or self.save_path or resolve_save_path(default_save_name(self.league), self.save_dir)
        try:
            save_league(self.league, target)
        except SaveError as exc:
            self.notify(str(exc), severity="error", timeout=8)
            return None
        self.save_path = target
        self.dirty = False
        self.notify(f"Saved to {target}")
        self.refresh_franchise()
        return target

    def save_as(self) -> None:
        if self.league is None:
            return
        default = (self.save_path or resolve_save_path(default_save_name(self.league), self.save_dir)).stem

        def got(value: str | None) -> None:
            if value:
                self.save(resolve_save_path(value, self.save_dir))

        self.push_screen(InputScreen("Save name:", default), got)

    def request_quit(self) -> None:
        if not self.dirty or self.league is None:
            self.exit()
            return

        def choose(answer: str | None) -> None:
            if answer == "save":
                if self.save() is not None:
                    self.exit()
            elif answer == "quit":
                self.exit()

        self.push_screen(ChoiceScreen("You have unsaved progress.",
                                      [("save", "Save & quit"), ("quit", "Quit without saving"),
                                       ("cancel", "Cancel")]), choose)

    # -- refresh ---------------------------------------------------------------------

    def refresh_franchise(self) -> None:
        for screen in self.screen_stack:
            if isinstance(screen, FranchiseScreen):
                screen.refresh_all()

    def after_change(self, message: str | None = None, severity: str = "information") -> None:
        self.dirty = True
        self.refresh_franchise()
        if message:
            self.notify(message, severity=severity, timeout=5)  # type: ignore[arg-type]

    # -- simulation ------------------------------------------------------------------

    def simulate(self, kind: str, confirmed: bool = False) -> None:
        lg = self.league
        if lg is None or self.busy:
            return
        if lg.phase not in (PHASE_REGULAR, PHASE_POSTSEASON):
            self.notify("The season is over. Use the offseason button in the sidebar.", severity="warning")
            return
        if lg.live_game is not None and not lg.live_game.is_over and not confirmed:
            self.push_screen(ConfirmScreen("A game is in progress in the dugout. Simulating will let the CPU "
                                           "finish it with your current instructions. Continue?",
                                           yes="Simulate"),
                             lambda ok: ok and self.simulate(kind, confirmed=True))
            return
        self.busy = True
        self.refresh_franchise()
        self.run_worker(lambda: self._sim_thread(kind), thread=True, exclusive=True, group="sim")

    def _sim_thread(self, kind: str) -> None:
        lg = self.league
        assert lg is not None
        before = lg.records()[lg.user_team]
        wins, losses = before.wins, before.losses
        try:
            if kind == "day":
                reports = lg.sim_days(1)
            elif lg.phase == PHASE_REGULAR:
                if kind == "season":
                    reports = lg.sim_regular_season()
                else:
                    reports = lg.sim_days({"week": 7, "month": 30}[kind])
            elif kind == "season":  # postseason: through the World Series
                reports = lg.sim_to_completion()
            else:  # postseason: finish the current round
                current = lg.playoffs.round_index if lg.playoffs else 0
                reports = lg.sim_until(
                    lambda x: x.phase != PHASE_POSTSEASON or x.playoffs is None or x.playoffs.round_index != current)
            after = lg.records()[lg.user_team]
            summary = (f"Simulated {len(reports)} day(s): {after.wins - wins}-{after.losses - losses} "
                       f"(now {after.wins}-{after.losses}).")
            if lg.phase == PHASE_POSTSEASON and kind != "day":
                summary = f"Simulated {len(reports)} postseason day(s)."
            if lg.phase == PHASE_COMPLETE and lg.history:
                summary += f" {lg.history[-1]['champion']} win the World Series."
            error = None
        except Exception as exc:  # noqa: BLE001 - reported to the user, never swallowed silently
            summary, error = "", exc
        self.call_from_thread(self._sim_done, summary, error)

    def _sim_done(self, summary: str, error: Exception | None) -> None:
        self.busy = False
        self.dirty = True
        self.last_sim_summary = summary
        self.refresh_franchise()
        if error is not None:
            self.notify(f"Simulation failed: {error!r}", severity="error", timeout=10)
        else:
            self.notify(summary, timeout=5)

    def play_next_game(self) -> None:
        lg = self.league
        if lg is None or self.busy:
            return
        if lg.phase not in (PHASE_REGULAR, PHASE_POSTSEASON):
            self.notify("No games until next season.", severity="warning")
            return
        self.busy = True
        self.refresh_franchise()

        def run() -> None:
            previous = lg.last_user_game
            error = None
            try:
                for _ in range(10):
                    lg.advance_day()
                    if lg.last_user_game is not previous or lg.phase not in (PHASE_REGULAR, PHASE_POSTSEASON):
                        break
            except Exception as exc:  # noqa: BLE001
                error = exc
            self.call_from_thread(self._game_done, error)

        self.run_worker(run, thread=True, exclusive=True, group="sim")

    def _game_done(self, error: Exception | None, message: str | None = None) -> None:
        self.busy = False
        self.dirty = True
        for screen in self.screen_stack:
            if isinstance(screen, FranchiseScreen):
                screen.action_show("game")
                if self.league is not None and self.league.live_game is not None:
                    screen.query_one("#dug-controls").focus()
        if error is not None:
            self.notify(f"Simulation failed: {error!r}", severity="error", timeout=10)
        elif message:
            self.notify(message, timeout=6)

    # -- live games (dugout) ---------------------------------------------------------

    def start_live_game(self) -> None:
        """Ask for the automation level, play any off-days, then open the dugout."""
        lg = self.league
        if lg is None or self.busy:
            return
        if lg.live_game is not None:
            self._game_done(None)
            return
        if lg.phase not in (PHASE_REGULAR, PHASE_POSTSEASON):
            self.notify("No games until next season.", severity="warning")
            return

        def chosen(preset: str | None) -> None:
            if preset is None:
                return
            self.busy = True
            self.refresh_franchise()

            def run() -> None:
                error, message = None, None
                try:
                    lg.advance_to_user_game()
                    if lg.user_game_today() is None:
                        message = "Your club has no game coming up."
                    else:
                        live = lg.start_live_game()
                        live.set_preset(preset)
                        message = f"Play ball! {live.sim.away.team.abbr} @ {live.sim.home.team.abbr}."
                except Exception as exc:  # noqa: BLE001
                    error = exc
                self.call_from_thread(self._game_done, error, message)

            self.run_worker(run, thread=True, exclusive=True, group="sim")

        self.push_screen(ChoiceScreen("How much do you want to manage? (You can change any category later.)",
                                      [("assisted", "Assisted"), ("manual", "Full manual"),
                                       ("auto", "Automatic")]), chosen)

    def finish_live_day(self) -> None:
        """Record the finished live game and play the rest of the day's games."""
        lg = self.league
        if lg is None or self.busy or lg.live_game is None:
            return
        self.busy = True
        self.refresh_franchise()

        def run() -> None:
            error, message = None, None
            try:
                report = lg.finish_live_game()
                result = lg.last_user_game
                if result is not None:
                    message = "Final: " + views.result_line(lg, result, lg.user_team)
                if report.news:
                    message = (message or "") + f"  ({len(report.news)} news item(s) on the dashboard)"
            except Exception as exc:  # noqa: BLE001
                error = exc
            self.call_from_thread(self._game_done, error, message)

        self.run_worker(run, thread=True, exclusive=True, group="sim")

    # -- offseason -------------------------------------------------------------------

    def advance_phase(self) -> None:
        lg = self.league
        if lg is None or self.busy:
            return

        def run(fn: Any, message: str) -> None:
            news = fn()
            self.after_change(message)
            for item in (news or [])[:3]:
                self.notify(item, timeout=6)

        if lg.phase == PHASE_COMPLETE:
            self.push_screen(ConfirmScreen("Begin the offseason? Players age and develop, contracts tick down "
                                           "and some veterans retire.", yes="Begin offseason"),
                             lambda ok: ok and run(lg.begin_offseason, "The offseason has begun."))
        elif lg.phase == PHASE_RESIGN:
            pending = lg.pending_free_agents(lg.user)
            warning = (f"{len(pending)} unsigned player(s) will leave as free agents. " if pending else "")
            self.push_screen(ConfirmScreen(f"{warning}Close the re-signing period and go to the draft?",
                                           yes="Go to draft"),
                             lambda ok: ok and run(lg.advance_offseason, "The draft class is ready."))
        elif lg.phase == PHASE_DRAFT:
            self.push_screen(ConfirmScreen("Finish the draft (your scouts pick for you) and open free agency?",
                                           yes="Finish draft"),
                             lambda ok: ok and run(lg.advance_offseason, "Free agency is open."))
        elif lg.phase == PHASE_FREE_AGENCY:
            over = len(lg.user.all_players) - frontoffice.MAX_ORG
            warning = (f"Your organisation is {over} over the limit; the lowest-value players will be released. "
                       if over > 0 else "")
            self.push_screen(ConfirmScreen(f"{warning}Close free agency and start the {lg.year + 1} season?",
                                           yes="Start season"),
                             lambda ok: ok and run(lg.advance_offseason, f"Welcome to the {lg.year + 1} season."))


def run(league: League | None = None, save_path: Path | None = None, save_dir: Path | None = None,
        salary_cap: float | None = None, new_team: str | None = None, new_seed: int | None = None,
        new_year: int | None = None) -> int:
    MLBManagerApp(league, save_path, save_dir, salary_cap, new_team, new_seed, new_year).run()
    return 0
