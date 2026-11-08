"""The Game tab: a live dugout console for the user's games.

Three areas:

* **game state** - scoreboard, inning/outs/count with the diamond, and the
  batter-pitcher matchup (stamina, pitch count, repertoire);
* **live feed and reviews** - pitch-by-pitch play-by-play, lineups and
  defense, bullpen, scouting (opponent tendencies, due up, fatigue),
  decisions, instructions and box score;
* **manager controls** - the context-sensitive controls of
  :meth:`mlb_manager.dugout.LiveGame.controls` (Enter or click to change),
  progression buttons (pitch / play / batter / half / inning / final, keys
  1-6), auto-play with pause (``a``) and assisted-mode decisions (``y``/``n``).

The pane holds only UI state; every decision goes through the dugout API.
"""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.timer import Timer
from textual.widgets import Button, DataTable, Select, Static, TabbedContent, TabPane

from mlb_manager import views
from mlb_manager.dugout import UNIT_LABELS, Control, LiveGame
from mlb_manager.game import RuleError
from mlb_manager.league import PHASE_POSTSEASON, PHASE_REGULAR
from mlb_manager.tui.base import Pane
from mlb_manager.tui.modals import ListChoiceScreen

PROGRESS_BUTTONS = (("pitch", "Pitch 1"), ("play", "Play 2"), ("pa", "Batter 3"), ("half", "Half 4"),
                    ("inning", "Inning 5"), ("game", "Final 6"))
AUTO_UNITS = [("Auto: pitch", "pitch"), ("Auto: play", "play"), ("Auto: batter", "pa"), ("Auto: half", "half")]
AUTO_INTERVAL = 0.6
GROUP_ORDER = ("Pitching", "Hitting", "Baserunning", "Defense", "Substitutions", "Automation")
FEED_STYLES = {"P": "#7d868f", "HALF": "bold #8fa7c4", "DEC": "#d6b46b", "SUB": "#d6b46b", "PC": "#d6b46b",
               "SB": "#c4a77a", "CS": "#c4a77a", "PK": "#c4a77a", "TO": "#c4a77a"}


class DugoutPane(Pane):
    pane_title = "Game"
    allowed_during_live_game = True

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.timer: Timer | None = None
        self.auto_unit = "pitch"

    # -- layout ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static("Game", classes="pane-title", id="dug-title")
        with Horizontal(id="dug-top"):
            yield Static("", id="dug-score", markup=False, classes="panel")
            yield Static("", id="dug-situation", markup=False, classes="panel")
            yield Static("", id="dug-matchup", markup=False, classes="panel")
        with Vertical(id="dug-prompt", classes="panel"):
            yield Static("", id="dug-prompt-text", markup=False)
            with Horizontal(classes="dialog-buttons"):
                yield Button("", id="dug-ans-0", classes="act")
                yield Button("", id="dug-ans-1", classes="act")
        with Horizontal(classes="split"):
            with TabbedContent(id="dug-tabs"):
                for key, label in (("feed", "Feed"), ("lineups", "Lineups"), ("bullpen", "Bullpen"),
                                   ("scouting", "Scouting"), ("decisions", "Decisions"), ("plan", "Plan"),
                                   ("box", "Box score")):
                    with TabPane(label, id=f"dug-tab-{key}"):
                        with VerticalScroll():
                            yield Static("", id=f"dug-text-{key}", markup=False, classes="mono")
            with Vertical(id="dug-controls-box"):
                yield Static("MANAGER CONTROLS", classes="section-label")
                yield DataTable(id="dug-controls", cursor_type="row", zebra_stripes=False)
                yield Static("", id="dug-hint", markup=False, classes="hint")
        with Horizontal(classes="actions", id="dug-bar"):
            for unit, label in PROGRESS_BUTTONS:
                yield Button(label, id=f"dug-go-{unit}", classes="act")
            yield Button("▶ Auto  a", id="dug-auto", classes="act")
            yield Select(AUTO_UNITS, value="pitch", allow_blank=False, id="dug-unit")
            yield Button("Sim to end", id="dug-finish", classes="act")
            yield Button("Play live", id="dug-start", classes="act")
            yield Button("Simulate game", id="dug-sim", classes="act")
            yield Button("Finish day", id="dug-day", classes="act")

    def on_mount(self) -> None:
        for wid, title in (("#dug-score", "Scoreboard"), ("#dug-situation", "Situation"),
                           ("#dug-matchup", "Matchup"), ("#dug-prompt", "Decision")):
            self.query_one(wid).border_title = title
        self.query_one("#dug-prompt").display = False

    # -- state -------------------------------------------------------------------

    @property
    def live(self) -> LiveGame | None:
        lg = self.mlb.league
        return lg.live_game if lg is not None else None

    def refresh_view(self) -> None:
        live = self.live
        lg = self.league
        in_season = lg.phase in (PHASE_REGULAR, PHASE_POSTSEASON)
        progressing = live is not None and not live.is_over
        for unit, _ in PROGRESS_BUTTONS:
            self.query_one(f"#dug-go-{unit}", Button).display = progressing
        for wid in ("#dug-auto", "#dug-unit", "#dug-finish"):
            self.query_one(wid).display = progressing
        self.query_one("#dug-start", Button).display = live is None and in_season
        self.query_one("#dug-sim", Button).display = live is None and in_season
        self.query_one("#dug-day", Button).display = live is not None and live.is_over
        busy = self.mlb.busy
        for button in self.query("#dug-bar Button").results(Button):
            button.disabled = busy
        self.query_one("#dug-auto", Button).label = "⏸ Pause  a" if self.timer is not None else "▶ Auto  a"
        if live is None:
            self._draw_idle()
        else:
            self._draw_live(live)

    def _set(self, key: str, text: str) -> None:
        self.query_one(f"#dug-text-{key}", Static).update(text)

    def _draw_idle(self) -> None:
        lg = self.league
        info = lg.user_game_today() if lg.in_season else None
        title = "Game"
        if info is not None:
            title = f"Next game: {info.away} @ {info.home}  ·  {info.label}"
        elif lg.in_season:
            nxt = views.upcoming_games(lg, lg.user_team, 1)
            title = f"Next game: {nxt[0]}" if nxt else "No game scheduled"
        self.query_one("#dug-title", Static).update(title)
        result = lg.last_user_game
        self.query_one("#dug-prompt").display = False
        if result is None:
            self.query_one("#dug-score", Static).update("Your club has not played yet.")
            self.query_one("#dug-situation", Static).update(
                "Play live: manage the game from the dugout,\npitch by pitch.\nSimulate game: let the CPU play it.")
            self.query_one("#dug-matchup", Static).update("")
            for key in ("feed", "lineups", "bullpen", "scouting", "decisions", "plan", "box"):
                self._set(key, "")
        else:
            self.query_one("#dug-score", Static).update(views.line_score(lg, result))
            self.query_one("#dug-situation", Static).update(
                f"Last game: Final{f' ({result.innings})' if result.innings != 9 else ''}\n"
                + views.result_line(lg, result, lg.user_team))
            self.query_one("#dug-matchup", Static).update("")
            events = result.events
            if events:
                self.query_one("#dug-text-feed", Static).update(self._feed(events, 300))
            else:
                self._set("feed", "No play-by-play for this game.")
            self._set("box", views.box_score(lg, result))
            self._set("decisions", views.manager_report(result.manager_report))
            for key in ("lineups", "bullpen", "scouting", "plan"):
                self._set(key, "Available during a live game.")
        table = self.query_one("#dug-controls", DataTable)
        table.clear(columns=True)
        table.add_columns("Control", "Setting")
        self.query_one("#dug-hint", Static).update(
            "Start a live game to manage pitches, at-bats, baserunning, defense and substitutions."
            if lg.in_season else "No games until next season.")

    def _draw_live(self, live: LiveGame) -> None:
        lg = self.league
        sim = live.sim
        self.query_one("#dug-title", Static).update(
            f"{live.label}: {sim.away.team.full_name} @ {sim.home.team.full_name}"
            + ("   ·   FINAL - press 'Finish day'" if live.is_over else ""))
        self.query_one("#dug-score", Static).update(views.live_line_score(lg, live))
        self.query_one("#dug-situation", Static).update(views.live_situation(lg, live))
        self.query_one("#dug-matchup", Static).update(views.matchup_card(lg, live) if not live.is_over else
                                                     views.manager_report(live.result.manager_report
                                                                          if live.result else None)
                                                     .split("\n", 1)[0])
        self.query_one("#dug-text-feed", Static).update(self._feed(live.sim.events or []))
        self._set("lineups", views.live_lineups(lg, live))
        self._set("bullpen", views.bullpen_text(lg, live))
        self._set("scouting", views.matchup_card(lg, live) + "\n\n" + views.scouting_text(lg, live)
                  if not live.is_over else "")
        self._set("decisions", views.decision_log(lg, live) if not live.is_over else
                  views.manager_report(live.result.manager_report if live.result else None))
        self._set("plan", views.live_instructions(live))
        if live.is_over and live.result is not None:
            self._set("box", views.box_score(lg, live.result))
        else:
            self._set("box", "The box score is shown at the end of the game; see Lineups for today's lines.")
        self._draw_prompt(live)
        self._draw_controls(live)

    def _feed(self, events: list[dict[str, Any]], limit: int = 150) -> Text:
        """Newest first, styled by kind: plays, scoring, pitches, managerial moves."""
        lg = self.league
        text = Text()
        for event in reversed(events[-limit:]):
            kind = event["k"]
            style = FEED_STYLES.get(kind, "")
            if kind == "PA" and event.get("n"):
                style = "bold #a7c0a0"
            elif kind == "PA":
                style = "#e3e7ea"
            text.append(views.event_text(lg, event) + "\n", style=style)
        return text if events else Text("Play ball!")

    def _draw_prompt(self, live: LiveGame) -> None:
        box = self.query_one("#dug-prompt")
        prompt = live.prompt
        box.display = prompt is not None
        if prompt is None:
            return
        self.query_one("#dug-prompt-text", Static).update(
            f"{prompt.question}\n{prompt.basis}" if prompt.basis else prompt.question)
        for index in range(2):
            button = self.query_one(f"#dug-ans-{index}", Button)
            if index < len(prompt.options):
                label = prompt.options[index][1]
                button.label = (f"{label}  y" if index == 0 else f"{label}  x")
                button.display = True
            else:
                button.display = False

    def _draw_controls(self, live: LiveGame) -> None:
        table = self.query_one("#dug-controls", DataTable)
        keep = None
        if table.row_count:
            try:
                keep = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
            except Exception:  # noqa: BLE001 - stale cursor
                keep = None
        table.clear(columns=True)
        table.add_columns("Control", "Setting")
        self.controls = {c.key: c for c in live.controls()}
        rows: list[str] = []
        for group in GROUP_ORDER:
            members = [c for c in self.controls.values() if c.group == group]
            if not members:
                continue
            table.add_row(Text(group.upper(), style="bold #69727b"), Text(""), key=f"hdr:{group}")
            rows.append(f"hdr:{group}")
            for c in members:
                table.add_row(*self._cells(c), key=c.key)
                rows.append(c.key)
        if keep in rows:
            table.move_cursor(row=rows.index(keep))
        elif len(rows) > 1:
            table.move_cursor(row=1)
        self._show_hint()

    def _cells(self, c: Control) -> tuple[Text, Text]:
        style = "" if c.enabled else "#59616a"
        label = Text(f"  {c.label}", style=style)
        if not c.enabled:
            return label, Text("unavailable", style="#59616a italic")
        if c.kind == "action":
            value = Text("● ON" if c.value else "do it", style="bold #a7c0a0" if c.value else "#8fa7c4")
        else:
            current = dict(c.options).get(c.value or "", "choose..." if c.value is None else c.value)
            value = Text(current, style="#d3d8dc" if c.value is not None else "#8fa7c4")
        return label, value

    def _selected_control(self) -> Control | None:
        table = self.query_one("#dug-controls", DataTable)
        if table.row_count == 0:
            return None
        try:
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:  # noqa: BLE001
            return None
        return getattr(self, "controls", {}).get(str(key))

    def _show_hint(self) -> None:
        c = self._selected_control()
        hint = self.query_one("#dug-hint", Static)
        if c is None:
            hint.update("Enter/click a control to change it. Keys: 1-6 advance, a auto-play.")
            return
        if not c.enabled:
            hint.update(f"{c.label}: {c.reason}")
        else:
            scope = {"next pitch": "applies to the next pitch", "plan": "persists until changed",
                     "batter": "for this hitter (matchup)", "now": "takes effect immediately",
                     "setting": "automation level for this category", "next play": "next ball in play",
                     "until two strikes": "stays on until two strikes"}.get(c.scope, c.scope)
            hint.update(f"{c.label}: {scope}.")

    # -- events ------------------------------------------------------------------

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "dug-controls":
            self._show_hint()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "dug-controls":
            return
        event.stop()
        control = getattr(self, "controls", {}).get(str(event.row_key.value))
        if control is not None:
            self.use_control(control)

    def use_control(self, control: Control) -> None:
        live = self.live
        if live is None or self.mlb.busy:
            return
        if not control.enabled:
            self.mlb.notify(control.reason or "Not available right now.", severity="warning")
            return
        if control.kind == "action":
            self._apply(control.key, None)
            return

        def chosen(value: str | None) -> None:
            if value is not None:
                self._apply(control.key, value)

        note = {"next pitch": "Applies to the next pitch only.", "plan": "Stays in effect until you change it.",
                "batter": "Applies to this hitter whenever he bats.", "now": "Takes effect immediately."
                }.get(control.scope, "")
        self.mlb.push_screen(ListChoiceScreen(control.label, control.options, control.value, note), chosen)

    def _apply(self, key: str, value: str | None) -> None:
        live = self.live
        if live is None:
            return
        try:
            message = live.apply(key, value)
        except (RuleError, ValueError) as exc:
            self.mlb.notify(str(exc), severity="warning", timeout=5)
            return
        self.mlb.dirty = True
        self.mlb.notify(message, timeout=3)
        self.refresh_view()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "dug-unit":
            event.stop()
            if isinstance(event.value, str):
                self.auto_unit = event.value

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if not bid.startswith("dug-"):
            return
        event.stop()
        if bid.startswith("dug-go-"):
            self.action_go(bid.removeprefix("dug-go-"))
        elif bid == "dug-auto":
            self.action_autoplay()
        elif bid == "dug-finish":
            self.sim_to_end()
        elif bid.startswith("dug-ans-"):
            self.action_answer(int(bid.removeprefix("dug-ans-")))
        elif bid == "dug-start":
            self.mlb.start_live_game()
        elif bid == "dug-sim":
            self.mlb.play_next_game()
        elif bid == "dug-day":
            self.mlb.finish_live_day()

    # -- actions -----------------------------------------------------------------

    def action_go(self, unit: str) -> None:
        live = self.live
        if live is None or live.is_over or self.mlb.busy:
            return
        if live.prompt is not None:
            self.mlb.notify("A decision is waiting (y = recommended, x = the alternative).", severity="warning")
            self.refresh_view()
            return
        report = live.advance(unit)
        self.mlb.dirty = True
        if report.stopped == "final":
            self._stop_timer()
            self.mlb.notify(f"Final: {views.result_line(self.league, live.result, self.league.user_team)}",
                            timeout=6)
            self.mlb.refresh_franchise()
            return
        if report.stopped == "prompt":
            self._stop_timer()
        self.refresh_view()

    def action_autoplay(self) -> None:
        live = self.live
        if self.timer is not None:
            self._stop_timer()
        elif live is not None and not live.is_over:
            self.timer = self.set_interval(AUTO_INTERVAL, self._tick)
        self.refresh_view()

    def _tick(self) -> None:
        live = self.live
        if live is None or live.is_over or live.prompt is not None or self.mlb.busy:
            self._stop_timer()
            if self.is_mounted:
                self.refresh_view()
            return
        self.action_go(self.auto_unit)

    def _stop_timer(self) -> None:
        if self.timer is not None:
            self.timer.stop()
            self.timer = None

    def action_answer(self, index: int) -> None:
        live = self.live
        if live is None or live.prompt is None:
            return
        options = live.prompt.options
        if index >= len(options):
            return
        try:
            message = live.answer_prompt(options[index][0])
        except (RuleError, ValueError) as exc:
            self.mlb.notify(str(exc), severity="warning")
            return
        self.mlb.dirty = True
        self.mlb.notify(message, timeout=3)
        self.refresh_view()

    def sim_to_end(self) -> None:
        live = self.live
        if live is None or live.is_over:
            return
        self._stop_timer()
        live.finish()
        self.mlb.dirty = True
        self.mlb.refresh_franchise()

    def on_unmount(self) -> None:
        self._stop_timer()


__all__ = ["DugoutPane", "UNIT_LABELS"]
