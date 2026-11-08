"""The ``League`` aggregate: season state, day advancement and queries.

All randomness is derived from ``League.seed`` with stable string seeds, so a
league replays identically given the same manager decisions, including across
save/load round trips.
"""

from __future__ import annotations

import random
import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from mlb_manager import awards as awards_mod
from mlb_manager import careers, contracts, development, finance, freeagency, frontoffice, injuries, morale, seasonlog, trades
from mlb_manager.dugout import LiveGame
from mlb_manager import draft as draft_mod
from mlb_manager import ratings as rt
from mlb_manager import roster
from mlb_manager.draft import Draft, DraftPick, DraftSelection
from mlb_manager.franchises import LEAGUES, get_franchise
from mlb_manager.game import GameResult, GameSimulator, simulate_game
from mlb_manager.generation import PlayerFactory, generate_rosters
from mlb_manager.models import BattingStats, PitchingStats, Player, RetiredPlayer, Team
from mlb_manager.playoffs import Playoffs, Series, SeriesGame
from mlb_manager.schedule import ScheduledGame, generate_schedule
from mlb_manager.standings import TeamRecord, compute_records, playoff_seeds, rank
from mlb_manager.trades import TradeOffer

PHASE_REGULAR = "regular"
PHASE_POSTSEASON = "postseason"
PHASE_COMPLETE = "complete"
PHASE_RESIGN = "resign"
PHASE_DRAFT = "draft"
PHASE_FREE_AGENCY = "free_agency"
OFFSEASON_PHASES: tuple[str, ...] = (PHASE_RESIGN, PHASE_DRAFT, PHASE_FREE_AGENCY)
PHASE_LABELS: dict[str, str] = {
    PHASE_REGULAR: "Regular season",
    PHASE_POSTSEASON: "Postseason",
    PHASE_COMPLETE: "Season complete",
    PHASE_RESIGN: "Offseason: re-signing period",
    PHASE_DRAFT: "Offseason: amateur draft",
    PHASE_FREE_AGENCY: "Offseason: free agency",
}

DEFAULT_YEAR = 2026
DAILY_RECOVERY = 25
STARTER_READY_FATIGUE = 20
NEWS_LIMIT = 60
TRANSACTION_LIMIT = 300
FREE_AGENT_POOL_LIMIT = 160
RESIGN_LOYALTY_DISCOUNT = 0.95
MILESTONE_LIMIT = 250
CPU_ROSTER_REVIEW_DAYS = 14  # CPU promotions/demotions and lineup review
FRANCHISE_LOG_LIMIT = 400
MAJOR_CONTRACT_SALARY = 10.0  # $M per year: logged in franchise history
CAREER_MILESTONE_LIMIT = 400


@dataclass(slots=True)
class PlayedGame:
    """A game played on a given day, with a human label (e.g. series name)."""

    label: str
    result: GameResult


@dataclass(slots=True)
class UserGame:
    """The user's game on the next day: who plays where and how it counts."""

    away: str
    home: str
    key: str
    label: str
    record_stats: bool
    round_code: str | None = None


@dataclass(slots=True)
class DayReport:
    day: int
    phase: str
    games: list[PlayedGame] = field(default_factory=list)
    news: list[str] = field(default_factory=list)


@dataclass(slots=True)
class League:
    seed: int
    year: int
    user_team: str
    teams: dict[str, Team]
    players: dict[int, Player]
    schedule: list[ScheduledGame]
    next_player_id: int
    day: int = 0
    phase: str = PHASE_REGULAR
    playoffs: Playoffs | None = None
    news: list[str] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
    last_user_game: GameResult | None = None
    finance: finance.LeagueFinance = field(default_factory=finance.LeagueFinance)  # rules + money state
    draft_picks: list[DraftPick] = field(default_factory=list)
    draft: Draft | None = None
    pending_draft_order: list[str] = field(default_factory=list)
    trade_offers: list[TradeOffer] = field(default_factory=list)
    next_offer_id: int = 1
    transactions: list[str] = field(default_factory=list)
    milestones: list[str] = field(default_factory=list)  # current season
    milestone_keys: list[str] = field(default_factory=list)  # dedupe keys, current season
    awards_history: list[dict[str, Any]] = field(default_factory=list)  # {"year", "entries"}
    franchise_log: list[dict[str, Any]] = field(default_factory=list)  # user's major events
    retired: dict[int, RetiredPlayer] = field(default_factory=dict)  # permanent career records
    postseason_awards: list[dict[str, Any]] = field(default_factory=list)  # series MVPs, all years
    career_milestones: list[str] = field(default_factory=list)  # all-time career milestones
    trade_memory: list[dict[str, Any]] = field(default_factory=list)  # user proposals and outcomes
    trade_log: list[dict[str, Any]] = field(default_factory=list)  # executed trades (anti-flip, limits)
    live_game: LiveGame | None = field(default=None, repr=False)  # the user's game in progress (dugout)
    _by_day: dict[int, list[ScheduledGame]] | None = field(default=None, repr=False)
    _pending_news: list[str] = field(default_factory=list, repr=False)
    _injury_teams: set[str] = field(default_factory=set, repr=False)
    _career_base: dict[int, tuple[BattingStats, PitchingStats]] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------ creation

    @classmethod
    def create(
        cls, seed: int, user_team: str, year: int = DEFAULT_YEAR, salary_cap: float | None = None,
        finance_config: finance.FinanceConfig | None = None,
    ) -> League:
        """Generate a new league.

        Financial rules default to the standard MLB rules of ``year``;
        ``salary_cap`` is a shortcut for custom rules with that hard cap.
        """
        user_team = get_franchise(user_team).abbr
        errors = finance.validate_season(year)
        if errors:
            raise ValueError(errors[0])
        if finance_config is None:
            finance_config = (finance.hard_cap_config(year, salary_cap) if salary_cap is not None
                              else finance.new_config(year))
        teams, players, next_id = generate_rosters(seed)
        league = cls(
            seed=seed,
            year=year,
            user_team=user_team,
            teams=teams,
            players=players,
            schedule=generate_schedule(f"{seed}:{year}"),
            next_player_id=next_id,
            finance=finance.LeagueFinance(finance_config),
        )
        frontoffice.initialize(league)
        finance.fit_initial_payrolls(league)
        league.assign_initial_service()
        finance.start_season(league)
        league.add_news(f"Welcome to the {year} season, {teams[user_team].full_name}!")
        return league

    # ------------------------------------------------------------------ helpers

    def _rng(self, *parts: object) -> random.Random:
        return random.Random(":".join(str(p) for p in (self.seed, self.year, *parts)))

    def add_news(self, message: str) -> None:
        self.news.append(message)
        if len(self.news) > NEWS_LIMIT:
            del self.news[: len(self.news) - NEWS_LIMIT]

    def log_transaction(self, message: str) -> None:
        self.transactions.append(f"{self.year} {self.phase_label_short()}: {message}")
        if len(self.transactions) > TRANSACTION_LIMIT:
            del self.transactions[: len(self.transactions) - TRANSACTION_LIMIT]
        if self.user_team in message and self._is_major(message):
            self.log_franchise("transaction", message)

    def _is_major(self, message: str) -> bool:
        if message.startswith("TRADE"):
            return True
        if "signed free agent" in message or "re-signed" in message or "extended" in message:
            amounts = [float(m) for m in re.findall(r"\$([\d.]+)M", message)]
            return bool(amounts) and max(amounts) >= MAJOR_CONTRACT_SALARY
        return message.startswith("Draft #") and "(R1)" in message

    def log_franchise(self, kind: str, text: str) -> None:
        self.franchise_log.append({"year": self.year, "kind": kind, "text": text})
        if len(self.franchise_log) > FRANCHISE_LOG_LIMIT:
            del self.franchise_log[: len(self.franchise_log) - FRANCHISE_LOG_LIMIT]

    def add_milestone(self, key: str, message: str, news: list[str] | None = None) -> bool:
        if key in self.milestone_keys:
            return False
        self.milestone_keys.append(key)
        self.milestones.append(f"{self.phase_label_short()}: {message}")
        if len(self.milestones) > MILESTONE_LIMIT:
            del self.milestones[: len(self.milestones) - MILESTONE_LIMIT]
        if news is not None:
            news.append(message)
        return True

    def assign_initial_service(self) -> None:
        """Estimate MLB service time for a freshly generated or migrated league."""
        for player in self.players.values():
            if player.draft_year is not None:
                continue
            team = self.teams.get(player.team or "")
            in_minors = team is not None and player.id in team.reserves
            player.service_years = max(0, player.age - (25 if in_minors else 23))

    def phase_label_short(self) -> str:
        if self.phase == PHASE_REGULAR:
            return f"day {self.day + 1}"
        return PHASE_LABELS.get(self.phase, self.phase).replace("Offseason: ", "")

    @property
    def in_season(self) -> bool:
        return self.phase in (PHASE_REGULAR, PHASE_POSTSEASON)

    @property
    def in_offseason(self) -> bool:
        return self.phase in OFFSEASON_PHASES

    @property
    def draft_year(self) -> int:
        """Year of the next draft (held in the offseason after this season)."""
        return self.year

    @property
    def user(self) -> Team:
        return self.teams[self.user_team]

    def games_by_day(self) -> dict[int, list[ScheduledGame]]:
        if self._by_day is None:
            by_day: dict[int, list[ScheduledGame]] = defaultdict(list)
            for g in self.schedule:
                by_day[g.day].append(g)
            self._by_day = dict(by_day)
        return self._by_day

    @property
    def last_regular_day(self) -> int:
        return max(g.day for g in self.schedule)

    @property
    def is_complete(self) -> bool:
        return self.phase == PHASE_COMPLETE

    def records(self) -> dict[str, TeamRecord]:
        return compute_records(self.schedule, self.teams)

    def better_record(self, a: str, b: str) -> str:
        records = self.records()
        return rank([a, b], records)[0]

    # ------------------------------------------------------------------ games

    def choose_starter(self, team: Team) -> int:
        """Next rested starter in rotation order (falls back to the freshest)."""
        size = len(team.rotation)
        for offset in range(size):
            index = (team.rotation_index + offset) % size
            pid = team.rotation[index]
            player = self.players[pid]
            if player.fatigue <= STARTER_READY_FATIGUE and not player.is_injured and pid in team.active:
                team.rotation_index = (index + 1) % size
                return pid
        healthy_rotation = [p for p in team.rotation if not self.players[p].is_injured and p in team.active]
        if healthy_rotation:
            pid = min(healthy_rotation, key=lambda p: (self.players[p].fatigue, p))
            team.rotation_index = (team.rotation.index(pid) + 1) % size
            return pid
        # Whole rotation hurt: start the freshest healthy active pitcher (a bullpen game).
        pool = [p for p in team.active if self.players[p].is_pitcher and not self.players[p].is_injured]
        return min(pool, key=lambda p: (self.players[p].fatigue, p))

    def _prepare(self, team: Team) -> None:
        notes = frontoffice.ensure_active_roster(self, team)
        if team.abbr == self.user_team:
            for note in notes:
                self.add_news(note)

    def play_game(self, away: str, home: str, key: str, record_stats: bool,
                  round_code: str | None = None) -> GameResult:
        """Play one game. Regular-season stats are recorded when ``record_stats``;
        postseason stats go to the round ``round_code`` (never mixed).

        If the user is managing this game live, its result is used (the rest of
        the game is completed automatically if it is still in progress)."""
        live = self.live_game
        if live is not None and live.key == key:
            if not live.is_over:
                live.finish()
            result = live.result
            assert result is not None
            self.live_game = None
            self._apply_result(result, record_stats, round_code)
            self._apply_injuries(result, key)
            self.last_user_game = result
            return result
        away_team, home_team = self.teams[away], self.teams[home]
        self._prepare(away_team)
        self._prepare(home_team)
        away_starter = self.choose_starter(away_team)
        home_starter = self.choose_starter(home_team)
        away_day, _ = injuries.game_day_team(away_team, self.players)
        home_day, _ = injuries.game_day_team(home_team, self.players)
        result = simulate_game(
            away_day, home_day, self.game_players(away_day, home_day), self._rng("game", key),
            away_starter, home_starter, record_events=self.user_team in (away, home),
        )
        self._apply_result(result, record_stats, round_code)
        self._apply_injuries(result, key)
        if self.user_team in (away, home):
            self.last_user_game = result
        return result

    # ------------------------------------------------------------------ live games (dugout)

    def user_game_today(self) -> UserGame | None:
        """The user's game on the next day to be played, if any."""
        if self.phase == PHASE_REGULAR:
            for g in self.games_by_day().get(self.day, []):
                if not g.played and self.user_team in (g.away, g.home):
                    return UserGame(g.away, g.home, f"reg:{g.game_id}", "Regular season", True)
            return None
        if self.phase == PHASE_POSTSEASON and self.playoffs is not None:
            for series in self.playoffs.active_series():
                if self.user_team in (series.high, series.low):
                    away, home = series.next_matchup()
                    number = len(series.games) + 1
                    return UserGame(away, home, f"post:{series.series_id}:{number}",
                                    f"{series.round_name} G{number}", False, careers.round_code(series.round_index))
        return None

    def advance_to_user_game(self, max_days: int = 10) -> list[DayReport]:
        """Play days without a user game until the user's club plays next (or the season ends)."""
        reports: list[DayReport] = []
        for _ in range(max_days):
            if not self.in_season or self.user_game_today() is not None or self.live_game is not None:
                break
            reports.append(self.advance_day())
        return reports

    def start_live_game(self) -> LiveGame:
        """Set up the user's next game for pitch-by-pitch management from the dugout.

        Pre-game work (roster checks, starting pitchers, injury replacements) is
        exactly what an automatic game would do; the game itself is then played
        through decision points. Playing the day (``advance_day``) consumes it."""
        if self.live_game is not None:
            return self.live_game
        info = self.user_game_today()
        if info is None:
            raise RuntimeError("Your club does not play today.")
        away_team, home_team = self.teams[info.away], self.teams[info.home]
        self._prepare(away_team)
        self._prepare(home_team)
        away_starter = self.choose_starter(away_team)
        home_starter = self.choose_starter(home_team)
        away_day, _ = injuries.game_day_team(away_team, self.players)
        home_day, _ = injuries.game_day_team(home_team, self.players)
        sim = GameSimulator(away_day, home_day, self.game_players(away_day, home_day), self._rng("game", info.key),
                            away_starter, home_starter, pitch_by_pitch=True)
        user_key = "away" if info.away == self.user_team else "home"
        self.live_game = LiveGame(sim, user_key, info.key, info.label, info.record_stats, info.round_code,
                                  self._rng("dugout", info.key))
        return self.live_game

    def finish_live_game(self) -> DayReport:
        """Complete the day of the live game (finishing the game automatically if needed)."""
        if self.live_game is None:
            raise RuntimeError("No game in progress.")
        return self.advance_day()

    def form_modifier(self, player: Player) -> int:
        """Rating points added in games today: morale and post-injury rust."""
        return morale.performance_modifier(player) + injuries.form_modifier(player)

    def game_players(self, *teams: Team) -> dict[int, Player]:
        """Players as they perform today: copies with form-adjusted ratings.

        The engine reads ratings from this mapping; the stored players are never
        modified, so form effects are purely game-day.
        """
        adjusted: dict[int, Player] = {}
        for team in teams:
            for pid in team.active:
                player = self.players[pid]
                delta = self.form_modifier(player)
                if delta:
                    ratings = {attr: (value if attr == "stamina" else rt.clamp_rating(value + delta))
                               for attr, value in player.ratings.items()}
                    adjusted[pid] = replace(player, ratings=ratings)
        if not adjusted:
            return self.players
        view = dict(self.players)
        view.update(adjusted)
        return view

    def _career_base_for(self, player: Player) -> tuple[BattingStats, PitchingStats]:
        base = self._career_base.get(player.id)
        if base is None:
            base = careers.archived_totals(player)
            self._career_base[player.id] = base
        return base

    def _career_milestones(self, player: Player, batting: BattingStats | None,
                           pitching: PitchingStats | None) -> None:
        if not player.history:
            return  # first season: no career milestone can be reached yet
        for message in careers.career_milestones(player, self._career_base_for(player), batting, pitching):
            if self.add_milestone(f"career:{player.id}:{message}", message, self._pending_news):
                self.career_milestones.append(f"{self.year}: {message}")
                if len(self.career_milestones) > CAREER_MILESTONE_LIMIT:
                    del self.career_milestones[: len(self.career_milestones) - CAREER_MILESTONE_LIMIT]

    def _apply_result(self, result: GameResult, record_stats: bool, round_code: str | None = None) -> None:
        for box in (result.away_box, result.home_box):
            for pid, line in box.pitching.items():
                player = self.players[pid]
                player.fatigue += result.workload.get(pid, line.pitches)
                if record_stats:
                    player.pitching.add(line)
                    for message in seasonlog.stat_milestones(player, None, line):
                        self.add_milestone(f"{pid}:{message}", message, self._pending_news)
                    self._career_milestones(player, None, line)
                elif round_code is not None:
                    careers.add_postseason(player, round_code, None, line)
            for pid, position in box.batting_order:
                player = self.players[pid]
                line = box.batting[pid]
                if record_stats:
                    player.batting.add(line)
                    if pid not in box.subs:  # games *started* per position
                        player.games_by_pos[position] = player.games_by_pos.get(position, 0) + 1
                    for message in seasonlog.stat_milestones(player, line, None):
                        self.add_milestone(f"{pid}:{message}", message, self._pending_news)
                    self._career_milestones(player, line, None)
                elif round_code is not None:
                    careers.add_postseason(player, round_code, line, None)
        if record_stats:
            for message in seasonlog.no_hitters(result, self.players):
                self.add_milestone(f"nohit:{self.day}:{message}", message, self._pending_news)

    def _injury_multiplier(self, player: Player) -> float:
        return injuries.risk_multiplier(player, self.seed, self.year)

    def _apply_injuries(self, result: GameResult, key: str) -> None:
        rng = self._rng("injury", key)
        for player, description, days in injuries.injuries_from_game(
            (result.away_box, result.home_box), self.players, rng, self._injury_multiplier, result.workload
        ):
            injuries.apply_injury(player, description, days, self.year, self.day)
            if player.team == self.user_team:
                self._pending_news.append(
                    f"INJURY: {player.name} ({player.position}) - {description}, out ~{days} day(s)."
                    + (" Consider placing him on the IL." if days >= injuries.IL_THRESHOLD_DAYS else "")
                )
            elif player.team and days >= injuries.IL_THRESHOLD_DAYS:
                self._injury_teams.add(player.team)
            if days >= 60 and player.overall >= 60:
                self.log_transaction(f"{player.team}: {player.name} out ~{days} days ({description}).")

    def _recover(self) -> None:
        for player in self.players.values():
            if player.fatigue:
                player.fatigue = max(0, player.fatigue - DAILY_RECOVERY)
            if not (player.injury_days or player.rust_days):
                continue
            was_rehab = player.rehab
            healed = injuries.heal_one_day(player)
            if not player.team:
                continue
            team = self.teams[player.team]
            if healed:
                if player.team == self.user_team:
                    if was_rehab:
                        self.add_news(f"{player.name} completed his rehab assignment and is ready "
                                      "to be activated (Injuries menu).")
                    else:
                        on_il = player.id in team.injured_list
                        rust = f" He will be rusty for ~{player.rust_days} days." if player.rust_days else ""
                        self.add_news(f"{player.name} is healthy again"
                                      + (" - activate him from the IL (Injuries menu)." if on_il else ".")
                                      + rust)
                elif player.id in team.injured_list or was_rehab:
                    self._injury_teams.add(player.team)
            elif player.id in team.injured_list:
                if player.team == self.user_team and player.injury_days == injuries.REHAB_WINDOW_DAYS \
                        and player.injury_total >= injuries.REHAB_MIN_INJURY:
                    self.add_news(f"{player.name} can begin a rehab assignment (Injuries menu).")
                elif player.team != self.user_team and player.injury_days == injuries.CPU_REHAB_DAYS:
                    self._injury_teams.add(player.team)

    # ------------------------------------------------------------------ advancing

    def advance_day(self) -> DayReport:
        """Play every game scheduled for the next day."""
        self._pending_news = []
        if self.phase == PHASE_REGULAR:
            report = self._advance_regular_day()
        elif self.phase == PHASE_POSTSEASON:
            report = self._advance_postseason_day()
        else:
            return DayReport(self.day, self.phase)
        self._recover()
        self._cpu_injury_moves()
        report.news[:0] = self._pending_news
        self._pending_news = []
        for item in report.news:
            self.add_news(item)
        return report

    def _advance_regular_day(self) -> DayReport:
        report = DayReport(self.day, PHASE_REGULAR)
        for g in self.games_by_day().get(self.day, []):
            if g.played:
                continue
            result = self.play_game(g.away, g.home, f"reg:{g.game_id}", record_stats=True)
            g.away_runs, g.home_runs = result.away_runs, result.home_runs
            g.innings = result.innings
            g.winning_pitcher = result.winning_pitcher
            g.losing_pitcher = result.losing_pitcher
            g.save_pitcher = result.save_pitcher
            report.games.append(PlayedGame("Regular season", result))
        report.news.extend(self._front_office_day())
        finance.accrue_day(self)
        self.day += 1
        if self.day > self.last_regular_day:
            report.news.extend(self._start_postseason())
        return report

    def _cpu_injury_moves(self) -> None:
        """CPU clubs IL injured players, call up replacements and reinstate the healed."""
        for abbr in sorted(self._injury_teams):
            if abbr == self.user_team:
                continue
            team = self.teams[abbr]
            before = set(team.injured_list)
            rehab_before = {pid for pid in team.reserves if self.players[pid].rehab}
            frontoffice.optimize_roster(self, team, use_performance=self.phase == PHASE_REGULAR)
            for pid in sorted(set(team.injured_list) - before):
                self.log_transaction(f"{abbr} placed {self.players[pid].name} on the injured list "
                                     f"({self.players[pid].injury}).")
            for pid in sorted(p for p in team.reserves if self.players[p].rehab and p not in rehab_before):
                self.log_transaction(f"{abbr} sent {self.players[pid].name} on a rehab assignment.")
        self._injury_teams = set()

    def _monthly_development(self) -> list[str]:
        rng = self._rng("in-season-development", self.day)
        records = self.records()
        news = []
        for player in sorted(self.players.values(), key=lambda p: p.id):
            if player.team is None:
                continue
            playing_time = self._playing_time(player, records[player.team].games)
            delta = development.in_season_development(player, rng, playing_time)
            if player.team == self.user_team and delta >= 2:
                news.append(f"{player.name} is developing quickly ({delta:+d} OVR, now {player.overall}).")
        return news

    def _playing_time(self, player: Player, games: int) -> float:
        team = self.teams[player.team]  # type: ignore[index]
        if player.is_injured or player.id in team.injured_list:
            return 0.3
        if player.id in team.reserves:
            return 0.8  # everyday playing time in the minors
        games = max(1, games)
        if player.is_pitcher:
            per_game = 3.6 if player.id in team.rotation else 1.2
            share = player.pitching.outs / (games * per_game)
        else:
            share = player.batting.pa / (games * 4.0)
        return 0.5 + min(1.0, share) * 0.7

    def _front_office_day(self) -> list[str]:
        """Weekly CPU front-office activity during the regular season."""
        news: list[str] = []
        self.trade_offers = [o for o in self.trade_offers if o.expires_day >= self.day]
        for key, message in seasonlog.standings_milestones(self):
            self.add_milestone(key, message, news if self.user_team in key or "clinch" in key else None)
        if self.day and self.day % development.IN_SEASON_INTERVAL_DAYS == 0:
            news.extend(self._monthly_development())
        if self.day == 0 or self.day % 7 != 0:
            return news
        for player in morale.weekly_update(self):
            reasons = [r for r, d in morale.factors(self, player) if d < 0]
            news.append(f"{player.name} is unhappy" + (f" ({reasons[0].lower()})." if reasons else "."))
        frontoffice.update_strategies(self)
        if self.day % CPU_ROSTER_REVIEW_DAYS == 0:
            for team in sorted(self.teams.values(), key=lambda t: t.abbr):
                if team.abbr != self.user_team:
                    frontoffice.optimize_roster(self, team, use_performance=True)
        rng = self._rng("front-office", self.day)
        for note in trades.cpu_trade_round(self, rng, attempts=1):
            if self.user_team in note:
                news.append(note)
        if rng.random() < 0.5:
            news.extend(self._add_cpu_offer(rng))
        if self.day == trades.deadline_day(self):
            news.append("The trade deadline passes at the end of today.")
        return news

    def _add_cpu_offer(self, rng: random.Random) -> list[str]:
        offer = trades.generate_offer_for_user(self, rng)
        if offer is None:
            return []
        if any(o.proposal.key() == offer.proposal.key() for o in self.trade_offers):
            return []  # never repeat an identical pending offer
        self.trade_offers.append(offer)
        return [f"{offer.proposal.team_a} has made you a trade offer (Front Office > Trade Center)."]

    def _start_postseason(self) -> list[str]:
        records = self.records()
        seeds = {lg: playoff_seeds(self.teams, records, lg) for lg in LEAGUES}
        self.playoffs = Playoffs.create(seeds)
        self.phase = PHASE_POSTSEASON
        news = [f"The {self.year} regular season is over."]
        for lg in LEAGUES:
            news.append(f"{lg} playoff seeds: " + ", ".join(
                f"{i}. {abbr}" for i, abbr in enumerate(seeds[lg], start=1)
            ))
        user_in = any(self.user_team in s for s in seeds.values())
        news.append(
            f"{self.user_team} {'clinched a playoff berth!' if user_in else 'missed the postseason.'}"
        )
        if user_in:
            self.log_franchise("postseason", f"Clinched a playoff berth (seed "
                               f"{next(s.index(self.user_team) + 1 for s in seeds.values() if self.user_team in s)}).")
        entries = awards_mod.compute_awards(self)
        self.awards_history.append({"year": self.year, "entries": entries})
        for entry in awards_mod.headline_awards(entries):
            news.append(f"{entry['award']}: {entry['name']} ({entry['team']}) - {entry['line']}")
        for entry in entries:
            if entry["team"] == self.user_team:
                self.log_franchise("award", f"{entry['name']} wins the {entry['award']}.")
        return news

    def season_awards(self, year: int | None = None) -> list[dict[str, Any]]:
        year = self.year if year is None else year
        for item in self.awards_history:
            if item["year"] == year:
                return item["entries"]
        return []

    def _advance_postseason_day(self) -> DayReport:
        assert self.playoffs is not None
        report = DayReport(self.day, PHASE_POSTSEASON)
        for series in self.playoffs.active_series():
            away, home = series.next_matchup()
            number = len(series.games) + 1
            code = careers.round_code(series.round_index)
            result = self.play_game(
                away, home, f"post:{series.series_id}:{number}", record_stats=False, round_code=code
            )
            series.record(
                SeriesGame(away, home, result.away_runs, result.home_runs, result.innings)
            )
            report.games.append(PlayedGame(f"{series.round_name} G{number}", result))
            if series.is_over:
                report.news.append(f"{series.round_name}: {series.summary()}")
                report.news.extend(self._series_awards(series, code))
        self.day += 1
        if not self.playoffs.active_series():
            if self.playoffs.is_complete:
                report.news.extend(self._finish_season())
            else:
                self.playoffs.advance_round(self.better_record)
                names = ", ".join(
                    f"{s.high} vs {s.low}" for s in self.playoffs.current_round
                )
                report.news.append(f"{self.playoffs.current_round[0].round_name} set: {names}")
        return report

    def _series_awards(self, series: Series, code: str) -> list[str]:
        if code not in ("CS", "WS") or series.winner is None:
            return []
        mvp = careers.series_mvp(self, series.winner, code)
        if mvp is None:
            return []
        award = "World Series MVP" if code == "WS" else f"{series.league}CS MVP"
        entry = {"year": self.year, "award": award, "player_id": mvp.id, "name": mvp.name,
                 "team": series.winner, "line": careers.series_line(mvp, code)}
        self.postseason_awards.append(entry)
        if series.winner == self.user_team:
            self.log_franchise("award", f"{mvp.name} named {award}.")
        return [f"{award}: {mvp.name} ({series.winner}) - {entry['line']}"]

    def postseason_award_list(self, year: int | None = None) -> list[dict[str, Any]]:
        return [a for a in self.postseason_awards if year is None or a["year"] == year]

    def _finish_season(self) -> list[str]:
        assert self.playoffs is not None
        self.phase = PHASE_COMPLETE
        ws = self.playoffs.current_round[0]
        champion = ws.winner
        assert champion is not None
        records = self.records()
        rec = records[self.user_team]
        result = self.postseason_finish(self.user_team)
        self.history.append(
            {
                "year": self.year,
                "champion": champion,
                "runner_up": ws.loser,
                "user_wins": rec.wins,
                "user_losses": rec.losses,
                "user_result": result,
                "user_runs": rec.runs_for,
                "user_runs_allowed": rec.runs_against,
                "best_record": max(records, key=lambda a: (records[a].wins, a)),
                "awards": [
                    {"award": e["award"], "name": e["name"], "team": e["team"]}
                    for e in awards_mod.headline_awards(self.season_awards())
                ],
                "leaders": self._leader_snapshot(),
                "milestones": list(self.milestones[-12:]),
                "series": [f"{s.round_name}: {s.summary()}" for rnd in self.playoffs.rounds for s in rnd],
                "ws_mvp": next((a["name"] + f" ({a['team']})" for a in self.postseason_award_list(self.year)
                                if a["award"] == "World Series MVP"), None),
            }
        )
        if champion == self.user_team:
            self.log_franchise("championship", f"Won the {self.year} World Series!")
        elif result != "Missed playoffs":
            self.log_franchise("postseason", f"{result} in the {self.year} postseason.")
        rounds: dict[str, int] = defaultdict(int)
        for rnd in self.playoffs.rounds:
            for s in rnd:
                rounds[s.high] += 1
                rounds[s.low] += 1
        news = [f"The {self.teams[champion].full_name} win the {self.year} World Series!"]
        news.extend(finance.settle_season(self, dict(rounds)))
        return news

    def _leader_snapshot(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for label, stat, fmt in (("HR", "hr", "{:.0f}"), ("AVG", "avg", "{:.3f}"), ("RBI", "rbi", "{:.0f}")):
            rows = self.batting_leaders(stat, 1)
            if rows:
                p, v = rows[0]
                out[label] = f"{p.name} ({p.team}) {fmt.format(v)}"
        for label, stat, fmt in (("ERA", "era", "{:.2f}"), ("W", "w", "{:.0f}"), ("SO", "so", "{:.0f}"),
                                 ("SV", "sv", "{:.0f}")):
            rows = self.pitching_leaders(stat, 1)
            if rows:
                p, v = rows[0]
                out[label] = f"{p.name} ({p.team}) {fmt.format(v)}"
        return out

    def postseason_finish(self, abbr: str) -> str:
        if self.playoffs is None:
            return "Season in progress"
        if self.playoffs.champion == abbr:
            return "Won World Series"
        for rnd in self.playoffs.rounds:
            for s in rnd:
                if s.loser == abbr:
                    return f"Lost {s.round_name}"
        if not any(abbr in seeds for seeds in self.playoffs.seeds.values()):
            return "Missed playoffs"
        return "Still alive"

    def sim_days(self, days: int) -> list[DayReport]:
        reports = []
        for _ in range(days):
            if self.is_complete:
                break
            reports.append(self.advance_day())
        return reports

    def sim_until(self, stop: Callable[[League], bool]) -> list[DayReport]:
        reports = []
        while not self.is_complete and not stop(self):
            reports.append(self.advance_day())
        return reports

    def sim_regular_season(self) -> list[DayReport]:
        return self.sim_until(lambda lg: lg.phase != PHASE_REGULAR)

    def sim_to_completion(self) -> list[DayReport]:
        return self.sim_until(lambda lg: False)

    def user_series(self) -> Series | None:
        if self.playoffs is None:
            return None
        for s in self.playoffs.current_round:
            if self.user_team in (s.high, s.low):
                return s
        return None

    # ------------------------------------------------------------------ new season

    def start_new_season(self) -> None:
        """Run the whole offseason automatically (CPU logic for every club)."""
        if not self.is_complete:
            raise RuntimeError("The current season is not complete yet.")
        self.begin_offseason()
        while self.in_offseason:
            self.advance_offseason(auto_user=True)

    # ------------------------------------------------------------------ offseason

    def begin_offseason(self) -> list[str]:
        """Season complete -> aging, development, contract years, retirements."""
        if not self.is_complete:
            raise RuntimeError("The current season is not complete yet.")
        records = self.records()
        best_first = rank(list(self.teams), records)
        finish: dict[str, int] = {}
        if self.playoffs is not None:
            for round_index, rnd in enumerate(self.playoffs.rounds):
                for series in rnd:
                    if series.loser:
                        finish[series.loser] = round_index + 1
            if self.playoffs.champion:
                finish[self.playoffs.champion] = len(self.playoffs.rounds) + 1
        self.pending_draft_order = draft_mod.draft_order(self.teams, best_first, finish)

        news: list[str] = [f"The {self.year} offseason begins."]
        for player in sorted(self.players.values(), key=lambda p: p.id):
            careers.archive_season(player, self.year)
        self._career_base = {}
        morale.offseason_reset(self, self.playoffs.champion if self.playoffs else None)
        rng = self._rng("development")
        user_changes: list[tuple[Player, int]] = []
        for team in self.teams.values():
            team.reserves.extend(team.injured_list)
            team.injured_list = []
        for player in sorted(self.players.values(), key=lambda p: p.id):
            if player.batting.g or player.pitching.g:
                player.service_years += 1
            injuries.heal_fully(player)
            player.age += 1
            player.fatigue = 0
            delta = development.develop(player, rng)
            if player.team is not None:
                contracts.advance_contract_year(player)
            if player.team == self.user_team and abs(delta) >= 3:
                user_changes.append((player, delta))
        for player, delta in sorted(user_changes, key=lambda x: -abs(x[1]))[:6]:
            verb = "improved" if delta > 0 else "declined"
            news.append(f"{player.name} {verb} ({delta:+d} OVR, now {player.overall}).")

        retire_rng = self._rng("retirement")
        for player in sorted(self.players.values(), key=lambda p: p.id):
            if development.should_retire(player, retire_rng):
                if player.team == self.user_team or player.overall >= 62:
                    news.append(f"{player.name} ({player.team or 'FA'}, age {player.age}) retires.")
                self._remove_player(player.id)

        for team in self.teams.values():
            team.dead_money = 0.0
            frontoffice.ensure_active_roster(self, team)
        frontoffice.update_strategies(self)
        self.trade_offers = []
        self.phase = PHASE_RESIGN
        pending = self.pending_free_agents(self.user)
        if pending:
            news.append(
                f"{len(pending)} of your players have expiring contracts. Re-sign them in "
                "Front Office > Contracts & Payroll before the draft."
            )
        for item in news:
            self.add_news(item)
        return news

    def _remove_player(self, player_id: int) -> None:
        """Retire a player: he leaves the league but keeps his career record."""
        player = self.players.pop(player_id)
        if player.team is not None and player_id in self.teams[player.team].all_players:
            self.teams[player.team].remove_player(player_id)
        self._career_base.pop(player_id, None)
        if player.history:
            self.retired[player_id] = RetiredPlayer.from_player(player, self.year)

    def note_departure(self, player: Player) -> None:
        """Bookkeeping when a player leaves his club (trade, release).

        Before the season is archived, his stats so far are credited to the old
        club; morale gets a fresh start with a new club.
        """
        if not self.in_offseason:
            careers.close_stint(player)
        morale.new_club(player)

    def pending_free_agents(self, team: Team) -> list[Player]:
        return [self.players[pid] for pid in team.all_players if self.players[pid].contract_years <= 0]

    def resign_ask(self, player: Player, years: int | None = None) -> tuple[float, int]:
        """Re-signing ask: loyalty discount, adjusted by morale (happy players ask less).

        Pending free agents short of six service years are under club control:
        their one-year terms are an arbitration (or pre-arbitration) salary.
        """
        minimum = finance.min_salary(self)
        if contracts.arbitration_eligible(player) and years in (None, 1):
            return contracts.arbitration_salary(player, minimum), 1
        return contracts.asking_contract(
            player, self.seed, discount=RESIGN_LOYALTY_DISCOUNT * morale.contract_multiplier(player),
            minimum=minimum,
        )

    def contract_check(self, team: Team, kind: str, added: float, salary: float = 0.0) -> finance.TransactionCheck:
        """Apply the league's financial rules to a contract move (raises ContractError if illegal)."""
        check = finance.check_transaction(self, team, kind, added, salary=salary)
        if not check.allowed:
            raise contracts_error(check.reason)
        return check

    def extend_contract(self, team: Team, player_id: int, years: int) -> str:
        """Re-sign a pending free agent (offseason) or extend a player in his final year.

        Players accept their asking salary; extensions may exceed the cap.
        """
        player = self.players.get(player_id)
        if player is None or player_id not in team.all_players:
            raise contracts_error(f"Player #{player_id} is not in your organisation.")
        if not 1 <= years <= contracts.max_years(player):
            raise contracts_error(f"{player.name} will sign for 1-{contracts.max_years(player)} years.")
        arbitration = contracts.arbitration_eligible(player) and years == 1
        if morale.refuses_extension(player) and not arbitration:
            raise contracts_error(
                f"{player.name} is unhappy ({morale.label(player.morale).lower()}) and refuses to negotiate."
            )
        salary, _ = self.resign_ask(player, years)
        minimum = finance.min_salary(self)
        if player.contract_years <= 0:
            if self.phase != PHASE_RESIGN:
                raise contracts_error("Pending free agents can only be re-signed in the re-signing period.")
            kind = finance.KIND_ARBITRATION if arbitration else finance.KIND_RESIGN
            added = contracts.first_year_salary(salary, years)
            check = self.contract_check(team, kind, added, salary)
            contracts.sign(player, team.abbr, salary, years, minimum)
            verb = "agreed to an arbitration contract with" if arbitration else "re-signed"
        elif player.contract_years == 1:
            added = contracts.first_year_salary(salary, years + 1) - player.salary
            check = self.contract_check(team, finance.KIND_EXTENSION, added, salary)
            contracts.sign(player, team.abbr, salary, years + 1, minimum)
            verb = "extended"
        else:
            raise contracts_error(
                f"{player.name} has {player.contract_years} years left; only final-year players can be extended."
            )
        note = f"{team.abbr} {verb} {player.name} ({years} yr, {contracts.money(salary)}/yr)."
        self.log_transaction(note)
        if team.abbr == self.user_team:
            for warning in check.warnings:
                self.add_news(warning)
        return note

    def release(self, team: Team, player_id: int) -> str:
        note = frontoffice.release_player(self, team, player_id)
        if team.abbr == self.user_team:
            for extra in frontoffice.ensure_active_roster(self, team):
                self.add_news(extra)
        return note

    def advance_offseason(self, auto_user: bool = False) -> list[str]:
        """Move to the next offseason phase, running CPU decisions on the way."""
        if self.phase == PHASE_RESIGN:
            news = self._close_resign_period(auto_user)
        elif self.phase == PHASE_DRAFT:
            news = self._close_draft()
        elif self.phase == PHASE_FREE_AGENCY:
            news = self._close_free_agency()
        else:
            raise RuntimeError("Not in the offseason.")
        for item in news:
            self.add_news(item)
        return news

    def _close_resign_period(self, auto_user: bool) -> list[str]:
        news: list[str] = []
        for team in sorted(self.teams.values(), key=lambda t: t.abbr):
            if team.abbr == self.user_team and not auto_user:
                continue
            for player in sorted(self.pending_free_agents(team), key=lambda p: -p.overall):
                salary, years = self.resign_ask(player)
                arbitration = contracts.arbitration_eligible(player) and years == 1
                wanted = player.overall >= 55 or (player.age <= 26 and player.potential >= 62)
                if arbitration:
                    wanted = wanted or (player.overall >= 50 and salary <= finance.min_salary(self) * 3)
                kind = finance.KIND_ARBITRATION if arbitration else finance.KIND_RESIGN
                added = contracts.first_year_salary(salary, years)
                if not wanted or player.age > 36 or (morale.refuses_extension(player) and not arbitration):
                    continue
                if finance.cpu_can_add(self, team, kind, added, salary=salary):
                    contracts.sign(player, team.abbr, salary, years, finance.min_salary(self))
                    verb = "tendered" if arbitration else "re-signed"
                    self.log_transaction(f"{team.abbr} {verb} {player.name} ({years} yr, {contracts.money(salary)}/yr).")
        for team in self.teams.values():
            for player in self.pending_free_agents(team):
                if team.abbr == self.user_team or player.overall >= 62:
                    news.append(f"{player.name} ({team.abbr}, {player.overall} OVR) becomes a free agent.")
                self._remove_from_org(team, player)
                self.log_transaction(f"{player.name} left {team.abbr} as a free agent.")
        for team in self.teams.values():
            frontoffice.ensure_active_roster(self, team)

        factory = PlayerFactory(self._rng("draft-class"), start_id=self.next_player_id)
        prospects = draft_mod.generate_class(self._rng("draft-class-ratings"), factory)
        self.next_player_id = factory.next_id
        for prospect in prospects:
            self.players[prospect.id] = prospect
        penalised = self.finance.state.draft_penalties.pop(self.draft_year, [])
        self.draft = Draft(self.draft_year, list(self.pending_draft_order), [p.id for p in prospects],
                           first_round=finance.penalised_first_round(list(self.pending_draft_order), penalised))
        moved = [a for a in penalised if self.draft.first_round and
                 self.draft.first_round.index(a) > self.draft.order.index(a)]
        if moved:
            news.append("CBT draft penalty: " + ", ".join(moved) + " first-round pick(s) move back 10 places.")
        self.phase = PHASE_DRAFT
        mine = [i + 1 for i, abbr in enumerate(self.draft.first_round or self.draft.order)
                if self.draft_owner(1, abbr) == self.user_team]
        news.append(
            f"The {self.draft_year} draft class is ready ({len(prospects)} prospects). "
            + (f"Your first-round pick: #{mine[0]}." if mine else "You have no first-round pick.")
        )
        return news

    def _remove_from_org(self, team: Team, player: Player) -> None:
        self.note_departure(player)
        if player.id in team.all_players:
            team.remove_player(player.id)
        player.team = None
        player.contract_years = 0

    # -- draft -------------------------------------------------------------------------

    def draft_owner(self, rnd: int, original: str) -> str:
        assert self.draft is not None
        pick = draft_mod.find_pick(self.draft_picks, self.draft.year, rnd, original)
        return pick.owner if pick else original

    def draft_on_clock(self) -> tuple[int, int, str] | None:
        """``(overall pick number, round, owner)`` of the current selection."""
        if self.draft is None or self.phase != PHASE_DRAFT or self.draft.is_complete:
            return None
        rnd, original = self.draft.current_slot()  # type: ignore[misc]
        return len(self.draft.selections) + 1, rnd, self.draft_owner(rnd, original)

    def draft_select(self, player_id: int) -> DraftSelection:
        clock = self.draft_on_clock()
        if clock is None:
            raise RuntimeError("No draft is in progress.")
        assert self.draft is not None
        if player_id not in self.draft.pool:
            raise ValueError(f"Player #{player_id} is not available in the draft.")
        number, rnd, owner = clock
        _, original = self.draft.current_slot()  # type: ignore[misc]
        player = self.players[player_id]
        self.draft.pool.remove(player_id)
        minimum = finance.min_salary(self)
        rookie = contracts.rookie_salary(rnd, minimum)
        check = finance.check_transaction(self, self.teams[owner], finance.KIND_DRAFT, rookie, salary=rookie)
        contracts.sign(player, owner, rookie, contracts.ROOKIE_CONTRACT_YEARS, minimum, structure=False)
        if owner == self.user_team:
            for warning in check.warnings:
                self.add_news(warning)
        player.draft_year, player.draft_round, player.draft_pick = self.draft.year, rnd, number
        self.teams[owner].reserves.append(player_id)
        pick = draft_mod.find_pick(self.draft_picks, self.draft.year, rnd, original)
        if pick is not None:
            self.draft_picks.remove(pick)
        selection = DraftSelection(number, rnd, owner, original, player_id)
        self.draft.selections.append(selection)
        self.log_transaction(
            f"Draft #{number} (R{rnd}): {owner} selects {player.name}, {player.position}, age {player.age}."
        )
        return selection

    def draft_cpu_pick(self) -> DraftSelection:
        clock = self.draft_on_clock()
        assert clock is not None and self.draft is not None
        team = self.teams[clock[2]]
        return self.draft_select(draft_mod.cpu_choose(self.draft, team, self.players, self.seed))

    def draft_until_user(self) -> list[DraftSelection]:
        """CPU clubs pick until the user is on the clock (or the draft ends)."""
        made = []
        while (clock := self.draft_on_clock()) is not None and clock[2] != self.user_team:
            made.append(self.draft_cpu_pick())
        return made

    def draft_auto_rest(self) -> list[DraftSelection]:
        """Finish the draft; the user's picks use the scouting department's board."""
        made = []
        while self.draft_on_clock() is not None:
            made.append(self.draft_cpu_pick())
        return made

    def _close_draft(self) -> list[str]:
        assert self.draft is not None
        self.draft_auto_rest()
        mine = [s for s in self.draft.selections if s.team == self.user_team]
        for leftover in list(self.draft.pool):
            self.players.pop(leftover, None)  # undrafted prospects return to school
        self.draft.pool = []
        self.draft_picks = [p for p in self.draft_picks if p.year != self.draft.year]
        news = [f"The {self.draft.year} draft is complete. Your class:"]
        for sel in mine:
            p = self.players[sel.player_id]
            news.append(f"  #{sel.overall_pick} (R{sel.round}) {p.name}, {p.position}, age {p.age}")
        self.phase = PHASE_FREE_AGENCY
        rng = self._rng("offseason-trades")
        trades.cpu_trade_round(self, rng, attempts=4)
        for _ in range(2):
            news.extend(self._add_cpu_offer(rng))
        fa_count = len(freeagency.free_agents(self))
        news.append(f"Free agency is open: {fa_count} players available (Front Office > Free Agency).")
        return news

    def _close_free_agency(self) -> list[str]:
        news = freeagency.cpu_free_agency(self)
        news = [n for n in news if self.user_team in n][:5]
        for team in sorted(self.teams.values(), key=lambda t: t.abbr):
            if team.abbr == self.user_team:
                cut = frontoffice.cutdown(self, team)
                news.extend(f"Roster limit: {note}" for note in cut)
                news.extend(frontoffice.fill_minimums(self, team))
                news.extend(frontoffice.ensure_active_roster(self, team))
            else:
                frontoffice.fill_minimums(self, team)
                frontoffice.optimize_roster(self, team)
                frontoffice.cutdown(self, team)
        self._trim_free_agent_pool()
        self._start_new_season()
        news.append(f"The {self.year} season begins. Good luck!")
        return news

    def _trim_free_agent_pool(self) -> None:
        rng = self._rng("fa-retirement")
        pool = freeagency.free_agents(self)
        for index, player in enumerate(pool):
            if index >= FREE_AGENT_POOL_LIMIT or development.should_retire(player, rng):
                self._remove_player(player.id)

    def _start_new_season(self) -> None:
        self.year += 1
        for player in self.players.values():
            player.fatigue = 0
            player.reset_season_stats()
        for team in self.teams.values():
            team.rotation_index = 0
        existing = {p.year for p in self.draft_picks}
        for year in range(self.draft_year, self.draft_year + draft_mod.FUTURE_DRAFT_YEARS):
            if year not in existing:
                self.draft_picks.extend(draft_mod.create_picks(list(self.teams), year))
        self.schedule = generate_schedule(f"{self.seed}:{self.year}")
        self._by_day = None
        self.day = 0
        self.phase = PHASE_REGULAR
        self.playoffs = None
        self.last_user_game = None
        self.trade_offers = []
        self.milestones = []
        self.milestone_keys = []
        for note in finance.start_season(self):
            self.add_news(note)
        frontoffice.update_strategies(self)

    # -- injuries ----------------------------------------------------------------------

    def place_on_il(self, team: Team, player_id: int) -> list[str]:
        notes = injuries.place_on_il(team, self.players, player_id)
        player = self.players[player_id]
        message = f"{team.abbr} placed {player.name} on the injured list ({player.injury})."
        self.log_transaction(message)
        notes.append(message)
        notes.extend(frontoffice.ensure_active_roster(self, team))
        return notes

    def activate_from_il(self, team: Team, player_id: int) -> str:
        message = injuries.activate_from_il(team, self.players, player_id)
        self.log_transaction(f"{team.abbr}: {message}")
        roster.repair(team, self.players)
        return message

    def start_rehab(self, team: Team, player_id: int) -> str:
        message = injuries.start_rehab(team, self.players, player_id)
        self.log_transaction(f"{team.abbr}: {message}")
        return message

    def injured_players(self, team: Team) -> list[Player]:
        """Injured players, IL players, rehab assignments and rusty active players."""
        return sorted(
            (self.players[pid] for pid in team.all_players
             if self.players[pid].is_injured or pid in team.injured_list or self.players[pid].rehab
             or (self.players[pid].rust_days and pid in team.active)),
            key=lambda p: (-p.injury_days, p.id),
        )

    # -- trades ------------------------------------------------------------------------

    def offer_by_id(self, offer_id: int) -> TradeOffer | None:
        return next((o for o in self.trade_offers if o.offer_id == offer_id), None)

    def accept_offer(self, offer_id: int) -> list[str]:
        offer = self.offer_by_id(offer_id)
        if offer is None:
            raise trades.TradeError(f"No trade offer #{offer_id}.")
        self.trade_offers.remove(offer)
        cpu = offer.proposal.team_a
        errors = trades.validate(self, offer.proposal)
        if errors:
            raise trades.TradeError("Offer is no longer valid: " + "; ".join(errors))
        if not trades.evaluate(self, offer.proposal, evaluator=cpu).accepted:
            raise trades.TradeError(f"{cpu} has withdrawn the offer.")
        return trades.execute(self, offer.proposal)

    def reject_offer(self, offer_id: int) -> None:
        offer = self.offer_by_id(offer_id)
        if offer is not None:
            self.trade_offers.remove(offer)

    # ------------------------------------------------------------------ queries

    def team_games(self, abbr: str) -> list[ScheduledGame]:
        return [g for g in self.schedule if g.involves(abbr)]

    def batting_leaders(self, stat: str, limit: int = 10) -> list[tuple[Player, float]]:
        games = max((r.games for r in self.records().values()), default=0)
        min_pa = 3.1 * games
        rows = [
            p for p in self.players.values() if not p.is_pitcher and p.batting.pa > 0
        ]
        rate_stats = {"avg", "obp", "slg", "ops"}
        if stat in rate_stats:
            rows = [p for p in rows if p.batting.pa >= min_pa]
        value: Callable[[Player], float] = lambda p: float(getattr(p.batting, stat))  # noqa: E731
        rows.sort(key=lambda p: (-value(p), p.id))
        return [(p, value(p)) for p in rows[:limit]]

    def pitching_leaders(self, stat: str, limit: int = 10) -> list[tuple[Player, float]]:
        games = max((r.games for r in self.records().values()), default=0)
        min_outs = 3 * games
        rows = [p for p in self.players.values() if p.pitching.g > 0]
        ascending = stat in {"era", "whip"}
        if ascending:
            rows = [p for p in rows if p.pitching.outs >= max(1, min_outs)]
        value: Callable[[Player], float] = lambda p: float(getattr(p.pitching, stat))  # noqa: E731
        rows.sort(key=lambda p: ((value(p) if ascending else -value(p)), p.id))
        return [(p, value(p)) for p in rows[:limit]]

    # ------------------------------------------------------------------ serialisation

    def to_dict(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "year": self.year,
            "user_team": self.user_team,
            "day": self.day,
            "phase": self.phase,
            "next_player_id": self.next_player_id,
            "teams": [t.to_dict() for t in self.teams.values()],
            "players": [p.to_dict() for p in self.players.values()],
            "schedule": [g.to_dict() for g in self.schedule],
            "playoffs": self.playoffs.to_dict() if self.playoffs else None,
            "news": list(self.news),
            "history": list(self.history),
            "last_user_game": self.last_user_game.to_dict() if self.last_user_game else None,
            "finance": self.finance.to_dict(),
            "draft_picks": [p.to_dict() for p in self.draft_picks],
            "draft": self.draft.to_dict() if self.draft else None,
            "pending_draft_order": list(self.pending_draft_order),
            "trade_offers": [o.to_dict() for o in self.trade_offers],
            "next_offer_id": self.next_offer_id,
            "transactions": list(self.transactions),
            "milestones": list(self.milestones),
            "milestone_keys": list(self.milestone_keys),
            "awards_history": list(self.awards_history),
            "franchise_log": list(self.franchise_log),
            "retired": [r.to_dict() for r in self.retired.values()],
            "postseason_awards": list(self.postseason_awards),
            "career_milestones": list(self.career_milestones),
            "trade_memory": list(self.trade_memory),
            "trade_log": list(self.trade_log),
            "live_game": self.live_game.to_dict() if self.live_game else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> League:
        teams = {t["abbr"]: Team.from_dict(t) for t in data["teams"]}
        players = {int(p["id"]): Player.from_dict(p) for p in data["players"]}
        league = cls(
            seed=int(data["seed"]),
            year=int(data["year"]),
            user_team=data["user_team"],
            teams=teams,
            players=players,
            schedule=[ScheduledGame.from_dict(g) for g in data["schedule"]],
            next_player_id=int(data["next_player_id"]),
            day=int(data["day"]),
            phase=data["phase"],
            playoffs=Playoffs.from_dict(data["playoffs"]) if data.get("playoffs") else None,
            news=list(data.get("news", [])),
            history=list(data.get("history", [])),
            last_user_game=(
                GameResult.from_dict(data["last_user_game"]) if data.get("last_user_game") else None
            ),
            finance=(finance.LeagueFinance.from_dict(data["finance"]) if data.get("finance")
                     else finance.legacy_finance(data)),
            draft_picks=[DraftPick.from_dict(p) for p in data.get("draft_picks", [])],
            draft=Draft.from_dict(data["draft"]) if data.get("draft") else None,
            pending_draft_order=list(data.get("pending_draft_order", [])),
            trade_offers=[TradeOffer.from_dict(o) for o in data.get("trade_offers", [])],
            next_offer_id=int(data.get("next_offer_id", 1)),
            transactions=list(data.get("transactions", [])),
            milestones=list(data.get("milestones", [])),
            milestone_keys=list(data.get("milestone_keys", [])),
            awards_history=list(data.get("awards_history", [])),
            franchise_log=list(data.get("franchise_log", [])),
            retired={int(r["id"]): RetiredPlayer.from_dict(r) for r in data.get("retired", [])},
            postseason_awards=list(data.get("postseason_awards", [])),
            career_milestones=list(data.get("career_milestones", [])),
            trade_memory=list(data.get("trade_memory", [])),
            trade_log=list(data.get("trade_log", [])),
        )
        live = data.get("live_game")
        if live:
            sides = live["sim"]
            away, home = Team.from_dict(sides["away"]["team"]), Team.from_dict(sides["home"]["team"])
            league.live_game = LiveGame.from_dict(live, league.game_players(away, home))
        return league


def contracts_error(message: str) -> ValueError:
    return ContractError(message)


class ContractError(ValueError):
    """Raised for an invalid contract action."""
