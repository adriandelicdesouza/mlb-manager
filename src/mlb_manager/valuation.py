"""Centralised, team-specific asset valuation for trades and roster decisions.

Every value is expressed in one currency: roughly "$ millions of surplus
value". A player's value to a club is built year by year over his expected
productive years:

``production_t``
    What his projected rating is worth on the field in season ``t`` (a
    replacement-level curve priced like the free-agent market, adjusted for
    positional scarcity, availability and durability). Projections grow young
    players toward the *evaluating club's scouted* potential (imperfect
    information) and age veterans downward; the current season blends in
    actual performance.
``cost_t``
    His cash salary while under contract, weighted by how much budget room
    means to the club in that season. Salaries enter the model divided by
    ``contracts.MARKET_SCALE`` so value units stay comparable across the
    v0.9 move to MLB-level pay (all tolerances are in value units).

The total is split into four transparent components:

* **asset**     – window-neutral production over the control years plus the
  (small) value of re-signing rights afterwards;
* **contract**  – minus the weighted salary owed;
* **strategic** – the club's competitive window (contenders weight this
  season heavily, rebuilders the future), pick-inventory effects;
* **fit**       – positional need, redundancy, essential-starter losses and
  roster-spot costs.

Draft picks are valued as the expected prospect at the projected slot.
Everything is deterministic and side-effect free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mlb_manager import contracts, development, finance, injuries
from mlb_manager import ratings as rt
from mlb_manager.draft import DraftPick
from mlb_manager.models import Player, Team

if TYPE_CHECKING:
    from mlb_manager.league import League

HORIZON = 6  # seasons projected
DISCOUNT = 0.88  # per future season
WIN_PRICE = 0.10  # $M per (rating points over replacement) ** CURVE
CURVE = 1.6
REPLACEMENT: dict[str, float] = {"SP": 44.0, "RP": 46.0}
REPLACEMENT_HITTER = 44.0
SCARCITY: dict[str, float] = {
    "C": 1.12, "SS": 1.10, "CF": 1.06, "2B": 1.03, "3B": 1.02, "RF": 1.0, "LF": 0.97,
    "1B": 0.93, "DH": 0.88, "SP": 1.0, "RP": 0.55,
}
GROWTH: dict[str, float] = {"fast": 0.40, "normal": 0.30, "slow": 0.20}  # share of gap closed per year
REALISATION = 0.85  # expected share of scouted upside that materialises (bust risk)
RESIGN_SHARE = 0.12  # value of re-signing rights after the contract ends (veterans)
RESIGN_SHARE_YOUNG = 0.35  # ... for players still 25 or younger when the deal ends (team control)
WINDOW: dict[str, tuple[float, ...]] = {
    "contending": (1.35, 1.10, 0.95, 0.90, 0.85, 0.80),
    "balanced": (1.0, 1.0, 1.0, 1.0, 1.0, 1.0),
    "rebuilding": (0.55, 0.75, 1.0, 1.10, 1.15, 1.15),
}
PICK_BASE: dict[int, float] = {1: 18.0, 2: 7.0, 3: 3.5, 4: 2.0, 5: 1.2}
PICK_STRATEGY: dict[str, float] = {"contending": 0.8, "balanced": 1.0, "rebuilding": 1.3}
ROSTER_SPOT_COST = 0.75  # a below-replacement veteran takes a roster spot
POSTSEASON_BONUS = 0.15  # contenders value a deadline acquisition's October games


@dataclass(slots=True)
class AssetValue:
    """A player's or pick's value to one club, by component."""

    asset: float = 0.0
    contract: float = 0.0
    strategic: float = 0.0
    fit: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def total(self) -> float:
        return self.asset + self.contract + self.strategic + self.fit

    def add(self, other: AssetValue) -> None:
        self.asset += other.asset
        self.contract += other.contract
        self.strategic += other.strategic
        self.fit += other.fit

    def scaled(self, factor: float) -> AssetValue:
        return AssetValue(self.asset * factor, self.contract * factor, self.strategic * factor,
                          self.fit * factor, list(self.notes))


# ----------------------------------------------------------------------------- pure helpers


def replacement_level(position: str) -> float:
    return REPLACEMENT.get(position, REPLACEMENT_HITTER)


def production_worth(rating: float, position: str) -> float:
    """$M a season of play at ``rating`` is worth (market-priced, replacement = 0)."""
    over = max(0.0, rating - replacement_level(position))
    return WIN_PRICE * over**CURVE * SCARCITY.get(position, 1.0)


def performance_rating(player: Player) -> tuple[float, float]:
    """``(rating implied by this season's stats, weight)``; weight 0 without a sample."""
    if player.is_pitcher:
        s = player.pitching
        if s.outs < 30:
            return 0.0, 0.0
        sample = s.innings / (180 if player.position == "SP" else 60)
        return 50 + (4.40 - s.era) * 5, min(0.3, sample * 0.3)
    b = player.batting
    if b.pa < 60:
        return 0.0, 0.0
    return 50 + (b.ops - 0.710) * 100, min(0.3, b.pa / 600 * 0.3)


def project(player: Player, potential: int, seasons: int = HORIZON) -> list[float]:
    """Expected overall rating for this season and the following ones."""
    ovr = float(player.overall)
    perf, weight = performance_rating(player)
    current = ovr * (1 - weight) + perf * weight if weight else ovr
    out = [current]
    peak = development.PEAK_AGE.get(player.development, 27)
    rate = GROWTH.get(player.development, 0.3)
    target = ovr + max(0.0, potential - ovr) * REALISATION
    rating = ovr
    for k in range(1, seasons):
        age = player.age + k
        if age <= peak:
            rating += (target - rating) * rate
        elif age <= 31:
            rating -= 0.3
        else:
            rating -= (age - 31) * 0.9 + 0.5
        blend = weight * 0.5 if k == 1 else 0.0
        out.append(rating * (1 - blend) + perf * blend if blend else rating)
    return out


# ----------------------------------------------------------------------------- club outlook


class TeamOutlook:
    """Everything about a club that changes how it values assets (computed once)."""

    def __init__(self, league: League, team: Team) -> None:
        self.league = league
        self.team = team
        players = league.players
        self.strategy = "balanced" if team.abbr == league.user_team else team.strategy
        self.window = WINDOW[self.strategy]
        org = [players[pid] for pid in team.all_players]
        self.hitters = [p for p in org if not p.is_pitcher]
        self.starter_at: dict[str, float] = {}
        for slot in team.lineup:
            if slot.player_id in players:
                self.starter_at[slot.position] = players[slot.player_id].overall_at(slot.position)
        rotation = sorted((players[pid].overall for pid in team.rotation if pid in players), reverse=True)
        self.fifth_starter = rotation[-1] if rotation else 40.0
        pen = sorted((p.overall for p in org if p.position == "RP"), reverse=True)
        self.pen_cut = pen[6] if len(pen) > 6 else (pen[-1] if pen else 40.0)
        self.catchers = sum(p.can_play("C") for p in org)
        self.starting_pitchers = sum(p.position == "SP" for p in org)
        self.org_size = len(team.all_players)
        self.commitments = contracts.commitments(team, players, seasons=HORIZON)
        self.limit = finance.payroll_budget(league, team)  # owners' payroll + tax budget (rules-aware)
        picks_next = [p for p in league.draft_picks if p.owner == team.abbr and p.year == league.draft_year]
        self.pick_inventory = len(picks_next)
        self._depth_cache: dict[str, list[tuple[float, int]]] = {}

    def cap_weight(self, season: int) -> float:
        """How much a salary dollar hurts in ``season`` (0 = now)."""
        committed = self.commitments[min(season, len(self.commitments) - 1)]
        space = (self.limit - committed) / contracts.MARKET_SCALE
        if space < 5:
            weight = 1.15
        elif space < 15:
            weight = 1.0
        elif space > 35:
            weight = 0.7
        else:
            weight = 0.85
        if self.strategy == "rebuilding":
            weight += 0.1
        return weight

    def depth_at(self, position: str) -> list[tuple[float, int]]:
        """Org ratings at a hitting position, best first: ``[(rating, pid), ...]``."""
        if position not in self._depth_cache:
            rows = [(p.overall_at(position), p.id) for p in self.hitters
                    if p.can_play(position) and p.injury_days < 30]
            self._depth_cache[position] = sorted(rows, reverse=True)
        return self._depth_cache[position]


# ----------------------------------------------------------------------------- valuator


class Valuator:
    """Caches club outlooks and per-club player values for one decision pass.

    Create a new one whenever rosters change (values depend on rosters).
    """

    def __init__(self, league: League) -> None:
        self.league = league
        self._outlooks: dict[str, TeamOutlook] = {}
        self._values: dict[tuple[int, str], AssetValue] = {}
        self._ranks: dict[str, int] | None = None

    def outlook(self, team: Team) -> TeamOutlook:
        if team.abbr not in self._outlooks:
            self._outlooks[team.abbr] = TeamOutlook(self.league, team)
        return self._outlooks[team.abbr]

    # -- season timing -------------------------------------------------------------

    def season_share(self) -> float:
        """Share of the current season still to play (1.0 in the offseason)."""
        lg = self.league
        if lg.phase == "regular":
            return max(0.0, (lg.last_regular_day - lg.day + 1) / (lg.last_regular_day + 1))
        if lg.phase in ("postseason", "complete"):
            return 0.0
        return 1.0

    # -- players ---------------------------------------------------------------------

    def player(self, player: Player, team: Team) -> AssetValue:
        key = (player.id, team.abbr)
        if key not in self._values:
            self._values[key] = self._player_value(player, team)
        return self._values[key]

    def _player_value(self, player: Player, team: Team) -> AssetValue:
        lg = self.league
        out = self.outlook(team)
        potential = development.scouted_potential(player, team.abbr, lg.seed, lg.year)
        ratings = project(player, potential)
        share = self.season_share()
        if player.team is None:  # free agent: value him on his asking contract
            salary, control = contracts.asking_contract(player, lg.seed, minimum=finance.min_salary(lg))
            cash = contracts.cash_schedule(salary, control, contracts.escalation_for(control))
        else:
            control = max(1, player.contract_years)
            cash = [contracts.salary_in(player, t) for t in range(control)]
        durability = injuries.durability(player, lg.seed)
        health = {"durable": 1.03, "normal": 1.0, "injury-prone": 0.92}[durability]
        health *= 1 - 0.04 * min(3, injuries.recent_serious_injuries(player, lg.year))
        incoming = player.team != team.abbr
        fit = self._fit(player, team, ratings[0]) if incoming else self._essential(player, team)
        window = list(out.window)
        if out.strategy == "rebuilding":
            # A rebuilding club counts a young player's production as part of its future.
            floor = 1.2 if player.age <= 25 else (0.85 if player.age <= 28 else 0.0)
            window = [max(w, floor) for w in window]
        value = AssetValue()
        for t, rating in enumerate(ratings):
            disc = DISCOUNT**t
            worth = production_worth(rating, player.position) * health
            if t == 0:
                availability = share
                if player.injury_days and share > 0:
                    remaining_days = max(1, (lg.last_regular_day - lg.day + 1) if lg.phase == "regular" else 180)
                    availability *= max(0.0, 1 - player.injury_days / remaining_days)
                if out.strategy == "contending" and lg.phase == "regular":
                    availability += POSTSEASON_BONUS * (1 if player.injury_days < 30 else 0)
                worth *= availability
            if t < control:
                base = worth * disc
                cost = cash[t] / contracts.MARKET_SCALE * (share if t == 0 else 1.0) * out.cap_weight(t) * disc
                value.contract -= cost
            else:
                young = player.age + control <= 25
                base = worth * disc * (RESIGN_SHARE_YOUNG if young else RESIGN_SHARE)
            value.asset += base
            value.strategic += base * (window[t] - 1)
            value.fit += base * window[t] * (fit - 1)
        if incoming and ratings[0] < replacement_level(player.position) - 2 and potential < 55:
            value.fit -= ROSTER_SPOT_COST
            value.notes.append("roster filler")
        return value

    def _fit(self, player: Player, team: Team, rating_now: float) -> float:
        """Positional need multiplier for a player joining ``team``."""
        out = self.outlook(team)
        shift = rating_now - player.overall
        if player.is_pitcher:
            if player.position == "SP":
                gain = rating_now - out.fifth_starter
                better = sum(1 for p in team.all_players
                             if self.league.players[p].position == "SP" and self.league.players[p].overall >= rating_now)
                crowded = 6
            else:
                gain = rating_now - out.pen_cut
                better = sum(1 for p in team.all_players
                             if self.league.players[p].position == "RP" and self.league.players[p].overall >= rating_now)
                crowded = 9
        else:
            gains = [player.overall_at(pos) + shift - out.starter_at.get(pos, 40.0)
                     for pos in [*player.eligible_positions, "DH"]]
            gain = max(gains)
            better = sum(1 for rating, _ in out.depth_at(player.position) if rating >= rating_now)
            crowded = 2
        if gain >= 2:
            return 1 + min(0.25, gain * 0.025)
        if player.age <= 24 and player.potential > player.overall + 5:
            return 1.0  # prospects: future fit matters little today
        return 0.6 if better >= crowded else 0.85

    def _essential(self, player: Player, team: Team) -> float:
        """Extra loss when a club gives up a regular it cannot replace internally."""
        out = self.outlook(team)
        players = self.league.players
        if player.is_pitcher:
            if player.id == team.closer:
                spare = [players[pid].overall for pid in team.active if players[pid].is_pitcher
                         and pid != player.id and pid not in team.rotation and not players[pid].is_injured]
                gap = player.overall - (max(spare) if spare else 40)
                return 1 + min(0.15, max(0.0, gap - 3) * 0.02)
            if player.id not in team.rotation:
                return 1.0
            spare = [players[pid].overall for pid in team.all_players
                     if players[pid].position == "SP" and pid not in team.rotation and not players[pid].is_injured]
            gap = player.overall - (max(spare) if spare else 40)
        else:
            slot = next((s for s in team.lineup if s.player_id == player.id), None)
            if slot is None:
                return 1.0
            spare = [r for r, pid in out.depth_at(slot.position) if pid != player.id]
            gap = player.overall_at(slot.position) - (spare[0] if spare else 40)
        return 1 + min(0.2, max(0.0, gap - 3) * 0.02)

    # -- picks -----------------------------------------------------------------------

    def ranks(self) -> dict[str, int]:
        if self._ranks is None:
            from mlb_manager import frontoffice

            self._ranks = frontoffice.strength_ranks(self.league)
        return self._ranks

    def pick(self, pick: DraftPick, team: Team) -> AssetValue:
        lg = self.league
        out = self.outlook(team)
        draft = lg.draft
        if draft is not None and draft.year == pick.year and pick.original in draft.order:
            order = draft.first_round if pick.round == 1 and draft.first_round else draft.order
            slot = order.index(pick.original)
        else:
            slot = self.ranks()[pick.original]
        slot_factor = 1.45 - 0.75 * slot / max(1, len(lg.teams) - 1)
        years_out = max(0, pick.year - lg.draft_year)
        base = PICK_BASE[pick.round] * slot_factor * (0.9**years_out)
        strategic = base * (PICK_STRATEGY[out.strategy] - 1)
        fit = 0.0
        if pick.year == lg.draft_year:
            if out.pick_inventory < 4:
                fit = base * 0.1
            elif out.pick_inventory > 6:
                fit = -base * 0.1
        return AssetValue(asset=base, strategic=strategic, fit=fit)


# ----------------------------------------------------------------------------- convenience


def player_value(league: League, player: Player, team: Team) -> float:
    return Valuator(league).player(player, team).total


def pick_value(league: League, pick: DraftPick, team: Team) -> float:
    return Valuator(league).pick(pick, team).total


def is_hitter_position(position: str) -> bool:
    return position not in rt.PITCHER_POSITIONS
