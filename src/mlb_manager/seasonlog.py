"""Season milestones: statistical thresholds, no-hitters, clinches and streaks."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from mlb_manager.game import GameResult
from mlb_manager.models import BattingStats, PitchingStats, Player
from mlb_manager.standings import division_standings

if TYPE_CHECKING:
    from mlb_manager.league import League

GAMES_PER_SEASON = 162
BATTING_MILESTONES: dict[str, tuple[int, ...]] = {
    "hr": (30, 40, 50, 60),
    "rbi": (100, 125, 150),
    "h": (200,),
    "sb": (40, 60),
}
PITCHING_MILESTONES: dict[str, tuple[int, ...]] = {
    "w": (15, 20),
    "so": (200, 250, 300),
    "sv": (40, 50),
}
LABELS: dict[str, str] = {
    "hr": "home runs", "rbi": "RBI", "h": "hits", "sb": "stolen bases",
    "w": "wins", "so": "strikeouts", "sv": "saves",
}
WIN_STREAK = 10


def _crossed(after: int, gained: int, thresholds: tuple[int, ...]) -> list[int]:
    before = after - gained
    return [t for t in thresholds if before < t <= after]


def stat_milestones(player: Player, batting: BattingStats | None, pitching: PitchingStats | None) -> list[str]:
    """Thresholds crossed by today's line (call *after* adding it to season stats)."""
    out: list[str] = []
    if batting is not None:
        for stat, marks in BATTING_MILESTONES.items():
            for mark in _crossed(getattr(player.batting, stat), getattr(batting, stat), marks):
                out.append(f"{player.name} ({player.team}) reaches {mark} {LABELS[stat]}.")
    if pitching is not None:
        for stat, marks in PITCHING_MILESTONES.items():
            for mark in _crossed(getattr(player.pitching, stat), getattr(pitching, stat), marks):
                out.append(f"{player.name} ({player.team}) reaches {mark} {LABELS[stat]}.")
    return out


def no_hitters(result: GameResult, players: Mapping[int, Player]) -> list[str]:
    out = []
    for batting_box, pitching_box in ((result.away_box, result.home_box), (result.home_box, result.away_box)):
        if batting_box.hits == 0:
            pitchers = list(pitching_box.pitching)
            listed = [players[p].name for p in pitchers if p in players]
            names = listed[0] if len(listed) == 1 else ", ".join(listed[:-1]) + " and " + listed[-1]
            kind = "a no-hitter" if len(pitchers) == 1 else "a combined no-hitter"
            out.append(f"{names} ({pitching_box.abbr}) {'throws' if len(pitchers) == 1 else 'throw'} "
                       f"{kind} against {batting_box.abbr}!")
    return out


def standings_milestones(league: League) -> list[tuple[str, str]]:
    """``(key, message)`` for division clinches, eliminations and long streaks."""
    records = league.records()
    out: list[tuple[str, str]] = []
    for lg in ("AL", "NL"):
        for div in ("East", "Central", "West"):
            order = division_standings(league.teams, records, lg, div)
            leader = records[order[0]]
            rivals_best = max(
                records[a].wins + (GAMES_PER_SEASON - records[a].games) for a in order[1:]
            )
            if leader.wins > rivals_best:
                out.append((f"clinch:{order[0]}",
                            f"{league.teams[order[0]].full_name} clinch the {lg} {div} title!"))
            user = league.user_team
            if user in order[1:]:
                rec = records[user]
                if rec.wins + (GAMES_PER_SEASON - rec.games) < leader.wins:
                    out.append((f"eliminated:{user}",
                                f"{user} have been eliminated from the {lg} {div} race."))
    for abbr, rec in records.items():
        streak = rec.streak
        if streak == f"W{WIN_STREAK}":
            out.append((f"streak:{abbr}:{rec.games}",
                        f"{league.teams[abbr].full_name} have won {WIN_STREAK} straight!"))
    return out
