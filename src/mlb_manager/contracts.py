"""Simple contract and salary-cap rules.

Salaries are in $ millions. ``Player.contract_years`` counts the seasons left
on the deal *including* the current one; it is decremented when a season ends.
A player with ``contract_years == 0`` still on a club is a pending free agent
(only during the re-signing period of the offseason).

Cap rules (deliberately simple):

* A hard cap applies to free-agent signings and to trades: a club may not end
  up above the cap unless the move does not increase its payroll.
* Re-signing or extending your own players is allowed above the cap.
* Releasing a player keeps his current-season salary on the books as dead
  money; future years are forgiven.
"""

from __future__ import annotations

import random
from collections.abc import Mapping

from mlb_manager.models import Player, Team

MIN_SALARY = 0.75
MAX_SALARY = 40.0
MAX_CONTRACT_YEARS = 6
DEFAULT_SALARY_CAP = 125.0
ROOKIE_CONTRACT_YEARS = 6
FIRST_ROUND_ROOKIE_SALARY = 1.5


def round_salary(value: float) -> float:
    value = max(MIN_SALARY, min(MAX_SALARY, value))
    return round(round(value * 20) / 20, 2)


def market_salary(player: Player) -> float:
    """What a player would earn on the open market for one season."""
    ovr = player.overall
    effective = float(ovr)
    if player.age < 27 and player.potential > ovr:
        effective += (player.potential - ovr) * 0.25
    base = MIN_SALARY + 38.0 * max(0.0, (effective - 45.0) / 35.0) ** 1.8
    if player.position == "RP":
        base *= 0.55
    if player.age >= 34:
        base *= 0.7
    elif player.age >= 31:
        base *= 0.88
    return round_salary(base)


def preferred_years(player: Player) -> int:
    if player.age <= 28:
        return 5 if player.overall >= 60 else 3
    if player.age <= 31:
        return 3
    if player.age <= 33:
        return 2
    return 1


def max_years(player: Player) -> int:
    return max(1, min(MAX_CONTRACT_YEARS, 38 - player.age))


def demand_factor(player: Player, seed: int | str) -> float:
    """Stable per-player negotiation temperament (0.95 - 1.15)."""
    return random.Random(f"{seed}:demand:{player.id}").uniform(0.95, 1.15)


def asking_contract(player: Player, seed: int | str, discount: float = 1.0) -> tuple[float, int]:
    """Return the ``(salary, years)`` a player asks for."""
    salary = round_salary(market_salary(player) * demand_factor(player, seed) * discount)
    return salary, min(preferred_years(player), max_years(player))


def sign(player: Player, team: str, salary: float, years: int) -> None:
    player.team = team
    player.salary = round_salary(salary)
    player.contract_years = years
    player.contract_length = years


def rookie_salary(draft_round: int) -> float:
    return FIRST_ROUND_ROOKIE_SALARY if draft_round == 1 else MIN_SALARY


def payroll(team: Team, players: Mapping[int, Player]) -> float:
    total = sum(players[pid].salary for pid in team.all_players) + team.dead_money
    return round(total, 2)


def cap_space(team: Team, players: Mapping[int, Player], cap: float) -> float:
    return round(cap - payroll(team, players), 2)


def commitments(team: Team, players: Mapping[int, Player], seasons: int = 4) -> list[float]:
    """Committed salary for the current season and the following ones."""
    out = []
    for offset in range(seasons):
        total = sum(
            players[pid].salary for pid in team.all_players if players[pid].contract_years > offset
        )
        if offset == 0:
            total += team.dead_money
        out.append(round(total, 2))
    return out


def contract_status(player: Player) -> str:
    if player.team is None:
        return "Free agent"
    if player.contract_years <= 0:
        return "Pending FA"
    if player.contract_years == 1:
        return "Expiring"
    if player.draft_year is not None and player.contract_length == ROOKIE_CONTRACT_YEARS:
        return "Rookie deal"
    return "Signed"


def fits_under_cap(before: float, after: float, cap: float) -> bool:
    """A move is legal if it ends under the cap or does not add payroll."""
    return after <= cap + 1e-9 or after <= before + 1e-9


def money(value: float) -> str:
    return f"${value:,.2f}M"
