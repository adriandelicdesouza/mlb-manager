"""Annual amateur draft: picks, class generation, order and CPU selections."""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from mlb_manager import development, ratings as rt
from mlb_manager.generation import PlayerFactory, generate_ratings
from mlb_manager.models import Player, Team

DRAFT_ROUNDS = 5
CLASS_SIZE = 200
FUTURE_DRAFT_YEARS = 3  # current draft year plus the next two can be traded

# Realistic-ish split: about half of all picks are pitchers.
CLASS_POSITION_WEIGHTS: dict[str, float] = {
    "SP": 0.32, "RP": 0.16, "C": 0.07, "1B": 0.05, "2B": 0.06,
    "3B": 0.07, "SS": 0.10, "LF": 0.05, "CF": 0.07, "RF": 0.05,
}
# (age, probability, mean gap between current overall and potential)
CLASS_AGES: tuple[tuple[int, float, float], ...] = ((18, 0.4, 24.0), (21, 0.35, 16.0), (22, 0.25, 13.0))


@dataclass(slots=True)
class DraftPick:
    year: int
    round: int
    original: str  # club whose record determines the slot
    owner: str  # club that currently holds the pick

    @property
    def pick_id(self) -> str:
        return f"{self.year}-R{self.round}-{self.original}"

    def label(self) -> str:
        via = f" (via {self.original})" if self.owner != self.original else ""
        return f"{self.year} round {self.round}{via}"

    def to_dict(self) -> dict[str, Any]:
        return {"year": self.year, "round": self.round, "original": self.original, "owner": self.owner}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DraftPick:
        return cls(int(data["year"]), int(data["round"]), data["original"], data["owner"])


@dataclass(slots=True)
class DraftSelection:
    overall_pick: int
    round: int
    team: str
    original: str
    player_id: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall_pick": self.overall_pick,
            "round": self.round,
            "team": self.team,
            "original": self.original,
            "player_id": self.player_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DraftSelection:
        return cls(**data)


@dataclass(slots=True)
class Draft:
    year: int
    order: list[str]  # original clubs in first-round order (worst record first)
    pool: list[int]  # undrafted prospect ids
    selections: list[DraftSelection] = field(default_factory=list)

    @property
    def total_picks(self) -> int:
        return len(self.order) * DRAFT_ROUNDS

    @property
    def is_complete(self) -> bool:
        return len(self.selections) >= self.total_picks or not self.pool

    def slot(self, index: int) -> tuple[int, str]:
        """``(round, original club)`` for 0-based overall pick ``index``."""
        return index // len(self.order) + 1, self.order[index % len(self.order)]

    def current_slot(self) -> tuple[int, str] | None:
        if self.is_complete:
            return None
        return self.slot(len(self.selections))

    def to_dict(self) -> dict[str, Any]:
        return {
            "year": self.year,
            "order": list(self.order),
            "pool": list(self.pool),
            "selections": [s.to_dict() for s in self.selections],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Draft:
        return cls(
            year=int(data["year"]),
            order=list(data["order"]),
            pool=[int(x) for x in data["pool"]],
            selections=[DraftSelection.from_dict(s) for s in data["selections"]],
        )


def create_picks(teams: Sequence[str], year: int) -> list[DraftPick]:
    return [DraftPick(year, rnd, abbr, abbr) for abbr in teams for rnd in range(1, DRAFT_ROUNDS + 1)]


def find_pick(picks: Sequence[DraftPick], year: int, rnd: int, original: str) -> DraftPick | None:
    for pick in picks:
        if pick.year == year and pick.round == rnd and pick.original == original:
            return pick
    return None


def generate_class(rng: random.Random, factory: PlayerFactory, size: int = CLASS_SIZE) -> list[Player]:
    positions = list(CLASS_POSITION_WEIGHTS)
    weights = list(CLASS_POSITION_WEIGHTS.values())
    prospects: list[Player] = []
    for _ in range(size):
        position = rng.choices(positions, weights=weights)[0]
        age, gap_mean = 18, 24.0
        roll = rng.random()
        acc = 0.0
        for a, prob, gap in CLASS_AGES:
            acc += prob
            if roll < acc:
                age, gap_mean = a, gap
                break
        potential = rt.clamp_rating(min(85, rng.gauss(47, 8) + max(0.0, rng.gauss(0, 6))))
        current = max(25, round(potential - rng.gauss(gap_mean, 4)))
        player = factory.create(position, talent=current - 50, age_mu=age, age_sigma=0)
        player.age = age
        player.ratings = generate_ratings(rng, position, current - 50)
        player.potential = max(potential, player.overall)
        player.development = development.random_development(rng)
        prospects.append(player)
    return prospects


def draft_order(
    teams: Mapping[str, Team], ranked_best_first: Sequence[str], playoff_finish: Mapping[str, int]
) -> list[str]:
    """Worst regular-season record picks first; playoff clubs pick last.

    ``playoff_finish`` maps playoff clubs to how far they went (higher =
    deeper run, champion highest).
    """
    worst_first = list(reversed(ranked_best_first))
    non_playoff = [a for a in worst_first if a not in playoff_finish]
    playoff = sorted(
        (a for a in worst_first if a in playoff_finish),
        key=lambda a: (playoff_finish[a], -worst_first.index(a)),
    )
    order = non_playoff + playoff
    assert set(order) == set(teams)
    return order


def prospect_score(
    player: Player, team: Team, players: Mapping[int, Player], seed: int | str, year: int
) -> float:
    """How a club's scouts rank a prospect on their board (imperfect info)."""
    pot = development.scouted_potential(player, team.abbr, seed, year)
    weight_now = {"contending": 0.45, "balanced": 0.3, "rebuilding": 0.15}.get(team.strategy, 0.3)
    score = pot + player.overall * weight_now - (player.age - 18) * 0.3
    # Mild positional need: thin organisations value the position more.
    same = sum(
        1 for pid in team.all_players if players[pid].position == player.position
    )
    typical = {"SP": 8, "RP": 12}.get(player.position, 3)
    if same < typical:
        score += 1.5
    return score


def cpu_choose(
    draft: Draft, team: Team, players: Mapping[int, Player], seed: int | str
) -> int:
    return max(
        draft.pool,
        key=lambda pid: (prospect_score(players[pid], team, players, seed, draft.year), -pid),
    )
