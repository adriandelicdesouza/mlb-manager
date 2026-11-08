"""Roster rules, automatic lineup/rotation building, validation and moves."""

from __future__ import annotations

from collections.abc import Mapping

from mlb_manager import ratings as rt
from mlb_manager.models import LineupSlot, Player, Team

MAX_ACTIVE = 26
MAX_ACTIVE_PITCHERS = 13
ROTATION_SIZE = 5

# Fill the scarcest defensive positions first when auto-building a lineup.
AUTO_FILL_ORDER: tuple[str, ...] = ("C", "SS", "CF", "2B", "3B", "RF", "LF", "1B")
# Batting-order slot (0-based) for the n-th best hitter.
BATTING_ORDER_BY_RANK: tuple[int, ...] = (2, 3, 1, 0, 4, 5, 6, 7, 8)

Players = Mapping[int, Player]


class RosterError(ValueError):
    """Raised when a requested roster change violates the rules."""


def active_players(team: Team, players: Players) -> list[Player]:
    return [players[pid] for pid in team.active]


def reserve_players(team: Team, players: Players) -> list[Player]:
    return [players[pid] for pid in team.reserves]


def active_hitters(team: Team, players: Players) -> list[Player]:
    return [p for p in active_players(team, players) if not p.is_pitcher]


def active_pitchers(team: Team, players: Players) -> list[Player]:
    return [p for p in active_players(team, players) if p.is_pitcher]


def bullpen(team: Team, players: Players) -> list[Player]:
    rotation = set(team.rotation)
    return [p for p in active_pitchers(team, players) if p.id not in rotation]


def bench(team: Team, players: Players) -> list[Player]:
    starters = {slot.player_id for slot in team.lineup}
    return [p for p in active_hitters(team, players) if p.id not in starters]


def position_value(player: Player, position: str) -> float:
    eligible = player.can_play(position)
    share = rt.DEFENSE_SHARE[position]
    value = rt.batting_rating(player.ratings) * (1 - share)
    value += rt.defense_rating(player.ratings, position, eligible) * share
    return value - (0 if eligible else 15)


def hitting_value(player: Player, use_performance: bool = False) -> float:
    """Batting rating, optionally blended with this season's OPS once meaningful."""
    base = rt.batting_rating(player.ratings)
    if use_performance and player.batting.pa >= 100:
        weight = min(0.4, player.batting.pa / 1000)
        perf = 50 + (player.batting.ops - 0.720) * 100
        return base * (1 - weight) + perf * weight
    return base


def auto_lineup(team: Team, players: Players, use_performance: bool = False) -> None:
    """Pick the best nine hitters for each position and order them.

    Injured players are only used if there are not enough healthy hitters.
    """
    everyone = active_hitters(team, players)
    healthy = [p for p in everyone if not p.is_injured]
    pool = healthy if len(healthy) >= len(rt.LINEUP_POSITIONS) else everyone
    if len(pool) < len(rt.LINEUP_POSITIONS):
        raise RosterError(f"{team.abbr} has only {len(pool)} active position players")
    chosen: dict[str, Player] = {}
    remaining = list(pool)
    for position in AUTO_FILL_ORDER:
        best = max(remaining, key=lambda p: (position_value(p, position), -p.id))
        chosen[position] = best
        remaining.remove(best)
    chosen["DH"] = max(remaining, key=lambda p: (hitting_value(p, use_performance), -p.id))

    ranked = sorted(
        chosen.items(), key=lambda kv: (-hitting_value(kv[1], use_performance), kv[1].id)
    )
    order: list[LineupSlot | None] = [None] * 9
    for rank, (position, player) in enumerate(ranked):
        order[BATTING_ORDER_BY_RANK[rank]] = LineupSlot(player.id, position)
    team.lineup = [slot for slot in order if slot is not None]


def auto_pitching(team: Team, players: Players, use_performance: bool = False) -> None:
    """Pick a five-man rotation and a closer from the active pitchers."""
    everyone = active_pitchers(team, players)
    healthy = [p for p in everyone if not p.is_injured]
    pitchers = healthy if len(healthy) >= ROTATION_SIZE + 1 else everyone
    if len(pitchers) < ROTATION_SIZE + 1:
        raise RosterError(f"{team.abbr} needs at least {ROTATION_SIZE + 1} active pitchers")

    def starter_score(p: Player) -> tuple[float, int]:
        bonus = 15 if p.position == "SP" else 0
        stamina_penalty = max(0, 45 - p.rating("stamina")) * 0.8
        perf = 0.0
        if use_performance and p.pitching.outs >= 90:
            perf = max(-8.0, min(8.0, (4.2 - p.pitching.era) * 3))
        return (rt.pitching_rating(p.ratings, "SP") + bonus - stamina_penalty + perf, -p.id)

    rotation = sorted(pitchers, key=starter_score, reverse=True)[:ROTATION_SIZE]
    team.rotation = [p.id for p in rotation]
    pen = [p for p in pitchers if p.id not in team.rotation]
    closer = max(pen, key=lambda p: (rt.pitching_rating(p.ratings, "RP"), -p.id))
    team.closer = closer.id
    team.rotation_index %= ROTATION_SIZE


def auto_configure(team: Team, players: Players, use_performance: bool = False) -> None:
    auto_lineup(team, players, use_performance)
    auto_pitching(team, players, use_performance)


def validate(team: Team, players: Players) -> list[str]:
    """Return human-readable rule violations (empty list means valid)."""
    problems: list[str] = []
    active = set(team.active)
    if len(team.active) > MAX_ACTIVE:
        problems.append(f"Active roster has {len(team.active)} players (max {MAX_ACTIVE}).")
    pitcher_count = len(active_pitchers(team, players))
    if pitcher_count > MAX_ACTIVE_PITCHERS:
        problems.append(
            f"Active roster has {pitcher_count} pitchers (max {MAX_ACTIVE_PITCHERS})."
        )
    if len(set(team.all_players)) != len(team.all_players):
        problems.append("A player is listed on more than one roster (active, reserves, IL).")

    if len(team.lineup) != len(rt.LINEUP_POSITIONS):
        problems.append(f"Lineup has {len(team.lineup)} batters (need 9).")
    ids = [slot.player_id for slot in team.lineup]
    if len(set(ids)) != len(ids):
        problems.append("A player appears twice in the lineup.")
    positions = sorted(slot.position for slot in team.lineup)
    if positions != sorted(rt.LINEUP_POSITIONS):
        problems.append("Lineup must cover C, 1B, 2B, 3B, SS, LF, CF, RF and DH exactly once.")
    for slot in team.lineup:
        if slot.player_id not in active:
            problems.append(f"Lineup player #{slot.player_id} is not on the active roster.")
        elif players[slot.player_id].is_pitcher:
            problems.append(f"{players[slot.player_id].name} is a pitcher and cannot bat.")

    if len(team.rotation) != ROTATION_SIZE:
        problems.append(f"Rotation has {len(team.rotation)} starters (need {ROTATION_SIZE}).")
    if len(set(team.rotation)) != len(team.rotation):
        problems.append("A pitcher appears twice in the rotation.")
    for pid in team.rotation:
        if pid not in active or not players[pid].is_pitcher:
            problems.append(f"Rotation member #{pid} is not an active pitcher.")
    if team.closer is not None and (
        team.closer not in active or team.closer in team.rotation or not players[team.closer].is_pitcher
    ):
        problems.append("Closer must be an active bullpen pitcher.")
    return problems


def repair(team: Team, players: Players) -> list[str]:
    """Fix an invalid lineup or staff automatically; return a list of notes."""
    notes: list[str] = []
    problems = validate(team, players)
    lineup_bad = any(
        "Lineup" in p or "lineup" in p or "cannot bat" in p for p in problems
    )
    staff_bad = any("Rotation" in p or "rotation" in p or "Closer" in p for p in problems)
    if lineup_bad:
        auto_lineup(team, players)
        notes.append(f"{team.abbr}: lineup was invalid and has been rebuilt automatically.")
    if staff_bad:
        auto_pitching(team, players)
        notes.append(f"{team.abbr}: pitching staff was invalid and has been rebuilt automatically.")
    return notes


# --------------------------------------------------------------------------- moves


def swap_active_reserve(team: Team, players: Players, active_id: int, reserve_id: int) -> list[str]:
    """Option ``active_id`` to the reserves and promote ``reserve_id``.

    Keeps the active roster at its current size. Returns notes about any
    automatic lineup/staff adjustments that were needed.
    """
    if active_id not in team.active:
        raise RosterError(f"Player #{active_id} is not on the active roster.")
    if reserve_id not in team.reserves:
        raise RosterError(f"Player #{reserve_id} is not in the reserves.")
    outgoing, incoming = players[active_id], players[reserve_id]
    pitchers_after = len(active_pitchers(team, players)) - outgoing.is_pitcher + incoming.is_pitcher
    if pitchers_after > MAX_ACTIVE_PITCHERS:
        raise RosterError(
            f"Move would leave {pitchers_after} active pitchers (max {MAX_ACTIVE_PITCHERS})."
        )
    hitters_after = len(active_hitters(team, players)) - (not outgoing.is_pitcher) + (
        not incoming.is_pitcher
    )
    if hitters_after < len(rt.LINEUP_POSITIONS):
        raise RosterError("Move would leave fewer than 9 active position players.")

    team.active[team.active.index(active_id)] = reserve_id
    team.reserves[team.reserves.index(reserve_id)] = active_id
    return replace_departed(team, players, active_id)


def replace_departed(team: Team, players: Players, departed_id: int) -> list[str]:
    """Fill lineup/rotation/closer holes left by a player leaving the active roster."""
    outgoing = players[departed_id]
    active_id = departed_id
    notes: list[str] = []
    for index, slot in enumerate(team.lineup):
        if slot.player_id == active_id:
            replacement = _best_bench_for(team, players, slot.position)
            team.lineup[index] = LineupSlot(replacement.id, slot.position)
            notes.append(f"{replacement.name} replaces {outgoing.name} at {slot.position}.")
    if active_id in team.rotation:
        replacement = _best_bullpen_starter(team, players)
        team.rotation[team.rotation.index(active_id)] = replacement.id
        notes.append(f"{replacement.name} replaces {outgoing.name} in the rotation.")
    if team.closer == active_id:
        pen = bullpen(team, players)
        team.closer = max(pen, key=lambda p: rt.pitching_rating(p.ratings, "RP")).id
        notes.append(f"{players[team.closer].name} is the new closer.")
    return notes


def _best_bench_for(team: Team, players: Players, position: str) -> Player:
    candidates = bench(team, players)
    if not candidates:
        raise RosterError("No bench player available.")
    return max(candidates, key=lambda p: (position_value(p, position), -p.id))


def _best_bullpen_starter(team: Team, players: Players) -> Player:
    pen = [p for p in bullpen(team, players) if p.id != team.closer] or bullpen(team, players)
    return max(pen, key=lambda p: (rt.pitching_rating(p.ratings, "SP"), -p.id))


def swap_batting_order(team: Team, first: int, second: int) -> None:
    """Swap two batting-order spots (0-based)."""
    for index in (first, second):
        if not 0 <= index < len(team.lineup):
            raise RosterError(f"Batting order spot {index + 1} does not exist.")
    team.lineup[first], team.lineup[second] = team.lineup[second], team.lineup[first]


def substitute_starter(team: Team, players: Players, order_index: int, bench_id: int) -> None:
    """Replace the batter in ``order_index`` with a bench player at the same position."""
    if not 0 <= order_index < len(team.lineup):
        raise RosterError(f"Batting order spot {order_index + 1} does not exist.")
    if bench_id not in {p.id for p in bench(team, players)}:
        raise RosterError(f"Player #{bench_id} is not an available bench player.")
    slot = team.lineup[order_index]
    team.lineup[order_index] = LineupSlot(bench_id, slot.position)


def swap_positions(team: Team, first: int, second: int) -> None:
    """Exchange the defensive positions of two batting-order spots."""
    for index in (first, second):
        if not 0 <= index < len(team.lineup):
            raise RosterError(f"Batting order spot {index + 1} does not exist.")
    a, b = team.lineup[first], team.lineup[second]
    team.lineup[first] = LineupSlot(a.player_id, b.position)
    team.lineup[second] = LineupSlot(b.player_id, a.position)


def set_rotation_spot(team: Team, players: Players, spot: int, pitcher_id: int) -> None:
    """Put an active pitcher into rotation ``spot`` (0-based), swapping if needed."""
    if not 0 <= spot < len(team.rotation):
        raise RosterError(f"Rotation spot {spot + 1} does not exist.")
    if pitcher_id not in team.active or not players[pitcher_id].is_pitcher:
        raise RosterError(f"Player #{pitcher_id} is not an active pitcher.")
    if pitcher_id in team.rotation:
        other = team.rotation.index(pitcher_id)
        team.rotation[spot], team.rotation[other] = team.rotation[other], team.rotation[spot]
        return
    team.rotation[spot] = pitcher_id
    if team.closer == pitcher_id:
        pen = bullpen(team, players)
        team.closer = max(pen, key=lambda p: rt.pitching_rating(p.ratings, "RP")).id


def set_closer(team: Team, players: Players, pitcher_id: int) -> None:
    if pitcher_id not in {p.id for p in bullpen(team, players)}:
        raise RosterError(f"Player #{pitcher_id} is not an active bullpen pitcher.")
    team.closer = pitcher_id


def promote(team: Team, players: Players, player_id: int) -> None:
    """Move a reserve to an open active-roster spot."""
    if player_id not in team.reserves:
        raise RosterError(f"Player #{player_id} is not in your minor league system.")
    if players[player_id].rehab:
        raise RosterError(f"{players[player_id].name} is on a rehab assignment until he is healthy.")
    if len(team.active) >= MAX_ACTIVE:
        raise RosterError(f"Active roster is full ({MAX_ACTIVE}). Demote or swap someone instead.")
    if players[player_id].is_pitcher and len(active_pitchers(team, players)) >= MAX_ACTIVE_PITCHERS:
        raise RosterError(f"Already {MAX_ACTIVE_PITCHERS} active pitchers.")
    team.reserves.remove(player_id)
    team.active.append(player_id)


def demote(team: Team, players: Players, player_id: int) -> list[str]:
    """Send an active player to the minors; lineup/staff holes are filled."""
    if player_id not in team.active:
        raise RosterError(f"Player #{player_id} is not on the active roster.")
    player = players[player_id]
    if player.is_pitcher and len(active_pitchers(team, players)) <= ROTATION_SIZE + 1:
        raise RosterError("Not enough active pitchers left to demote him.")
    if not player.is_pitcher and len(active_hitters(team, players)) <= len(rt.LINEUP_POSITIONS):
        raise RosterError("Not enough active position players left to demote him.")
    team.active.remove(player_id)
    team.reserves.append(player_id)
    return replace_departed(team, players, player_id)
