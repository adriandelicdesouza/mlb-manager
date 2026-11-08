"""Content panes of the franchise screen.

Every pane reads state from ``app.league`` and acts through the existing
domain APIs (``League``, ``roster``, ``trades``, ``freeagency``...). Panes hold
only UI state (current selection, filters); no game rules live here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from textual.app import ComposeResult
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Select, Static, TabbedContent, TabPane

from mlb_manager import contracts, development, finance, freeagency, morale, roster, trades, views
from mlb_manager import ratings as rt
from mlb_manager.league import PHASE_DRAFT, PHASE_POSTSEASON, ContractError, League
from mlb_manager.models import Player
from mlb_manager.tui.base import Pane, fill_table, selected_key
from mlb_manager.tui.dugout import DugoutPane
from mlb_manager.tui.modals import ConfirmScreen, InputScreen, PlayerProfileScreen

if TYPE_CHECKING:
    from mlb_manager.tui.app import MLBManagerApp

__all__ = ["PANES", "DugoutPane", "Pane", "fill_table", "selected_key"]

POSITIONS = ("SP", "RP", "C", "1B", "2B", "3B", "SS", "LF", "CF", "RF", "DH")


def money(value: float) -> str:
    return f"${value:,.2f}M"


def status_of(league: League, p: Player) -> str:
    team = league.teams.get(p.team or "")
    if team is None:
        return "FA"
    if p.id in team.injured_list:
        return "IL"
    if p.rehab:
        return "Rehab"
    return "MLB" if p.id in team.active else "Minors"


def health_of(p: Player) -> str:
    if p.is_injured:
        return f"{p.injury_days}d"
    if p.rust_days:
        return "rusty"
    return "-"


# ----------------------------------------------------------------------------- dashboard


class DashboardPane(Pane):
    pane_title = "Dashboard"

    def compose(self) -> ComposeResult:
        yield Static("", id="dash-title", classes="pane-title")
        with VerticalScroll():
            with Grid(id="dash-grid"):
                for key, title in (("club", "Club"), ("next", "Upcoming"), ("recent", "Recent results"),
                                   ("payroll", "Payroll"), ("injuries", "Injuries"), ("offers", "Trade offers")):
                    panel = Static("", id=f"dash-{key}", classes="panel", markup=False)
                    panel.border_title = title
                    yield panel
            milestones = Static("", id="dash-milestones", classes="panel", markup=False)
            milestones.border_title = "Recent milestones"
            yield milestones
            news = Static("", id="dash-news", classes="panel", markup=False)
            news.border_title = "News"
            yield news

    def refresh_view(self) -> None:
        lg = self.league
        team = lg.user
        rec = lg.records()[team.abbr]
        self.query_one("#dash-title", Static).update(f"{team.full_name} ({team.abbr})")
        streak = f"   Streak {rec.streak}" if rec.games else ""
        self.query_one("#dash-club", Static).update(
            f"{lg.year} · {views.phase_label(lg)}\n"
            f"Record {rec.wins}-{rec.losses}{streak}\n"
            f"{views.division_position(lg, team.abbr)}\n"
            f"Run differential {rec.run_diff:+d}\nStrategy: {team.strategy}")
        upcoming = views.upcoming_games(lg, team.abbr, 4)
        if lg.in_offseason:
            upcoming = [views.PHASE_LABELS[lg.phase]]
        self.query_one("#dash-next", Static).update("\n".join(upcoming) or "No games scheduled.")
        recent = views.recent_results(lg, team.abbr, 5)
        if lg.last_user_game is not None and lg.phase == PHASE_POSTSEASON:
            recent.insert(0, "Last: " + views.result_line(lg, lg.last_user_game, team.abbr))
        self.query_one("#dash-recent", Static).update("\n".join(recent) or "No games played yet.")
        commitments = contracts.commitments(team, lg.players, seasons=2)
        payroll = contracts.payroll(team, lg.players)
        r = finance.rules(lg)
        room = finance.cap_room(lg, team)
        cap_line = ("Cap       none (unlimited)" if room is None else
                    f"Cap       {finance.format_amount(r.cap_amount)} {r.cap_type} · "
                    + (f"room {money(room)}" if room >= 0 else f"OVER {money(-room)}"))
        if r.cbt_enabled:
            bill = finance.projected_tax(lg, team)
            cbt_line = (f"CBT       {finance.format_amount(bill.cbt_payroll)} / {finance.format_amount(r.cbt_threshold)}"
                        + (f" · tax {finance.format_amount(bill.total)}" if bill.total else ""))
        else:
            cbt_line = "CBT       none"
        floor_line = f"\nFloor     {finance.format_amount(r.floor_amount)}" if r.has_floor else ""
        self.query_one("#dash-payroll", Static).update(
            f"Payroll   {money(payroll)}\n{cap_line}{floor_line}\n{cbt_line}\n"
            f"Next year {money(commitments[1])} committed\n"
            f"Org {len(team.all_players)}/45 · active {len(team.active)}/26")
        hurt = lg.injured_players(team)
        lines = [f"{p.short_name:<14} {p.position:<2} {p.injury or 'rusty':<18.18} {health_of(p):>5}"
                 for p in hurt[:5]]
        self.query_one("#dash-injuries", Static).update("\n".join(lines) or "Everyone is healthy.")
        offers = [f"#{o.offer_id} {o.proposal.team_a}: {o.reason or 'trade offer'}" for o in lg.trade_offers]
        self.query_one("#dash-offers", Static).update("\n".join(offers[:5]) or "No pending offers.")
        self.query_one("#dash-milestones", Static).update(
            "\n".join(lg.milestones[-5:]) or "No milestones yet this season.")
        self.query_one("#dash-news", Static).update("\n".join(f"· {n}" for n in lg.news[-8:]))


# ----------------------------------------------------------------------------- roster


class RosterPane(Pane):
    pane_title = "Roster"
    FILTERS = (("Active roster", "active"), ("Minor leagues", "minors"), ("Injured / IL / rehab", "injured"),
               ("Whole organisation", "all"), ("Pending free agents", "pending"))

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.level_filter = "active"

    def compose(self) -> ComposeResult:
        yield Static("Roster", classes="pane-title")
        yield Select(self.FILTERS, value="active", allow_blank=False, id="roster-filter")
        table = DataTable(id="roster-table", cursor_type="row", zebra_stripes=True)
        yield table
        with Horizontal(classes="actions"):
            yield Button("Profile", id="roster-profile", classes="act")
            yield Button("Promote", id="roster-promote", classes="act")
            yield Button("Demote", id="roster-demote", classes="act")
            yield Button("Place on IL", id="roster-il", classes="act")
            yield Button("Activate", id="roster-activate", classes="act")
            yield Button("Rehab", id="roster-rehab", classes="act")
            yield Button("Re-sign / extend", id="roster-extend", classes="act")
            yield Button("Release", id="roster-release", classes="act -danger")
        yield Static("Enter or click a row for the player profile.", classes="hint", id="roster-hint")

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "roster-filter" and isinstance(event.value, str):
            self.level_filter = event.value
            self.refresh_view()

    def members(self) -> list[Player]:
        lg, team = self.league, self.league.user
        ids = {
            "active": team.active,
            "minors": [p for p in team.reserves if not lg.players[p].rehab],
            "injured": [p.id for p in lg.injured_players(team)],
            "all": team.all_players,
            "pending": [p.id for p in lg.pending_free_agents(team)],
        }[self.level_filter]
        players = [lg.players[pid] for pid in ids]
        return sorted(players, key=lambda p: (p.is_pitcher, rt.ALL_POSITIONS.index(p.position)
                                              if p.position in rt.ALL_POSITIONS else 99, -p.overall, p.id))

    def refresh_view(self) -> None:
        lg = self.league
        rows = []
        for p in self.members():
            pot = development.scouted_potential(p, lg.user_team, lg.seed, lg.year)
            rows.append((str(p.id), [p.name, p.position, p.age, p.overall, pot, money(p.salary),
                                     p.contract_years, status_of(lg, p), health_of(p), morale.label(p.morale)]))
        fill_table(self.query_one("#roster-table", DataTable),
                   ["Name", "Pos", "Age", "OVR", "POT", "Salary", "Yrs", "Level", "Health", "Morale"], rows)
        team = lg.user
        self.query_one("#roster-hint", Static).update(
            f"Active {len(team.active)}/{roster.MAX_ACTIVE} · minors {len(team.reserves)} · IL "
            f"{len(team.injured_list)} · organisation {len(team.all_players)}/45. Enter or click a row for the profile.")

    def current(self) -> Player | None:
        key = selected_key(self.query_one("#roster-table", DataTable))
        return self.league.players.get(int(key)) if key else None

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        if event.row_key.value is not None:
            self.mlb.push_screen(PlayerProfileScreen(self.league, int(event.row_key.value)))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if self.busy():
            return
        player = self.current()
        if player is None:
            self.fail("Select a player first.")
            return
        lg, team = self.league, self.league.user
        action = event.button.id
        try:
            if action == "roster-profile":
                self.mlb.push_screen(PlayerProfileScreen(lg, player.id))
            elif action == "roster-promote":
                roster.promote(team, lg.players, player.id)
                self.done(f"{player.name} promoted to the active roster.")
            elif action == "roster-demote":
                notes = roster.demote(team, lg.players, player.id)
                self.done(" ".join([f"{player.name} sent to the minors.", *notes]))
            elif action == "roster-il":
                notes = lg.place_on_il(team, player.id)
                self.done(" ".join(notes))
            elif action == "roster-activate":
                self.done(lg.activate_from_il(team, player.id))
            elif action == "roster-rehab":
                self.done(lg.start_rehab(team, player.id))
            elif action == "roster-extend":
                self._extend(player)
            elif action == "roster-release":
                cost = player.salary if player.contract_years > 0 else 0.0
                self.confirm(f"Release {player.name}? Dead money this season: {money(cost)}.",
                             lambda: self.done(lg.release(team, player.id)), yes="Release")
        except (roster.RosterError, ContractError, ValueError) as exc:
            self.fail(str(exc))

    def _extend(self, player: Player) -> None:
        lg = self.league
        salary, years = lg.resign_ask(player)
        verb = "Re-sign" if player.contract_years <= 0 else "Extend"

        def go() -> None:
            try:
                self.done(lg.extend_contract(lg.user, player.id, years))
            except (ContractError, ValueError) as exc:
                self.fail(str(exc))

        self.confirm(f"{verb} {player.name}: {years} year(s) at {money(salary)} per year "
                     f"(mood: {morale.label(player.morale).lower()})?", go, yes=verb)


# ----------------------------------------------------------------------------- lineup & staff


class LineupPane(Pane):
    pane_title = "Lineup & staff"

    def compose(self) -> ComposeResult:
        yield Static("Lineup & pitching staff", classes="pane-title")
        with Horizontal(classes="split"):
            with Vertical(classes="half"):
                yield Static("Batting order", classes="hint")
                yield DataTable(id="lineup-table", cursor_type="row")
                yield Static("Bench", classes="hint")
                yield DataTable(id="bench-table", cursor_type="row")
                with Horizontal(classes="actions"):
                    yield Button("Move up", id="lineup-up", classes="act")
                    yield Button("Move down", id="lineup-down", classes="act")
                    yield Button("Start bench player", id="lineup-sub", classes="act")
                    yield Button("Auto lineup", id="lineup-auto", classes="act")
            with Vertical(classes="half"):
                yield Static("Rotation", classes="hint")
                yield DataTable(id="rotation-table", cursor_type="row")
                yield Static("Bullpen", classes="hint")
                yield DataTable(id="bullpen-table", cursor_type="row")
                with Horizontal(classes="actions"):
                    yield Button("To rotation slot", id="staff-rotation", classes="act")
                    yield Button("Make closer", id="staff-closer", classes="act")
                    yield Button("Auto staff", id="staff-auto", classes="act")

    def refresh_view(self) -> None:
        lg, team = self.league, self.league.user
        players = lg.players
        rows = []
        for i, slot in enumerate(team.lineup):
            p = players[slot.player_id]
            rows.append((str(i), [i + 1, slot.position, views.display_name(p), p.overall_at(slot.position),
                                  views.pct(p.batting.avg), views.pct(p.batting.ops), p.batting.hr]))
        fill_table(self.query_one("#lineup-table", DataTable),
                   ["#", "Pos", "Player", "Rtg", "AVG", "OPS", "HR"], rows)
        starters = {s.player_id for s in team.lineup}
        bench = [p for p in roster.active_hitters(team, players) if p.id not in starters]
        fill_table(self.query_one("#bench-table", DataTable), ["Player", "Pos", "OVR", "AVG", "Health"],
                   [(str(p.id), [p.name, "/".join(p.eligible_positions), p.overall, views.pct(p.batting.avg),
                                 health_of(p)]) for p in bench])
        fill_table(self.query_one("#rotation-table", DataTable), ["#", "Pitcher", "OVR", "Fatigue", "W-L", "ERA"],
                   [(str(i), [i + 1, views.display_name(players[pid]), players[pid].overall, players[pid].fatigue,
                              f"{players[pid].pitching.w}-{players[pid].pitching.l}",
                              f"{players[pid].pitching.era:.2f}"]) for i, pid in enumerate(team.rotation)])
        fill_table(self.query_one("#bullpen-table", DataTable), ["Pitcher", "Role", "OVR", "Fatigue", "SV", "ERA"],
                   [(str(p.id), [p.name, "Closer" if p.id == team.closer else p.position, p.overall, p.fatigue,
                                 p.pitching.sv, f"{p.pitching.era:.2f}"]) for p in roster.bullpen(team, players)])

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if self.busy():
            return
        lg, team = self.league, self.league.user
        action = event.button.id or ""
        lineup_key = selected_key(self.query_one("#lineup-table", DataTable))
        try:
            if action in ("lineup-up", "lineup-down"):
                if lineup_key is None:
                    raise roster.RosterError("Select a batting-order spot.")
                index = int(lineup_key)
                other = index - 1 if action == "lineup-up" else index + 1
                if not 0 <= other < len(team.lineup):
                    return
                roster.swap_batting_order(team, index, other)
                self.done("Batting order updated.")
                self.query_one("#lineup-table", DataTable).move_cursor(row=other)
            elif action == "lineup-sub":
                bench_key = selected_key(self.query_one("#bench-table", DataTable))
                if lineup_key is None or bench_key is None:
                    raise roster.RosterError("Select a lineup spot and a bench player.")
                roster.substitute_starter(team, lg.players, int(lineup_key), int(bench_key))
                self.done(f"{lg.players[int(bench_key)].name} enters the lineup.")
            elif action == "lineup-auto":
                roster.auto_lineup(team, lg.players)
                self.done("Lineup rebuilt.")
            elif action == "staff-rotation":
                slot = selected_key(self.query_one("#rotation-table", DataTable))
                pid = selected_key(self.query_one("#bullpen-table", DataTable))
                if slot is None or pid is None:
                    raise roster.RosterError("Select a rotation slot and a bullpen pitcher.")
                roster.set_rotation_spot(team, lg.players, int(slot), int(pid))
                self.done(f"{lg.players[int(pid)].name} joins the rotation.")
            elif action == "staff-closer":
                pid = selected_key(self.query_one("#bullpen-table", DataTable))
                if pid is None:
                    raise roster.RosterError("Select a bullpen pitcher.")
                roster.set_closer(team, lg.players, int(pid))
                self.done(f"{lg.players[int(pid)].name} is the new closer.")
            elif action == "staff-auto":
                roster.auto_pitching(team, lg.players)
                self.done("Rotation and bullpen rebuilt.")
        except roster.RosterError as exc:
            self.fail(str(exc))


# ----------------------------------------------------------------------------- trades


VERDICT_LABEL = {trades.ACCEPTABLE: "Accepted", trades.BORDERLINE: "Close", trades.UNACCEPTABLE: "Far apart"}


class TradePane(Pane):
    pane_title = "Trade center"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.partner: str | None = None
        self.mine: set[str] = set()
        self.theirs: set[str] = set()
        self.counter_prop: trades.TradeProposal | None = None
        self.result_text = ""

    def compose(self) -> ComposeResult:
        yield Static("Trade center", classes="pane-title")
        with Horizontal(classes="toolbar"):
            yield Select([], prompt="Choose a trade partner", id="trade-partner")
            yield Static("", id="trade-window", classes="hint")
        with Horizontal(classes="split"):
            with Vertical(classes="half"):
                yield Static("Your assets (Enter / click to add or remove)", classes="hint", id="trade-mine-label")
                yield DataTable(id="trade-mine", cursor_type="row")
            with Vertical(classes="half"):
                yield Static("Their assets", classes="hint", id="trade-theirs-label")
                yield DataTable(id="trade-theirs", cursor_type="row")
        with VerticalScroll(id="trade-summary-scroll"):
            yield Static("", id="trade-summary", markup=False, classes="mono")
            yield Static("", id="trade-result", markup=False, classes="status-line")
        with Horizontal(classes="actions"):
            yield Button("Submit offer", id="trade-submit", classes="act")
            yield Button("Accept counter", id="trade-accept-counter", classes="act", disabled=True)
            yield Button("Clear", id="trade-clear", classes="act")
            yield Button("Incoming offers", id="trade-offers", classes="act")

    def on_mount(self) -> None:
        self.query_one("#trade-summary-scroll").styles.height = 12

    def refresh_view(self) -> None:
        lg = self.league
        select = self.query_one("#trade-partner", Select)
        options = [(f"{abbr} - {lg.teams[abbr].full_name} ({lg.teams[abbr].strategy})", abbr)
                   for abbr in sorted(lg.teams) if abbr != lg.user_team]
        select.set_options(options)
        if self.partner:
            select.value = self.partner
        self.query_one("#trade-window", Static).update(trades.trade_window(lg)[1])
        self._fill_assets()
        self._show_summary()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "trade-partner":
            return
        value = event.value if isinstance(event.value, str) else None
        if value != self.partner:
            self.partner = value
            self.theirs = set()
            self.counter_prop = None
            self.result_text = ""
            self._fill_assets()
            self._show_summary()

    def _asset_rows(self, abbr: str, chosen: set[str]) -> list[tuple[str, list[Any]]]:
        lg = self.league
        team = lg.teams[abbr]
        rows: list[tuple[str, list[Any]]] = []
        for p in sorted(trades.tradable_players(lg, team), key=lambda x: (-x.overall, x.id)):
            key = f"p:{p.id}"
            pot = development.scouted_potential(p, lg.user_team, lg.seed, lg.year)
            rows.append((key, ["✓" if key in chosen else "", p.name, p.position, p.age, p.overall, pot,
                               money(p.salary), p.contract_years, status_of(lg, p)]))
        for pick in sorted((k for k in lg.draft_picks if k.owner == abbr), key=lambda k: k.pick_id):
            key = f"k:{pick.pick_id}"
            rows.append((key, ["✓" if key in chosen else "", f"{pick.label()} pick", "Pick", "", "", "", "", "", ""]))
        return rows

    def _fill_assets(self) -> None:
        lg = self.league
        cols = ["", "Asset", "Pos", "Age", "OVR", "POT", "Salary", "Yrs", "Lvl"]
        fill_table(self.query_one("#trade-mine", DataTable), cols, self._asset_rows(lg.user_team, self.mine))
        theirs = self._asset_rows(self.partner, self.theirs) if self.partner else []
        fill_table(self.query_one("#trade-theirs", DataTable), cols, theirs)

    def proposal(self) -> trades.TradeProposal | None:
        if not self.partner:
            return None

        def split(keys: set[str]) -> tuple[list[int], list[str]]:
            players = sorted(int(k[2:]) for k in keys if k.startswith("p:"))
            picks = sorted(k[2:] for k in keys if k.startswith("k:"))
            return players, picks

        mp, mk = split(self.mine)
        tp, tk = split(self.theirs)
        return trades.TradeProposal(self.league.user_team, self.partner, mp, mk, tp, tk)

    def _show_summary(self) -> None:
        lg = self.league
        prop = self.proposal()
        summary = self.query_one("#trade-summary", Static)
        if prop is None or not (self.mine or self.theirs):
            summary.update("Pick a partner, then select assets on both sides.")
        else:
            text = views.proposal_view(lg, prop)
            if (self.mine or self.theirs) and prop.asset_count() and all(
                    lg.players.get(pid) for pid in [*prop.a_players, *prop.b_players]):
                text += "\n\n" + views.contract_impact_view(lg, prop)
            summary.update(text)
        self.query_one("#trade-result", Static).update(self.result_text)
        self.query_one("#trade-accept-counter", Button).disabled = self.counter_prop is None

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        key = str(event.row_key.value)
        chosen = self.mine if event.data_table.id == "trade-mine" else self.theirs
        if event.data_table.id not in ("trade-mine", "trade-theirs"):
            return
        if key in chosen:
            chosen.remove(key)
        else:
            chosen.add(key)
        self.counter_prop = None
        self.result_text = ""
        self._fill_assets()
        self._show_summary()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if self.busy():
            return
        action = event.button.id
        if action == "trade-clear":
            self.mine, self.theirs, self.counter_prop, self.result_text = set(), set(), None, ""
            self.refresh_view()
        elif action == "trade-submit":
            self.submit()
        elif action == "trade-accept-counter" and self.counter_prop is not None:
            counter = self.counter_prop
            self.confirm("Accept the counteroffer?\n\n" + views.proposal_view(self.league, counter),
                         lambda: self.execute(counter), yes="Accept")
        elif action == "trade-offers":
            self.mlb.push_screen(OffersScreen())

    def submit(self) -> None:
        lg = self.league
        prop = self.proposal()
        if prop is None:
            self.fail("Choose a trade partner first.")
            return
        result = trades.negotiate(lg, prop)
        self.mlb.dirty = True
        self.counter_prop = None
        if result.errors:
            self.result_text = "Not allowed: " + " ".join(result.errors)
        else:
            ev = result.evaluation
            assert ev is not None
            lines = [f"{VERDICT_LABEL[ev.verdict]} - {ev.message}"]
            lines += [f"  · {r}" for r in trades.describe_reasons(ev)]
            if result.counter is not None:
                self.counter_prop = result.counter
                lines += ["", f"Counteroffer: {result.counter_note}", views.proposal_view(lg, result.counter)]
            self.result_text = "\n".join(lines)
            if result.accepted:
                self.confirm(f"{prop.team_b} accepts. Complete this trade?\n\n" + views.proposal_view(lg, prop),
                             lambda: self.execute(prop), yes="Complete trade")
        self._show_summary()

    def execute(self, prop: trades.TradeProposal) -> None:
        try:
            notes = trades.execute(self.league, prop)
        except trades.TradeError as exc:
            self.fail(str(exc))
            return
        for note in notes:
            self.league.add_news(note)
        self.mine, self.theirs, self.counter_prop = set(), set(), None
        self.result_text = notes[0]
        self.done(notes[0])


class OffersScreen(ModalScreen[None]):
    """Incoming CPU offers: review, accept or reject."""

    BINDINGS = [("escape", "close", "Close")]

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog-wide"):
            yield Static("Incoming trade offers", classes="pane-title")
            yield DataTable(id="offers-table", cursor_type="row")
            with VerticalScroll():
                yield Static("", id="offer-detail", markup=False, classes="mono")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Accept", id="offer-accept", classes="act")
                yield Button("Reject", id="offer-reject", classes="act")
                yield Button("Close", id="offer-close", classes="act")

    @property
    def league(self) -> League:
        return cast("MLBManagerApp", self.app).league  # type: ignore[return-value]

    def on_mount(self) -> None:
        self.reload()

    def reload(self) -> None:
        lg = self.league
        rows = [(str(o.offer_id), [o.offer_id, o.proposal.team_a, o.reason or "-", f"day {o.expires_day + 1}"])
                for o in lg.trade_offers]
        fill_table(self.query_one("#offers-table", DataTable), ["#", "From", "Motive", "Expires"], rows)
        self.show_detail()

    def current(self) -> trades.TradeOffer | None:
        key = selected_key(self.query_one("#offers-table", DataTable))
        return self.league.offer_by_id(int(key)) if key else None

    def show_detail(self) -> None:
        offer = self.current()
        detail = self.query_one("#offer-detail", Static)
        if offer is None:
            detail.update("No pending offers." if not self.league.trade_offers else "Select an offer.")
            return
        detail.update(views.proposal_view(self.league, offer.proposal) + "\n\n"
                      + views.contract_impact_view(self.league, offer.proposal))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self.show_detail()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        app = cast("MLBManagerApp", self.app)
        offer = self.current()
        if event.button.id == "offer-close":
            self.dismiss(None)
            return
        if offer is None:
            app.notify("Select an offer.", severity="warning")
            return
        if event.button.id == "offer-reject":
            self.league.reject_offer(offer.offer_id)
            app.after_change("Offer rejected.")
            self.reload()
            return
        if self.league.live_game is not None:
            app.notify("A game is in progress in the dugout. Finish it before accepting a trade.",
                       severity="warning")
            return

        def accept(answer: bool | None) -> None:
            if not answer:
                return
            try:
                notes = self.league.accept_offer(offer.offer_id)
            except trades.TradeError as exc:
                app.notify(str(exc), severity="error")
                self.reload()
                return
            for note in notes:
                self.league.add_news(note)
            app.after_change(notes[0])
            self.reload()

        app.push_screen(ConfirmScreen("Accept this trade?\n\n" + views.proposal_view(self.league, offer.proposal),
                                      yes="Accept"), accept)

    def action_close(self) -> None:
        self.dismiss(None)


# ----------------------------------------------------------------------------- draft room


class DraftPane(Pane):
    pane_title = "Draft room"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.pos_filter: str | None = None

    def compose(self) -> ComposeResult:
        yield Static("Draft room", classes="pane-title")
        yield Static("", id="draft-info", markup=False, classes="mono")
        with Horizontal(classes="toolbar"):
            yield Select([("All positions", "ALL"), *[(pos, pos) for pos in POSITIONS if pos != "DH"]],
                         value="ALL", allow_blank=False, id="draft-filter")
        with Horizontal(classes="split"):
            with Vertical(classes="half"):
                yield DataTable(id="draft-table", cursor_type="row", zebra_stripes=True)
            with VerticalScroll(classes="half"):
                yield Static("", id="draft-log", markup=False, classes="mono")
        with Horizontal(classes="actions"):
            yield Button("Draft selected", id="draft-pick", classes="act")
            yield Button("Sim to my pick", id="draft-sim", classes="act")
            yield Button("Auto-draft rest", id="draft-auto", classes="act")

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "draft-filter" and isinstance(event.value, str):
            self.pos_filter = None if event.value == "ALL" else event.value
            self.refresh_view()

    def refresh_view(self) -> None:
        lg = self.league
        table = self.query_one("#draft-table", DataTable)
        draft = lg.draft
        live = draft is not None and lg.phase == PHASE_DRAFT
        for bid in ("#draft-pick", "#draft-sim", "#draft-auto"):
            self.query_one(bid, Button).disabled = not live or lg.draft_on_clock() is None
        if not live:
            self.query_one("#draft-info", Static).update(
                "The amateur draft is held in the offseason after the re-signing period.\n"
                "Your tradable picks are listed on the right.")
            fill_table(table, ["Pick", "Year", "Rd"], [])
            self.query_one("#draft-log", Static).update(views.picks_view(lg, lg.user_team))
            return
        assert draft is not None
        clock = lg.draft_on_clock()
        mine = [i for i in range(len(draft.selections), draft.total_picks)
                if lg.draft_owner(draft.slot(i)[0], draft.slot(i)[1]) == lg.user_team]
        info = [f"{draft.year} draft · {len(draft.selections)}/{draft.total_picks} picks made · "
                f"{len(draft.pool)} prospects available"]
        if clock:
            number, rnd, owner = clock
            info.append(f"On the clock: #{number} (round {rnd}) {owner}"
                        + ("  ← YOUR PICK" if owner == lg.user_team else ""))
        info.append("Your next picks: " + (", ".join(f"#{i + 1}" for i in mine[:5]) or "none"))
        self.query_one("#draft-info", Static).update("\n".join(info))
        pool = [lg.players[pid] for pid in draft.pool]
        if self.pos_filter:
            pool = [p for p in pool if p.position == self.pos_filter]

        def scouted(p: Player) -> int:
            return development.scouted_potential(p, lg.user_team, lg.seed, draft.year)

        pool.sort(key=lambda p: (-scouted(p), -p.overall, p.id))
        rows = [(str(p.id), [p.name, p.position, p.age, p.overall, scouted(p),
                             development.potential_grade(scouted(p))]) for p in pool[:150]]
        fill_table(table, ["Prospect", "Pos", "Age", "OVR", "POT", "Grade"], rows)
        log = []
        for sel in reversed(draft.selections[-14:]):
            p = lg.players.get(sel.player_id)
            log.append(f"#{sel.overall_pick:<3} R{sel.round} {sel.team:<3} {p.name if p else '?'} "
                       f"({p.position if p else ''})")
        self.query_one("#draft-log", Static).update("Latest selections\n" + ("\n".join(log) or "  none yet"))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if self.busy():
            return
        lg = self.league
        try:
            if event.button.id == "draft-pick":
                clock = lg.draft_on_clock()
                if clock is None or clock[2] != lg.user_team:
                    raise ValueError("You are not on the clock. Use 'Sim to my pick' first.")
                key = selected_key(self.query_one("#draft-table", DataTable))
                if key is None:
                    raise ValueError("Select a prospect.")
                prospect = lg.players[int(key)]

                def pick() -> None:
                    sel = lg.draft_select(prospect.id)
                    self.done(f"With pick #{sel.overall_pick} you select {prospect.name} ({prospect.position}).")

                self.confirm(f"Draft {prospect.name} ({prospect.position}, age {prospect.age}) with pick #{clock[0]}?",
                             pick, yes="Draft")
            elif event.button.id == "draft-sim":
                made = lg.draft_until_user()
                self.done(f"{len(made)} picks made. " + ("You are on the clock." if lg.draft_on_clock() else
                                                         "The draft is complete."))
            elif event.button.id == "draft-auto":
                def auto() -> None:
                    lg.draft_auto_rest()
                    self.done("The draft is complete. Advance the offseason to open free agency.")

                self.confirm("Let your scouts make all remaining selections?", auto, yes="Auto-draft")
        except (ValueError, RuntimeError) as exc:
            self.fail(str(exc))


# ----------------------------------------------------------------------------- free agency


class FreeAgencyPane(Pane):
    pane_title = "Free agents"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.pos_filter: str | None = None

    def compose(self) -> ComposeResult:
        yield Static("Free agents", classes="pane-title")
        yield Select([("All positions", "ALL"), *[(pos, pos) for pos in POSITIONS]], value="ALL",
                     allow_blank=False, id="fa-filter")
        yield DataTable(id="fa-table", cursor_type="row", zebra_stripes=True)
        with Horizontal(classes="actions"):
            yield Button("Profile", id="fa-profile", classes="act")
            yield Button("Sign at asking price", id="fa-sign", classes="act")
            yield Button("Make offer", id="fa-offer", classes="act")
        yield Static("", id="fa-hint", classes="hint")

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "fa-filter" and isinstance(event.value, str):
            self.pos_filter = None if event.value == "ALL" else event.value
            self.refresh_view()

    def refresh_view(self) -> None:
        lg = self.league
        pool = freeagency.free_agents(lg)
        if self.pos_filter:
            pool = [p for p in pool if p.position == self.pos_filter]
        rows = []
        for p in pool[:80]:
            salary, years = freeagency.asking(lg, p)
            pot = development.scouted_potential(p, lg.user_team, lg.seed, lg.year)
            rows.append((str(p.id), [p.name, p.position, p.age, p.overall, pot, money(salary), years]))
        fill_table(self.query_one("#fa-table", DataTable), ["Player", "Pos", "Age", "OVR", "POT", "Asking", "Yrs"],
                   rows)
        self.query_one("#fa-hint", Static).update(
            f"{views.cap_text(lg, lg.user)} · organisation {len(lg.user.all_players)}/45"
            + (" · in-season free agents take one-year deals" if lg.in_season else ""))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if self.busy():
            return
        lg = self.league
        key = selected_key(self.query_one("#fa-table", DataTable))
        if key is None:
            self.fail("Select a free agent.")
            return
        player = lg.players[int(key)]
        salary, years = freeagency.asking(lg, player)
        if event.button.id == "fa-profile":
            self.mlb.push_screen(PlayerProfileScreen(lg, player.id))
            return

        def sign(amount: float) -> None:
            try:
                self.done(freeagency.sign_free_agent(lg, lg.user, player.id, amount, years))
            except freeagency.FreeAgencyError as exc:
                self.fail(str(exc))
                self.mlb.after_change(None)

        if event.button.id == "fa-sign":
            self.confirm(f"Sign {player.name} for {years} year(s) at {money(salary)} per year?",
                         lambda: sign(salary), yes="Sign")
        elif event.button.id == "fa-offer":
            def got(value: str | None) -> None:
                if value is None:
                    return
                try:
                    amount = float(value)
                except ValueError:
                    self.fail("Enter a salary in $ millions, e.g. 4.5")
                    return
                sign(amount)

            self.mlb.push_screen(InputScreen(f"Salary per year for {player.name} ($M, asking {salary:.2f}):",
                                             f"{salary * 0.9:.2f}"), got)


# ----------------------------------------------------------------------------- season center


SEASON_TABS: tuple[tuple[str, str], ...] = (
    ("standings", "Standings"), ("leaders", "Statistics"), ("career", "Career leaders"),
    ("awards", "Awards"), ("milestones", "Milestones"), ("records", "Franchise records"),
    ("postseason", "Postseason"), ("titles", "Championships"), ("retired", "Retired"),
    ("finances", "Finances"),
)


class SeasonPane(Pane):
    pane_title = "Season center"

    def compose(self) -> ComposeResult:
        yield Static("Season center", classes="pane-title")
        with TabbedContent(id="season-tabs"):
            for key, label in SEASON_TABS:
                with TabPane(label, id=f"season-{key}"):
                    with VerticalScroll():
                        yield Static("", id=f"season-text-{key}", markup=False, classes="mono")

    def refresh_view(self) -> None:
        lg = self.league
        user = lg.user_team
        awards = views.awards_view(lg, lg.awards_history[-1]["year"] if lg.awards_history else None)
        post_awards = lg.postseason_award_list()
        if post_awards:
            awards += "\n\nPostseason awards\n" + "\n".join(
                f"  {a['year']} {a['award']}: {a['name']} ({a['team']}) - {a['line']}" for a in post_awards[-12:])
        texts = {
            "standings": "\n\n".join([views.standings(lg, "AL"), views.wild_card(lg, "AL"),
                                      views.standings(lg, "NL"), views.wild_card(lg, "NL")]),
            "leaders": views.leaders_view(lg, limit=10),
            "career": views.career_leaders_view(lg) + "\n\n" + views.career_leaders_view(lg, postseason=True),
            "awards": awards,
            "milestones": views.milestones_view(lg) + "\n\n" + views.career_milestones_view(lg),
            "records": "\n\n".join([views.records_view(lg, user), views.career_leaders_view(lg, team=user),
                                    views.records_view(lg, None)]),
            "postseason": views.bracket_view(lg) + "\n\n" + views.postseason_history_view(lg),
            "titles": views.championships_view(lg) + "\n\n" + views.franchise_history(lg),
            "retired": views.retired_view(lg, limit=60),
            "finances": views.league_finances_view(lg),
        }
        for key, text in texts.items():
            self.query_one(f"#season-text-{key}", Static).update(text)


PANES: tuple[tuple[str, str, str, type[Pane]], ...] = (
    # (id, label, key, class)
    ("dashboard", "Dashboard", "d", DashboardPane),
    ("roster", "Roster", "r", RosterPane),
    ("lineup", "Lineup & staff", "l", LineupPane),
    ("trades", "Trade center", "t", TradePane),
    ("draft", "Draft room", "p", DraftPane),
    ("freeagents", "Free agents", "f", FreeAgencyPane),
    ("season", "Season center", "s", SeasonPane),
    ("game", "Game", "g", DugoutPane),
)
