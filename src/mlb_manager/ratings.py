"""Attribute definitions and position-specific rating math.

All ratings are on a 1-99 scale where 50 is a league-average MLB player.
"""

from __future__ import annotations

from collections.abc import Mapping

RATING_MIN = 1
RATING_MAX = 99

FIELD_POSITIONS: tuple[str, ...] = ("C", "1B", "2B", "3B", "SS", "LF", "CF", "RF")
LINEUP_POSITIONS: tuple[str, ...] = FIELD_POSITIONS + ("DH",)
PITCHER_POSITIONS: tuple[str, ...] = ("SP", "RP")
ALL_POSITIONS: tuple[str, ...] = LINEUP_POSITIONS + PITCHER_POSITIONS

HITTING_ATTRS: tuple[str, ...] = ("contact", "power", "eye", "speed")
FIELDING_ATTRS: tuple[str, ...] = ("fielding", "arm", "range")
CATCHER_ATTRS: tuple[str, ...] = ("framing",)
PITCHING_ATTRS: tuple[str, ...] = ("stuff", "control", "movement", "stamina")

ATTRIBUTE_LABELS: dict[str, str] = {
    "contact": "Contact",
    "power": "Power",
    "eye": "Eye",
    "speed": "Speed",
    "fielding": "Fielding",
    "arm": "Arm",
    "range": "Range",
    "framing": "Framing",
    "stuff": "Stuff",
    "control": "Control",
    "movement": "Movement",
    "stamina": "Stamina",
}

# How much each fielding attribute matters at each position.
DEFENSE_WEIGHTS: dict[str, dict[str, float]] = {
    "C": {"fielding": 0.30, "arm": 0.35, "framing": 0.35},
    "1B": {"fielding": 0.70, "range": 0.30},
    "2B": {"fielding": 0.45, "range": 0.40, "arm": 0.15},
    "3B": {"fielding": 0.45, "arm": 0.35, "range": 0.20},
    "SS": {"fielding": 0.40, "range": 0.35, "arm": 0.25},
    "LF": {"range": 0.50, "fielding": 0.30, "arm": 0.20},
    "CF": {"range": 0.60, "fielding": 0.25, "arm": 0.15},
    "RF": {"range": 0.40, "fielding": 0.25, "arm": 0.35},
}

# Relative importance of each position to overall team defense.
POSITION_DEFENSE_IMPORTANCE: dict[str, float] = {
    "C": 1.0,
    "1B": 0.6,
    "2B": 1.1,
    "3B": 1.0,
    "SS": 1.3,
    "LF": 0.8,
    "CF": 1.2,
    "RF": 0.9,
}

# Share of a position player's overall that comes from defense.
DEFENSE_SHARE: dict[str, float] = {
    "C": 0.40,
    "1B": 0.15,
    "2B": 0.35,
    "3B": 0.30,
    "SS": 0.40,
    "LF": 0.20,
    "CF": 0.35,
    "RF": 0.25,
    "DH": 0.0,
}

OUT_OF_POSITION_PENALTY = 20
UNTRAINED_CATCHER_FRAMING = 15

BATTING_WEIGHTS: dict[str, float] = {"contact": 0.35, "power": 0.30, "eye": 0.20, "speed": 0.15}


def clamp_rating(value: float) -> int:
    return int(max(RATING_MIN, min(RATING_MAX, round(value))))


def batting_rating(ratings: Mapping[str, int]) -> float:
    return sum(ratings.get(attr, RATING_MIN) * w for attr, w in BATTING_WEIGHTS.items())


def defense_rating(
    ratings: Mapping[str, int], position: str, eligible: bool = True
) -> float:
    """Defensive value of a player at ``position`` (1-99 scale)."""
    if position == "DH":
        return 0.0
    weights = DEFENSE_WEIGHTS[position]
    total = 0.0
    for attr, w in weights.items():
        if attr == "framing":
            value = ratings.get("framing", UNTRAINED_CATCHER_FRAMING)
        else:
            value = ratings.get(attr, RATING_MIN)
        total += value * w
    if not eligible:
        total -= OUT_OF_POSITION_PENALTY
    return max(float(RATING_MIN), total)


def pitching_rating(ratings: Mapping[str, int], role: str = "SP") -> float:
    stuff = ratings.get("stuff", RATING_MIN)
    control = ratings.get("control", RATING_MIN)
    movement = ratings.get("movement", RATING_MIN)
    stamina = ratings.get("stamina", RATING_MIN)
    if role == "SP":
        return stuff * 0.35 + control * 0.25 + movement * 0.25 + stamina * 0.15
    return stuff * 0.45 + control * 0.25 + movement * 0.30


def position_player_overall(ratings: Mapping[str, int], position: str) -> int:
    share = DEFENSE_SHARE.get(position, 0.0)
    bat = batting_rating(ratings)
    if position == "DH":
        return clamp_rating(bat)
    return clamp_rating(bat * (1.0 - share) + defense_rating(ratings, position) * share)


def overall(ratings: Mapping[str, int], position: str) -> int:
    if position in PITCHER_POSITIONS:
        return clamp_rating(pitching_rating(ratings, position))
    return position_player_overall(ratings, position)
