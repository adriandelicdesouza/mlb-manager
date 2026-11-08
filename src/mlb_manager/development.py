"""Player potential, yearly development, aging, retirement and scouting."""

from __future__ import annotations

import random

from mlb_manager import ratings as rt
from mlb_manager.models import Player

DEVELOPMENT_TYPES: tuple[str, ...] = ("fast", "normal", "slow")
PEAK_AGE: dict[str, int] = {"fast": 26, "normal": 27, "slow": 29}
GROWTH_RATE: dict[str, float] = {"fast": 0.40, "normal": 0.28, "slow": 0.18}


def random_development(rng: random.Random) -> str:
    return rng.choices(DEVELOPMENT_TYPES, weights=(0.25, 0.5, 0.25))[0]


def initial_potential(rng: random.Random, overall: int, age: int) -> int:
    """Ceiling for an existing player: young players have headroom."""
    if age >= 28:
        return overall
    headroom = (28 - age) * rng.uniform(0.6, 2.2)
    return rt.clamp_rating(overall + headroom)


def _developing_attrs(player: Player) -> list[str]:
    if player.is_pitcher:
        return [a for a in rt.PITCHING_ATTRS if a != "stamina"]
    attrs = [*rt.HITTING_ATTRS, *rt.FIELDING_ATTRS]
    if "framing" in player.ratings:
        attrs.append("framing")
    return attrs


def apply_rating_change(player: Player, delta: float, rng: random.Random) -> None:
    for attr in _developing_attrs(player):
        if attr in player.ratings:
            player.ratings[attr] = rt.clamp_rating(player.ratings[attr] + delta + rng.gauss(0, 1.0))


def develop(player: Player, rng: random.Random) -> int:
    """Apply one offseason of growth or decline (call *after* aging).

    Returns the change in overall rating.
    """
    before = player.overall
    potential = max(player.potential, before)
    peak = PEAK_AGE.get(player.development, 27)
    age = player.age
    if age <= peak:
        roll = rng.random()
        if roll < 0.05:
            potential = rt.clamp_rating(potential + rng.randint(4, 8))  # breakout
        elif roll < 0.10:
            potential = max(before, potential - rng.randint(3, 8))  # bust
        delta = (potential - before) * GROWTH_RATE.get(player.development, 0.28) + rng.gauss(0, 1.5)
    elif age <= 31:
        delta = min(1.0, (potential - before) * 0.2) + rng.gauss(-0.4, 1.2)
    else:
        delta = -(age - 31) * 0.9 - 0.5 + rng.gauss(0, 1.2)
    apply_rating_change(player, delta, rng)
    after = player.overall
    player.potential = max(potential, after) if age <= 31 else after
    return after - before


def should_retire(player: Player, rng: random.Random) -> bool:
    ovr, age = player.overall, player.age
    if age >= 40:
        return True
    if age >= 35 and ovr < 52:
        return rng.random() < 0.6
    if age >= 33 and ovr < 44:
        return rng.random() < 0.5
    if player.team is None and age >= 30 and ovr < 46:
        return rng.random() < 0.6
    return False


def scouted_potential(player: Player, viewer: str | None, seed: int | str, year: int) -> int:
    """Potential as seen by ``viewer``'s scouts: noisy for young players.

    Scouts know their own organisation better than other clubs or amateurs.
    The report is stable for a given season.
    """
    if player.age >= 28:
        return max(player.potential, player.overall)
    if player.team is None:
        sd = 7.0
    elif player.team == viewer:
        sd = 2.5
    else:
        sd = 5.0
    sd *= min(1.0, (28 - player.age) / 8)
    noise = random.Random(f"{seed}:{year}:scout:{viewer}:{player.id}").gauss(0, sd)
    return max(player.overall, rt.clamp_rating(player.potential + noise))


def potential_grade(potential: int) -> str:
    for cutoff, grade in ((75, "A+"), (70, "A"), (65, "B+"), (60, "B"), (55, "C+"), (50, "C"), (45, "D")):
        if potential >= cutoff:
            return grade
    return "F"
