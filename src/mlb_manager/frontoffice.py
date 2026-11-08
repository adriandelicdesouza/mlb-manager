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

from mlb_manager import contracts, development, finance, injuries, roster
from mlb_manager import ratings as rt
from mlb_manager.draft import FUTURE_DRAFT_YEARS, DraftPick, create_picks
from mlb_manager.generation import PlayerFactory
from mlb_manager.models import Player, Team

if TYPE_CHECKING:
    from mlb_manager.league import League

STRATEGIES: tuple[str, ...] = ("contending", "balanced", "rebuilding")

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


def player_value(league: League, player: Player, team: Team) -> float:
    """Value of ``player`` to ``team`` (can be negative for bad contracts).

    Single entry point for trades, re-signing priorities and cut-downs; the
    model lives in :mod:`mlb_manager.valuation` (ability and potential
    projections, age curve, position, performance, contract, competitive
    window, roster fit and injuries).
    """
    from mlb_manager import valuation

    return valuation.player_value(league, player, team)


def pick_value(league: League, pick: DraftPick, team: Team, ranks: dict[str, int] | None = None) -> float:
    from mlb_manager import valuation

    val = valuation.Valuator(league)
    if ranks is not None:
        val._ranks = ranks
    return val.pick(pick, team).total


# ----------------------------------------------------------------------------- roster management


def sync_injured_list(league: League, team: Team) -> None:
    """CPU-style IL management: IL longer injuries, reinstate healed players."""
    players = league.players
    for pid in list(team.injured_list):
        player = players[pid]
        if not player.is_injured:
            team.injured_list.remove(pid)
            team.reserves.append(pid)
        elif player.injury_days <= injuries.CPU_REHAB_DAYS and player.injury_total >= injuries.REHAB_MIN_INJURY:
            injuries.start_rehab(team, players, pid)
    for pid in [*team.active, *team.reserves]:
        if players[pid].injury_days >= injuries.IL_THRESHOLD_DAYS and not players[pid].rehab:
            team.remove_player(pid)
            team.injured_list.append(pid)


def optimize_roster(league: League, team: Team, use_performance: bool = False) -> None:
    """Choose the best healthy 26 from the organisation and configure the staff.

    ``use_performance`` blends season statistics into lineup order and rotation
    choice (CPU in-season management).
    """
    players = league.players
    sync_injured_list(league, team)
    everyone = [players[pid] for pid in [*team.active, *team.reserves]]
    members = [p for p in everyone if not p.is_injured]
    hurt = [p for p in everyone if p.is_injured]
    pitchers = [p for p in members if p.is_pitcher]
    hitters = [p for p in members if not p.is_pitcher]

    def starter_score(p: Player) -> tuple[float, int]:
        bonus = 15 if p.position == "SP" else 0
        penalty = max(0, 45 - p.rating("stamina")) * 0.8
        return (rt.pitching_rating(p.ratings, "SP") + bonus - penalty, -p.id)

    rotation = sorted(pitchers, key=starter_score, reverse=True)[: roster.ROTATION_SIZE]
    relief = sorted(
        (p for p in pitchers if p.id not in {r.id for r in rotation}),
        key=lambda p: (rt.pitching_rating(p.ratings, "RP"), -p.id),
        reverse=True,
    )[: roster.MAX_ACTIVE_PITCHERS - len(rotation)]
    active: list[Player] = rotation + relief

    chosen: list[Player] = sorted(
        (h for h in hitters if h.can_play("C")),
        key=lambda p: (p.overall_at("C"), -p.id),
        reverse=True,
    )[:MIN_ORG_CATCHERS]
    chosen_ids = {c.id for c in chosen}
    remaining = [h for h in hitters if h.id not in chosen_ids]
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

    taken = {p.id for p in active}
    leftovers = sorted(
        (p for p in members if p.id not in taken), key=lambda p: (-p.overall, p.id)
    ) + sorted(hurt, key=lambda p: (p.injury_days, -p.overall, p.id))
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
    team.reserves = [p.id for p in everyone if p.id not in active_ids]
    roster.auto_configure(team, players, use_performance=use_performance)


def ensure_active_roster(league: League, team: Team) -> list[str]:
    """Fill empty active spots from the reserves and repair the lineup/staff."""
    players = league.players
    notes: list[str] = []
    if not _has_minimum_depth(league, team):
        notes.extend(fill_minimums(league, team))
    notes.extend(_fill_open_spots(league, team))
    notes.extend(_ensure_healthy_depth(league, team))
    if len(roster.active_hitters(team, players)) < len(rt.LINEUP_POSITIONS) or len(
        roster.active_pitchers(team, players)
    ) < roster.ROTATION_SIZE + 1:
        optimize_roster(league, team)
        notes.append(f"{team.abbr}: active roster reorganised to field a legal team.")
    notes.extend(roster.repair(team, players))
    return notes


HEALTHY_HITTERS_NEEDED = 10
HEALTHY_PITCHERS_NEEDED = 10


def _ensure_healthy_depth(league: League, team: Team) -> list[str]:
    """Move injured active players to the IL until enough healthy players are active."""
    players = league.players
    notes: list[str] = []
    for _ in range(12):
        hitters, pitchers = injuries.healthy_counts(team, players)
        need_hitter = hitters < HEALTHY_HITTERS_NEEDED
        need_pitcher = pitchers < HEALTHY_PITCHERS_NEEDED
        if not need_hitter and not need_pitcher:
            break
        hurt = [
            players[pid] for pid in team.active
            if players[pid].is_injured and players[pid].is_pitcher == (not need_hitter)
        ]
        if not hurt:
            break
        worst = max(hurt, key=lambda p: (p.injury_days, p.id))
        notes.extend(injuries.place_on_il(team, players, worst.id))
        notes.append(f"{team.abbr} placed {worst.name} on the injured list ({worst.injury}).")
        league.log_transaction(notes[-1])
        if not any(not players[pid].is_injured and players[pid].is_pitcher == worst.is_pitcher
                   for pid in team.reserves):
            notes.extend(fill_minimums(league, team, force=("P" if worst.is_pitcher else "H")))
        notes.extend(_fill_open_spots(league, team, prefer_pitcher=worst.is_pitcher))
    return notes


def _fill_open_spots(league: League, team: Team, prefer_pitcher: bool | None = None) -> list[str]:
    players = league.players
    notes: list[str] = []
    while len(team.active) < roster.MAX_ACTIVE and team.reserves:
        pitchers = len(roster.active_pitchers(team, players))
        hitters = len(team.active) - pitchers
        reserves = [p for p in roster.reserve_players(team, players) if not p.is_injured]
        if not reserves:
            break
        want_pitcher = pitchers < roster.MAX_ACTIVE_PITCHERS and (
            hitters >= roster.MAX_ACTIVE - roster.MAX_ACTIVE_PITCHERS or pitchers < 11
        )
        if prefer_pitcher is not None:
            want_pitcher = prefer_pitcher and pitchers < roster.MAX_ACTIVE_PITCHERS
            prefer_pitcher = None
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
    return notes


def _has_minimum_depth(league: League, team: Team) -> bool:
    members = [league.players[pid] for pid in [*team.active, *team.reserves]]
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


def fill_minimums(league: League, team: Team, force: str | None = None) -> list[str]:
    """Guarantee 13 pitchers, 13 hitters and 2 catchers in the organisation.

    Signs the best minimum-salary free agent of the needed type, or a
    replacement-level player if the market is empty.
    """
    from mlb_manager import freeagency  # local import: freeagency depends on this module

    notes: list[str] = []
    for _ in range(40):
        members = [league.players[pid] for pid in [*team.active, *team.reserves]]
        pitchers = sum(p.is_pitcher for p in members)
        catchers = sum(p.can_play("C") for p in members)
        hitters = len(members) - pitchers
        if force is not None:
            need, force = force, None
        elif catchers < MIN_ORG_CATCHERS:
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
        minimum = finance.min_salary(league)
        finance.check_transaction(league, team, finance.KIND_MINIMUM, minimum, salary=minimum)  # never blocks
        contracts.sign(player, team.abbr, minimum, 1, minimum)
        team.reserves.append(player.id)
        notes.append(f"{team.abbr} signed {player.name} ({player.position}) for organisational depth.")
        league.log_transaction(notes[-1])
    return notes


def release_player(league: League, team: Team, player_id: int) -> str:
    player = league.players[player_id]
    if player_id not in team.all_players:
        raise roster.RosterError(f"{player.name} is not in your organisation.")
    league.note_departure(player)
    team.remove_player(player_id)
    player.rehab = False
    if player.contract_years > 0:
        team.dead_money = round(team.dead_money + player.salary, 2)
    player.team = None
    player.contract_years = 0
    message = f"{team.abbr} released {player.name}."
    league.log_transaction(message)
    return message


def cutdown(league: League, team: Team) -> list[str]:
    """Release the least valuable reserves until the organisation fits MAX_ORG."""
    from mlb_manager import valuation

    notes = []
    while len(team.all_players) > MAX_ORG and team.reserves:
        val = valuation.Valuator(league)
        worst = min(
            (league.players[pid] for pid in team.reserves),
            key=lambda p: (val.player(p, team).total, p.id),
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
            minimum = finance.min_salary(league)
            if player.age <= 25:
                salary = minimum
                years = rng.randint(2, min(6, max(2, 31 - player.age)))
            else:
                salary = contracts.market_salary(player, minimum) * rng.uniform(0.8, 1.2)
                years = rng.randint(1, contracts.preferred_years(player) + 1)
                years = min(years, contracts.max_years(player))
            contracts.sign(player, player.team, salary, years, minimum, structure=False)
            player.contract_length = years + rng.randint(0, 2)
    if not any(p.team is None for p in league.players.values()):
        _seed_free_agents(league)
    if not league.draft_picks:
        for year in range(league.draft_year, league.draft_year + FUTURE_DRAFT_YEARS):
            league.draft_picks.extend(create_picks(list(league.teams), year))
    update_strategies(league)


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
