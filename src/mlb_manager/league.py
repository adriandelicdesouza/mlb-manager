"""The ``League`` aggregate: season state, day advancement and queries.

All randomness is derived from ``League.seed`` with stable string seeds, so a
league replays identically given the same manager decisions, including across
save/load round trips.
"""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from mlb_manager import roster
from mlb_manager.franchises import LEAGUES, get_franchise
from mlb_manager.game import GameResult, simulate_game
from mlb_manager.generation import generate_rosters
from mlb_manager.models import Player, Team
from mlb_manager.playoffs import Playoffs, Series, SeriesGame
from mlb_manager.ratings import clamp_rating
from mlb_manager.schedule import ScheduledGame, generate_schedule
from mlb_manager.standings import TeamRecord, compute_records, playoff_seeds, rank

PHASE_REGULAR = "regular"
PHASE_POSTSEASON = "postseason"
PHASE_COMPLETE = "complete"

DEFAULT_YEAR = 2026
DAILY_RECOVERY = 25
STARTER_READY_FATIGUE = 20
NEWS_LIMIT = 60


@dataclass(slots=True)
class PlayedGame:
    """A game played on a given day, with a human label (e.g. series name)."""

    label: str
    result: GameResult


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
    _by_day: dict[int, list[ScheduledGame]] | None = field(default=None, repr=False)

    # ------------------------------------------------------------------ creation

    @classmethod
    def create(cls, seed: int, user_team: str, year: int = DEFAULT_YEAR) -> League:
        user_team = get_franchise(user_team).abbr
        teams, players, next_id = generate_rosters(seed)
        league = cls(
            seed=seed,
            year=year,
            user_team=user_team,
            teams=teams,
            players=players,
            schedule=generate_schedule(f"{seed}:{year}"),
            next_player_id=next_id,
        )
        league.add_news(f"Welcome to the {year} season, {teams[user_team].full_name}!")
        return league

    # ------------------------------------------------------------------ helpers

    def _rng(self, *parts: object) -> random.Random:
        return random.Random(":".join(str(p) for p in (self.seed, self.year, *parts)))

    def add_news(self, message: str) -> None:
        self.news.append(message)
        if len(self.news) > NEWS_LIMIT:
            del self.news[: len(self.news) - NEWS_LIMIT]

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
            if self.players[pid].fatigue <= STARTER_READY_FATIGUE:
                team.rotation_index = (index + 1) % size
                return pid
        pid = min(team.rotation, key=lambda p: (self.players[p].fatigue, p))
        team.rotation_index = (team.rotation.index(pid) + 1) % size
        return pid

    def _prepare(self, team: Team) -> None:
        notes = roster.repair(team, self.players)
        if team.abbr == self.user_team:
            for note in notes:
                self.add_news(note)

    def play_game(self, away: str, home: str, key: str, record_stats: bool) -> GameResult:
        away_team, home_team = self.teams[away], self.teams[home]
        self._prepare(away_team)
        self._prepare(home_team)
        result = simulate_game(
            away_team,
            home_team,
            self.players,
            self._rng("game", key),
            self.choose_starter(away_team),
            self.choose_starter(home_team),
        )
        self._apply_result(result, record_stats)
        if self.user_team in (away, home):
            self.last_user_game = result
        return result

    def _apply_result(self, result: GameResult, record_stats: bool) -> None:
        for box in (result.away_box, result.home_box):
            for pid, line in box.pitching.items():
                self.players[pid].fatigue += line.pitches
                if record_stats:
                    self.players[pid].pitching.add(line)
            if record_stats:
                for pid, line in box.batting.items():
                    self.players[pid].batting.add(line)

    def _recover(self) -> None:
        for player in self.players.values():
            if player.fatigue:
                player.fatigue = max(0, player.fatigue - DAILY_RECOVERY)

    # ------------------------------------------------------------------ advancing

    def advance_day(self) -> DayReport:
        """Play every game scheduled for the next day."""
        if self.phase == PHASE_REGULAR:
            report = self._advance_regular_day()
        elif self.phase == PHASE_POSTSEASON:
            report = self._advance_postseason_day()
        else:
            return DayReport(self.day, self.phase)
        self._recover()
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
        self.day += 1
        if self.day > self.last_regular_day:
            report.news.extend(self._start_postseason())
        return report

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
        return news

    def _advance_postseason_day(self) -> DayReport:
        assert self.playoffs is not None
        report = DayReport(self.day, PHASE_POSTSEASON)
        for series in self.playoffs.active_series():
            away, home = series.next_matchup()
            number = len(series.games) + 1
            result = self.play_game(
                away, home, f"post:{series.series_id}:{number}", record_stats=False
            )
            series.record(
                SeriesGame(away, home, result.away_runs, result.home_runs, result.innings)
            )
            report.games.append(PlayedGame(f"{series.round_name} G{number}", result))
            if series.is_over:
                report.news.append(f"{series.round_name}: {series.summary()}")
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

    def _finish_season(self) -> list[str]:
        assert self.playoffs is not None
        self.phase = PHASE_COMPLETE
        ws = self.playoffs.current_round[0]
        champion = ws.winner
        assert champion is not None
        rec = self.records()[self.user_team]
        self.history.append(
            {
                "year": self.year,
                "champion": champion,
                "runner_up": ws.loser,
                "user_wins": rec.wins,
                "user_losses": rec.losses,
                "user_result": self.postseason_finish(self.user_team),
            }
        )
        return [f"The {self.teams[champion].full_name} win the {self.year} World Series!"]

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
        if not self.is_complete:
            raise RuntimeError("The current season is not complete yet.")
        self.year += 1
        rng = self._rng("development")
        for player in sorted(self.players.values(), key=lambda p: p.id):
            player.age += 1
            player.fatigue = 0
            player.reset_season_stats()
            change = (27 - player.age) * 0.6 + rng.gauss(0, 2)
            change = max(-6.0, min(6.0, change))
            for attr in player.ratings:
                if attr == "stamina":
                    continue
                player.ratings[attr] = clamp_rating(player.ratings[attr] + change + rng.gauss(0, 1))
        for team in self.teams.values():
            team.rotation_index = 0
            if team.abbr != self.user_team:
                roster.auto_configure(team, self.players)
            else:
                roster.repair(team, self.players)
        self.schedule = generate_schedule(f"{self.seed}:{self.year}")
        self._by_day = None
        self.day = 0
        self.phase = PHASE_REGULAR
        self.playoffs = None
        self.last_user_game = None
        self.add_news(f"The {self.year} season begins. Players have aged and developed.")

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
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> League:
        teams = {t["abbr"]: Team.from_dict(t) for t in data["teams"]}
        players = {int(p["id"]): Player.from_dict(p) for p in data["players"]}
        return cls(
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
        )
