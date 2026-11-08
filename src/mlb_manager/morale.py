"""Lightweight, deterministic player morale.

Morale is 0-100 (50 = neutral). Once a week during the regular season it moves
part of the way toward a *target* built from a handful of transparent factors:

* playing time versus what the player's ability says he deserves,
* his role (stuck in the minors, regular, closer, ...),
* contract satisfaction (salary versus market value),
* team performance (winning percentage),
* the current winning or losing streak.

There is no randomness and no personality simulation. Effects are modest:

* ``performance_modifier`` shifts ratings by -2..+1 points in games;
* ``contract_multiplier`` changes re-signing asks by up to about +/-10 %;
* very unhappy players refuse extensions (``refuses_extension``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mlb_manager import contracts
from mlb_manager.models import Player, Team

if TYPE_CHECKING:
    from mlb_manager.league import League
    from mlb_manager.standings import TeamRecord

NEUTRAL = 50
MIN_MORALE, MAX_MORALE = 5, 95
WEEKLY_STEP = 0.35  # share of the gap to the target closed each week
OFFSEASON_RESET = 0.5  # share of the distance to neutral removed each winter
REFUSE_BELOW = 20
STREAK_LENGTH = 5

LABELS: tuple[tuple[int, str], ...] = ((85, "Ecstatic"), (65, "Happy"), (40, "Content"),
                                       (20, "Unhappy"), (0, "Angry"))


def label(value: int) -> str:
    for cutoff, text in LABELS:
        if value >= cutoff:
            return text
    return LABELS[-1][1]


def performance_modifier(player: Player) -> int:
    """Rating points added in games (modest by design)."""
    if player.morale >= 75:
        return 1
    if player.morale <= 12:
        return -2
    if player.morale <= 25:
        return -1
    return 0


def contract_multiplier(player: Player) -> float:
    """Multiplier on a re-signing ask: happy players give a discount."""
    return max(0.9, min(1.12, 1 + (NEUTRAL - player.morale) / 400))


def refuses_extension(player: Player) -> bool:
    return player.morale < REFUSE_BELOW


# ----------------------------------------------------------------------------- factors


def _role(team: Team, player: Player) -> str:
    if player.id in team.injured_list:
        return "injured"
    if player.id in team.reserves:
        return "rehab" if player.rehab else "minors"
    if player.is_pitcher:
        if player.id in team.rotation:
            return "rotation"
        return "closer" if team.closer == player.id else "bullpen"
    return "starter" if any(s.player_id == player.id for s in team.lineup) else "bench"


class _TeamContext:
    """Per-club numbers shared by all of its players' morale factors."""

    __slots__ = ("weakest_starter", "weakest_rotation", "median_active")

    def __init__(self, league: League, team: Team) -> None:
        players = league.players
        lineup = [players[s.player_id].overall for s in team.lineup if s.player_id in players]
        rotation = [players[pid].overall for pid in team.rotation if pid in players]
        self.weakest_starter = min(lineup) if lineup else None
        self.weakest_rotation = min(rotation) if rotation else None
        self.median_active: dict[bool, int] = {}
        for pitcher in (True, False):
            values = sorted(players[pid].overall for pid in team.active if players[pid].is_pitcher == pitcher)
            self.median_active[pitcher] = values[len(values) // 2] if values else 99


def _deserves_regular_role(ctx: _TeamContext, player: Player, overall: int) -> bool:
    """Is the player better than someone holding a regular job on his club?"""
    if player.is_pitcher:
        if player.position != "SP":
            return False
        weakest = ctx.weakest_rotation
    else:
        weakest = ctx.weakest_starter
    return weakest is not None and overall >= weakest + 3


def factors(league: League, player: Player, records: dict[str, TeamRecord] | None = None,
            ctx: _TeamContext | None = None) -> list[tuple[str, int]]:
    """``(reason, delta)`` pairs that make up the player's morale target."""
    if player.team is None or player.team not in league.teams:
        return []
    team = league.teams[player.team]
    records = records or league.records()
    ctx = ctx or _TeamContext(league, team)
    overall = player.overall
    rec = records[team.abbr]
    out: list[tuple[str, int]] = []
    role = _role(team, player)
    games = max(1, rec.games)

    # Playing time and role ---------------------------------------------------
    if role in ("starter", "rotation"):
        if player.is_pitcher:
            share = player.pitching.gs / max(1.0, games / 5)
        else:
            share = player.batting.pa / (games * 4.0)
        if rec.games >= 10 and share >= 0.6:
            out.append(("Regular playing time", 5))
        else:
            out.append(("Starting role", 3))
    elif role == "closer":
        out.append(("Closer role", 5))
    elif role in ("bench", "bullpen"):
        if _deserves_regular_role(ctx, player, overall):
            out.append(("Wants a bigger role", -8))
        elif player.age >= 30 and overall >= 60:
            out.append(("Veteran in a reserve role", -3))
    elif role == "minors":
        if overall >= ctx.median_active[player.is_pitcher]:
            out.append(("Wants a call-up to the majors", -10 if player.age >= 26 else -6))
        elif player.age >= 29:
            out.append(("Veteran stuck in the minors", -4))
    elif role in ("injured", "rehab"):
        out.append(("Frustrated by injury", -3))

    # Contract -----------------------------------------------------------------
    market = contracts.market_salary(player)
    rookie_scale = player.service_years < 3 or contracts.contract_status(player) == "Rookie deal"
    if not rookie_scale and market - player.salary >= 4 and player.salary < market * 0.6:
        out.append(("Feels underpaid", -6))
    elif player.salary >= max(3.0, market * 1.3):
        out.append(("Well paid", 3))
    if player.contract_years == 1 and player.age <= 32 and overall >= 60:
        out.append(("Contract year", -2))

    # Team performance ----------------------------------------------------------
    if rec.games >= 10:
        delta = round((rec.pct - 0.5) * 40)
        if delta:
            out.append(("Team winning" if delta > 0 else "Team losing", max(-8, min(8, delta))))
        streak = rec.streak
        if streak and len(streak) > 1 and int(streak[1:]) >= STREAK_LENGTH:
            out.append((f"{'Winning' if streak[0] == 'W' else 'Losing'} streak ({streak})",
                        3 if streak[0] == "W" else -3))
    return out


def target(league: League, player: Player, records: dict[str, TeamRecord] | None = None,
           ctx: _TeamContext | None = None) -> int:
    value = NEUTRAL + sum(delta for _, delta in factors(league, player, records, ctx))
    return max(MIN_MORALE, min(MAX_MORALE, value))


def weekly_update(league: League) -> list[Player]:
    """Move every rostered player toward his target; returns user players who turned unhappy."""
    records = league.records()
    newly_unhappy: list[Player] = []
    for team in league.teams.values():
        ctx = _TeamContext(league, team)
        for pid in team.all_players:
            player = league.players[pid]
            goal = target(league, player, records, ctx)
            before = player.morale
            step = (goal - player.morale) * WEEKLY_STEP
            player.morale = max(MIN_MORALE, min(MAX_MORALE, player.morale + round(step)))
            if team.abbr == league.user_team and before >= 30 > player.morale:
                newly_unhappy.append(player)
    return newly_unhappy


def offseason_reset(league: League, champion: str | None) -> None:
    for player in league.players.values():
        player.morale = round(NEUTRAL + (player.morale - NEUTRAL) * OFFSEASON_RESET)
        if champion is not None and player.team == champion:
            player.morale = min(MAX_MORALE, player.morale + 10)


def new_club(player: Player) -> None:
    """A change of scenery: morale starts near neutral with a new club."""
    player.morale = round(NEUTRAL + (player.morale - NEUTRAL) * 0.3)
