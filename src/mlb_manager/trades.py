"""Trades between clubs: proposals, evaluation, counters, CPU offers.

Anti-exploit measures:

* Incoming assets are weighted with diminishing returns (1.0, 0.75, 0.55, ...)
  so a pile of fringe players never adds up to a star.
* Contract surplus/deficit is part of a player's value, so salary dumps
  must be paid for.
* CPU clubs require a small premium to say yes, and evaluations are
  deterministic: re-submitting the same offer gets the same answer.
* Cap legality, roster limits and a trade deadline are enforced.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mlb_manager import contracts, frontoffice
from mlb_manager.draft import DraftPick
from mlb_manager.injuries import IL_THRESHOLD_DAYS
from mlb_manager.models import Player, Team

if TYPE_CHECKING:
    from mlb_manager.league import League

MAX_ASSETS_PER_SIDE = 5
ACCEPT_MARGIN = 1.05
ACCEPT_FLAT = 5.0
DIMINISHING: tuple[float, ...] = (1.0, 0.75, 0.55, 0.4, 0.3)
DEADLINE_FRACTION = 0.65
OFFER_LIFETIME_DAYS = 7
USER_OFFER_APPEAL = 0.9  # CPU offers give the user at least 90% of the target's value
MIN_ORG_AFTER_TRADE = 10  # pitchers and hitters each


class TradeError(ValueError):
    """Raised when a proposal is invalid."""


@dataclass(slots=True)
class TradeProposal:
    team_a: str  # proposing club
    team_b: str  # receiving club
    a_players: list[int] = field(default_factory=list)
    a_picks: list[str] = field(default_factory=list)
    b_players: list[int] = field(default_factory=list)
    b_picks: list[str] = field(default_factory=list)

    def copy(self) -> TradeProposal:
        return TradeProposal(
            self.team_a, self.team_b, list(self.a_players), list(self.a_picks),
            list(self.b_players), list(self.b_picks),
        )

    def reversed(self) -> TradeProposal:
        return TradeProposal(
            self.team_b, self.team_a, list(self.b_players), list(self.b_picks),
            list(self.a_players), list(self.a_picks),
        )

    def assets_of(self, abbr: str) -> tuple[list[int], list[str]]:
        if abbr == self.team_a:
            return self.a_players, self.a_picks
        if abbr == self.team_b:
            return self.b_players, self.b_picks
        raise KeyError(abbr)

    def to_dict(self) -> dict[str, Any]:
        return {
            "team_a": self.team_a, "team_b": self.team_b,
            "a_players": list(self.a_players), "a_picks": list(self.a_picks),
            "b_players": list(self.b_players), "b_picks": list(self.b_picks),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TradeProposal:
        return cls(
            data["team_a"], data["team_b"],
            [int(x) for x in data["a_players"]], list(data["a_picks"]),
            [int(x) for x in data["b_players"]], list(data["b_picks"]),
        )


@dataclass(slots=True)
class TradeOffer:
    offer_id: int
    proposal: TradeProposal  # team_a = CPU club making the offer, team_b = user
    expires_day: int

    def to_dict(self) -> dict[str, Any]:
        return {"offer_id": self.offer_id, "proposal": self.proposal.to_dict(), "expires_day": self.expires_day}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TradeOffer:
        return cls(int(data["offer_id"]), TradeProposal.from_dict(data["proposal"]), int(data["expires_day"]))


@dataclass(slots=True)
class TradeEvaluation:
    accepted: bool
    value_in: float
    value_out: float
    message: str

    @property
    def ratio(self) -> float:
        return self.value_in / self.value_out if self.value_out > 0 else float("inf")


# ----------------------------------------------------------------------------- rules


def deadline_day(league: League) -> int:
    return int(league.last_regular_day * DEADLINE_FRACTION)


def trade_window(league: League) -> tuple[bool, str]:
    if league.in_season:
        if league.phase != "regular":
            return False, "Trades are frozen during the postseason."
        if league.day > deadline_day(league):
            return False, "The trade deadline has passed. Trading reopens in the offseason."
        return True, f"Trade deadline: day {deadline_day(league) + 1}."
    if league.phase == "complete":
        return False, "Trading reopens when the offseason begins."
    return True, "Offseason trading is open."


def get_pick(league: League, pick_id: str) -> DraftPick | None:
    return next((p for p in league.draft_picks if p.pick_id == pick_id), None)


def tradable_players(league: League, team: Team) -> list[Player]:
    return [league.players[pid] for pid in team.all_players if league.players[pid].contract_years > 0]


def validate(league: League, prop: TradeProposal) -> list[str]:
    errors: list[str] = []
    open_, reason = trade_window(league)
    if not open_:
        errors.append(reason)
    if prop.team_a == prop.team_b or prop.team_a not in league.teams or prop.team_b not in league.teams:
        return errors + ["A trade needs two different clubs."]
    for abbr in (prop.team_a, prop.team_b):
        team = league.teams[abbr]
        pids, picks = prop.assets_of(abbr)
        if not pids and not picks:
            errors.append(f"{abbr} must include at least one player or pick.")
        if len(pids) + len(picks) > MAX_ASSETS_PER_SIDE:
            errors.append(f"At most {MAX_ASSETS_PER_SIDE} assets per side.")
        if len(set(pids)) != len(pids) or len(set(picks)) != len(picks):
            errors.append("An asset is listed twice.")
        for pid in pids:
            player = league.players.get(pid)
            if player is None or pid not in team.all_players:
                errors.append(f"Player #{pid} does not belong to {abbr}.")
            elif player.contract_years <= 0:
                errors.append(f"{player.name} is a pending free agent and cannot be traded.")
        for pick_id in picks:
            pick = get_pick(league, pick_id)
            if pick is None or pick.owner != abbr:
                errors.append(f"Pick {pick_id} is not owned by {abbr}.")
    if errors:
        return errors

    for abbr, other in ((prop.team_a, prop.team_b), (prop.team_b, prop.team_a)):
        team = league.teams[abbr]
        out_ids, _ = prop.assets_of(abbr)
        in_ids, _ = prop.assets_of(other)
        size_after = len(team.all_players) - len(out_ids) + len(in_ids)
        if abbr == league.user_team and size_after > frontoffice.MAX_ORG:
            errors.append(
                f"Your organisation would have {size_after} players (max {frontoffice.MAX_ORG}). "
                "Release someone first."
            )
        remaining = [league.players[p] for p in team.all_players if p not in out_ids]
        incoming = [league.players[p] for p in in_ids]
        pitchers = sum(p.is_pitcher for p in remaining + incoming)
        hitters = len(remaining) + len(incoming) - pitchers
        if pitchers < MIN_ORG_AFTER_TRADE or hitters < MIN_ORG_AFTER_TRADE:
            errors.append(f"{abbr} would be left without enough pitchers or position players.")
        before = contracts.payroll(team, league.players)
        after = before - sum(league.players[p].salary for p in out_ids) + sum(
            league.players[p].salary for p in in_ids
        )
        if not contracts.fits_under_cap(before, after, league.salary_cap):
            errors.append(
                f"{abbr} would be over the salary cap ({contracts.money(after)} > "
                f"{contracts.money(league.salary_cap)})."
            )
    return errors


# ----------------------------------------------------------------------------- valuation


def asset_values(league: League, team: Team, pids: list[int], picks: list[str],
                 ranks: dict[str, int] | None = None) -> list[float]:
    ranks = ranks or frontoffice.strength_ranks(league)
    values = [frontoffice.player_value(league, league.players[pid], team) for pid in pids]
    for pick_id in picks:
        pick = get_pick(league, pick_id)
        if pick is not None:
            values.append(frontoffice.pick_value(league, pick, team, ranks))
    return values


def incoming_total(values: list[float]) -> float:
    positives = sorted((v for v in values if v > 0), reverse=True)
    total = sum(v * DIMINISHING[min(i, len(DIMINISHING) - 1)] for i, v in enumerate(positives))
    return total + sum(v for v in values if v <= 0)


def evaluate(league: League, prop: TradeProposal, evaluator: str | None = None,
             ranks: dict[str, int] | None = None) -> TradeEvaluation:
    """Judge the trade from ``evaluator``'s side (default: the receiving club)."""
    evaluator = evaluator or prop.team_b
    other = prop.team_a if evaluator == prop.team_b else prop.team_b
    team = league.teams[evaluator]
    ranks = ranks or frontoffice.strength_ranks(league)
    in_p, in_k = prop.assets_of(other)
    out_p, out_k = prop.assets_of(evaluator)
    value_in = incoming_total(asset_values(league, team, in_p, in_k, ranks))
    value_out = sum(asset_values(league, team, out_p, out_k, ranks))
    required = value_out * ACCEPT_MARGIN + ACCEPT_FLAT
    if value_in >= required:
        return TradeEvaluation(True, value_in, value_out, f"{evaluator} accepts the trade.")
    ratio = value_in / required if required > 0 else 0.0
    if ratio >= 0.85:
        msg = f"{evaluator}: \"Close. Add a little more and we have a deal.\""
    elif ratio >= 0.6:
        msg = f"{evaluator}: \"We'd need significantly more value back.\""
    else:
        msg = f"{evaluator}: \"Not interested.\""
    return TradeEvaluation(False, value_in, value_out, msg)


def is_acceptable(league: League, prop: TradeProposal, evaluator: str,
                  ranks: dict[str, int] | None = None) -> bool:
    return not validate(league, prop) and evaluate(league, prop, evaluator, ranks).accepted


def counter(league: League, prop: TradeProposal) -> TradeProposal | None:
    """CPU (``team_b``) proposes the smallest change that makes the deal work."""
    ranks = frontoffice.strength_ranks(league)
    partner = league.teams[prop.team_a]
    options: list[tuple[float, TradeProposal]] = []
    # 1) ask for one more asset from the proposer (cheapest that works)
    if len(prop.a_players) + len(prop.a_picks) < MAX_ASSETS_PER_SIDE:
        for player in tradable_players(league, partner):
            if player.id in prop.a_players:
                continue
            candidate = prop.copy()
            candidate.a_players.append(player.id)
            if is_acceptable(league, candidate, prop.team_b, ranks):
                options.append((frontoffice.player_value(league, player, partner), candidate))
        for pick in league.draft_picks:
            if pick.owner == partner.abbr and pick.pick_id not in prop.a_picks:
                candidate = prop.copy()
                candidate.a_picks.append(pick.pick_id)
                if is_acceptable(league, candidate, prop.team_b, ranks):
                    options.append((frontoffice.pick_value(league, pick, partner, ranks), candidate))
    # 2) or hold back one of the CPU's own assets
    if len(prop.b_players) + len(prop.b_picks) > 1:
        for pid in prop.b_players:
            candidate = prop.copy()
            candidate.b_players.remove(pid)
            if is_acceptable(league, candidate, prop.team_b, ranks):
                options.append((frontoffice.player_value(league, league.players[pid], partner), candidate))
        for pick_id in prop.b_picks:
            candidate = prop.copy()
            candidate.b_picks.remove(pick_id)
            pick = get_pick(league, pick_id)
            if pick and is_acceptable(league, candidate, prop.team_b, ranks):
                options.append((frontoffice.pick_value(league, pick, partner, ranks), candidate))
    if not options:
        return None
    return min(options, key=lambda item: item[0])[1]


# ----------------------------------------------------------------------------- execution


def describe(league: League, prop: TradeProposal) -> str:
    def side(abbr: str) -> str:
        pids, picks = prop.assets_of(abbr)
        parts = [
            f"{league.players[p].name} ({league.players[p].position}, {league.players[p].overall})"
            for p in pids
        ]
        for pick_id in picks:
            pick = get_pick(league, pick_id)
            parts.append(f"{pick.label()} pick" if pick else pick_id)
        return ", ".join(parts) or "nothing"

    return f"{prop.team_a} sends {side(prop.team_a)} to {prop.team_b} for {side(prop.team_b)}"


def execute(league: League, prop: TradeProposal) -> list[str]:
    errors = validate(league, prop)
    if errors:
        raise TradeError("; ".join(errors))
    summary = describe(league, prop)
    for giver, receiver in ((prop.team_a, prop.team_b), (prop.team_b, prop.team_a)):
        give, take = league.teams[giver], league.teams[receiver]
        pids, picks = prop.assets_of(giver)
        for pid in pids:
            give.remove_player(pid)
            player = league.players[pid]
            if player.injury_days >= IL_THRESHOLD_DAYS:
                take.injured_list.append(pid)
            else:
                take.reserves.append(pid)
            player.team = receiver
        for pick_id in picks:
            pick = get_pick(league, pick_id)
            assert pick is not None
            pick.owner = receiver
    notes = [f"TRADE: {summary}."]
    league.log_transaction(notes[0])
    for abbr in (prop.team_a, prop.team_b):
        team = league.teams[abbr]
        if abbr == league.user_team:
            notes.extend(frontoffice.ensure_active_roster(league, team))
        else:
            frontoffice.fill_minimums(league, team)
            frontoffice.optimize_roster(league, team)
            frontoffice.cutdown(league, team)
    league.trade_offers = [o for o in league.trade_offers if not _offer_stale(league, o)]
    return notes


def _offer_stale(league: League, offer: TradeOffer) -> bool:
    return bool(validate(league, offer.proposal)) or league.day > offer.expires_day


# ----------------------------------------------------------------------------- CPU behaviour


def _core_ids(league: League, team: Team, count: int) -> set[int]:
    ranked = sorted(
        tradable_players(league, team),
        key=lambda p: (-frontoffice.player_value(league, p, team), p.id),
    )
    return {p.id for p in ranked[:count]}


def _targets_for(league: League, cpu: Team, partner: Team) -> list[Player]:
    pool = tradable_players(league, partner)
    if cpu.strategy == "contending":
        pool = [p for p in pool if p.overall >= 58]
    elif cpu.strategy == "rebuilding":
        pool = [p for p in pool if p.age <= 25 and p.potential >= 60]
    pool.sort(key=lambda p: (-frontoffice.player_value(league, p, cpu), p.id))
    return pool[:3]


def _build_package(
    league: League, giver: Team, receiver: Team, target: Player, ranks: dict[str, int],
    appeal: float | None = None,
) -> TradeProposal | None:
    """``giver`` assembles assets for ``target`` (owned by ``receiver``).

    The giver must accept the result under its own valuation. The receiver
    must either accept it too (``appeal`` is None, used for CPU-CPU deals) or
    get at least ``appeal`` x the target's value back (offers to the user).
    """
    target_value = frontoffice.player_value(league, target, receiver)
    core = _core_ids(league, giver, 4)
    assets: list[tuple[str, int | str, float, float]] = []
    for p in tradable_players(league, giver):
        if p.id in core:
            continue
        assets.append(("p", p.id, frontoffice.player_value(league, p, giver),
                       frontoffice.player_value(league, p, receiver)))
    for pick in league.draft_picks:
        if pick.owner == giver.abbr:
            assets.append(("k", pick.pick_id, frontoffice.pick_value(league, pick, giver, ranks),
                           frontoffice.pick_value(league, pick, receiver, ranks)))
    assets = [a for a in assets if a[3] > 0]
    # Prefer assets the receiver values more than the giver does.
    assets.sort(key=lambda a: (-(a[3] / max(a[2], 1.0)), str(a[1])))
    prop = TradeProposal(giver.abbr, receiver.abbr, b_players=[target.id])
    for kind, ident, _, _ in assets:
        if len(prop.a_players) + len(prop.a_picks) >= 3:
            break
        trial = prop.copy()
        (trial.a_players if kind == "p" else trial.a_picks).append(ident)  # type: ignore[arg-type]
        if not evaluate(league, trial, giver.abbr, ranks).accepted:
            continue  # adding this asset would make the giver say no
        prop = trial
        if appeal is None:
            good = evaluate(league, prop, receiver.abbr, ranks).accepted
        else:
            received = incoming_total(asset_values(league, receiver, prop.a_players, prop.a_picks, ranks))
            good = received >= appeal * target_value
        if good:
            return prop if not validate(league, prop) else None
    return None


def generate_offer_for_user(league: League, rng: random.Random) -> TradeOffer | None:
    open_, _ = trade_window(league)
    if not open_:
        return None
    ranks = frontoffice.strength_ranks(league)
    user = league.user
    cpu_teams = sorted(a for a in league.teams if a != league.user_team)
    cpu = league.teams[rng.choice(cpu_teams)]
    targets = _targets_for(league, cpu, user)
    if not targets:
        return None
    target = rng.choice(targets)
    prop = _build_package(league, cpu, user, target, ranks, appeal=USER_OFFER_APPEAL)
    if prop is None:
        return None
    offer = TradeOffer(league.next_offer_id, prop, league.day + OFFER_LIFETIME_DAYS)
    league.next_offer_id += 1
    return offer


def cpu_trade_round(league: League, rng: random.Random, attempts: int = 3) -> list[str]:
    """Contenders buy veterans from rebuilders for prospects and picks."""
    open_, _ = trade_window(league)
    if not open_:
        return []
    notes: list[str] = []
    cpu = [t for t in league.teams.values() if t.abbr != league.user_team]
    buyers = sorted((t for t in cpu if t.strategy == "contending"), key=lambda t: t.abbr)
    sellers = sorted((t for t in cpu if t.strategy == "rebuilding"), key=lambda t: t.abbr)
    if not buyers or not sellers:
        return notes
    ranks = frontoffice.strength_ranks(league)
    for _ in range(attempts):
        buyer, seller = rng.choice(buyers), rng.choice(sellers)
        vets = [
            p for p in tradable_players(league, seller)
            if p.age >= 27 and p.overall >= 56
        ]
        if not vets:
            continue
        vets.sort(key=lambda p: (
            -(frontoffice.player_value(league, p, buyer) - frontoffice.player_value(league, p, seller)),
            p.id,
        ))
        prop = _build_package(league, buyer, seller, vets[0], ranks)
        if prop is None:
            continue
        notes.extend(execute(league, prop))
        ranks = frontoffice.strength_ranks(league)
    return notes
