"""Interactive terminal interface.

The ``App`` class takes injectable ``input_fn``/``output_fn`` callables so the
whole UI can be driven by scripted input in tests.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from pathlib import Path

from mlb_manager import __version__, roster, views
from mlb_manager.franchises import FRANCHISES_BY_ABBR
from mlb_manager.league import PHASE_COMPLETE, PHASE_POSTSEASON, PHASE_REGULAR, League
from mlb_manager.persistence import (
    SaveError,
    default_save_dir,
    default_save_name,
    list_saves,
    load_league,
    resolve_save_path,
    save_league,
)


class QuitApp(Exception):
    """Raised to leave the application (e.g. on end of input)."""


class App:
    def __init__(
        self,
        input_fn: Callable[[str], str] = input,
        output_fn: Callable[[str], None] = print,
        save_dir: Path | None = None,
    ) -> None:
        self._input = input_fn
        self._output = output_fn
        self.save_dir = save_dir or default_save_dir()
        self.league: League | None = None
        self.save_path: Path | None = None
        self.dirty = False

    # ------------------------------------------------------------------ io helpers

    def say(self, text: str = "") -> None:
        self._output(text)

    def ask(self, prompt: str) -> str:
        try:
            return self._input(prompt).strip()
        except EOFError:
            raise QuitApp from None

    def ask_int(self, prompt: str, lo: int | None = None, hi: int | None = None) -> int | None:
        """Ask for an integer; blank input returns None (cancel)."""
        while True:
            raw = self.ask(prompt)
            if raw == "":
                return None
            try:
                value = int(raw)
            except ValueError:
                self.say("Please enter a number (or press Enter to cancel).")
                continue
            if (lo is not None and value < lo) or (hi is not None and value > hi):
                self.say(f"Please enter a number between {lo} and {hi}.")
                continue
            return value

    def confirm(self, prompt: str) -> bool:
        return self.ask(f"{prompt} [y/N]: ").lower() in ("y", "yes")

    def menu(self, heading: str, options: list[tuple[str, str]]) -> str:
        """Show ``options`` as (key, label) pairs and return the chosen key."""
        self.say("")
        self.say(heading)
        for key, label in options:
            self.say(f"  {key:>2}) {label}")
        keys = {key for key, _ in options}
        while True:
            choice = self.ask("> ").lower()
            if choice in keys:
                return choice
            self.say("Invalid choice.")

    # ------------------------------------------------------------------ entry points

    def run(self, league: League | None = None, save_path: Path | None = None) -> int:
        """Run the UI. If ``league`` is given, start directly in the franchise menu."""
        try:
            if league is not None:
                self.league = league
                self.save_path = save_path
                self.dirty = save_path is None
                self.franchise_menu()
            self.title_screen()
        except (QuitApp, KeyboardInterrupt):
            self.say("")
            if self.dirty and self.league is not None:
                self.autosave_on_exit()
            self.say("Goodbye!")
        return 0

    def autosave_on_exit(self) -> None:
        assert self.league is not None
        # Never overwrite a manual save implicitly: write a separate autosave file.
        path = resolve_save_path(f"autosave_{default_save_name(self.league)}", self.save_dir)
        try:
            save_league(self.league, path)
            self.say(f"Unsaved progress written to {path}")
        except SaveError as exc:
            self.say(f"Could not autosave: {exc}")

    # ------------------------------------------------------------------ title

    def title_screen(self) -> None:
        self.say(views.title(f"MLB MANAGER {__version__}"))
        self.say("Manage a big-league franchise of fictional players through a 162-game season.")
        while True:
            choice = self.menu("Main menu", [("1", "New franchise"), ("2", "Load franchise"), ("0", "Quit")])
            if choice == "0":
                raise QuitApp
            if choice == "1" and self.new_franchise():
                self.franchise_menu()
            elif choice == "2" and self.load_franchise():
                self.franchise_menu()

    def new_franchise(self, team: str | None = None, seed: int | None = None) -> bool:
        if team is None:
            self.say("")
            self.say(views.team_list())
            while True:
                raw = self.ask("Choose your club by abbreviation (Enter to cancel): ").upper()
                if raw == "":
                    return False
                if raw in FRANCHISES_BY_ABBR:
                    team = raw
                    break
                self.say(f"Unknown club {raw!r}.")
        if seed is None:
            raw = self.ask("League seed (Enter for random): ")
            if raw:
                try:
                    seed = int(raw)
                except ValueError:
                    seed = int.from_bytes(raw.encode()[:8].ljust(8, b"\0"), "big") % 10**9
            else:
                seed = random.SystemRandom().randrange(1, 10**6)
        self.say(f"Generating league with seed {seed} ...")
        self.league = League.create(seed=seed, user_team=team)
        self.save_path = None
        self.dirty = True
        self.say(f"You are now the manager of the {self.league.user.full_name}.")
        return True

    def load_franchise(self) -> bool:
        saves = list_saves(self.save_dir)
        if saves:
            self.say("")
            self.say(f"Saves in {self.save_dir}:")
            for i, path in enumerate(saves, start=1):
                self.say(f"  {i}) {path.name}")
        else:
            self.say(f"No saves found in {self.save_dir}.")
        raw = self.ask("Number or path of save to load (Enter to cancel): ")
        if not raw:
            return False
        if raw.isdigit() and 1 <= int(raw) <= len(saves):
            path = saves[int(raw) - 1]
        else:
            path = resolve_save_path(raw, self.save_dir)
        try:
            self.league = load_league(path)
        except SaveError as exc:
            self.say(f"Error: {exc}")
            return False
        self.save_path = path
        self.dirty = False
        self.say(f"Loaded {path.name}: {self.league.user.full_name}, {self.league.year}.")
        return True

    # ------------------------------------------------------------------ franchise hub

    def franchise_menu(self) -> None:
        assert self.league is not None
        actions: dict[str, Callable[[], None]] = {
            "1": self.show_dashboard,
            "2": self.show_roster,
            "3": self.lineup_menu,
            "4": self.staff_menu,
            "5": self.roster_moves_menu,
            "6": self.player_lookup,
            "7": self.show_standings,
            "8": self.show_schedule,
            "9": self.show_leaders,
            "10": self.show_last_box,
            "11": self.show_bracket,
            "12": self.simulate_menu,
            "13": self.save_menu,
        }
        while True:
            self.say("")
            self.say(views.rule())
            self.say(views.status_line(self.league))
            choice = self.menu(
                "Franchise menu",
                [
                    ("1", "Dashboard"),
                    ("2", "Roster"),
                    ("3", "Lineup"),
                    ("4", "Pitching staff"),
                    ("5", "Roster moves (promote / demote)"),
                    ("6", "Player details"),
                    ("7", "Standings"),
                    ("8", "Schedule & results"),
                    ("9", "League leaders"),
                    ("10", "Last game box score"),
                    ("11", "Postseason bracket & history"),
                    ("12", "SIMULATE"),
                    ("13", "Save franchise"),
                    ("0", "Quit to main menu"),
                ],
            )
            if choice == "0":
                if self.dirty and self.confirm("Save before leaving?"):
                    self.save_menu()
                self.league = None
                return
            actions[choice]()

    # ------------------------------------------------------------------ views

    def show_dashboard(self) -> None:
        self.say(views.dashboard(self.league))

    def show_roster(self) -> None:
        self.say(views.roster_view(self.league, self.league.user))

    def show_standings(self) -> None:
        choice = self.menu("Standings", [("1", "American League"), ("2", "National League"), ("3", "Both")])
        for lg in {"1": ["AL"], "2": ["NL"], "3": ["AL", "NL"]}[choice]:
            self.say("")
            self.say(views.standings(self.league, lg))

    def show_schedule(self) -> None:
        self.say(views.schedule_view(self.league, self.league.user_team))

    def show_leaders(self) -> None:
        self.say(views.leaders_view(self.league))

    def show_last_box(self) -> None:
        if self.league.last_user_game is None:
            self.say("Your club has not played a game yet.")
            return
        self.say(views.box_score(self.league, self.league.last_user_game))

    def show_bracket(self) -> None:
        self.say(views.bracket_view(self.league))
        self.say("")
        self.say("Franchise history")
        self.say(views.history_view(self.league))

    def player_lookup(self) -> None:
        pid = self.ask_int("Player ID (Enter to cancel): ", 1)
        if pid is None:
            return
        player = self.league.players.get(pid)
        if player is None:
            self.say(f"No player with ID {pid}.")
            return
        self.say(views.player_card(self.league, player))

    # ------------------------------------------------------------------ lineup

    def lineup_menu(self) -> None:
        team = self.league.user
        while True:
            self.say("")
            self.say(views.lineup_view(self.league, team))
            choice = self.menu("Lineup", [
                ("1", "Swap two batting-order spots"),
                ("2", "Swap defensive positions of two starters"),
                ("3", "Start a bench player in place of a starter"),
                ("4", "Auto-build best lineup"),
                ("0", "Back"),
            ])
            if choice == "0":
                return
            try:
                if choice in ("1", "2"):
                    a = self.ask_int("First batting-order spot (1-9): ", 1, 9)
                    b = self.ask_int("Second batting-order spot (1-9): ", 1, 9) if a else None
                    if a is None or b is None:
                        continue
                    if choice == "1":
                        roster.swap_batting_order(team, a - 1, b - 1)
                    else:
                        roster.swap_positions(team, a - 1, b - 1)
                elif choice == "3":
                    spot = self.ask_int("Batting-order spot to replace (1-9): ", 1, 9)
                    pid = self.ask_int("Bench player ID: ", 1) if spot else None
                    if spot is None or pid is None:
                        continue
                    roster.substitute_starter(team, self.league.players, spot - 1, pid)
                elif choice == "4":
                    roster.auto_lineup(team, self.league.players)
                self.dirty = True
                self.say("Lineup updated.")
            except roster.RosterError as exc:
                self.say(f"Not allowed: {exc}")

    # ------------------------------------------------------------------ staff

    def staff_menu(self) -> None:
        team = self.league.user
        while True:
            self.say("")
            self.say(views.staff_view(self.league, team))
            choice = self.menu("Pitching staff", [
                ("1", "Set a rotation spot"),
                ("2", "Name a new closer"),
                ("3", "Auto-build rotation and closer"),
                ("0", "Back"),
            ])
            if choice == "0":
                return
            try:
                if choice == "1":
                    spot = self.ask_int(f"Rotation spot (1-{len(team.rotation)}): ", 1, len(team.rotation))
                    pid = self.ask_int("Pitcher ID: ", 1) if spot else None
                    if spot is None or pid is None:
                        continue
                    roster.set_rotation_spot(team, self.league.players, spot - 1, pid)
                elif choice == "2":
                    pid = self.ask_int("Bullpen pitcher ID: ", 1)
                    if pid is None:
                        continue
                    roster.set_closer(team, self.league.players, pid)
                else:
                    roster.auto_pitching(team, self.league.players)
                self.dirty = True
                self.say("Pitching staff updated.")
            except roster.RosterError as exc:
                self.say(f"Not allowed: {exc}")

    # ------------------------------------------------------------------ moves

    def roster_moves_menu(self) -> None:
        team = self.league.user
        players = self.league.players
        self.say("")
        self.say("Swap an active player with a reserve. The active roster stays at "
                 f"{roster.MAX_ACTIVE} (max {roster.MAX_ACTIVE_PITCHERS} pitchers).")
        self.say("")
        self.say(views.roster_view(self.league, team))
        out_id = self.ask_int("ID of ACTIVE player to send down (Enter to cancel): ", 1)
        if out_id is None:
            return
        in_id = self.ask_int("ID of RESERVE player to call up (Enter to cancel): ", 1)
        if in_id is None:
            return
        try:
            notes = roster.swap_active_reserve(team, players, out_id, in_id)
        except roster.RosterError as exc:
            self.say(f"Not allowed: {exc}")
            return
        self.dirty = True
        message = f"Called up {players[in_id].name}; optioned {players[out_id].name} to the reserves."
        self.league.add_news(message)
        self.say(message)
        for note in notes:
            self.say(f"  - {note}")

    # ------------------------------------------------------------------ simulation

    def simulate_menu(self) -> None:
        lg = self.league
        options = []
        if lg.phase == PHASE_REGULAR:
            options = [
                ("1", "Next day"),
                ("2", "Next 7 days"),
                ("3", "Next 30 days"),
                ("4", "To end of regular season"),
                ("5", "Entire rest of season (incl. postseason)"),
            ]
        elif lg.phase == PHASE_POSTSEASON:
            options = [
                ("1", "Next postseason day"),
                ("6", "To end of current round"),
                ("5", "Through the World Series"),
            ]
        else:
            self.say("The season is complete.")
            self.say(views.bracket_view(lg))
            if self.confirm(f"Start the {lg.year + 1} season?"):
                lg.start_new_season()
                self.dirty = True
                self.say(f"Welcome to {lg.year}! Ratings have been updated for aging and development.")
            return
        options.append(("0", "Back"))
        choice = self.menu("Simulate", options)
        if choice == "0":
            return
        if choice == "1":
            reports = lg.sim_days(1)
        elif choice == "2":
            reports = lg.sim_days(7)
        elif choice == "3":
            reports = lg.sim_days(30)
        elif choice == "4":
            self.say("Simulating ...")
            reports = lg.sim_regular_season()
        elif choice == "6":
            current = lg.playoffs.round_index if lg.playoffs else 0
            reports = lg.sim_until(
                lambda x: x.phase != PHASE_POSTSEASON or x.playoffs.round_index != current
            )
        else:
            self.say("Simulating ...")
            reports = lg.sim_to_completion()
        self.dirty = True
        self.say(views.day_summary(lg, reports))
        if lg.phase == PHASE_COMPLETE:
            self.say("")
            self.say(views.bracket_view(lg))

    # ------------------------------------------------------------------ saving

    def save_menu(self) -> None:
        default = self.save_path or resolve_save_path(default_save_name(self.league), self.save_dir)
        raw = self.ask(f"Save name or path [{default}]: ")
        path = resolve_save_path(raw, self.save_dir) if raw else default
        try:
            save_league(self.league, path)
        except SaveError as exc:
            self.say(f"Error: {exc}")
            return
        self.save_path = path
        self.dirty = False
        self.say(f"Franchise saved to {path}")
