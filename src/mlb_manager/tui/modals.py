"""Modal dialogs: confirmation, text input and the player profile."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static, TabbedContent, TabPane

from mlb_manager import contracts, development, views
from mlb_manager import ratings as rt
from mlb_manager.models import Player

if TYPE_CHECKING:
    from mlb_manager.league import League


class ConfirmScreen(ModalScreen[bool]):
    """Yes/No confirmation for consequential actions (keys: y / n / Esc)."""

    BINDINGS = [Binding("y", "answer(True)", "Yes"), Binding("n", "answer(False)", "No"),
                Binding("escape", "answer(False)", "Cancel")]

    def __init__(self, message: str, yes: str = "Confirm", no: str = "Cancel") -> None:
        super().__init__()
        self.message = message
        self.yes_label = yes
        self.no_label = no

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(self.message, markup=False)
            with Horizontal(classes="dialog-buttons"):
                yield Button(self.yes_label, id="confirm-yes", classes="act")
                yield Button(self.no_label, id="confirm-no", classes="act")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(event.button.id == "confirm-yes")

    def action_answer(self, value: bool) -> None:
        self.dismiss(value)


class ChoiceScreen(ModalScreen[str | None]):
    """Pick one of several labelled options (returns the option id or None)."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, message: str, options: list[tuple[str, str]]) -> None:
        super().__init__()
        self.message = message
        self.options = options

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(self.message, markup=False)
            with Horizontal(classes="dialog-buttons"):
                for key, label in self.options:
                    yield Button(label, id=f"choice-{key}", classes="act")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss((event.button.id or "").removeprefix("choice-"))

    def action_cancel(self) -> None:
        self.dismiss(None)


class InputScreen(ModalScreen[str | None]):
    """Ask for a short text value (Enter accepts, Esc cancels)."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, prompt: str, value: str = "") -> None:
        super().__init__()
        self.prompt = prompt
        self.value = value

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.prompt)
            yield Input(value=self.value, id="input-value")
            with Horizontal(classes="dialog-buttons"):
                yield Button("OK", id="input-ok", classes="act")
                yield Button("Cancel", id="input-cancel", classes="act")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.dismiss(event.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(self.query_one(Input).value if event.button.id == "input-ok" else None)

    def action_cancel(self) -> None:
        self.dismiss(None)


def _ratings_text(p: Player) -> str:
    groups = [rt.PITCHING_ATTRS] if p.is_pitcher else []
    groups += [rt.HITTING_ATTRS, rt.FIELDING_ATTRS]
    if "framing" in p.ratings:
        groups.append(rt.CATCHER_ATTRS)
    lines = []
    for group in groups:
        for attr in group:
            value = p.rating(attr)
            lines.append(f"{rt.ATTRIBUTE_LABELS[attr]:<10}{value:>3}  {'▮' * (value // 5)}")
        lines.append("")
    if not p.is_pitcher:
        lines.append("Defense by position: " + "  ".join(
            f"{pos} {round(rt.defense_rating(p.ratings, pos, p.can_play(pos)))}" for pos in rt.FIELD_POSITIONS))
    return "\n".join(lines).rstrip()


def _overview_text(league: League, p: Player) -> str:
    team = league.teams.get(p.team or "")
    if team is None:
        level = "Free agent"
    elif p.id in team.active:
        level = "Active roster"
    elif p.id in team.injured_list:
        level = "Injured list"
    else:
        level = "Rehab assignment" if p.rehab else "Minor leagues"
    positions = p.position + (f" (also {'/'.join(p.secondary_positions)})" if p.secondary_positions else "")
    lines = [
        f"{positions}   Age {p.age}   Bats/Throws {p.bats}/{p.throws}   {team.full_name if team else 'Free agent'} - {level}",
        f"Overall {p.overall}   Potential (scouted) {views.pot_text(league, p)}"
        + (f"   Development: {p.development}" if p.team == league.user_team else ""),
        "",
        "Contract",
        f"  {contracts.money(p.salary)} per year, {p.contract_years} year(s) left "
        f"({contracts.contract_status(p)}); market value about {contracts.money(contracts.market_salary(p))}",
    ]
    if p.draft_year is not None:
        lines.append(f"  Drafted {p.draft_year}, round {p.draft_round}, pick #{p.draft_pick}")
    lines.append(f"  MLB service: {p.service_years} season(s)")
    lines += ["", "Development"]
    pot = development.scouted_potential(p, league.user_team, league.seed, league.year)
    if p.age >= 31:
        outlook = "veteran: expect gradual decline"
    elif pot > p.overall + 3:
        outlook = f"still growing toward ~{pot} ({development.potential_grade(pot)})"
    else:
        outlook = "at or near his peak"
    lines.append(f"  {outlook}")
    lines += ["", views.health_section(league, p)]
    if p.team:
        lines += ["", views.morale_section(league, p, detailed=p.team == league.user_team)]
    return "\n".join(lines)


def _season_text(league: League, p: Player) -> str:
    from mlb_manager import careers

    lines = [f"{league.year} regular season"]
    if p.batting.pa or p.pitching.g:
        lines.append(views.stat_table(p.is_pitcher, [("", p.batting, p.pitching)]))
    else:
        lines.append("No games played yet.")
    post_b, post_p = careers.current_postseason(p)
    if not (post_b.is_empty and post_p.is_empty):
        lines += ["", f"{league.year} postseason (separate from the regular season)"]
        lines.append(views.stat_table(p.is_pitcher, [("", post_b, post_p)]))
    if p.is_pitcher:
        lines += ["", f"Fatigue: {p.fatigue}"]
    return "\n".join(lines)


class PlayerProfileScreen(ModalScreen[None]):
    """Detailed player profile: overview, attributes, statistics, career, health."""

    BINDINGS = [Binding("escape", "close", "Close"), Binding("q", "close", "Close")]

    def __init__(self, league: League, player_id: int) -> None:
        super().__init__()
        self.league = league
        self.player_id = player_id

    def compose(self) -> ComposeResult:
        lg = self.league
        p = lg.players[self.player_id]
        with Vertical(classes="dialog-wide"):
            yield Static(f"{p.name}  ·  #{p.id}", classes="pane-title", markup=False)
            with TabbedContent(id="profile-tabs"):
                with TabPane("Overview", id="tab-overview"):
                    with VerticalScroll():
                        yield Static(_overview_text(lg, p), markup=False, classes="mono")
                with TabPane("Attributes", id="tab-attributes"):
                    with VerticalScroll():
                        yield Static(_ratings_text(p), markup=False, classes="mono")
                with TabPane("Season", id="tab-season"):
                    with VerticalScroll():
                        yield Static(_season_text(lg, p), markup=False, classes="mono")
                with TabPane("Career", id="tab-career"):
                    with VerticalScroll():
                        yield Static(views.career_section(lg, p), markup=False, classes="mono",
                                     id="profile-career")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Close", id="profile-close", classes="act")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)
