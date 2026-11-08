"""Front-office logic shared by the user's club and the 29 CPU clubs.

* Club strategy (contending / balanced / rebuilding)
* Player and draft-pick valuation used by trades, the draft and free agency
* Organisation management: best-26 selection, minimum depth, cut-downs
* One-time initialisation of contracts, potential and draft picks
"""

from __future__ import annotations

import random
from collections.abc import Iterable
from typing import TYPE_CHECKING

from mlb_manager import contracts, development, roster
from mlb_manager import ratings as rt
from mlb_manager.draft import FUTURE_DRAFT_YEARS, DraftPick, create_picks
from mlb_manager.generation import PlayerFactory
from mlb_manager.models import Player, Team

if TYPE_CHECKING:
    from mlb_manager.league import League

STRATEGIES: tuple[str, ...] = ("contending", "balanced", "rebuilding")
NOW_WEIGHT: dict[str, float] = {"contending": 0.75, "balanced": 0.55, "rebuilding": 0.3}
PICK_STRATEGY_FACTOR: dict[str, float] = {"contending": 0.8, "balanced": 1.0, "rebuilding": 1.25}
PICK_ROUND_VALUE: dict[int, float] = {1: 260.0, 2: 110.0, 3: 60.0, 4: 35.0, 5: 22.0}

MAX_ORG = 45  # active + reserves
MIN_ORG_PITCHERS = 13
MIN_ORG_HITTERS = 13
MIN_ORG_CATCHERS = 2
CONTENDERS = 10
REBUILDERS = 9


# ----------------------------------------------------------------------------- basics


def org(league: League, team: Team) -> list[Player]:
    return [league.players[pid] for pid in team.all_players]


def _avg(values: Iterable[float], default: float = 40.0) -> float:
    values = list(values)
    return sum(values) / len(values) if values else default


def team_strength(team: Team, players: dict[int, Player]) -> float:
    """Quality of the current active configuration (≈ 1-99 scale)."""
    lineup = [players[s.player_id].overall_at(s.position) for s in team.lineup if s.player_id in players]
    rotation = [players[pid].overall for pid in team.rotation if pid in players]
    pen = sorted((p.overall for p in roster.bullpen(team, players)), reverse=True)[:4]
    return _avg(lineup) * 0.5 + _avg(rotation) * 0.32 + _avg(pen) * 0.18


def update_strategies(league: League) -> None:
    records = league.records()
    scores: dict[str, float] = {}
    for abbr, team in league.teams.items():
        rec = records[abbr]
        weight = min(1.0, rec.games / 60)
        scores[abbr] = team_strength(team, league.players) + (rec.pct - 0.5) * 40 * weight
    ranked = sorted(scores, key=lambda a: (-scores[a], a))
    for index, abbr in enumerate(ranked):
        if index < CONTENDERS:
            league.teams[abbr].strategy = "contending"
        elif index >= len(ranked) - REBUILDERS:
            league.teams[abbr].strategy = "rebuilding"
        else:
            league.teams[abbr].strategy = "balanced"


def strength_ranks(league: League) -> dict[str, int]:
    """0 = weakest club (projects to pick first)."""
    ranked = sorted(league.teams, key=lambda a: (team_strength(league.teams[a], league.players), a))
    return {abbr: i for i, abbr in enumerate(ranked)}


# ----------------------------------------------------------------------------- valuation


def _curve(overall: float) -> float:
    return max(0.0, overall - 40.0) ** 1.7


def incumbent_rating(team: Team, players: dict[int, Player], player: Player) -> float:
    if player.position == "SP":
        values = [players[pid].overall for pid in team.rotation if pid in players]
        return min(values) if values else 40.0
    if player.position == "RP":
        values = sorted(p.overall for p in roster.bullpen(team, players))
        return values[len(values) // 2] if values else 40.0
    for slot in team.lineup:
        if slot.position == player.position and slot.player_id in players:
            return players[slot.player_id].overall_at(slot.position)
    return 40.0


def need_factor(league: League, player: Player, team: Team) -> float:
    if player.team == team.abbr:
        return 1.0
    players = league.players
    factor = 1.0
    if player.overall > incumbent_rating(team, players, player) + 2:
        factor *= 1.15
    better = sum(
        1
        for pid in team.all_players
        if players[pid].position == player.position and players[pid].overall >= player.overall
    )
    crowded = {"SP": 5, "RP": 7}.get(player.position, 2)
    if better >= crowded:
        factor *= 0.85
    return factor


def player_value(league: League, player: Player, team: Team) -> float:
    """Value of ``player`` to ``team`` (can be negative for bad contracts).

    Considers current ability, potential, age, position, roster need,
    contract surplus and the club's competitive strategy.
    """
    strategy = "balanced" if team.abbr == league.user_team else team.strategy
    ovr, age = player.overall, player.age
    now = _curve(ovr)
    if age < 30:
        peak = max(ovr, player.potential)
        discount = 1.0 if age <= 23 else max(0.6, 1.0 - (age - 23) * 0.06)
        future = _curve(peak) * discount
    else:
        future = now * max(0.0, (35 - age) / 6)
    weight = NOW_WEIGHT[strategy]
    value = weight * now + (1 - weight) * future
    if player.position == "RP":
        value *= 0.6
    years = max(1, player.contract_years)
    surplus = (contracts.market_salary(player) - player.salary) * min(years, 4)
    value += surplus * 4
    if strategy == "rebuilding" and age >= 30:
        value -= player.salary * min(years, 4) * 2
    return value * need_factor(league, player, team)


def pick_value(league: League, pick: DraftPick, team: Team, ranks: dict[str, int] | None = None) -> float:
    strategy = "balanced" if team.abbr == league.user_team else team.strategy
    draft = league.draft
    if draft is not None and draft.year == pick.year and pick.original in draft.order:
        slot = draft.order.index(pick.original)
    else:
        slot = (ranks or strength_ranks(league))[pick.original]
    slot_factor = 1.4 - 0.7 * slot / 29
    years_out = max(0, pick.year - league.draft_year)
    return (
        PICK_ROUND_VALUE[pick.round]
        * slot_factor
        * (0.9**years_out)
        * PICK_STRATEGY_FACTOR[strategy]
    )


# ----------------------------------------------------------------------------- roster management


def optimize_roster(league: League, team: Team) -> None:
    """Choose the best 26 from the organisation and configure the staff (CPU logic)."""
    players = league.players
    members = org(league, team)
    pitchers = [p for p in members if p.is_pitcher]
    hitters = [p for p in members if not p.is_pitcher]

    def starter_score(p: Player) -> tuple[float, int]:
        bonus = 15 if p.position == "SP" else 0
        penalty = max(0, 45 - p.rating("stamina")) * 0.8
        return (rt.pitching_rating(p.ratings, "SP") + bonus - penalty, -p.id)

    rotation = sorted(pitchers, key=starter_score, reverse=True)[: roster.ROTATION_SIZE]
    relief = sorted(
        (p for p in pitchers if p not in rotation),
        key=lambda p: (rt.pitching_rating(p.ratings, "RP"), -p.id),
        reverse=True,
    )[: roster.MAX_ACTIVE_PITCHERS - len(rotation)]
    active: list[Player] = rotation + relief

    chosen: list[Player] = sorted(
        (h for h in hitters if h.can_play("C")),
        key=lambda p: (p.overall_at("C"), -p.id),
        reverse=True,
    )[:MIN_ORG_CATCHERS]
    remaining = [h for h in hitters if h not in chosen]
    for position in roster.AUTO_FILL_ORDER:
        if position == "C" or not remaining:
            continue
        best = max(remaining, key=lambda p: (roster.position_value(p, position), -p.id))
        chosen.append(best)
        remaining.remove(best)
    remaining.sort(key=lambda p: (rt.batting_rating(p.ratings), -p.id), reverse=True)
    while remaining and len(chosen) < roster.MAX_ACTIVE - roster.MAX_ACTIVE_PITCHERS:
        chosen.append(remaining.pop(0))
    active += chosen

    leftovers = sorted(
        (p for p in members if p not in active), key=lambda p: (-p.overall, p.id)
    )
    pitcher_count = sum(p.is_pitcher for p in active)
    for p in leftovers:
        if len(active) >= roster.MAX_ACTIVE:
            break
        if p.is_pitcher and pitcher_count >= roster.MAX_ACTIVE_PITCHERS:
            continue
        active.append(p)
        pitcher_count += p.is_pitcher

    active_ids = {p.id for p in active}
    team.active = [p.id for p in active]
    team.reserves = [p.id for p in members if p.id not in active_ids]
    roster.auto_configure(team, players)


def ensure_active_roster(league: League, team: Team) -> list[str]:
    """Fill empty active spots from the reserves and repair the lineup/staff."""
    players = league.players
    notes: list[str] = []
    if not _has_minimum_depth(league, team):
        notes.extend(fill_minimums(league, team))
    while len(team.active) < roster.MAX_ACTIVE and team.reserves:
        pitchers = len(roster.active_pitchers(team, players))
        hitters = len(team.active) - pitchers
        reserves = roster.reserve_players(team, players)
        want_pitcher = pitchers < roster.MAX_ACTIVE_PITCHERS and (
            hitters >= roster.MAX_ACTIVE - roster.MAX_ACTIVE_PITCHERS or pitchers < 11
        )
        pool = [p for p in reserves if p.is_pitcher == want_pitcher]
        if not pool:
            pool = [p for p in reserves if not p.is_pitcher] if want_pitcher else []
            if not pool and pitchers < roster.MAX_ACTIVE_PITCHERS:
                pool = [p for p in reserves if p.is_pitcher]
        if not pool:
            break
        best = max(pool, key=lambda p: (p.overall, -p.id))
        team.reserves.remove(best.id)
        team.active.append(best.id)
        notes.append(f"{team.abbr}: {best.name} called up to fill an open roster spot.")
    if len(roster.active_hitters(team, players)) < len(rt.LINEUP_POSITIONS) or len(
        roster.active_pitchers(team, players)
    ) < roster.ROTATION_SIZE + 1:
        optimize_roster(league, team)
        notes.append(f"{team.abbr}: active roster reorganised to field a legal team.")
    notes.extend(roster.repair(team, players))
    return notes


def _has_minimum_depth(league: League, team: Team) -> bool:
    members = org(league, team)
    pitchers = sum(p.is_pitcher for p in members)
    catchers = sum(p.can_play("C") for p in members)
    return (
        pitchers >= MIN_ORG_PITCHERS
        and len(members) - pitchers >= MIN_ORG_HITTERS
        and catchers >= MIN_ORG_CATCHERS
    )


def new_player_factory(league: League, purpose: str) -> PlayerFactory:
    factory = PlayerFactory(league._rng(purpose, league.next_player_id), start_id=league.next_player_id)
    return factory


def fill_minimums(league: League, team: Team) -> list[str]:
    """Guarantee 13 pitchers, 13 hitters and 2 catchers in the organisation.

    Signs the best minimum-salary free agent of the needed type, or a
    replacement-level player if the market is empty.
    """
    from mlb_manager import freeagency  # local import: freeagency depends on this module

    notes: list[str] = []
    for _ in range(40):
        members = org(league, team)
        pitchers = sum(p.is_pitcher for p in members)
        catchers = sum(p.can_play("C") for p in members)
        hitters = len(members) - pitchers
        if catchers < MIN_ORG_CATCHERS:
            need = "C"
        elif pitchers < MIN_ORG_PITCHERS:
            need = "P"
        elif hitters < MIN_ORG_HITTERS:
            need = "H"
        else:
            break
        candidates = [
            p
            for p in freeagency.free_agents(league)
            if (need == "P" and p.is_pitcher)
            or (need == "H" and not p.is_pitcher)
            or (need == "C" and p.can_play("C"))
        ]
        if candidates:
            player = max(candidates, key=lambda p: (p.overall, -p.id))
        else:
            factory = new_player_factory(league, f"filler:{team.abbr}")
            position = {"C": "C", "P": "RP", "H": "2B"}[need]
            player = factory.create(position, talent=-12, age_mu=27, age_sigma=2)
            player.potential = player.overall
            league.players[player.id] = player
            league.next_player_id = factory.next_id
        contracts.sign(player, team.abbr, contracts.MIN_SALARY, 1)
        team.reserves.append(player.id)
        notes.append(f"{team.abbr} signed {player.name} ({player.position}) for organisational depth.")
        league.log_transaction(notes[-1])
    return notes


def release_player(league: League, team: Team, player_id: int) -> str:
    player = league.players[player_id]
    if player_id not in team.all_players:
        raise roster.RosterError(f"{player.name} is not in your organisation.")
    if player_id in team.active:
        team.active.remove(player_id)
    else:
        team.reserves.remove(player_id)
    if player.contract_years > 0:
        team.dead_money = round(team.dead_money + player.salary, 2)
    player.team = None
    player.contract_years = 0
    message = f"{team.abbr} released {player.name}."
    league.log_transaction(message)
    return message


def cutdown(league: League, team: Team) -> list[str]:
    """Release the least valuable reserves until the organisation fits MAX_ORG."""
    notes = []
    while len(team.all_players) > MAX_ORG and team.reserves:
        worst = min(
            (league.players[pid] for pid in team.reserves),
            key=lambda p: (player_value(league, p, team), p.id),
        )
        notes.append(release_player(league, team, worst.id))
    return notes


# ----------------------------------------------------------------------------- initialisation


def initialize(league: League) -> None:
    """Give every player potential and a contract, and every club draft picks.

    Idempotent: only fills in data that is missing, so it doubles as the
    migration step for version-1 saves.
    """
    for player in sorted(league.players.values(), key=lambda p: p.id):
        rng = random.Random(f"{league.seed}:init:{player.id}")
        if player.potential <= 0:
            player.potential = development.initial_potential(rng, player.overall, player.age)
            player.development = development.random_development(rng)
        if player.team is not None and player.contract_years <= 0 and player.salary <= 0:
            if player.age <= 25:
                salary = contracts.MIN_SALARY
                years = rng.randint(2, min(6, max(2, 31 - player.age)))
            else:
                salary = contracts.market_salary(player) * rng.uniform(0.8, 1.2)
                years = rng.randint(1, contracts.preferred_years(player) + 1)
                years = min(years, contracts.max_years(player))
            contracts.sign(player, player.team, salary, years)
            player.contract_length = years + rng.randint(0, 2)
    for team in league.teams.values():
        _fit_under_cap(league, team)
    if not any(p.team is None for p in league.players.values()):
        _seed_free_agents(league)
    if not league.draft_picks:
        for year in range(league.draft_year, league.draft_year + FUTURE_DRAFT_YEARS):
            league.draft_picks.extend(create_picks(list(league.teams), year))
    update_strategies(league)


def _fit_under_cap(league: League, team: Team) -> None:
    target = league.salary_cap * 0.97
    total = contracts.payroll(team, league.players)
    if total <= target:
        return
    big = [league.players[pid] for pid in team.all_players if league.players[pid].salary > contracts.MIN_SALARY]
    big_total = sum(p.salary for p in big)
    excess = total - target
    if big_total <= 0:
        return
    scale = max(0.1, (big_total - excess) / big_total)
    for p in big:
        p.salary = contracts.round_salary(p.salary * scale)


INITIAL_FREE_AGENTS = 60


def _seed_free_agents(league: League) -> None:
    """Create an opening free-agent pool: veterans and fringe major leaguers."""
    rng = league._rng("initial-free-agents")
    factory = PlayerFactory(rng, start_id=league.next_player_id)
    positions = ["SP", "RP", "RP", "C", "1B", "2B", "3B", "SS", "LF", "CF", "RF", "DH"]
    for index in range(INITIAL_FREE_AGENTS):
        position = positions[index % len(positions)]
        player = factory.create(position, talent=rng.gauss(-4, 5), age_mu=31, age_sigma=3.5)
        player.potential = development.initial_potential(rng, player.overall, player.age)
        player.development = development.random_development(rng)
        league.players[player.id] = player
    league.next_player_id = factory.next_id
