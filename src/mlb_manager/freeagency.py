"""Free agency: the market, user offers and CPU signings."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mlb_manager import contracts, frontoffice, valuation
from mlb_manager.models import Player, Team

if TYPE_CHECKING:
    from mlb_manager.league import League

IN_SEASON_DISCOUNT = 0.6  # unsigned players take less once the season is under way
NEGOTIATION_FLOOR = 0.85  # offers below this share of the ask are always refused
CPU_SIGNING_ROUNDS = 3
CPU_CAP_BUFFER = 3.0


class FreeAgencyError(ValueError):
    """Raised for an illegal signing (roster full, over the cap, ...)."""


def free_agents(league: League) -> list[Player]:
    in_draft = set(league.draft.pool) if league.draft else set()
    return sorted(
        (p for p in league.players.values() if p.team is None and p.id not in in_draft),
        key=lambda p: (-p.overall, p.id),
    )


def asking(league: League, player: Player) -> tuple[float, int]:
    if league.in_season:
        salary = contracts.market_salary(player) * contracts.demand_factor(player, league.seed)
        return contracts.round_salary(salary * IN_SEASON_DISCOUNT), 1
    return contracts.asking_contract(player, league.seed)


def evaluate_offer(league: League, player: Player, salary: float, years: int) -> tuple[bool, str]:
    ask_salary, _ = asking(league, player)
    limit = 1 if league.in_season else contracts.max_years(player)
    if not 1 <= years <= limit:
        return False, f"{player.name} will only consider 1-{limit} year(s)."
    if salary >= ask_salary - 1e-9:
        return True, f"{player.name} accepts."
    share = salary / ask_salary if ask_salary else 1.0
    if share < NEGOTIATION_FLOOR:
        return False, f"{player.name}'s agent calls the offer insulting (asking {contracts.money(ask_salary)})."
    chance = (share - NEGOTIATION_FLOOR) / (1 - NEGOTIATION_FLOOR)
    roll = league._rng("fa-offer", player.id, league.day, f"{salary:.2f}", years).random()
    if roll < chance:
        return True, f"{player.name} accepts after some negotiation."
    return False, f"{player.name} turns it down (asking {contracts.money(ask_salary)})."


def sign_free_agent(league: League, team: Team, player_id: int, salary: float, years: int) -> str:
    player = league.players.get(player_id)
    if player is None or player not in free_agents(league):
        raise FreeAgencyError(f"Player #{player_id} is not a free agent.")
    if len(team.all_players) >= frontoffice.MAX_ORG:
        raise FreeAgencyError(
            f"Organisation is full ({frontoffice.MAX_ORG}). Release someone first."
        )
    salary = contracts.round_salary(salary)
    space = contracts.cap_space(team, league.players, league.salary_cap)
    if salary > space + 1e-9:
        raise FreeAgencyError(
            f"Signing would exceed the salary cap (space {contracts.money(space)})."
        )
    accepted, message = evaluate_offer(league, player, salary, years)
    if not accepted:
        raise FreeAgencyError(message)
    contracts.sign(player, team.abbr, salary, years)
    if team.abbr == league.user_team and len(team.active) < 26:
        team.active.append(player.id)
    else:
        team.reserves.append(player.id)
    note = (
        f"{team.abbr} signed free agent {player.name} ({player.position}, {player.overall} OVR) "
        f"for {years} yr / {contracts.money(salary)}."
    )
    league.log_transaction(note)
    return note


def _cpu_wants(league: League, team: Team, player: Player, ask_salary: float,
               val: valuation.Valuator | None = None) -> float | None:
    """Score a free agent for a CPU club, or None if it should pass."""
    if player.overall <= frontoffice.incumbent_rating(team, league.players, player) + 1:
        return None
    if team.strategy == "rebuilding" and player.age > 29 and ask_salary > 3.0:
        return None
    space = contracts.cap_space(team, league.players, league.salary_cap)
    if ask_salary > space - CPU_CAP_BUFFER:
        return None
    value = (val or valuation.Valuator(league)).player(player, team).total
    return value / (1.0 + ask_salary * 0.05)


def cpu_signing_round(league: League) -> list[str]:
    notes: list[str] = []
    order = sorted(
        (t for t in league.teams.values() if t.abbr != league.user_team),
        key=lambda t: (frontoffice.STRATEGIES.index(t.strategy),
                       -contracts.cap_space(t, league.players, league.salary_cap), t.abbr),
    )
    for team in order:
        if len(team.all_players) >= frontoffice.MAX_ORG:
            continue
        market = free_agents(league)[:60]
        best: tuple[float, Player, float, int] | None = None
        val = valuation.Valuator(league)
        for player in market:
            salary, years = asking(league, player)
            score = _cpu_wants(league, team, player, salary, val)
            if score is not None and (best is None or score > best[0]):
                best = (score, player, salary, years)
        if best is None:
            continue
        _, player, salary, years = best
        contracts.sign(player, team.abbr, salary, years)
        team.reserves.append(player.id)
        note = (
            f"{team.abbr} signed free agent {player.name} ({player.position}, {player.overall} OVR) "
            f"for {years} yr / {contracts.money(salary)}."
        )
        league.log_transaction(note)
        notes.append(note)
    return notes


def cpu_free_agency(league: League, rounds: int = CPU_SIGNING_ROUNDS) -> list[str]:
    notes: list[str] = []
    for _ in range(rounds):
        notes.extend(cpu_signing_round(league))
    return notes
