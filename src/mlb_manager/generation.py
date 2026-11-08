"""Seeded generation of fictional players and team rosters."""

from __future__ import annotations

import random
from mlb_manager import ratings as rt
from mlb_manager import roster
from mlb_manager.franchises import FRANCHISES
from mlb_manager.models import Player, Team
from mlb_manager.names import NameGenerator

# Primary positions of the 26-man active roster: 13 pitchers, 13 position players.
ACTIVE_TEMPLATE: tuple[str, ...] = (
    "SP", "SP", "SP", "SP", "SP",
    "RP", "RP", "RP", "RP", "RP", "RP", "RP", "RP",
    "C", "C", "1B", "2B", "3B", "SS", "LF", "CF", "RF", "DH",
    "SS", "CF", "3B",
)
# Primary positions of the reserve (minor-league depth) players.
RESERVE_TEMPLATE: tuple[str, ...] = (
    "SP", "SP", "SP",
    "RP", "RP", "RP", "RP",
    "C", "2B", "SS", "LF", "RF", "1B", "3B",
)

# Mean offset from 50 for each attribute, by primary position.
POSITION_PROFILES: dict[str, dict[str, int]] = {
    "C": {"contact": -4, "power": 0, "eye": 0, "speed": -15,
          "fielding": 5, "arm": 8, "range": -5, "framing": 0},
    "1B": {"contact": 0, "power": 10, "eye": 3, "speed": -10,
           "fielding": 0, "arm": -10, "range": -10},
    "2B": {"contact": 3, "power": -5, "eye": 0, "speed": 5,
           "fielding": 6, "arm": 0, "range": 6},
    "3B": {"contact": 0, "power": 5, "eye": 0, "speed": -3,
           "fielding": 4, "arm": 10, "range": 0},
    "SS": {"contact": 0, "power": -5, "eye": -2, "speed": 8,
           "fielding": 8, "arm": 8, "range": 8},
    "LF": {"contact": 0, "power": 6, "eye": 0, "speed": 0,
           "fielding": -3, "arm": -5, "range": 0},
    "CF": {"contact": 2, "power": -2, "eye": 0, "speed": 12,
           "fielding": 4, "arm": 0, "range": 12},
    "RF": {"contact": 0, "power": 7, "eye": 0, "speed": 2,
           "fielding": 0, "arm": 10, "range": 2},
    "DH": {"contact": 4, "power": 12, "eye": 5, "speed": -12,
           "fielding": -15, "arm": -10, "range": -15},
    "SP": {"stuff": 0, "control": 2, "movement": 0, "stamina": 15},
    "RP": {"stuff": 5, "control": -3, "movement": 2, "stamina": -25},
}

# Pitchers hit poorly; these are absolute means, not offsets.
PITCHER_HITTING: dict[str, int] = {"contact": 15, "power": 12, "eye": 15, "speed": 30}

# (position, probability) of a secondary position being learned.
SECONDARY_POSITIONS: dict[str, tuple[tuple[str, float], ...]] = {
    "C": (("1B", 0.3),),
    "1B": (("LF", 0.25), ("3B", 0.15)),
    "2B": (("SS", 0.4), ("3B", 0.4)),
    "3B": (("1B", 0.5), ("2B", 0.3)),
    "SS": (("2B", 0.8), ("3B", 0.6)),
    "LF": (("RF", 0.7), ("1B", 0.3)),
    "CF": (("LF", 0.9), ("RF", 0.9)),
    "RF": (("LF", 0.8), ("1B", 0.25)),
    "DH": (("1B", 0.6), ("LF", 0.3)),
}

RIGHT_HANDED_ONLY = {"C", "2B", "3B", "SS"}

STARTER_TALENT_OFFSET = 0.0
BENCH_TALENT_OFFSET = -5.0
RESERVE_TALENT_OFFSET = -7.0


def _gauss_int(rng: random.Random, mu: float, sigma: float, lo: int, hi: int) -> int:
    return int(max(lo, min(hi, round(rng.gauss(mu, sigma)))))


def generate_ratings(rng: random.Random, position: str, talent: float) -> dict[str, int]:
    """Create a position-appropriate attribute set around a talent level."""
    profile = POSITION_PROFILES[position]
    ratings: dict[str, int] = {}
    if position in rt.PITCHER_POSITIONS:
        for attr in rt.PITCHING_ATTRS:
            value = 50 + profile[attr] + talent + rng.gauss(0, 8)
            if attr == "stamina":
                value = 50 + profile[attr] + rng.gauss(0, 8)  # stamina is role-driven
            ratings[attr] = rt.clamp_rating(value)
        for attr, mean in PITCHER_HITTING.items():
            ratings[attr] = rt.clamp_rating(rng.gauss(mean, 6))
        ratings["fielding"] = rt.clamp_rating(rng.gauss(45, 8))
        ratings["arm"] = rt.clamp_rating(rng.gauss(55, 8))
        ratings["range"] = rt.clamp_rating(rng.gauss(40, 8))
        return ratings

    for attr in (*rt.HITTING_ATTRS, *rt.FIELDING_ATTRS):
        ratings[attr] = rt.clamp_rating(50 + profile[attr] + talent + rng.gauss(0, 8))
    if position == "C":
        ratings["framing"] = rt.clamp_rating(50 + talent + rng.gauss(0, 10))
    return ratings


class PlayerFactory:
    """Creates players with sequential ids from a deterministic RNG."""

    def __init__(self, rng: random.Random, start_id: int = 1) -> None:
        self.rng = rng
        self.names = NameGenerator(rng)
        self.next_id = start_id

    def create(self, position: str, talent: float, age_mu: float, age_sigma: float) -> Player:
        rng = self.rng
        pid = self.next_id
        self.next_id += 1
        first, last = self.names.next_name()

        if position in RIGHT_HANDED_ONLY:
            throws = "R"
        elif position in rt.PITCHER_POSITIONS:
            throws = "L" if rng.random() < 0.28 else "R"
        else:
            throws = "L" if rng.random() < 0.2 else "R"
        roll = rng.random()
        if position in rt.PITCHER_POSITIONS:
            bats = throws if roll < 0.9 else ("S" if roll < 0.93 else ("R" if throws == "L" else "L"))
        else:
            bats = "S" if roll < 0.1 else ("L" if roll < 0.43 else "R")

        secondary = [
            pos for pos, prob in SECONDARY_POSITIONS.get(position, ()) if rng.random() < prob
        ]
        return Player(
            id=pid,
            first_name=first,
            last_name=last,
            age=_gauss_int(rng, age_mu, age_sigma, 19, 40),
            bats=bats,
            throws=throws,
            position=position,
            secondary_positions=secondary,
            ratings=generate_ratings(rng, position, talent),
        )


def generate_team_players(
    factory: PlayerFactory, team_strength: float
) -> tuple[list[Player], list[Player]]:
    """Return ``(active, reserves)`` player lists for one club."""
    rng = factory.rng
    active: list[Player] = []
    for index, position in enumerate(ACTIVE_TEMPLATE):
        offset = BENCH_TALENT_OFFSET if index >= len(ACTIVE_TEMPLATE) - 3 else STARTER_TALENT_OFFSET
        talent = team_strength + offset + rng.gauss(0, 4)
        active.append(factory.create(position, talent, age_mu=28.5, age_sigma=3.5))
    reserves: list[Player] = []
    for position in RESERVE_TEMPLATE:
        talent = team_strength + RESERVE_TALENT_OFFSET + rng.gauss(0, 4)
        reserves.append(factory.create(position, talent, age_mu=24, age_sigma=2.5))
    return active, reserves


def generate_rosters(seed: int) -> tuple[dict[str, Team], dict[int, Player], int]:
    """Create all 30 clubs with configured rosters.

    Returns ``(teams_by_abbr, players_by_id, next_player_id)``.
    """
    rng = random.Random(f"{seed}:generation")
    factory = PlayerFactory(rng)
    teams: dict[str, Team] = {}
    players: dict[int, Player] = {}
    for franchise in FRANCHISES:
        team = Team(
            abbr=franchise.abbr,
            city=franchise.city,
            name=franchise.name,
            league=franchise.league,
            division=franchise.division,
        )
        strength = rng.gauss(0, 1.5)
        active, reserves = generate_team_players(factory, strength)
        for player in (*active, *reserves):
            player.team = team.abbr
            players[player.id] = player
        team.active = [p.id for p in active]
        team.reserves = [p.id for p in reserves]
        roster.auto_configure(team, players)
        teams[team.abbr] = team
    return teams, players, factory.next_id
