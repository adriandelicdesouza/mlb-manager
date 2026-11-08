"""Injuries, the injured list (IL) and game-day lineup substitutions.

* Injuries are rolled after every game for each participant, from a seeded
  RNG, so a league replays identically.
* Injured players never take the field: the game-day roster substitutes a
  healthy bench player without touching the manager's saved lineup, so a
  player returns to his spot automatically once healthy.
* Placing a player on the IL frees his active-roster spot for a call-up.
"""

from __future__ import annotations

import random
from collections.abc import Mapping
from dataclasses import replace

from mlb_manager import ratings as rt
from mlb_manager import roster
from mlb_manager.game import TeamBox
from mlb_manager.models import LineupSlot, Player, Team

IL_THRESHOLD_DAYS = 6  # CPU clubs IL anyone out longer than day-to-day
BATTER_RISK = 0.0040  # per game started
STARTER_RISK = 0.0110  # per start
RELIEVER_RISK = 0.0040  # per appearance

# (label, probability, min days, max days)
SEVERITIES: tuple[tuple[str, float, int, int], ...] = (
    ("day-to-day", 0.45, 1, 5),
    ("minor", 0.33, 8, 20),
    ("moderate", 0.16, 21, 50),
    ("severe", 0.06, 60, 130),
)
HITTER_INJURIES: dict[str, tuple[str, ...]] = {
    "day-to-day": ("bruised knee", "sore back", "jammed finger", "tight hamstring", "neck stiffness"),
    "minor": ("hamstring strain", "sprained ankle", "wrist inflammation", "groin strain"),
    "moderate": ("oblique strain", "broken finger", "quad strain", "knee sprain"),
    "severe": ("torn hamstring", "broken wrist", "shoulder surgery", "torn ACL"),
}
PITCHER_INJURIES: dict[str, tuple[str, ...]] = {
    "day-to-day": ("blister", "sore shoulder", "back spasms", "forearm tightness"),
    "minor": ("shoulder inflammation", "elbow soreness", "lat strain", "oblique tightness"),
    "moderate": ("rotator cuff strain", "forearm strain", "elbow inflammation"),
    "severe": ("torn UCL", "shoulder labrum tear", "flexor tendon tear"),
}


def is_healthy(player: Player) -> bool:
    return player.injury_days <= 0


def injury_risk(player: Player, starter_pitches: int | None = None, relief: bool = False) -> float:
    if starter_pitches is not None:
        risk = STARTER_RISK * (1 + max(0, starter_pitches - 100) * 0.02)
    elif relief:
        risk = RELIEVER_RISK
    else:
        risk = BATTER_RISK
    return risk * (1 + max(0, player.age - 30) * 0.05)


def roll_injury(player: Player, rng: random.Random) -> tuple[str, int]:
    """Pick a severity and an injury for ``player``; returns (description, days)."""
    roll = rng.random()
    acc = 0.0
    label, low, high = SEVERITIES[0][0], SEVERITIES[0][2], SEVERITIES[0][3]
    for name, prob, lo, hi in SEVERITIES:
        acc += prob
        if roll < acc:
            label, low, high = name, lo, hi
            break
    table = PITCHER_INJURIES if player.is_pitcher else HITTER_INJURIES
    return rng.choice(table[label]), rng.randint(low, high)


def injuries_from_game(
    boxes: tuple[TeamBox, TeamBox], players: Mapping[int, Player], rng: random.Random
) -> list[tuple[Player, str, int]]:
    """Roll injuries for everyone who appeared in a game."""
    results: list[tuple[Player, str, int]] = []
    for box in boxes:
        for pid, _ in box.batting_order:
            player = players[pid]
            if rng.random() < injury_risk(player):
                results.append((player, *roll_injury(player, rng)))
        for pid, line in box.pitching.items():
            player = players[pid]
            risk = injury_risk(player, starter_pitches=line.pitches) if line.gs else injury_risk(player, relief=True)
            if rng.random() < risk:
                results.append((player, *roll_injury(player, rng)))
    return results


def apply_injury(player: Player, description: str, days: int) -> None:
    if days > player.injury_days:
        player.injury = description
        player.injury_days = days


def heal_one_day(player: Player) -> bool:
    """Returns True when the player becomes healthy today."""
    if player.injury_days <= 0:
        return False
    player.injury_days -= 1
    if player.injury_days <= 0:
        player.injury_days = 0
        player.injury = None
        return True
    return False


def heal_fully(player: Player) -> None:
    player.injury_days = 0
    player.injury = None


# ----------------------------------------------------------------------------- IL moves


def place_on_il(team: Team, players: Mapping[int, Player], player_id: int) -> list[str]:
    """Move an injured player from the active roster or minors onto the IL."""
    player = players[player_id]
    if player_id not in team.all_players or player_id in team.injured_list:
        raise roster.RosterError(f"{player.name} cannot be placed on the injured list.")
    if not player.is_injured:
        raise roster.RosterError(f"{player.name} is healthy; only injured players go on the IL.")
    notes: list[str] = []
    was_active = player_id in team.active
    team.remove_player(player_id)
    team.injured_list.append(player_id)
    if was_active:
        notes.extend(replace_with_healthy(team, players, player_id))
    return notes


def activate_from_il(team: Team, players: Mapping[int, Player], player_id: int) -> str:
    """Return a healthy IL player to the active roster (or the minors if it is full)."""
    player = players[player_id]
    if player_id not in team.injured_list:
        raise roster.RosterError(f"{player.name} is not on the injured list.")
    if player.is_injured:
        raise roster.RosterError(f"{player.name} is still injured ({player.injury_days} days).")
    team.injured_list.remove(player_id)
    pitchers = len(roster.active_pitchers(team, players))
    if len(team.active) < roster.MAX_ACTIVE and not (
        player.is_pitcher and pitchers >= roster.MAX_ACTIVE_PITCHERS
    ):
        team.active.append(player_id)
        return f"{player.name} activated from the injured list."
    team.reserves.append(player_id)
    return f"{player.name} activated to the minor league roster (active roster is full)."


def replace_with_healthy(team: Team, players: Mapping[int, Player], departed_id: int) -> list[str]:
    """Fill lineup/rotation/closer holes left by a departed player, preferring healthy players."""
    notes: list[str] = []
    outgoing = players[departed_id]
    for index, slot in enumerate(team.lineup):
        if slot.player_id == departed_id:
            sub = best_healthy_bench(team, players, slot.position, exclude=set())
            if sub is not None:
                team.lineup[index] = LineupSlot(sub.id, slot.position)
                notes.append(f"{sub.name} replaces {outgoing.name} at {slot.position}.")
    if departed_id in team.rotation:
        pen = [p for p in roster.bullpen(team, players) if is_healthy(p) and p.id != team.closer]
        pen = pen or roster.bullpen(team, players)
        if pen:
            sub = max(pen, key=lambda p: (rt.pitching_rating(p.ratings, "SP"), -p.id))
            team.rotation[team.rotation.index(departed_id)] = sub.id
            notes.append(f"{sub.name} replaces {outgoing.name} in the rotation.")
    if team.closer == departed_id:
        pen = [p for p in roster.bullpen(team, players) if is_healthy(p)] or roster.bullpen(team, players)
        team.closer = max(pen, key=lambda p: rt.pitching_rating(p.ratings, "RP")).id if pen else None
        if team.closer is not None:
            notes.append(f"{players[team.closer].name} takes over as closer.")
    return notes


def best_healthy_bench(
    team: Team, players: Mapping[int, Player], position: str, exclude: set[int]
) -> Player | None:
    starters = {s.player_id for s in team.lineup}
    candidates = [
        p for p in roster.active_hitters(team, players)
        if p.id not in starters and p.id not in exclude and is_healthy(p)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: (roster.position_value(p, position), -p.id))


# ----------------------------------------------------------------------------- game day


def game_day_team(team: Team, players: Mapping[int, Player]) -> tuple[Team, list[str]]:
    """A copy of ``team`` with injured players swapped out for this game only."""
    if all(is_healthy(players[pid]) for pid in team.active):
        return team, []
    healthy_active = [pid for pid in team.active if is_healthy(players[pid])]
    lineup = list(team.lineup)
    used = {s.player_id for s in lineup if is_healthy(players[s.player_id])}
    notes: list[str] = []
    for index, slot in enumerate(lineup):
        if is_healthy(players[slot.player_id]):
            continue
        bench = [
            players[pid] for pid in healthy_active
            if not players[pid].is_pitcher and pid not in used
        ]
        if not bench:
            continue
        sub = max(bench, key=lambda p: (roster.position_value(p, slot.position), -p.id))
        used.add(sub.id)
        lineup[index] = LineupSlot(sub.id, slot.position)
        notes.append(f"{sub.name} starts at {slot.position} for injured {players[slot.player_id].name}.")
    closer = team.closer if team.closer is not None and is_healthy(players[team.closer]) else None
    return replace(team, active=healthy_active, lineup=lineup, closer=closer), notes


def healthy_counts(team: Team, players: Mapping[int, Player]) -> tuple[int, int]:
    hitters = sum(1 for pid in team.active if not players[pid].is_pitcher and is_healthy(players[pid]))
    pitchers = sum(1 for pid in team.active if players[pid].is_pitcher and is_healthy(players[pid]))
    return hitters, pitchers
