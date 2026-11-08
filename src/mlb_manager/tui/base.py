"""Shared building blocks for the franchise screen panes."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from textual.containers import Vertical
from textual.widgets import DataTable

from mlb_manager.league import League
from mlb_manager.tui.modals import ConfirmScreen

if TYPE_CHECKING:
    from mlb_manager.tui.app import MLBManagerApp


class Pane(Vertical):
    """Base class: access to the league and a uniform refresh hook."""

    pane_title = ""

    @property
    def mlb(self) -> MLBManagerApp:
        return cast("MLBManagerApp", self.app)

    @property
    def league(self) -> League:
        league = self.mlb.league
        assert league is not None
        return league

    def refresh_view(self) -> None:  # pragma: no cover - overridden
        """Re-read the league and redraw."""

    def done(self, message: str | None = None, severity: str = "information") -> None:
        """Report the result of an action and refresh the whole app."""
        self.mlb.after_change(message, severity)

    def fail(self, message: str) -> None:
        self.mlb.notify(message, severity="error", timeout=6)

    def confirm(self, message: str, then: Callable[[], None], yes: str = "Confirm") -> None:
        def callback(answer: bool | None) -> None:
            if answer:
                then()

        self.mlb.push_screen(ConfirmScreen(message, yes=yes), callback)

    def busy(self) -> bool:
        if self.mlb.busy:
            self.mlb.notify("Simulation in progress.", severity="warning")
            return True
        live = self.mlb.league.live_game if self.mlb.league is not None else None
        if live is not None and not self.allowed_during_live_game:
            self.mlb.notify("A game is in progress in the dugout. Finish it (Game tab) before making roster "
                            "moves.", severity="warning")
            return True
        return False

    #: Panes whose actions are allowed while a live game is in progress.
    allowed_during_live_game = False


def selected_key(table: DataTable) -> str | None:
    if table.row_count == 0:
        return None
    try:
        key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
    except Exception:  # noqa: BLE001 - empty or stale cursor
        return None
    return str(key.value) if key is not None and key.value is not None else None


def fill_table(table: DataTable, columns: list[str], rows: list[tuple[str, list[Any]]]) -> None:
    """Replace the table contents, keeping the cursor row where possible."""
    keep = selected_key(table)
    table.clear(columns=True)
    table.add_columns(*columns)
    for key, cells in rows:
        table.add_row(*[str(c) for c in cells], key=key)
    if keep is not None:
        for index, (key, _) in enumerate(rows):
            if key == keep:
                table.move_cursor(row=index)
                break


