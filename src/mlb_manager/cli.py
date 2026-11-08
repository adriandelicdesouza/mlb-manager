"""Interactive terminal interface.

The ``App`` class takes injectable ``input_fn``/``output_fn`` callables so the
whole UI can be driven by scripted input in tests.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from pathlib import Path

from mlb_manager import __version__, contracts, freeagency, frontoffice, roster, trades, views
from mlb_manager.franchises import FRANCHISES_BY_ABBR
from mlb_manager.league import (
    PHASE_COMPLETE,
    PHASE_DRAFT,
    PHASE_FREE_AGENCY,
    PHASE_POSTSEASON,
    PHASE_REGULAR,
    PHASE_RESIGN,
    ContractError,
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


class QuitApp(Exception):
    """Raised to leave the application (e.g. on end of input)."""


class App:
    def __init__(
        self,
        input_fn: Callable[[str], str] = input,
        output_fn: Callable[[str], None] = print,
        save_dir: Path | None = None,
        salary_cap: float | None = None,
    ) -> None:
        self.salary_cap = salary_cap
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
        self.league = League.create(seed=seed, user_team=team, salary_cap=self.salary_cap)
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
            "14": self.front_office_menu,
            "15": self.injury_menu,
            "16": self.season_center,
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
                    ("14", "FRONT OFFICE (contracts, trades, draft, free agency, prospects)"),
                    ("15", f"Injuries & injured list ({len(self.league.injured_players(self.league.user))})"),
                    ("16", "Season center (milestones, awards, summary, franchise history)"),
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
        if lg.phase in (PHASE_REGULAR, PHASE_POSTSEASON):
            label = "Resume the game in progress (dugout)" if lg.live_game else "Manage your next game live (dugout)"
            options.insert(0, ("7", label))
        elif lg.phase == PHASE_COMPLETE:
            self.say("The season is complete.")
            self.say(views.bracket_view(lg))
            if self.confirm("Begin the offseason (aging, development, expiring contracts)?"):
                self.say_news(lg.begin_offseason())
                self.dirty = True
            return
        else:
            self.offseason_menu()
            return
        options.append(("0", "Back"))
        choice = self.menu("Simulate", options)
        if choice == "0":
            return
        if choice == "7":
            self.live_game_menu()
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
            self.say("")
            self.say("The season is over: see the Season center (16) for the season summary, "
                     "awards and franchise history.")

    # ------------------------------------------------------------------ dugout (live game)

    def live_game_menu(self) -> None:
        lg = self.league
        if lg.live_game is None:
            skipped = lg.advance_to_user_game()
            if skipped:
                self.say(views.day_summary(lg, skipped))
            if lg.user_game_today() is None:
                self.say("Your club has no game coming up.")
                return
            live = lg.start_live_game()
            self.dirty = True
            preset = self.menu("How much do you want to manage?", [
                ("1", "Assisted: the CPU handles routine calls and asks you at key moments"),
                ("2", "Full manual control"),
                ("3", "Automatic: watch the CPU manage")])
            live.set_preset({"1": "assisted", "2": "manual", "3": "auto"}[preset])
        live = lg.live_game
        assert live is not None
        units = {"1": "pitch", "2": "play", "3": "pa", "4": "half", "5": "inning", "6": "game"}
        while True:
            self.say("")
            self.say(views.rule())
            self.say(f"{live.label}: {live.sim.away.team.abbr} @ {live.sim.home.team.abbr}")
            self.say(views.live_line_score(lg, live))
            self.say(views.live_situation(lg, live))
            if live.is_over:
                self.say(views.box_score(lg, live.result))
                self.say("")
                self.say(views.manager_report(live.result.manager_report))
                reports = [lg.finish_live_game()]
                self.dirty = True
                self.say("")
                self.say(views.day_summary(lg, reports))
                return
            self.say(views.matchup_card(lg, live))
            self.say("Recent plays:")
            for line in views.live_feed(lg, live, 6).splitlines():
                self.say(f"  {line}")
            if live.prompt is not None:
                prompt = live.prompt
                self.say("")
                self.say(f"DECISION ({prompt.category}): {prompt.question}")
                if prompt.basis:
                    self.say(f"  Analysis: {prompt.basis}")
                opts = [(str(i), label + (" (recommended)" if i == 1 else ""))
                        for i, (_, label) in enumerate(prompt.options, start=1)]
                answer = self.menu("Your call", opts)
                self.say(live.answer_prompt(prompt.options[int(answer) - 1][0]))
                continue
            choice = self.menu("Dugout", [
                ("1", "Next pitch"), ("2", "Next play"), ("3", "Next batter"), ("4", "End of half-inning"),
                ("5", "End of inning"), ("6", "To the final"), ("7", "Managerial controls"),
                ("8", "Reviews (lineups, bullpen, scouting, decisions)"), ("9", "Automation level"),
                ("10", "Sim to the end without stopping"), ("0", "Leave the dugout (the game stays paused)")])
            if choice == "0":
                return
            if choice in units:
                report = live.advance(units[choice])
                for event in report.events[-12:]:
                    if event["k"] != "HALF":
                        self.say(f"  {views.event_text(lg, event)}")
            elif choice == "7":
                self.live_controls_menu()
            elif choice == "8":
                self.live_reviews_menu()
            elif choice == "9":
                preset = self.menu("Automation", [("1", "Assisted"), ("2", "Manual"), ("3", "Automatic"),
                                                  ("4", "Per category (via controls)"), ("0", "Back")])
                if preset in ("1", "2", "3"):
                    live.set_preset({"1": "assisted", "2": "manual", "3": "auto"}[preset])
                elif preset == "4":
                    self.live_controls_menu(group="Automation")
            elif choice == "10":
                live.finish()
            self.dirty = True

    def live_controls_menu(self, group: str | None = None) -> None:
        live = self.league.live_game
        while live is not None and not live.is_over:
            controls = [c for c in live.controls() if group is None or c.group == group]
            self.say("")
            self.say(views.live_instructions(live))
            options = []
            for index, c in enumerate(controls, start=1):
                value = dict(c.options).get(c.value or "", c.value or "") if c.kind == "choice" else (
                    "ON" if c.value else "")
                text = f"[{c.group}] {c.label}" + (f": {value}" if value else "") + (f"  ({c.scope})" if c.scope
                                                                                      else "")
                if not c.enabled:
                    text += f"  - unavailable: {c.reason}"
                options.append((str(index), text))
            options.append(("0", "Back"))
            choice = self.menu("Managerial controls", options)
            if choice == "0":
                return
            ctrl = controls[int(choice) - 1]
            if not ctrl.enabled:
                self.say(ctrl.reason)
                continue
            value = None
            if ctrl.kind == "choice":
                sub = [(str(i), label) for i, (_, label) in enumerate(ctrl.options, start=1)] + [("0", "Cancel")]
                pick = self.menu(ctrl.label, sub)
                if pick == "0":
                    continue
                value = ctrl.options[int(pick) - 1][0]
            try:
                self.say(live.apply(ctrl.key, value))
            except ValueError as exc:
                self.say(f"Not allowed: {exc}")

    def live_reviews_menu(self) -> None:
        lg = self.league
        live = lg.live_game
        if live is None:
            return
        choice = self.menu("Reviews", [("1", "Lineups & defense"), ("2", "Bullpen availability"),
                                       ("3", "Scouting: opponent tendencies, due up, fatigue"),
                                       ("4", "Decision log"), ("5", "Current instructions"),
                                       ("6", "Play-by-play"), ("0", "Back")])
        text = {"1": lambda: views.live_lineups(lg, live), "2": lambda: views.bullpen_text(lg, live),
                "3": lambda: views.scouting_text(lg, live), "4": lambda: views.decision_log(lg, live),
                "5": lambda: views.live_instructions(live), "6": lambda: views.live_feed(lg, live, 40)}
        if choice in text:
            self.say(text[choice]())

    def say_news(self, items: list[str]) -> None:
        for item in items:
            self.say(f"  * {item}")

    def offseason_menu(self) -> None:
        lg = self.league
        self.say(f"Offseason - {views.phase_label(lg)}")
        if lg.phase == PHASE_RESIGN:
            pending = lg.pending_free_agents(lg.user)
            self.say(f"You have {len(pending)} pending free agent(s).")
            options = [("1", "Contracts & payroll (re-sign players)"),
                       ("2", "Finish re-signing period and go to the draft")]
        elif lg.phase == PHASE_DRAFT:
            options = [("1", "Enter the draft room"),
                       ("2", "Auto-complete the draft and open free agency")]
        else:
            options = [("1", "Free agency market"),
                       ("2", f"Finish free agency and start the {lg.year + 1} season")]
        options += [("9", "Auto-run the rest of the offseason (CPU makes your decisions)"), ("0", "Back")]
        choice = self.menu("Offseason", options)
        if choice == "0":
            return
        if choice == "1":
            {PHASE_RESIGN: self.contracts_menu, PHASE_DRAFT: self.draft_menu,
             PHASE_FREE_AGENCY: self.free_agency_menu}[lg.phase]()
            return
        if choice == "2":
            if lg.phase == PHASE_RESIGN and lg.pending_free_agents(lg.user):
                if not self.confirm("Unsigned players will leave as free agents. Continue?"):
                    return
            if lg.phase == PHASE_FREE_AGENCY and len(lg.user.all_players) > frontoffice.MAX_ORG:
                self.say(f"Your organisation exceeds {frontoffice.MAX_ORG}; the lowest-value "
                         "players will be released automatically.")
                if not self.confirm("Continue?"):
                    return
            self.say_news(lg.advance_offseason())
        else:
            while lg.in_offseason:
                self.say_news(lg.advance_offseason(auto_user=True))
        self.dirty = True
        if lg.phase == PHASE_REGULAR:
            self.say(f"Welcome to {lg.year}!")

    # ------------------------------------------------------------------ front office

    def front_office_menu(self) -> None:
        actions: dict[str, Callable[[], None]] = {
            "1": self.contracts_menu,
            "2": self.trade_center,
            "3": self.free_agency_menu,
            "4": self.draft_menu,
            "5": self.prospects_menu,
            "6": lambda: self.say(views.transactions_view(self.league)),
        }
        while True:
            self.say("")
            self.say(views.front_office_overview(self.league))
            choice = self.menu("Front Office", [
                ("1", "Contracts & Payroll"),
                ("2", f"Trade Center ({len(self.league.trade_offers)} offers)"),
                ("3", "Free Agency"),
                ("4", "Draft"),
                ("5", "Minor League / Prospects"),
                ("6", "League transactions"),
                ("0", "Back"),
            ])
            if choice == "0":
                return
            actions[choice]()

    def contracts_menu(self) -> None:
        lg, team = self.league, self.league.user
        while True:
            self.say("")
            self.say(views.payroll_view(lg, team))
            choice = self.menu("Contracts & Payroll", [
                ("1", "Re-sign a pending free agent / extend a final-year player"),
                ("2", "Release a player"),
                ("0", "Back"),
            ])
            if choice == "0":
                return
            pid = self.ask_int("Player ID (Enter to cancel): ", 1)
            if pid is None or pid not in lg.players:
                continue
            player = lg.players[pid]
            if choice == "1":
                salary, years = lg.resign_ask(player)
                self.say(f"{player.name} asks for {contracts.money(salary)} per year "
                         f"(prefers {years} years, max {contracts.max_years(player)}).")
                chosen = self.ask_int(f"Years [{years}]: ", 1, contracts.max_years(player))
                chosen = chosen or years
                if not self.confirm(f"Sign {player.name} for {chosen} yr at {contracts.money(salary)}/yr?"):
                    continue
                try:
                    self.say(lg.extend_contract(team, pid, chosen))
                    self.dirty = True
                except ContractError as exc:
                    self.say(f"Not allowed: {exc}")
            else:
                cost = player.salary if player.contract_years > 0 else 0.0
                if not self.confirm(f"Release {player.name}? Dead money this season: {contracts.money(cost)}."):
                    continue
                try:
                    self.say(lg.release(team, pid))
                    self.dirty = True
                except roster.RosterError as exc:
                    self.say(f"Not allowed: {exc}")

    # -- trades --------------------------------------------------------------------------

    def _parse_ids(self, raw: str) -> list[int] | None:
        try:
            return [int(x) for x in raw.replace(",", " ").split()]
        except ValueError:
            self.say("Please enter numeric player IDs separated by commas.")
            return None

    def _ask_assets(self, prompt: str, default: list) -> list | None:  # noqa: ANN001
        shown = ", ".join(str(x) for x in default)
        hint = f" [{shown}] (blank = keep, '-' = none)" if default else " (blank = none)"
        raw = self.ask(f"{prompt}{hint}: ")
        if raw == "":
            return list(default)
        if raw == "-":
            return []
        return raw

    def trade_center(self) -> None:
        lg = self.league
        while True:
            self.say("")
            open_, reason = trades.trade_window(lg)
            self.say(f"TRADE CENTER - {reason}")
            choice = self.menu("Trade Center", [
                ("1", "Propose a trade"),
                ("2", f"Incoming offers ({len(lg.trade_offers)})"),
                ("3", "Scout another organisation"),
                ("4", "My draft picks"),
                ("0", "Back"),
            ])
            if choice == "0":
                return
            if choice == "1":
                abbr = self.ask("Trade partner (club abbreviation): ").upper()
                if abbr not in lg.teams or abbr == lg.user_team:
                    self.say("Unknown or invalid club.")
                    continue
                self.say(views.trade_partner_view(lg, abbr))
                self.build_trade(trades.TradeProposal(lg.user_team, abbr))
            elif choice == "2":
                self.offers_menu()
            elif choice == "3":
                abbr = self.ask("Club abbreviation: ").upper()
                if abbr in lg.teams:
                    self.say(views.trade_partner_view(lg, abbr))
            else:
                self.say(views.picks_view(lg, lg.user_team))

    def build_trade(self, prop: trades.TradeProposal) -> None:
        lg = self.league
        self.say("Enter player IDs separated by commas, and pick codes like 2027-R1-NYY.")
        fields = [
            ("Your players to send", "a_players", True),
            ("Your picks to send", "a_picks", False),
            (f"{prop.team_b} players you want", "b_players", True),
            (f"{prop.team_b} picks you want", "b_picks", False),
        ]
        for prompt, attr, numeric in fields:
            raw = self._ask_assets(prompt, getattr(prop, attr))
            if isinstance(raw, str):
                values = self._parse_ids(raw) if numeric else raw.replace(",", " ").upper().split()
                if values is None:
                    return
                raw = values
            setattr(prop, attr, raw)
        self.submit_trade(prop)

    def submit_trade(self, prop: trades.TradeProposal) -> None:
        lg = self.league
        self.say("")
        self.say(views.proposal_view(lg, prop))
        result = trades.negotiate(lg, prop)
        if result.errors:
            self.say("Trade not allowed:")
            for err in result.errors:
                self.say(f"  - {err}")
            return
        self.say(views.contract_impact_view(lg, prop))
        assert result.evaluation is not None
        self.dirty = True  # negotiation history is part of the save
        self.say(result.evaluation.message)
        for reason in trades.describe_reasons(result.evaluation):
            self.say(f"  - {reason}")
        if result.accepted:
            if self.confirm("Complete this trade?"):
                self.execute_trade(prop)
            return
        if result.counter is None:
            return
        self.say("")
        self.say(f"{prop.team_b} counters: {result.counter_note}")
        self.say(views.proposal_view(lg, result.counter))
        if self.confirm("Accept the counter-offer?"):
            self.execute_trade(result.counter)

    def execute_trade(self, prop: trades.TradeProposal) -> None:
        try:
            notes = trades.execute(self.league, prop)
        except trades.TradeError as exc:
            self.say(f"Trade failed: {exc}")
            return
        self.dirty = True
        for note in notes:
            self.say(note)
            self.league.add_news(note)

    def offers_menu(self) -> None:
        lg = self.league
        self.say(views.offers_view(lg))
        if not lg.trade_offers:
            return
        offer_id = self.ask_int("Offer number to respond to (Enter to go back): ", 1)
        offer = lg.offer_by_id(offer_id) if offer_id else None
        if offer is None:
            return
        choice = self.menu(f"Offer #{offer.offer_id} from {offer.proposal.team_a}",
                           [("1", "Accept"), ("2", "Reject"), ("3", "Counter"), ("0", "Back")])
        if choice == "1":
            try:
                notes = lg.accept_offer(offer.offer_id)
            except trades.TradeError as exc:
                self.say(str(exc))
                return
            self.dirty = True
            for note in notes:
                self.say(note)
                lg.add_news(note)
        elif choice == "2":
            lg.reject_offer(offer.offer_id)
            self.dirty = True
            self.say("Offer rejected.")
        elif choice == "3":
            lg.reject_offer(offer.offer_id)
            self.say("Build your counter-offer (current terms shown in brackets).")
            self.build_trade(offer.proposal.reversed())

    # -- free agency ---------------------------------------------------------------------

    def free_agency_menu(self) -> None:
        lg = self.league
        position: str | None = None
        limit = 25
        while True:
            self.say("")
            self.say(views.free_agents_view(lg, position, limit))
            choice = self.menu("Free Agency", [
                ("1", "Make an offer"), ("2", "Filter by position"), ("3", "Show more"), ("0", "Back"),
            ])
            if choice == "0":
                return
            if choice == "2":
                raw = self.ask("Position (SP, RP, C, 1B, ... ; blank = all): ").upper()
                position = raw or None
                continue
            if choice == "3":
                limit += 25
                continue
            pid = self.ask_int("Free agent ID (Enter to cancel): ", 1)
            if pid is None or pid not in lg.players:
                continue
            player = lg.players[pid]
            salary, years = freeagency.asking(lg, player)
            self.say(f"{player.name} is asking for {years} yr at {contracts.money(salary)}/yr.")
            chosen_years = self.ask_int(f"Years [{years}]: ", 1, contracts.MAX_CONTRACT_YEARS) or years
            raw = self.ask(f"Salary per year in $M [{salary:.2f}]: ")
            try:
                offer = float(raw) if raw else salary
            except ValueError:
                self.say("Invalid salary.")
                continue
            try:
                self.say(freeagency.sign_free_agent(lg, lg.user, pid, offer, chosen_years))
                self.dirty = True
            except freeagency.FreeAgencyError as exc:
                self.say(str(exc))

    # -- draft ---------------------------------------------------------------------------

    def draft_menu(self) -> None:
        lg = self.league
        while True:
            self.say("")
            self.say(views.draft_room_view(lg))
            if lg.phase != PHASE_DRAFT or lg.draft_on_clock() is None:
                return
            clock = lg.draft_on_clock()
            options = [("1", "Sim to my next pick"), ("3", "Auto-draft the rest (scouts pick for you)"),
                       ("4", "Show a longer board"), ("0", "Back")]
            if clock and clock[2] == lg.user_team:
                options.insert(0, ("2", "Make my selection"))
            choice = self.menu("Draft room", options)
            if choice == "0":
                return
            self.dirty = True
            if choice == "1":
                for sel in lg.draft_until_user():
                    p = lg.players[sel.player_id]
                    self.say(f"  #{sel.overall_pick} {sel.team}: {p.name} ({p.position}, {p.age})")
            elif choice == "2":
                pid = self.ask_int("Prospect ID to draft: ", 1)
                if pid is None:
                    continue
                try:
                    sel = lg.draft_select(pid)
                except ValueError as exc:
                    self.say(str(exc))
                    continue
                self.say(f"With pick #{sel.overall_pick} you select {lg.players[pid].name}!")
            elif choice == "3":
                lg.draft_auto_rest()
                self.say("The draft is complete. Finish the draft phase from SIMULATE to open free agency.")
            else:
                self.say(views.draft_board(lg, limit=60))

    # -- minor leagues -------------------------------------------------------------------

    def prospects_menu(self) -> None:
        lg, team = self.league, self.league.user
        while True:
            self.say("")
            self.say(views.prospects_view(lg, team))
            choice = self.menu("Minor League / Prospects", [
                ("1", "Promote a player to the active roster"),
                ("2", "Demote an active player to the minors"),
                ("3", "Swap an active player with a minor leaguer"),
                ("4", "Release a player"),
                ("5", "Auto-organise (best 26 on the active roster)"),
                ("0", "Back"),
            ])
            if choice == "0":
                return
            try:
                if choice == "1":
                    pid = self.ask_int("Minor leaguer ID: ", 1)
                    if pid is None:
                        continue
                    roster.promote(team, lg.players, pid)
                    self.say(f"{lg.players[pid].name} promoted.")
                elif choice == "2":
                    pid = self.ask_int("Active player ID: ", 1)
                    if pid is None:
                        continue
                    for note in roster.demote(team, lg.players, pid):
                        self.say(f"  - {note}")
                    self.say(f"{lg.players[pid].name} sent to the minors. Open active spots are "
                             "filled automatically before the next game.")
                elif choice == "3":
                    self.roster_moves_menu()
                elif choice == "4":
                    pid = self.ask_int("Player ID: ", 1)
                    if pid is None or pid not in lg.players:
                        continue
                    if self.confirm(f"Release {lg.players[pid].name}?"):
                        self.say(lg.release(team, pid))
                else:
                    frontoffice.optimize_roster(lg, team)
                    self.say("Active roster, lineup and pitching staff rebuilt from the whole organisation.")
                self.dirty = True
            except roster.RosterError as exc:
                self.say(f"Not allowed: {exc}")

    # ------------------------------------------------------------------ injuries & season

    def injury_menu(self) -> None:
        lg, team = self.league, self.league.user
        while True:
            self.say("")
            self.say(views.injury_report(lg, team))
            choice = self.menu("Injuries & injured list", [
                ("1", "Place an injured player on the IL (frees an active spot)"),
                ("2", "Activate a healthy player from the IL"),
                ("3", "Start a rehab assignment (IL player close to returning)"),
                ("4", "Clubhouse morale"),
                ("0", "Back"),
            ])
            if choice == "0":
                return
            if choice == "4":
                self.say(views.morale_view(lg, team))
                continue
            pid = self.ask_int("Player ID (Enter to cancel): ", 1)
            if pid is None or pid not in lg.players:
                continue
            try:
                if choice == "1":
                    for note in lg.place_on_il(team, pid):
                        self.say(f"  - {note}")
                elif choice == "3":
                    self.say(lg.start_rehab(team, pid))
                else:
                    self.say(lg.activate_from_il(team, pid))
                    if pid in team.reserves:
                        self.say("Promote him from Front Office > Minor League once a spot opens.")
                self.dirty = True
            except roster.RosterError as exc:
                self.say(f"Not allowed: {exc}")

    def season_center(self) -> None:
        lg = self.league
        while True:
            choice = self.menu("Season center", [
                ("1", "Season milestones"),
                ("2", "Awards"),
                ("3", "Season summary"),
                ("4", "Franchise history"),
                ("5", "Career leaderboards (all-time)"),
                ("6", "Franchise records & franchise career leaders"),
                ("7", "Postseason leaders (career)"),
                ("8", "Retired players"),
                ("9", "Career milestones"),
                ("10", "Postseason history"),
                ("11", "Championship history"),
                ("0", "Back"),
            ])
            if choice == "0":
                return
            if choice == "1":
                self.say(views.milestones_view(lg))
            elif choice == "2":
                years = [item["year"] for item in lg.awards_history]
                year = None
                if len(years) > 1:
                    year = self.ask_int(f"Year ({years[0]}-{years[-1]}, Enter = latest): ", years[0], years[-1])
                self.say(views.awards_view(lg, year if year is not None else (years[-1] if years else None)))
            elif choice == "3":
                self.say(views.season_summary(lg))
            elif choice == "4":
                self.say(views.franchise_history(lg))
            elif choice == "5":
                self.say(views.career_leaders_view(lg))
            elif choice == "6":
                raw = self.ask(f"Club abbreviation [{lg.user_team}]: ").upper() or lg.user_team
                if raw not in lg.teams:
                    self.say("Unknown club.")
                    continue
                self.say(views.records_view(lg, raw))
                self.say("")
                self.say(views.career_leaders_view(lg, team=raw))
            elif choice == "7":
                self.say(views.career_leaders_view(lg, postseason=True))
            elif choice == "8":
                self.say(views.retired_view(lg))
            elif choice == "9":
                self.say(views.career_milestones_view(lg))
            elif choice == "10":
                self.say(views.postseason_history_view(lg))
            else:
                self.say(views.championships_view(lg))

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
