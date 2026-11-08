"""Trades between clubs: proposals, evaluation, negotiation and CPU behaviour.

All valuation comes from :mod:`mlb_manager.valuation` (team-specific, in one
currency). This module turns values into decisions:

* **Evaluation** – each side's incoming and outgoing assets are summed by
  component (asset, contract, strategic, roster fit). Incoming pieces have
  diminishing returns, giving up the best asset for lesser pieces needs a
  premium, and clubs require a margin set by :class:`NegotiationTolerance`.
  The verdict is *acceptable*, *borderline* or *unacceptable*.
* **Counteroffers** – sized to the actual shortfall: drop unwanted pieces,
  ask for the smallest non-essential, non-redundant asset that closes the
  gap, or hold back one of the CPU's own assets. Offers far from acceptable
  get a plain refusal instead of an absurd counter.
* **Anti-exploit** – identical evaluations for identical situations; repeated
  identical offers are refused; repeated rejected offers to the same club in
  a short span raise its asking margin; CPU clubs keep minimum depth at
  catcher and starting pitcher; salary and contract length are part of value.
* **CPU trading** – need-driven: contenders fix weak spots, rebuilders sell
  veterans, prospect-rich clubs buy MLB help; a player is not flipped twice
  in a season and each club makes at most a few trades per season.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mlb_manager import contracts, finance, frontoffice
from mlb_manager.draft import DraftPick
from mlb_manager.injuries import IL_THRESHOLD_DAYS
from mlb_manager.models import Player, Team
from mlb_manager.valuation import AssetValue, Valuator

if TYPE_CHECKING:
    from mlb_manager.league import League

MAX_ASSETS_PER_SIDE = 5
DIMINISHING: tuple[float, ...] = (1.0, 0.8, 0.6, 0.45, 0.35)
DEADLINE_FRACTION = 0.65
OFFER_LIFETIME_DAYS = 7
USER_OFFER_APPEAL = 0.92  # CPU offers give the user at least this share of the target's value
MIN_ORG_AFTER_TRADE = 10  # pitchers and hitters each
CPU_MIN_CATCHERS = 2
CPU_MIN_STARTERS = 5
CPU_TRADES_PER_SEASON = 3
FLIP_PROTECTION_DAYS = 45  # CPU clubs do not re-trade a player acquired this recently
MEMORY_LIMIT = 200
TRADE_LOG_LIMIT = 300
FATIGUE_WINDOW_DAYS = 7

ACCEPTABLE, BORDERLINE, UNACCEPTABLE = "acceptable", "borderline", "unacceptable"


@dataclass(frozen=True, slots=True)
class NegotiationTolerance:
    """How demanding CPU clubs are. All values are shares unless noted."""

    accept_margin: float = 0.04  # premium over the value given up
    accept_flat: float = 1.0  # flat premium ($M-equivalent)
    borderline_band: float = 0.15  # within this share of the requirement = borderline
    hopeless_ratio: float = 0.5  # below this share of the requirement: no counteroffer
    star_premium: float = 0.12  # extra margin for giving up the best asset for lesser pieces
    fatigue_step: float = 0.03  # extra margin per recently rejected offer to the same club
    fatigue_max: float = 0.12
    counter_overshoot: float = 1.75  # a counter may ask for at most this x the shortfall (plus 4)
    talent_floor: float = 0.2  # giving up real production requires this share of it back as talent


DEFAULT_TOLERANCE = NegotiationTolerance()


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

    def key(self) -> str:
        """Canonical identity of the proposal (order of assets does not matter)."""
        return "|".join([
            self.team_a, self.team_b,
            ",".join(map(str, sorted(self.a_players))), ",".join(sorted(self.a_picks)),
            ",".join(map(str, sorted(self.b_players))), ",".join(sorted(self.b_picks)),
        ])

    def asset_count(self) -> int:
        return len(self.a_players) + len(self.a_picks) + len(self.b_players) + len(self.b_picks)

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
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"offer_id": self.offer_id, "proposal": self.proposal.to_dict(),
                "expires_day": self.expires_day, "reason": self.reason}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TradeOffer:
        return cls(int(data["offer_id"]), TradeProposal.from_dict(data["proposal"]),
                   int(data["expires_day"]), data.get("reason", ""))


@dataclass(slots=True)
class SideAssessment:
    """One club's view of a trade, by component (incoming minus outgoing)."""

    team: str
    incoming: AssetValue
    outgoing: AssetValue
    value_in: float  # after diminishing returns
    value_out: float
    required: float

    @property
    def net(self) -> float:
        return self.value_in - self.value_out

    def component(self, name: str) -> float:
        return getattr(self.incoming, name) - getattr(self.outgoing, name)


@dataclass(slots=True)
class TradeEvaluation:
    accepted: bool
    value_in: float
    value_out: float
    message: str
    verdict: str = UNACCEPTABLE
    required: float = 0.0
    reasons: list[str] = field(default_factory=list)
    side: SideAssessment | None = None

    @property
    def ratio(self) -> float:
        return self.value_in / self.value_out if self.value_out > 0 else float("inf")

    @property
    def shortfall(self) -> float:
        return max(0.0, self.required - self.value_in)

    @property
    def hopeless(self) -> bool:
        return self.verdict == UNACCEPTABLE and "hopeless" in self.reasons


@dataclass(slots=True)
class Negotiation:
    """Outcome of submitting a proposal to a CPU club."""

    evaluation: TradeEvaluation | None
    errors: list[str] = field(default_factory=list)
    counter: TradeProposal | None = None
    counter_note: str = ""
    repeated: bool = False

    @property
    def accepted(self) -> bool:
        return self.evaluation is not None and self.evaluation.accepted and not self.errors


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
        check = financial_check(league, prop, abbr)
        if not check.allowed:
            errors.append(check.reason)
    return errors


def salary_flow(league: League, prop: TradeProposal, abbr: str) -> tuple[float, float, float, float]:
    """``(outgoing cash, incoming cash, outgoing AAV, incoming AAV)`` for ``abbr`` this season."""
    other = prop.team_b if abbr == prop.team_a else prop.team_a
    out_ids, _ = prop.assets_of(abbr)
    in_ids, _ = prop.assets_of(other)
    players = league.players
    return (sum(players[p].salary for p in out_ids), sum(players[p].salary for p in in_ids),
            sum(contracts.aav(players[p]) for p in out_ids), sum(contracts.aav(players[p]) for p in in_ids))


def financial_check(league: League, prop: TradeProposal, abbr: str) -> finance.TransactionCheck:
    """The league's financial rules (cap, exceptions, CBT) applied to one side of a trade."""
    out_cash, in_cash, out_aav, in_aav = salary_flow(league, prop, abbr)
    return finance.check_transaction(league, league.teams[abbr], finance.KIND_TRADE, round(in_cash - out_cash, 4),
                                     outgoing=out_cash, incoming=in_cash, aav_added=round(in_aav - out_aav, 4))


def depth_problem(league: League, team: Team, out_ids: list[int], in_ids: list[int]) -> str | None:
    """CPU clubs never strip themselves below basic depth at C and SP."""
    remaining = [league.players[p] for p in team.all_players if p not in out_ids]
    remaining += [league.players[p] for p in in_ids]
    if sum(p.can_play("C") for p in remaining) < CPU_MIN_CATCHERS:
        return "We can't give up our catching depth."
    if sum(p.position == "SP" for p in remaining) < CPU_MIN_STARTERS:
        return "We can't give up our starting pitching depth."
    return None


# ----------------------------------------------------------------------------- valuation


def _asset_list(val: Valuator, league: League, team: Team, pids: list[int],
                picks: list[str]) -> list[AssetValue]:
    values = [val.player(league.players[pid], team) for pid in pids]
    for pick_id in picks:
        pick = get_pick(league, pick_id)
        if pick is not None:
            values.append(val.pick(pick, team))
    return values


def asset_values(league: League, team: Team, pids: list[int], picks: list[str],
                 val: Valuator | None = None) -> list[float]:
    """Total value to ``team`` of each listed asset (players first, then picks)."""
    return [v.total for v in _asset_list(val or Valuator(league), league, team, pids, picks)]


def incoming_total(values: list[float]) -> float:
    """Diminishing returns: a pile of lesser pieces never adds up to a star."""
    positives = sorted((v for v in values if v > 0), reverse=True)
    total = sum(v * DIMINISHING[min(i, len(DIMINISHING) - 1)] for i, v in enumerate(positives))
    return total + sum(v for v in values if v <= 0)


def _incoming_breakdown(values: list[AssetValue]) -> AssetValue:
    out = AssetValue()
    ordered = sorted(values, key=lambda v: -v.total)
    rank = 0
    for value in ordered:
        if value.total > 0:
            out.add(value.scaled(DIMINISHING[min(rank, len(DIMINISHING) - 1)]))
            rank += 1
        else:
            out.add(value)
    return out


def fatigue_margin(league: League, cpu: str, tolerance: NegotiationTolerance = DEFAULT_TOLERANCE) -> float:
    """Extra margin after several rejected user offers to the same club this week."""
    recent = sum(
        1 for m in league.trade_memory
        if m["team"] == cpu and not m["accepted"] and m["year"] == league.year
        and league.day - m["day"] < FATIGUE_WINDOW_DAYS
    )
    return min(tolerance.fatigue_max, recent * tolerance.fatigue_step)


def evaluate(league: League, prop: TradeProposal, evaluator: str | None = None,
             val: Valuator | None = None, tolerance: NegotiationTolerance = DEFAULT_TOLERANCE,
             extra_margin: float = 0.0) -> TradeEvaluation:
    """Judge the trade from ``evaluator``'s side (default: the receiving club)."""
    evaluator = evaluator or prop.team_b
    other = prop.team_a if evaluator == prop.team_b else prop.team_b
    team = league.teams[evaluator]
    val = val or Valuator(league)
    in_p, in_k = prop.assets_of(other)
    out_p, out_k = prop.assets_of(evaluator)
    incoming_list = _asset_list(val, league, team, in_p, in_k)
    outgoing_list = _asset_list(val, league, team, out_p, out_k)
    incoming = _incoming_breakdown(incoming_list)
    outgoing = AssetValue()
    for item in outgoing_list:
        outgoing.add(item)
    value_in, value_out = incoming.total, outgoing.total

    margin = tolerance.accept_margin + extra_margin
    reasons: list[str] = []
    best_out = max((v.total for v in outgoing_list), default=0.0)
    best_in = max((v.total for v in incoming_list), default=0.0)
    if best_out >= 10 and best_out > 2 * max(best_in, 0.1) and len(incoming_list) > 1:
        margin += tolerance.star_premium
        reasons.append("consolidation")
    # Required value: what we give up, plus a margin on its positive part and a flat
    # premium. Shedding a negative asset (a salary dump) lowers the bar accordingly.
    required = value_out + max(value_out, 0.0) * margin + tolerance.accept_flat
    side = SideAssessment(evaluator, incoming, outgoing, value_in, value_out, required)

    if evaluator != league.user_team:
        problem = depth_problem(league, team, out_p, in_p)
        if problem:
            return TradeEvaluation(False, value_in, value_out, f'{evaluator}: "{problem}"',
                                   UNACCEPTABLE, required, ["depth"], side)
        out_cash, in_cash, _, _ = salary_flow(league, prop, evaluator)
        if not finance.fits_budget(league, team, round(in_cash - out_cash, 4), finance.KIND_TRADE):
            return TradeEvaluation(False, value_in, value_out,
                                   f'{evaluator}: "That much salary doesn\'t fit our budget."',
                                   UNACCEPTABLE, required, ["budget"], side)
    talent_needed = tolerance.talent_floor * outgoing.asset if outgoing.asset >= 5 else 0.0
    if value_in >= required and incoming.asset >= talent_needed:
        return TradeEvaluation(True, value_in, value_out, f"{evaluator} accepts the trade.",
                               ACCEPTABLE, required, reasons, side)
    if value_in >= required:
        # The numbers work only as a salary dump: insist on some real talent back.
        reasons.append("talent")
        required = max(required, value_in + (talent_needed - incoming.asset))
        side.required = required
        return TradeEvaluation(False, value_in, value_out,
                               f'{evaluator}: "We need some real talent back, not just salary relief."',
                               BORDERLINE, required, reasons, side)
    # Classify by the size of the gap relative to the size of the deal.
    scale = max(abs(value_out) + tolerance.accept_flat, 4.0)
    shortfall = required - value_in
    reasons.extend(_qualitative_reasons(side, incoming_list, outgoing_list))
    if shortfall <= tolerance.borderline_band * scale:
        verdict = BORDERLINE
        msg = f'{evaluator}: "Close. Add a little more and we have a deal."'
    else:
        verdict = UNACCEPTABLE
        if shortfall > (1 - tolerance.hopeless_ratio) * scale:
            reasons.append("hopeless")
            msg = f'{evaluator}: "That isn\'t close to what we need. Not interested."'
        else:
            msg = f'{evaluator}: "We\'d need significantly more value back."'
    return TradeEvaluation(False, value_in, value_out, msg, verdict, required, reasons, side)


def _qualitative_reasons(side: SideAssessment, incoming: list[AssetValue],
                         outgoing: list[AssetValue]) -> list[str]:
    """Hints for the UI that explain a refusal without revealing numbers."""
    reasons: list[str] = []
    if side.incoming.contract < -1.5 and side.incoming.contract < side.outgoing.contract - 1.5:
        reasons.append("salary")
    if side.incoming.fit < 0 and any(v.fit < 0 for v in incoming):
        reasons.append("fit")
    if any(v.total <= 0 for v in incoming):
        reasons.append("unwanted pieces")
    if side.component("strategic") < -2:
        reasons.append("timeline")
    return reasons


REASON_TEXT: dict[str, str] = {
    "salary": "They are wary of the salary coming back.",
    "fit": "Some of what you offer doesn't fit their roster.",
    "unwanted pieces": "They see little value in part of your package.",
    "timeline": "The deal doesn't match their competitive timeline.",
    "consolidation": "Several lesser pieces don't add up to their best player.",
    "depth": "They won't strip their depth at a key position.",
    "talent": "Salary relief alone isn't enough; they want a real prospect or player back.",
    "budget": "Taking on that payroll (and any luxury tax) is beyond their owners' budget.",
}


def describe_reasons(evaluation: TradeEvaluation) -> list[str]:
    return [REASON_TEXT[r] for r in evaluation.reasons if r in REASON_TEXT]


def is_acceptable(league: League, prop: TradeProposal, evaluator: str, val: Valuator | None = None,
                  tolerance: NegotiationTolerance = DEFAULT_TOLERANCE, extra_margin: float = 0.0) -> bool:
    return not validate(league, prop) and evaluate(
        league, prop, evaluator, val, tolerance, extra_margin).accepted


# ----------------------------------------------------------------------------- negotiation


def _core_ids(val: Valuator, league: League, team: Team, count: int) -> set[int]:
    ranked = sorted(tradable_players(league, team), key=lambda p: (-val.player(p, team).total, p.id))
    return {p.id for p in ranked[:count]}


def _essential_to(val: Valuator, team: Team, player: Player) -> bool:
    return val._essential(player, team) > 1.04


def counter(league: League, prop: TradeProposal, val: Valuator | None = None,
            tolerance: NegotiationTolerance = DEFAULT_TOLERANCE, extra_margin: float = 0.0
            ) -> TradeProposal | None:
    """CPU (``team_b``) proposes the smallest change that makes the deal work.

    Returns None when the offer is acceptable already or too far away.
    """
    result = _counter(league, prop, val or Valuator(league), tolerance, extra_margin)
    return result[0] if result else None


def _counter(league: League, prop: TradeProposal, val: Valuator, tolerance: NegotiationTolerance,
             extra_margin: float) -> tuple[TradeProposal, str] | None:
    cpu, partner = league.teams[prop.team_b], league.teams[prop.team_a]
    ev = evaluate(league, prop, cpu.abbr, val, tolerance, extra_margin)
    if ev.accepted or ev.hopeless or "depth" in ev.reasons:
        return None

    def check(candidate: TradeProposal) -> TradeEvaluation | None:
        if validate(league, candidate):
            return None
        result = evaluate(league, candidate, cpu.abbr, val, tolerance, extra_margin)
        return result if result.accepted else None

    # Options are ranked by what the change costs the *proposer* (smallest request first),
    # then by how far past the requirement it lands.
    options: list[tuple[float, float, str, TradeProposal, str]] = []

    # 1) Drop pieces the CPU doesn't want (simplifies the deal and frees a roster spot).
    base = prop.copy()
    dropped = [pid for pid in prop.a_players if val.player(league.players[pid], cpu).total <= 0]
    for pid in dropped:
        base.a_players.remove(pid)
    if dropped and (base.a_players or base.a_picks):
        result = check(base)
        if result:
            names = ", ".join(league.players[pid].name for pid in dropped)
            return base, f"Keep {names}; the rest works for us."
    base_eval = evaluate(league, base, cpu.abbr, val, tolerance, extra_margin)
    shortfall = max(0.0, base_eval.required - base_eval.value_in)
    ceiling = shortfall * tolerance.counter_overshoot + 4.0
    keep_note = (f" Keep {', '.join(league.players[p].name for p in dropped)}." if dropped else "")

    # 2) Ask for one more asset: smallest non-essential, non-redundant piece that closes the gap.
    if len(base.a_players) + len(base.a_picks) < MAX_ASSETS_PER_SIDE:
        core = _core_ids(val, league, partner, 3)
        for player in tradable_players(league, partner):
            if player.id in base.a_players or player.id in core or _essential_to(val, partner, player):
                continue
            worth = val.player(player, cpu)
            if worth.total <= 0 or worth.total > ceiling + base_eval.required * 0.02:
                continue
            if val._fit(player, cpu, player.overall) < 0.7:
                continue  # redundant for us
            candidate = base.copy()
            candidate.a_players.append(player.id)
            result = check(candidate)
            if result:
                options.append((val.player(player, partner).total, result.value_in - result.required,
                                f"p{player.id}", candidate,
                                f"Add {player.name} ({player.position}) and we have a deal.{keep_note}"))
        for pick in sorted(league.draft_picks, key=lambda k: k.pick_id):
            if pick.owner != partner.abbr or pick.pick_id in base.a_picks:
                continue
            if val.pick(pick, cpu).total > ceiling:
                continue
            candidate = base.copy()
            candidate.a_picks.append(pick.pick_id)
            result = check(candidate)
            if result:
                options.append((val.pick(pick, partner).total, result.value_in - result.required,
                                f"k{pick.pick_id}", candidate,
                                f"Add your {pick.label()} pick and we have a deal.{keep_note}"))
    # 3) Or hold back one of the CPU's own assets.
    if len(base.b_players) + len(base.b_picks) > 1:
        for pid in base.b_players:
            candidate = base.copy()
            candidate.b_players.remove(pid)
            result = check(candidate)
            if result:
                options.append((val.player(league.players[pid], partner).total,
                                result.value_in - result.required, f"b{pid}", candidate,
                                f"We'll keep {league.players[pid].name}; the rest works.{keep_note}"))
        for pick_id in base.b_picks:
            candidate = base.copy()
            candidate.b_picks.remove(pick_id)
            result = check(candidate)
            if result:
                pick = get_pick(league, pick_id)
                options.append((val.pick(pick, partner).total if pick else 0.0,
                                result.value_in - result.required, f"c{pick_id}", candidate,
                                f"We'll keep our {pick.label() if pick else pick_id} pick.{keep_note}"))
    if not options:
        return None
    _, _, _, best, note = min(options, key=lambda o: (o[0], o[1], o[2]))
    return best, note


def negotiate(league: League, prop: TradeProposal,
              tolerance: NegotiationTolerance = DEFAULT_TOLERANCE) -> Negotiation:
    """Submit a user proposal (``team_a`` = user) to the CPU club ``team_b``.

    Records the outcome so identical repeat offers are refused outright and a
    stream of rejected offers makes the club a little more demanding.
    """
    errors = validate(league, prop)
    if errors:
        return Negotiation(None, errors)
    key = prop.key()
    if any(m["key"] == key and not m["accepted"] and m["year"] == league.year
           and league.day - m["day"] < FATIGUE_WINDOW_DAYS * 2 for m in league.trade_memory):
        ev = evaluate(league, prop, prop.team_b, tolerance=tolerance,
                      extra_margin=fatigue_margin(league, prop.team_b, tolerance))
        if not ev.accepted:
            ev.message = f'{prop.team_b}: "We already turned that offer down."'
            return Negotiation(ev, repeated=True)
    val = Valuator(league)
    extra = fatigue_margin(league, prop.team_b, tolerance)
    ev = evaluate(league, prop, prop.team_b, val, tolerance, extra)
    remember(league, prop, ev.accepted)
    if ev.accepted:
        return Negotiation(ev)
    result = _counter(league, prop, val, tolerance, extra)
    if result is None:
        return Negotiation(ev)
    return Negotiation(ev, counter=result[0], counter_note=result[1])


def remember(league: League, prop: TradeProposal, accepted: bool) -> None:
    league.trade_memory.append({"key": prop.key(), "team": prop.team_b, "year": league.year,
                                "day": league.day, "accepted": accepted})
    if len(league.trade_memory) > MEMORY_LIMIT:
        del league.trade_memory[: len(league.trade_memory) - MEMORY_LIMIT]


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


@dataclass(slots=True)
class ContractImpact:
    team: str
    payroll_before: float
    payroll_after: float
    cap_space_after: float | None  # None when the league has no salary cap
    future_change: list[float]  # change in committed salary for the next seasons
    years_in: int  # player-seasons of contract received
    years_out: int
    tax_before: float = 0.0  # projected Competitive Balance Tax
    tax_after: float = 0.0
    messages: list[str] = field(default_factory=list)  # rule violations and consequences

    @property
    def change(self) -> float:
        return round(self.payroll_after - self.payroll_before, 2)


def contract_impact(league: League, prop: TradeProposal) -> list[ContractImpact]:
    """Visible payroll consequences of a proposal for both clubs."""
    out = []
    for abbr, other in ((prop.team_a, prop.team_b), (prop.team_b, prop.team_a)):
        team = league.teams[abbr]
        out_ids, _ = prop.assets_of(abbr)
        in_ids, _ = prop.assets_of(other)
        before = contracts.payroll(team, league.players)
        delta = sum(league.players[p].salary for p in in_ids) - sum(league.players[p].salary for p in out_ids)
        future = []
        for offset in range(1, 4):
            change = sum(contracts.salary_in(league.players[p], offset) for p in in_ids
                         if league.players[p].contract_years > offset)
            change -= sum(contracts.salary_in(league.players[p], offset) for p in out_ids
                          if league.players[p].contract_years > offset)
            future.append(round(change, 2))
        room = finance.cap_room(league, team)
        _, _, out_aav, in_aav = salary_flow(league, prop, abbr)
        check = financial_check(league, prop, abbr)
        out.append(ContractImpact(
            abbr, before, round(before + delta, 2), None if room is None else round(room - delta, 2), future,
            sum(league.players[p].contract_years for p in in_ids),
            sum(league.players[p].contract_years for p in out_ids),
            finance.projected_tax(league, team).total,
            finance.projected_tax(league, team, round(in_aav - out_aav, 4)).total,
            check.messages(),
        ))
    return out


def execute(league: League, prop: TradeProposal, reason: str = "") -> list[str]:
    errors = validate(league, prop)
    if errors:
        raise TradeError("; ".join(errors))
    summary = describe(league, prop)
    for giver, receiver in ((prop.team_a, prop.team_b), (prop.team_b, prop.team_a)):
        give, take = league.teams[giver], league.teams[receiver]
        pids, picks = prop.assets_of(giver)
        for pid in pids:
            player = league.players[pid]
            league.note_departure(player)
            give.remove_player(pid)
            player.rehab = False
            if player.injury_days >= IL_THRESHOLD_DAYS:
                take.injured_list.append(pid)
            else:
                take.reserves.append(pid)
            player.team = receiver
        for pick_id in picks:
            pick = get_pick(league, pick_id)
            assert pick is not None
            pick.owner = receiver
    notes = [f"TRADE: {summary}" + (f" ({reason})." if reason else ".")]
    league.log_transaction(notes[0])
    league.trade_log.append({"year": league.year, "day": league.day, "teams": [prop.team_a, prop.team_b],
                             "players": [*prop.a_players, *prop.b_players]})
    if len(league.trade_log) > TRADE_LOG_LIMIT:
        del league.trade_log[: len(league.trade_log) - TRADE_LOG_LIMIT]
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


# ----------------------------------------------------------------------------- CPU needs


@dataclass(slots=True)
class Need:
    position: str  # lineup position, "SP" or "RP"
    severity: float  # rating points below the league norm
    reason: str


def _league_norms(league: League) -> dict[str, float]:
    """Average starter rating per lineup position, 5th starter and bullpen."""
    totals: dict[str, list[float]] = {}
    for team in league.teams.values():
        for slot in team.lineup:
            totals.setdefault(slot.position, []).append(league.players[slot.player_id].overall_at(slot.position))
        rotation = sorted((league.players[p].overall for p in team.rotation), reverse=True)
        if rotation:
            totals.setdefault("SP", []).append(rotation[-1])
        pen = sorted((league.players[p].overall for p in team.active
                      if league.players[p].is_pitcher and p not in team.rotation), reverse=True)[:7]
        if pen:
            totals.setdefault("RP", []).append(sum(pen) / len(pen))
    return {pos: sum(v) / len(v) for pos, v in totals.items()}


def team_needs(league: League, team: Team, norms: dict[str, float] | None = None) -> list[Need]:
    """Weak spots on a club's roster, most severe first."""
    norms = norms or _league_norms(league)
    players = league.players
    needs: list[Need] = []
    for slot in team.lineup:
        starter = players[slot.player_id]
        rating = starter.overall_at(slot.position)
        severity = norms.get(slot.position, 50) - rating
        reason = "weak starter"
        if starter.injury_days >= 30:
            severity += 6
            reason = f"{starter.name} injured"
        if severity >= 4:
            needs.append(Need(slot.position, severity, reason))
    rotation = sorted(players[p].overall for p in team.rotation)
    hurt_starters = [players[p] for p in team.rotation if players[p].injury_days >= 30]
    if rotation:
        severity = norms.get("SP", 50) - rotation[0] + 6 * len(hurt_starters)
        if severity >= 4:
            needs.append(Need("SP", severity, "rotation injuries" if hurt_starters else "thin rotation"))
    pen = sorted((players[p].overall for p in team.active
                  if players[p].is_pitcher and p not in team.rotation), reverse=True)[:7]
    if pen:
        severity = norms.get("RP", 50) - sum(pen) / len(pen)
        if severity >= 3:
            needs.append(Need("RP", severity * 1.5, "weak bullpen"))
    needs.sort(key=lambda n: (-n.severity, n.position))
    return needs


def fills(player: Player, need: Need) -> bool:
    if need.position in ("SP", "RP"):
        return player.position == need.position or (need.position == "RP" and player.position == "SP")
    return player.can_play(need.position) or (need.position == "DH" and not player.is_pitcher)


def _rating_at(player: Player, position: str) -> float:
    if position in ("SP", "RP"):
        return float(player.overall)
    return float(player.overall_at(position))


def _trades_this_season(league: League, abbr: str) -> int:
    return sum(1 for t in league.trade_log if t["year"] == league.year and abbr in t["teams"])


def recently_acquired(league: League, player_id: int) -> bool:
    return any(player_id in t["players"] and t["year"] == league.year
               and league.day - t["day"] < FLIP_PROTECTION_DAYS for t in league.trade_log)


# ----------------------------------------------------------------------------- CPU packages


def build_package(
    league: League, val: Valuator, giver: Team, receiver: Team, targets: list[int],
    tolerance: NegotiationTolerance = DEFAULT_TOLERANCE, appeal: float | None = None,
) -> TradeProposal | None:
    """``giver`` assembles assets for ``targets`` (owned by ``receiver``).

    The giver must accept the result under its own valuation. The receiver
    must accept it too (``appeal`` None, CPU-CPU) or get at least ``appeal`` x
    the targets' value back under its own valuation (offers to the user).
    Prefers assets the receiver values more than the giver ("surplus"), never
    the giver's core or essential players, and the smallest package that works.
    """
    core = _core_ids(val, league, giver, 4)
    target_value = sum(val.player(league.players[t], receiver).total for t in targets)
    if target_value <= 0:
        return None
    pool: list[tuple[float, str, str | int]] = []
    for p in tradable_players(league, giver):
        if p.id in core or _essential_to(val, giver, p) or recently_acquired(league, p.id):
            continue
        rv = val.player(p, receiver).total
        if rv <= 0.5:
            continue
        gv = val.player(p, giver).total
        pool.append((rv - gv, f"p{p.id:06d}", p.id))
    for pick in league.draft_picks:
        if pick.owner == giver.abbr:
            rv = val.pick(pick, receiver).total
            pool.append((rv - val.pick(pick, giver).total, f"k{pick.pick_id}", pick.pick_id))
    pool.sort(key=lambda a: (-a[0], a[1]))

    def receiver_ok(prop: TradeProposal) -> bool:
        if appeal is None:
            return evaluate(league, prop, receiver.abbr, val, tolerance).accepted
        received = incoming_total(asset_values(league, receiver, prop.a_players, prop.a_picks, val))
        return received >= appeal * target_value

    def legal(prop: TradeProposal) -> bool:
        if validate(league, prop):
            return False
        if depth_problem(league, giver, prop.a_players, prop.b_players):
            return False
        return appeal is not None or not depth_problem(league, receiver, prop.b_players, prop.a_players)

    def add(prop: TradeProposal, ident: str | int) -> TradeProposal:
        trial = prop.copy()
        (trial.a_players if isinstance(ident, int) else trial.a_picks).append(ident)  # type: ignore[arg-type]
        return trial

    start = TradeProposal(giver.abbr, receiver.abbr, b_players=list(targets))
    # Single asset first: the cheapest one that satisfies both clubs.
    singles = []
    for _, tag, ident in pool:
        trial = add(start, ident)
        if receiver_ok(trial) and evaluate(league, trial, giver.abbr, val, tolerance).accepted and legal(trial):
            cost = val.player(league.players[ident], giver).total if isinstance(ident, int) else \
                val.pick(get_pick(league, ident), giver).total  # type: ignore[arg-type]
            singles.append((cost, tag, trial))
    if singles:
        return min(singles, key=lambda s: (s[0], s[1]))[2]
    # Otherwise build up greedily from the best surplus assets (at most three).
    prop = start
    for _, _, ident in pool:
        if len(prop.a_players) + len(prop.a_picks) >= 3:
            break
        trial = add(prop, ident)
        if not evaluate(league, trial, giver.abbr, val, tolerance).accepted:
            continue
        prop = trial
        if receiver_ok(prop):
            return prop if legal(prop) else None
    return None


# ----------------------------------------------------------------------------- CPU behaviour


def generate_offer_for_user(league: League, rng: random.Random) -> TradeOffer | None:
    """A CPU club with a need asks for one of the user's players that fills it."""
    open_, _ = trade_window(league)
    if not open_:
        return None
    val = Valuator(league)
    user = league.user
    norms = _league_norms(league)
    cpu_teams = sorted(a for a in league.teams if a != league.user_team)
    cpu = league.teams[rng.choice(cpu_teams)]
    if _trades_this_season(league, cpu.abbr) >= CPU_TRADES_PER_SEASON:
        return None
    needs = team_needs(league, cpu, norms)
    pool = tradable_players(league, user)
    targets: list[tuple[float, Player, str]] = []
    if cpu.strategy == "rebuilding" or not needs:
        for p in pool:
            if p.age <= 25 and p.potential >= 60:
                targets.append((val.player(p, cpu).total, p, "is collecting young talent"))
    else:
        need = needs[0]
        label = {"SP": "a starting pitcher", "RP": "bullpen help"}.get(need.position, f"a {need.position}")
        for p in pool:
            if fills(p, need):
                gain = _rating_at(p, need.position) - (norms.get(need.position, 50) - need.severity)
                if gain >= 3:
                    targets.append((val.player(p, cpu).total, p, f"is looking for {label}"))
    targets = [t for t in targets if t[0] > 2]
    if not targets:
        return None
    targets.sort(key=lambda t: (-t[0], t[1].id))
    _, target, why = rng.choice(targets[:3])
    prop = build_package(league, val, cpu, user, [target.id], appeal=USER_OFFER_APPEAL)
    if prop is None:
        return None
    offer = TradeOffer(league.next_offer_id, prop, league.day + OFFER_LIFETIME_DAYS, f"{cpu.abbr} {why}.")
    league.next_offer_id += 1
    return offer


def _buyer_deals(league: League, val: Valuator, buyer: Team, norms: dict[str, float],
                 cpu: list[Team]) -> list[tuple[float, Team, Player, str]]:
    """Targets on other CPU clubs that fix the buyer's worst need."""
    out: list[tuple[float, Team, Player, str]] = []
    needs = team_needs(league, buyer, norms)
    if not needs:
        return out
    need = needs[0]
    current = norms.get(need.position, 50) - need.severity
    for seller in cpu:
        if seller.abbr == buyer.abbr or _trades_this_season(league, seller.abbr) >= CPU_TRADES_PER_SEASON:
            continue
        for p in tradable_players(league, seller):
            if not fills(p, need) or p.injury_days >= 15 or recently_acquired(league, p.id):
                continue
            gain = _rating_at(p, need.position) - current
            if gain < 3:
                continue
            if seller.strategy != "rebuilding" and _essential_to(val, seller, p):
                continue  # not available: a regular the seller cannot replace
            out.append((gain, seller, p, f"{buyer.abbr} addresses {need.reason} ({need.position})"))
    out.sort(key=lambda d: (-d[0], d[2].id))
    return out[:3]


def _seller_deals(league: League, val: Valuator, seller: Team, norms: dict[str, float],
                  cpu: list[Team]) -> list[tuple[float, Team, Player, str]]:
    """A rebuilding club shops a veteran to the contender that needs him most."""
    out: list[tuple[float, Team, Player, str]] = []
    vets = [p for p in tradable_players(league, seller)
            if p.age >= 29 and p.overall >= 56 and p.injury_days < 15 and not recently_acquired(league, p.id)]
    contenders = [t for t in cpu if t.strategy == "contending"
                  and _trades_this_season(league, t.abbr) < CPU_TRADES_PER_SEASON]
    for vet in vets:
        own = val.player(vet, seller).total
        for buyer in contenders:
            needs = team_needs(league, buyer, norms)
            match = next((n for n in needs if fills(vet, n)), None)
            if match is None:
                continue
            gain = val.player(vet, buyer).total - own
            if gain > 3:
                out.append((gain, buyer, vet, f"{seller.abbr} sells a veteran to {buyer.abbr}"))
    out.sort(key=lambda d: (-d[0], d[2].id))
    return out[:3]


def cpu_trade_round(league: League, rng: random.Random, attempts: int = 3) -> list[str]:
    """Need-driven CPU-CPU trades. At most ``attempts`` trades per round."""
    open_, _ = trade_window(league)
    if not open_:
        return []
    notes: list[str] = []
    cpu = sorted((t for t in league.teams.values() if t.abbr != league.user_team), key=lambda t: t.abbr)
    for _ in range(attempts):
        val = Valuator(league)
        norms = _league_norms(league)
        eligible = [t for t in cpu if _trades_this_season(league, t.abbr) < CPU_TRADES_PER_SEASON]
        buyers = [t for t in eligible if t.strategy == "contending"
                  or (t.strategy == "balanced" and league.phase == "regular")]
        sellers = [t for t in eligible if t.strategy == "rebuilding"]
        prospect_rich = [t for t in eligible if t.strategy != "rebuilding" and sum(
            1 for pid in t.reserves
            if league.players[pid].age <= 23 and league.players[pid].potential >= 60) >= 5]
        roles: list[tuple[str, Team]] = [("buy", t) for t in buyers] + [("sell", t) for t in sellers]
        roles += [("buy", t) for t in prospect_rich if t not in buyers]
        if not roles:
            break
        role, club = rng.choice(roles)
        if role == "buy":
            deals = [(gain, club, seller, target, why)
                     for gain, seller, target, why in _buyer_deals(league, val, club, norms, eligible)]
        else:
            deals = [(gain, buyer, club, target, why)
                     for gain, buyer, target, why in _seller_deals(league, val, club, norms, eligible)]
        for _, buyer, seller, target, why in deals:
            prop = build_package(league, val, buyer, seller, [target.id])
            if prop is not None:
                notes.extend(execute(league, prop, reason=why))
                break
    return notes
