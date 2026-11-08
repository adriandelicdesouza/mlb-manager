"""Contracts: market salaries, contract structures, payroll and commitments.

Salaries are in $ millions. ``Player.contract_years`` counts the seasons left
on the deal *including* the current one; it is decremented when a season ends.
A player with ``contract_years == 0`` still on a club is a pending free agent
(only during the re-signing period of the offseason).

``Player.salary`` is the *cash* salary of the current season. Long contracts
(three or more years) are back-loaded: cash rises by ``ESCALATOR`` of the
average annual value (AAV, ``Player.contract_aav``) each season around the
AAV, so the total equals AAV x years. The Competitive Balance Tax counts the
AAV (see ``finance.py``); payroll, caps and floors count cash.

League rules (caps, floors, the minimum salary, taxes) live in
``finance.py``; this module only knows contract mechanics. Releasing a
player keeps his current-season salary on the books as dead money; future
years are forgiven.
"""

from __future__ import annotations

import random
from collections.abc import Mapping

from mlb_manager.models import Player, Team

MARKET_SCALE = 1.9  # salary level relative to the pre-v0.9 economy (MLB-level pay)
MIN_SALARY = 0.78  # 2026 MLB minimum; the season's league minimum comes from finance.rules()
MAX_SALARY = 60.0
MAX_CONTRACT_YEARS = 6
ROOKIE_CONTRACT_YEARS = 6
FIRST_ROUND_ROOKIE_SALARY = 1.5
ESCALATOR = 0.05  # yearly cash step (share of AAV) on contracts of 3+ years
ESCALATING_MIN_YEARS = 3
ARBITRATION_SHARE: dict[int, float] = {3: 0.40, 4: 0.60, 5: 0.80}  # of market salary, by service years
FREE_AGENCY_SERVICE = 6  # service years needed to reach free agency without club control


def round_salary(value: float, minimum: float = MIN_SALARY) -> float:
    value = max(minimum, min(MAX_SALARY, value))
    if value <= minimum + 1e-9:
        return round(minimum, 4)
    return max(round(minimum, 4), round(round(value * 20) / 20, 2))


def market_salary(player: Player, minimum: float = MIN_SALARY) -> float:
    """What a player would earn on the open market for one season."""
    ovr = player.overall
    effective = float(ovr)
    if player.age < 27 and player.potential > ovr:
        effective += (player.potential - ovr) * 0.25
    base = minimum + 38.0 * MARKET_SCALE * max(0.0, (effective - 45.0) / 35.0) ** 1.8
    if player.position == "RP":
        base *= 0.55
    if player.age >= 34:
        base *= 0.7
    elif player.age >= 31:
        base *= 0.88
    return round_salary(base, minimum)


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


def asking_contract(player: Player, seed: int | str, discount: float = 1.0,
                    minimum: float = MIN_SALARY) -> tuple[float, int]:
    """Return the ``(AAV, years)`` a player asks for."""
    salary = round_salary(market_salary(player, minimum) * demand_factor(player, seed) * discount, minimum)
    return salary, min(preferred_years(player), max_years(player))


def arbitration_eligible(player: Player) -> bool:
    """Pending free agents short of six service years remain under club control."""
    return player.contract_years <= 0 and player.service_years < FREE_AGENCY_SERVICE


def arbitration_salary(player: Player, minimum: float = MIN_SALARY) -> float:
    """One-year salary for a club-controlled player (pre-arbitration players get the minimum)."""
    share = ARBITRATION_SHARE.get(min(player.service_years, 5), 0.0)
    if share <= 0:
        return round(minimum, 4)
    return round_salary(max(minimum, market_salary(player, minimum) * share), minimum)


def escalation_for(years: int) -> float:
    return ESCALATOR if years >= ESCALATING_MIN_YEARS else 0.0


def cash_schedule(aav: float, years: int, step: float) -> list[float]:
    """Cash salary per contract year; averages to ``aav``."""
    if step <= 0 or years <= 1:
        return [round(aav, 4)] * max(1, years)
    mid = (years - 1) / 2
    return [round(aav * (1 + step * (i - mid)), 4) for i in range(years)]


def first_year_salary(aav: float, years: int) -> float:
    return cash_schedule(aav, years, escalation_for(years))[0]


def aav(player: Player) -> float:
    """Average annual value of the current contract (CBT measure)."""
    return player.contract_aav if player.contract_aav > 0 else player.salary


def salary_in(player: Player, offset: int = 0) -> float:
    """Cash salary ``offset`` seasons from now (0 = current season) while under contract."""
    if offset >= player.contract_years and player.contract_years > 0:
        return 0.0
    if player.contract_raise <= 0 or player.contract_aav <= 0 or player.contract_length <= 1:
        return player.salary
    index = player.contract_length - player.contract_years + offset
    schedule = cash_schedule(player.contract_aav, player.contract_length, player.contract_raise)
    return schedule[max(0, min(index, len(schedule) - 1))]


def sign(player: Player, team: str, salary: float, years: int, minimum: float = MIN_SALARY,
         structure: bool = True) -> None:
    """Sign ``player`` for ``years`` at an AAV of ``salary``.

    Contracts of three or more years are back-loaded (``structure=False`` keeps
    the cash flat, e.g. for rookie deals).
    """
    player.team = team
    player.contract_aav = round_salary(salary, minimum)
    player.contract_years = years
    player.contract_length = years
    player.contract_raise = escalation_for(years) if structure else 0.0
    player.salary = salary_in(player, 0) if player.contract_raise else player.contract_aav


def set_flat_salary(player: Player, salary: float) -> None:
    """Replace the contract with a flat one at ``salary`` (initial league shaping)."""
    player.salary = salary
    player.contract_aav = salary
    player.contract_raise = 0.0


def advance_contract_year(player: Player) -> None:
    """A season has ended: one year fewer, next year's cash salary."""
    player.contract_years = max(0, player.contract_years - 1)
    if player.contract_years > 0 and player.contract_raise > 0:
        player.salary = salary_in(player, 0)


def raise_to_minimum(player: Player, minimum: float) -> None:
    player.salary = round(minimum, 4)
    if player.contract_aav < minimum:
        player.contract_aav = round(minimum, 4)
        player.contract_raise = 0.0


def rookie_salary(draft_round: int, minimum: float = MIN_SALARY) -> float:
    return max(minimum, FIRST_ROUND_ROOKIE_SALARY) if draft_round == 1 else minimum


def under_contract(player: Player) -> bool:
    return player.contract_years > 0


def payroll(team: Team, players: Mapping[int, Player]) -> float:
    """Current-season cash payroll: players under contract plus dead money.

    Pending free agents (offseason, ``contract_years == 0``) are not counted.
    """
    total = sum(players[pid].salary for pid in team.all_players if players[pid].contract_years > 0)
    return round(total + team.dead_money, 2)


def commitments(team: Team, players: Mapping[int, Player], seasons: int = 4) -> list[float]:
    """Committed cash salary for the current season and the following ones."""
    out = []
    for offset in range(seasons):
        total = sum(salary_in(players[pid], offset) for pid in team.all_players
                    if players[pid].contract_years > offset)
        if offset == 0:
            total += team.dead_money
        out.append(round(total, 2))
    return out


def contract_status(player: Player) -> str:
    if player.team is None:
        return "Free agent"
    if player.contract_years <= 0:
        return "Arb-eligible" if arbitration_eligible(player) else "Pending FA"
    if player.contract_years == 1:
        return "Expiring"
    if player.draft_year is not None and player.contract_length == ROOKIE_CONTRACT_YEARS:
        return "Rookie deal"
    return "Signed"


def money(value: float) -> str:
    return f"${value:,.2f}M"
