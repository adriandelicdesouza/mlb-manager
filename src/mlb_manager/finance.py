"""League financial rules: salary cap and floor, Competitive Balance Tax, revenue sharing.

This module is the single source of truth for money rules. Every transaction
system (free agency, re-signing, extensions, arbitration, trades, the draft,
depth signings) asks :func:`check_transaction` whether a payroll change is
legal and what it costs; CPU clubs additionally ask :func:`fits_budget`.

Rules belong to each league save (``League.finance``) and are *season-aware*:
a :class:`FinanceConfig` is a list of :class:`RuleEra` entries ("from season X
use preset P"), and :meth:`FinanceConfig.rules_for` resolves the immutable
:class:`FinancialRules` that apply to one season. The *Standard MLB* preset
looks the season up in a table of real collective bargaining agreements;
*custom* eras carry their own rules. Adding a future CBA is a new table entry,
and changing rules part-way through a save appends an era instead of
rewriting history.

Money is in $ millions throughout (``0.78`` = $780,000).

Payroll measures
================
* **Payroll** (cap and floor) - current-season cash salaries of every player
  under contract in the organisation plus dead money.
* **CBT payroll** - contract *average annual value* (AAV) of the same players,
  prorated by the days each spent with the club (accrued daily during the
  regular season), plus dead money and a per-club player-benefits charge.

All functions are deterministic and never consume random numbers.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field, fields, replace
from typing import TYPE_CHECKING, Any

from mlb_manager import contracts
from mlb_manager.models import Team

if TYPE_CHECKING:
    from mlb_manager.league import League

# ----------------------------------------------------------------------------- vocabulary

CAP_NONE, CAP_SOFT, CAP_HARD = "none", "soft", "hard"
CAP_TYPES: tuple[str, ...] = (CAP_NONE, CAP_SOFT, CAP_HARD)
CAP_LABELS: dict[str, str] = {
    CAP_NONE: "No salary cap",
    CAP_SOFT: "Soft cap (exceptions + penalty)",
    CAP_HARD: "Hard cap",
}

PRESET_MLB, PRESET_CUSTOM = "mlb", "custom"
PRESETS: tuple[str, ...] = (PRESET_MLB, PRESET_CUSTOM)
PRESET_LABELS: dict[str, str] = {PRESET_MLB: "Standard MLB rules", PRESET_CUSTOM: "Custom financial rules"}

# Soft-cap exceptions: moves that may take a club over (or further over) a soft cap.
EXC_OWN = "own_players"
EXC_MINIMUM = "minimum_salary"
EXC_ROOKIE = "rookie_contracts"
EXC_TRADE = "trade_matching"
SOFT_CAP_EXCEPTIONS: dict[str, str] = {
    EXC_OWN: "Re-signing, extending or tendering arbitration to your own players",
    EXC_MINIMUM: "Contracts at the league minimum salary",
    EXC_ROOKIE: "Draft (rookie) contracts",
    EXC_TRADE: "Trades that take back no more than the matching share of outgoing salary",
}

SOFT_CAP_EXCEPTION_SHORT: dict[str, str] = {
    EXC_OWN: "Own players (re-sign, extend, arbitration)",
    EXC_MINIMUM: "Minimum-salary contracts",
    EXC_ROOKIE: "Draft contracts",
    EXC_TRADE: "Salary-matched trades",
}

# Transaction kinds understood by check_transaction().
KIND_FREE_AGENT = "free_agent"
KIND_RESIGN = "re-sign"
KIND_EXTENSION = "extension"
KIND_ARBITRATION = "arbitration"
KIND_TRADE = "trade"
KIND_DRAFT = "draft"
KIND_MINIMUM = "minimum"  # league-mandated depth signings at the minimum salary
KIND_LABELS: dict[str, str] = {
    KIND_FREE_AGENT: "free-agent signing",
    KIND_RESIGN: "re-signing",
    KIND_EXTENSION: "contract extension",
    KIND_ARBITRATION: "arbitration contract",
    KIND_TRADE: "trade",
    KIND_DRAFT: "draft signing",
    KIND_MINIMUM: "minimum-salary depth signing",
}
OWN_PLAYER_KINDS = (KIND_RESIGN, KIND_EXTENSION, KIND_ARBITRATION)
# Signings a club cannot avoid (the draft, filling the organisation to its minimum):
# never blocked by a cap, but they still count toward it.
MANDATORY_KINDS = (KIND_DRAFT, KIND_MINIMUM)

MIN_SEASON, MAX_SEASON = 2017, 2045
EPS = 1e-6

# ----------------------------------------------------------------------------- rules


@dataclass(frozen=True, slots=True)
class SurchargeTier:
    """CBT surcharge on the part of the overage at or above ``over`` (until the next tier)."""

    over: float  # $M above the threshold where the tier starts
    rate: float  # surcharge rate for a first-time payor
    repeat_rate: float  # surcharge rate for a repeat payor

    def to_list(self) -> list[float]:
        return [self.over, self.rate, self.repeat_rate]

    @classmethod
    def from_list(cls, data: list[float]) -> SurchargeTier:
        return cls(float(data[0]), float(data[1]), float(data[2]))


SURCHARGES_2017 = (SurchargeTier(20.0, 0.12, 0.12), SurchargeTier(40.0, 0.425, 0.45))
SURCHARGES_2022 = (SurchargeTier(20.0, 0.12, 0.12), SurchargeTier(40.0, 0.42, 0.42),
                   SurchargeTier(60.0, 0.60, 0.60))


@dataclass(frozen=True, slots=True)
class FinancialRules:
    """Every financial rule in force for one season (immutable)."""

    season: int = 2026
    source: str = PRESET_LABELS[PRESET_MLB]
    agreement: str = "2022-2026 CBA"
    provisional: bool = False  # True when an expired agreement's terms are carried forward
    # Salary cap ------------------------------------------------------------------
    cap_type: str = CAP_NONE
    cap_amount: float = 0.0
    soft_cap_tax_rate: float = 1.0  # penalty per $ of season payroll above a soft cap
    soft_cap_exceptions: tuple[str, ...] = (EXC_OWN, EXC_MINIMUM, EXC_ROOKIE, EXC_TRADE)
    trade_match_pct: float = 1.25  # soft cap: incoming salary <= this x outgoing (+ $0.1M)
    # Salary floor ----------------------------------------------------------------
    floor_enabled: bool = False
    floor_amount: float = 0.0
    floor_penalty_rate: float = 1.0  # share of the shortfall charged at season end
    # Competitive Balance Tax ------------------------------------------------------
    cbt_enabled: bool = True
    cbt_threshold: float = 244.0
    cbt_rates: tuple[float, float, float] = (0.20, 0.30, 0.50)  # 1st, 2nd, 3rd+ consecutive year
    cbt_surcharges: tuple[SurchargeTier, ...] = SURCHARGES_2022
    cbt_draft_penalty_over: float = 40.0  # top draft pick moves back 10 spots (0 = never)
    player_benefits: float = 17.0  # added to every CBT payroll ($M, approximate)
    # Revenue sharing --------------------------------------------------------------
    revenue_sharing: bool = True
    revenue_sharing_rate: float = 0.48  # share of local revenue pooled and split equally
    # Salaries ---------------------------------------------------------------------
    min_salary: float = 0.78
    notes: tuple[str, ...] = ()

    # -- convenience ----------------------------------------------------------------

    @property
    def has_cap(self) -> bool:
        return self.cap_type != CAP_NONE

    @property
    def hard_cap(self) -> bool:
        return self.cap_type == CAP_HARD

    @property
    def soft_cap(self) -> bool:
        return self.cap_type == CAP_SOFT

    @property
    def has_floor(self) -> bool:
        return self.floor_enabled

    def cbt_rate(self, offence: int) -> float:
        return self.cbt_rates[min(max(offence, 1), 3) - 1]

    # -- serialisation (rule values only; meta fields are re-derived) --------------

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["soft_cap_exceptions"] = list(self.soft_cap_exceptions)
        data["cbt_rates"] = list(self.cbt_rates)
        data["cbt_surcharges"] = [t.to_list() for t in self.cbt_surcharges]
        data["notes"] = list(self.notes)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FinancialRules:
        known = {f.name for f in fields(cls)}
        kwargs: dict[str, Any] = {k: v for k, v in data.items() if k in known}
        if "soft_cap_exceptions" in kwargs:
            kwargs["soft_cap_exceptions"] = tuple(kwargs["soft_cap_exceptions"])
        if "cbt_rates" in kwargs:
            kwargs["cbt_rates"] = tuple(float(x) for x in kwargs["cbt_rates"])
        if "cbt_surcharges" in kwargs:
            kwargs["cbt_surcharges"] = tuple(SurchargeTier.from_list(t) for t in kwargs["cbt_surcharges"])
        if "notes" in kwargs:
            kwargs["notes"] = tuple(kwargs["notes"])
        for name in ("cap_amount", "soft_cap_tax_rate", "trade_match_pct", "floor_amount", "floor_penalty_rate",
                     "cbt_threshold", "cbt_draft_penalty_over", "player_benefits", "revenue_sharing_rate",
                     "min_salary"):
            if name in kwargs:
                kwargs[name] = float(kwargs[name])
        return cls(**kwargs)


# ----------------------------------------------------------------------------- MLB agreements


@dataclass(frozen=True, slots=True)
class Agreement:
    """One collective bargaining agreement's financial terms, season by season."""

    name: str
    first: int
    last: int
    thresholds: dict[int, float]
    minimums: dict[int, float]
    surcharges: tuple[SurchargeTier, ...]
    rates: tuple[float, float, float] = (0.20, 0.30, 0.50)
    draft_penalty_over: float = 40.0
    player_benefits: float = 17.0
    revenue_sharing_rate: float = 0.48
    note: str = ""
    cap_type: str = CAP_NONE  # a future agreement may introduce a cap ...
    caps: dict[int, float] = field(default_factory=dict)  # ... with per-season amounts
    floors: dict[int, float] = field(default_factory=dict)  # ... and/or a floor

    def rules(self, season: int) -> FinancialRules:
        return FinancialRules(
            season=season, source=PRESET_LABELS[PRESET_MLB], agreement=self.name,
            cap_type=self.cap_type if season in self.caps else CAP_NONE, cap_amount=self.caps.get(season, 0.0),
            floor_enabled=season in self.floors, floor_amount=self.floors.get(season, 0.0),
            cbt_enabled=True, cbt_threshold=self.thresholds[season], cbt_rates=self.rates,
            cbt_surcharges=self.surcharges, cbt_draft_penalty_over=self.draft_penalty_over,
            player_benefits=self.player_benefits, revenue_sharing=True,
            revenue_sharing_rate=self.revenue_sharing_rate, min_salary=self.minimums[season],
            notes=(self.note,) if self.note else (),
        )


# Actual MLB terms. CBT thresholds and minimum salaries by season; tax rates of 20% /
# 30% / 50% for first / second / third-or-later consecutive seasons over; surcharges on
# the overage beyond $20M / $40M (/ $60M from 2022); a $40M+ overage moves the club's
# highest draft pick back ten places. 48% of net local revenue is shared.
AGREEMENTS: tuple[Agreement, ...] = (
    Agreement(
        "2017-2021 CBA", 2017, 2021,
        thresholds={2017: 195.0, 2018: 197.0, 2019: 206.0, 2020: 208.0, 2021: 210.0},
        minimums={2017: 0.535, 2018: 0.545, 2019: 0.555, 2020: 0.5635, 2021: 0.5705},
        surcharges=SURCHARGES_2017, player_benefits=15.0,
    ),
    Agreement(
        "2022-2026 CBA", 2022, 2026,
        thresholds={2022: 230.0, 2023: 233.0, 2024: 237.0, 2025: 241.0, 2026: 244.0},
        minimums={2022: 0.70, 2023: 0.72, 2024: 0.74, 2025: 0.76, 2026: 0.78},
        surcharges=SURCHARGES_2022, player_benefits=17.0,
        note="The 2022-2026 CBA expires on December 1, 2026.",
    ),
)
CBA_EXPIRY_NOTE = (
    "No CBA is in force after December 1, 2026 and no successor agreement has been finalised: "
    "the 2026 terms are carried forward provisionally."
)


@dataclass(frozen=True, slots=True)
class ProposedTerm:
    """A bargaining proposal: shown for information, never applied as a league rule."""

    season: int
    party: str
    summary: str
    status: str = "Proposed - not adopted"


PROPOSED_TERMS: tuple[ProposedTerm, ...] = (
    ProposedTerm(2028, "MLB (owners)", "A salary cap and a salary floor from the 2028 season"),
)


def mlb_rules(season: int) -> FinancialRules:
    """The actual MLB financial rules for ``season`` (provisional after the last CBA)."""
    if season < MIN_SEASON:
        raise ValueError(f"Standard MLB rules are available from {MIN_SEASON}.")
    for agreement in AGREEMENTS:
        if agreement.first <= season <= agreement.last:
            return agreement.rules(season)
    latest = AGREEMENTS[-1]
    carried = latest.rules(latest.last)
    return replace(carried, season=season, provisional=True,
                   agreement=f"{latest.name} terms carried forward", notes=(CBA_EXPIRY_NOTE,))


def proposed_terms(season: int | None = None) -> list[ProposedTerm]:
    """Proposals relevant to ``season`` (all when None). Purely informational."""
    return [t for t in PROPOSED_TERMS if season is None or season >= t.season - 2]


def default_custom_rules(season: int) -> FinancialRules:
    """Starting point for custom rules: the MLB rules of that season, editable."""
    base = mlb_rules(max(MIN_SEASON, season))
    return replace(base, source=PRESET_LABELS[PRESET_CUSTOM], agreement="Custom", provisional=False, notes=())


# Suggested amounts when a user switches a custom league to a cap or floor.
SUGGESTED_CAP = 200.0
SUGGESTED_FLOOR = 110.0

# ----------------------------------------------------------------------------- configuration


@dataclass(slots=True)
class RuleEra:
    """From season ``start`` on, use ``preset`` (custom eras carry their own rules)."""

    start: int
    preset: str = PRESET_MLB
    custom: FinancialRules | None = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"start": self.start, "preset": self.preset,
                "custom": self.custom.to_dict() if self.custom else None, "note": self.note}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RuleEra:
        custom = data.get("custom")
        return cls(int(data["start"]), data.get("preset", PRESET_MLB),
                   FinancialRules.from_dict(custom) if custom else None, data.get("note", ""))


@dataclass(slots=True)
class FinanceConfig:
    """A league's financial rules over time (saved with the league)."""

    eras: list[RuleEra] = field(default_factory=lambda: [RuleEra(2026)])
    legacy_salary_cap: float | None = None  # hard cap of a pre-v0.9 save, kept for reference

    def era_for(self, season: int) -> RuleEra:
        chosen = self.eras[0]
        for era in self.eras:
            if era.start <= season:
                chosen = era
        return chosen

    def rules_for(self, season: int) -> FinancialRules:
        era = self.era_for(season)
        if era.preset == PRESET_CUSTOM and era.custom is not None:
            return replace(era.custom, season=season, source=PRESET_LABELS[PRESET_CUSTOM],
                           agreement="Custom", provisional=False)
        return mlb_rules(max(MIN_SEASON, season))

    def preset_for(self, season: int) -> str:
        return self.era_for(season).preset

    def set_rules(self, start: int, preset: str, custom: FinancialRules | None = None, note: str = "") -> None:
        """Use ``preset`` from season ``start`` on; earlier eras are preserved."""
        if preset not in PRESETS:
            raise ValueError(f"Unknown rules preset {preset!r}.")
        if preset == PRESET_CUSTOM and custom is None:
            raise ValueError("Custom rules need a rule set.")
        self.eras = [e for e in self.eras if e.start < start]
        self.eras.append(RuleEra(start, preset, custom if preset == PRESET_CUSTOM else None, note))

    def to_dict(self) -> dict[str, Any]:
        return {"eras": [e.to_dict() for e in self.eras], "legacy_salary_cap": self.legacy_salary_cap}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FinanceConfig:
        eras = [RuleEra.from_dict(e) for e in data.get("eras", [])] or [RuleEra(2026)]
        legacy = data.get("legacy_salary_cap")
        return cls(sorted(eras, key=lambda e: e.start), float(legacy) if legacy is not None else None)


def new_config(start_season: int, preset: str = PRESET_MLB, custom: FinancialRules | None = None) -> FinanceConfig:
    errors = validate_season(start_season) + (validate_rules(custom) if preset == PRESET_CUSTOM and custom else [])
    if preset == PRESET_CUSTOM and custom is None:
        errors.append("Custom rules need a rule set.")
    if errors:
        raise ValueError(" ".join(errors))
    config = FinanceConfig([])
    config.set_rules(start_season, preset, custom)
    return config


def hard_cap_config(start_season: int, amount: float) -> FinanceConfig:
    """Custom rules with a hard cap (``--salary-cap`` on the command line)."""
    custom = replace(default_custom_rules(start_season), cap_type=CAP_HARD, cap_amount=float(amount))
    return new_config(start_season, PRESET_CUSTOM, custom)


# ----------------------------------------------------------------------------- validation


def validate_season(season: int) -> list[str]:
    if not MIN_SEASON <= season <= MAX_SEASON:
        return [f"Starting season must be between {MIN_SEASON} and {MAX_SEASON}."]
    return []


def validate_rules(rules: FinancialRules) -> list[str]:
    """Every problem with a rule set, as user-facing sentences (empty = valid)."""
    errors: list[str] = []

    def amount(value: float, label: str, lo: float, hi: float) -> None:
        if not math.isfinite(value) or not lo <= value <= hi:
            errors.append(f"{label} must be between {format_amount(lo)} and {format_amount(hi)}.")

    def share(value: float, label: str, hi: float) -> None:
        if not math.isfinite(value) or not 0 <= value <= hi:
            errors.append(f"{label} must be between 0% and {hi:.0%}.")

    if rules.cap_type not in CAP_TYPES:
        errors.append(f"Unknown salary cap type {rules.cap_type!r}.")
    amount(rules.min_salary, "League minimum salary", 0.1, 5.0)
    if rules.has_cap:
        amount(rules.cap_amount, "Salary cap", 50.0, 1000.0)
    if rules.soft_cap:
        share(rules.soft_cap_tax_rate, "Soft-cap penalty rate", 5.0)
        unknown = [e for e in rules.soft_cap_exceptions if e not in SOFT_CAP_EXCEPTIONS]
        if unknown:
            errors.append(f"Unknown soft-cap exception(s): {', '.join(unknown)}.")
        if not 1.0 <= rules.trade_match_pct <= 2.0:
            errors.append("Trade salary matching must be between 100% and 200% of outgoing salary.")
    if rules.has_floor:
        amount(rules.floor_amount, "Salary floor", 20.0, 1000.0)
        share(rules.floor_penalty_rate, "Floor shortfall charge", 3.0)
        if rules.has_cap and rules.floor_amount >= rules.cap_amount:
            errors.append("The salary floor must be below the salary cap.")
    if rules.cbt_enabled:
        amount(rules.cbt_threshold, "CBT threshold", 50.0, 1000.0)
        for rate in rules.cbt_rates:
            share(rate, "CBT tax rates", 3.0)
        if list(rules.cbt_rates) != sorted(rules.cbt_rates):
            errors.append("CBT rates must not decrease for repeat offenders.")
        overs = [t.over for t in rules.cbt_surcharges]
        if overs != sorted(overs) or any(o <= 0 for o in overs):
            errors.append("CBT surcharge tiers must start above the threshold, in increasing order.")
        for tier in rules.cbt_surcharges:
            share(tier.rate, "CBT surcharge rates", 3.0)
            share(tier.repeat_rate, "CBT surcharge rates", 3.0)
        amount(rules.player_benefits, "Player benefits", 0.0, 100.0)
        if rules.cbt_draft_penalty_over < 0:
            errors.append("The CBT draft-pick penalty trigger cannot be negative.")
        if rules.hard_cap and rules.cbt_threshold >= rules.cap_amount:
            pass  # allowed: the tax simply never applies under such a hard cap
    if rules.revenue_sharing:
        share(rules.revenue_sharing_rate, "Revenue sharing rate", 0.9)
    if rules.has_cap and rules.min_salary * 26 > rules.cap_amount:
        errors.append("The salary cap is too low to pay a 26-man roster the minimum salary.")
    return errors


# ----------------------------------------------------------------------------- formatting & parsing


def format_amount(value: float | None) -> str:
    """$ millions as a readable amount: ``$244M``, ``$17.5M``, ``$780,000``."""
    if value is None:
        return "-"
    sign = "-" if value < 0 else ""
    value = abs(value)
    if value < 1.0:
        return f"{sign}${round(value * 1_000_000):,}"
    if abs(value - round(value)) < 0.005:
        return f"{sign}${round(value):,}M"
    if abs(value * 10 - round(value * 10)) < 0.005:
        return f"{sign}${value:,.1f}M"
    return f"{sign}${value:,.2f}M"


def format_rate(value: float) -> str:
    pct = value * 100
    return f"{pct:.0f}%" if abs(pct - round(pct)) < 0.05 else f"{pct:.1f}%"


_AMOUNT = re.compile(r"^\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?|\.[0-9]+)\s*(k|m|mm|million|b|bn)?$", re.IGNORECASE)


def parse_amount(text: str) -> float:
    """Parse a money input into $ millions.

    Accepts ``244``, ``244M``, ``$244m``, ``244,000,000``, ``780k``, ``$780,000``
    and ``0.78``. Bare numbers of 10,000 or more are read as dollars.
    """
    raw = text.strip().replace("_", "")
    match = _AMOUNT.match(raw)
    if not match:
        raise ValueError(f"{text.strip() or 'Empty input'!r} is not an amount (try 244, $244M or 780k).")
    number = float(match.group(1).replace(",", ""))
    unit = (match.group(2) or "").lower()
    if unit == "k":
        number /= 1000
    elif unit in ("b", "bn"):
        number *= 1000
    elif not unit and number >= 10_000:
        number /= 1_000_000
    return round(number, 4)


def parse_rate(text: str) -> float:
    """Parse ``50``, ``50%`` or ``0.5`` into a share (0.5)."""
    raw = text.strip().rstrip("%").strip()
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{text.strip()!r} is not a percentage (try 50%).") from None
    if value > 1.0 or text.strip().endswith("%"):
        value /= 100
    return round(value, 4)


# ----------------------------------------------------------------------------- league state


@dataclass(slots=True)
class FinanceState:
    """Mutable money bookkeeping of a league (saved)."""

    streaks: dict[str, int] = field(default_factory=dict)  # consecutive seasons over the CBT threshold
    accrued_cash: dict[str, float] = field(default_factory=dict)  # sum of daily payrolls this season
    accrued_aav: dict[str, float] = field(default_factory=dict)  # sum of daily CBT (AAV) payrolls
    accrued_days: int = 0
    ledger: list[dict[str, Any]] = field(default_factory=list)  # one row per club and settled season
    draft_penalties: dict[int, list[str]] = field(default_factory=dict)  # draft year -> clubs
    changes: list[dict[str, Any]] = field(default_factory=list)  # rule changes made to this save

    def to_dict(self) -> dict[str, Any]:
        return {
            "streaks": dict(self.streaks),
            "accrued_cash": {k: round(v, 4) for k, v in self.accrued_cash.items()},
            "accrued_aav": {k: round(v, 4) for k, v in self.accrued_aav.items()},
            "accrued_days": self.accrued_days,
            "ledger": [dict(r) for r in self.ledger],
            "draft_penalties": {str(k): list(v) for k, v in self.draft_penalties.items()},
            "changes": [dict(c) for c in self.changes],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FinanceState:
        return cls(
            streaks={k: int(v) for k, v in data.get("streaks", {}).items()},
            accrued_cash={k: float(v) for k, v in data.get("accrued_cash", {}).items()},
            accrued_aav={k: float(v) for k, v in data.get("accrued_aav", {}).items()},
            accrued_days=int(data.get("accrued_days", 0)),
            ledger=[dict(r) for r in data.get("ledger", [])],
            draft_penalties={int(k): list(v) for k, v in data.get("draft_penalties", {}).items()},
            changes=[dict(c) for c in data.get("changes", [])],
        )


@dataclass(slots=True)
class LeagueFinance:
    config: FinanceConfig = field(default_factory=FinanceConfig)
    state: FinanceState = field(default_factory=FinanceState)
    migrated: bool = False  # created from a save that predates financial rules
    _cache: dict[Any, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {"config": self.config.to_dict(), "state": self.state.to_dict(), "migrated": self.migrated}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LeagueFinance:
        return cls(FinanceConfig.from_dict(data.get("config", {})), FinanceState.from_dict(data.get("state", {})),
                   bool(data.get("migrated", False)))


LEDGER_SEASONS = 12


def legacy_finance(data: dict[str, Any]) -> LeagueFinance:
    """Financial rules for a save that predates them: standard MLB rules from its season.

    The old hard cap is remembered (``legacy_salary_cap``) so the user can
    restore it from the load screen; contracts and history are untouched.
    """
    year = int(data.get("year", 2026))
    config = new_config(max(MIN_SEASON, min(MAX_SEASON, year)))
    cap = data.get("salary_cap")
    config.legacy_salary_cap = float(cap) if cap is not None else None
    return LeagueFinance(config, FinanceState(), migrated=True)

# ----------------------------------------------------------------------------- queries


def contract_season(league: League) -> int:
    """The season transactions count toward (next season during the offseason)."""
    return league.year + 1 if league.in_offseason else league.year


def rules(league: League, season: int | None = None) -> FinancialRules:
    return league.finance.config.rules_for(contract_season(league) if season is None else season)


def min_salary(league: League) -> float:
    return rules(league).min_salary


def payroll(league: League, team: Team) -> float:
    """Cash payroll (cap and floor measure)."""
    return contracts.payroll(team, league.players)


def aav_payroll(league: League, team: Team) -> float:
    """Sum of contract AAVs plus dead money (the daily CBT measure)."""
    total = sum(contracts.aav(league.players[pid]) for pid in team.all_players
                if league.players[pid].contract_years > 0)
    return round(total + team.dead_money, 4)


def season_days(league: League) -> int:
    return max(league.games_by_day()) + 1 if league.schedule else 1


def _remaining_days(league: League) -> int:
    if league.in_offseason:
        return season_days(league)
    if league.phase != "regular":
        return 0
    return max(0, season_days(league) - league.finance.state.accrued_days)


def _season_average(league: League, accrued: float, current: float) -> float:
    """Season-average payroll: days already accrued plus the rest at ``current``."""
    if league.in_offseason:
        return current
    state = league.finance.state
    played = state.accrued_days
    remaining = _remaining_days(league)
    if played + remaining <= 0:
        return current
    return (accrued + current * remaining) / (played + remaining)


def remaining_share(league: League) -> float:
    """Share of the CBT season still to be accrued (1.0 in the offseason, 0 after the season)."""
    if league.in_offseason:
        return 1.0
    return _remaining_days(league) / season_days(league)


def cbt_payroll(league: League, team: Team, aav_delta: float = 0.0) -> float:
    """Projected CBT payroll for the contract season (AAV, prorated, plus benefits)."""
    r = rules(league)
    state = league.finance.state
    current = aav_payroll(league, team) + aav_delta
    average = _season_average(league, state.accrued_aav.get(team.abbr, 0.0), current)
    return round(average + r.player_benefits, 4)


def season_payroll(league: League, team: Team, cash_delta: float = 0.0) -> float:
    """Projected season-average cash payroll (soft-cap penalty and floor measure)."""
    state = league.finance.state
    current = payroll(league, team) + cash_delta
    return round(_season_average(league, state.accrued_cash.get(team.abbr, 0.0), current), 4)


@dataclass(slots=True)
class TaxBill:
    cbt_payroll: float
    threshold: float
    over: float = 0.0
    offence: int = 0  # 1 = first consecutive season over, 2 = second, ...
    base_rate: float = 0.0
    base_tax: float = 0.0
    surcharge: float = 0.0
    draft_penalty: bool = False

    @property
    def total(self) -> float:
        return round(self.base_tax + self.surcharge, 4)

    @property
    def effective_rate(self) -> float:
        return self.total / self.over if self.over > 0 else 0.0


def cbt_bill(r: FinancialRules, payroll_value: float, prior_streak: int) -> TaxBill:
    """Competitive Balance Tax owed on ``payroll_value`` after ``prior_streak`` seasons over."""
    bill = TaxBill(round(payroll_value, 4), r.cbt_threshold)
    if not r.cbt_enabled:
        return bill
    over = payroll_value - r.cbt_threshold
    if over <= EPS:
        return bill
    bill.over = round(over, 4)
    bill.offence = prior_streak + 1
    bill.base_rate = r.cbt_rate(bill.offence)
    bill.base_tax = over * bill.base_rate
    tiers = sorted(r.cbt_surcharges, key=lambda t: t.over)
    for index, tier in enumerate(tiers):
        upper = tiers[index + 1].over if index + 1 < len(tiers) else math.inf
        band = max(0.0, min(over, upper) - tier.over)
        bill.surcharge += band * (tier.rate if bill.offence == 1 else tier.repeat_rate)
    bill.draft_penalty = r.cbt_draft_penalty_over > 0 and over >= r.cbt_draft_penalty_over - EPS
    return bill


def projected_tax(league: League, team: Team, aav_delta: float = 0.0) -> TaxBill:
    """CBT bill the club is on course for (contract season)."""
    streak = league.finance.state.streaks.get(team.abbr, 0)
    return cbt_bill(rules(league), cbt_payroll(league, team, aav_delta), streak)


def soft_cap_penalty(r: FinancialRules, season_payroll_value: float) -> float:
    if not r.soft_cap:
        return 0.0
    return round(max(0.0, season_payroll_value - r.cap_amount) * r.soft_cap_tax_rate, 4)


def floor_penalty(r: FinancialRules, season_payroll_value: float) -> float:
    if not r.has_floor:
        return 0.0
    return round(max(0.0, r.floor_amount - season_payroll_value) * r.floor_penalty_rate, 4)


def cap_room(league: League, team: Team) -> float | None:
    """Room under the salary cap (negative = over); None when there is no cap."""
    r = rules(league)
    if not r.has_cap:
        return None
    return round(r.cap_amount - payroll(league, team), 4)


# ----------------------------------------------------------------------------- transaction checks


@dataclass(slots=True)
class TransactionCheck:
    """Whether a payroll change is legal, and its financial consequences."""

    allowed: bool
    payroll_before: float
    payroll_after: float
    reason: str = ""
    warnings: list[str] = field(default_factory=list)
    exception: str | None = None  # soft-cap exception used

    def messages(self) -> list[str]:
        return ([self.reason] if self.reason else []) + list(self.warnings)


def check_transaction(league: League, team: Team, kind: str, added: float, *, salary: float = 0.0,
                      outgoing: float = 0.0, incoming: float = 0.0, aav_added: float | None = None
                      ) -> TransactionCheck:
    """Apply the league's rules to a payroll change of ``added`` ($M, this season) for ``team``.

    ``salary`` is the new contract's annual salary (signings); ``outgoing`` and
    ``incoming`` are the salaries leaving and arriving in a trade. Decreasing
    payroll is always legal; a club over a cap may still make such moves.
    """
    r = rules(league)
    before = payroll(league, team)
    after = round(before + added, 4)
    check = TransactionCheck(True, before, after)
    label = KIND_LABELS.get(kind, kind)
    if kind not in (KIND_TRADE,) and salary and salary < r.min_salary - EPS:
        check.allowed = False
        check.reason = f"Salary {format_amount(salary)} is below the league minimum of {format_amount(r.min_salary)}."
        return check
    increases = added > EPS
    if r.has_cap and increases and after > r.cap_amount + EPS:
        over_by = after - r.cap_amount
        if kind in MANDATORY_KINDS:
            check.warnings.append(
                f"{CAP_LABELS[r.cap_type].split(' (')[0]}: this {label} is exempt (league-mandated) but takes "
                f"payroll to {format_amount(after)}, {format_amount(over_by)} over the {format_amount(r.cap_amount)} cap.")
        elif r.hard_cap:
            room = r.cap_amount - before
            check.allowed = False
            check.reason = (
                f"Hard cap: this {label} would take {team.abbr}'s payroll to {format_amount(after)}, over the "
                f"{format_amount(r.cap_amount)} hard cap (room {format_amount(max(0.0, room))}).")
            return check
        else:
            exception = _soft_cap_exception(r, kind, salary, outgoing, incoming)
            if exception is None:
                allowed = ", ".join(SOFT_CAP_EXCEPTIONS[e].lower() for e in r.soft_cap_exceptions) or "none"
                check.allowed = False
                check.reason = (
                    f"Soft cap: this {label} would take {team.abbr}'s payroll to {format_amount(after)}, over the "
                    f"{format_amount(r.cap_amount)} soft cap, and no exception applies (allowed: {allowed}).")
                return check
            check.exception = exception
            penalty = soft_cap_penalty(r, season_payroll(league, team, added))
            check.warnings.append(
                f"Soft-cap exception used ({SOFT_CAP_EXCEPTIONS[exception].lower()}): payroll "
                f"{format_amount(after)} is over the {format_amount(r.cap_amount)} soft cap; projected penalty "
                f"at season end {format_amount(penalty)} ({format_rate(r.soft_cap_tax_rate)} of the excess).")
    if r.has_floor and added < -EPS and after < r.floor_amount - EPS:
        short = r.floor_amount - after
        check.warnings.append(
            f"Salary floor: payroll would be {format_amount(after)}, {format_amount(short)} below the "
            f"{format_amount(r.floor_amount)} floor; a season-average shortfall is charged at season end.")
    if r.cbt_enabled and abs(added) > EPS:
        delta_aav = added if aav_added is None else aav_added
        old = projected_tax(league, team)
        new = projected_tax(league, team, delta_aav)
        if new.total > EPS or old.total > EPS:
            change = new.total - old.total
            offence = {1: "first", 2: "second"}.get(new.offence, "third+") if new.offence else ""
            text = (f"Competitive Balance Tax: projected CBT payroll {format_amount(new.cbt_payroll)} vs the "
                    f"{format_amount(r.cbt_threshold)} threshold; projected tax {format_amount(new.total)} "
                    f"({'+' if change >= 0 else '-'}{format_amount(abs(change))})")
            if offence:
                text += f", {offence} consecutive season over ({format_rate(new.base_rate)} base rate)"
            if new.draft_penalty and not old.draft_penalty:
                text += "; $40M+ over: top draft pick would move back 10 places"
            check.warnings.append(text + ".")
    return check


def _soft_cap_exception(r: FinancialRules, kind: str, salary: float, outgoing: float, incoming: float) -> str | None:
    allowed = set(r.soft_cap_exceptions)
    if EXC_OWN in allowed and kind in OWN_PLAYER_KINDS:
        return EXC_OWN
    if EXC_ROOKIE in allowed and kind == KIND_DRAFT:
        return EXC_ROOKIE
    if EXC_MINIMUM in allowed and (kind == KIND_MINIMUM or (salary and salary <= r.min_salary + EPS)):
        return EXC_MINIMUM
    if EXC_TRADE in allowed and kind == KIND_TRADE and outgoing > 0 and incoming <= outgoing * r.trade_match_pct + 0.1:
        return EXC_TRADE
    return None


# ----------------------------------------------------------------------------- revenue & budgets

# Revenue potential index (market size and fan base; 1.0 = typical club). An approximation
# that drives local revenue and owner budgets; only relative sizes matter.
REVENUE_INDEX: dict[str, float] = {
    "NYY": 2.6, "LAD": 2.5, "NYM": 2.0, "BOS": 1.9, "CHC": 1.8, "SF": 1.6, "PHI": 1.6, "ATL": 1.5,
    "TOR": 1.4, "HOU": 1.4, "TEX": 1.4, "LAA": 1.3, "SD": 1.3, "SEA": 1.25, "STL": 1.2, "WSH": 1.2,
    "ARI": 1.0, "COL": 1.0, "DET": 1.0, "MIN": 1.0, "CWS": 1.0, "BAL": 0.95, "MIL": 0.9, "CIN": 0.85,
    "CLE": 0.85, "PIT": 0.8, "KC": 0.8, "MIA": 0.75, "TB": 0.7, "ATH": 0.65,
}
LOCAL_REVENUE_BASE = 210.0  # $M for a 1.0-index club at .500
CENTRAL_REVENUE = 120.0  # national media and licensing, shared equally
POSTSEASON_REVENUE = 8.0  # per postseason round played (x index)
STRATEGY_BUDGET: dict[str, float] = {"contending": 1.08, "balanced": 1.0, "rebuilding": 0.82}


def revenue_index(abbr: str) -> float:
    return REVENUE_INDEX.get(abbr, 1.0)


def local_revenue(abbr: str, win_pct: float = 0.5, postseason_rounds: int = 0) -> float:
    index = revenue_index(abbr)
    return round(LOCAL_REVENUE_BASE * index * (0.85 + 0.3 * win_pct) + POSTSEASON_REVENUE * index * postseason_rounds, 4)


def _last_row(league: League, abbr: str) -> dict[str, Any] | None:
    for row in reversed(league.finance.state.ledger):
        if row["team"] == abbr:
            return row
    return None


def _projected_locals(league: League) -> dict[str, float]:
    key = ("locals", league.year, len(league.finance.state.ledger))
    cache = league.finance._cache
    if key not in cache:
        out = {}
        for abbr in league.teams:
            row = _last_row(league, abbr)
            out[abbr] = local_revenue(abbr, row["win_pct"] if row else 0.5)
        cache.clear()
        cache[key] = out
    return cache[key]


def projected_revenue(league: League, team: Team) -> dict[str, float]:
    """Next settlement's revenue estimate: local, central and revenue sharing."""
    r = rules(league)
    locals_ = _projected_locals(league)
    local = locals_[team.abbr]
    shared_out = shared_in = 0.0
    if r.revenue_sharing:
        shared_out = local * r.revenue_sharing_rate
        shared_in = sum(locals_.values()) * r.revenue_sharing_rate / len(locals_)
    net = local - shared_out + shared_in + CENTRAL_REVENUE
    return {"local": local, "central": CENTRAL_REVENUE, "shared_out": shared_out, "shared_in": shared_in,
            "net": round(net, 4)}


def payroll_budget(league: League, team: Team) -> float:
    """What ownership will spend on payroll plus luxury tax (CPU spending limit; a guide for the user).

    Bigger markets spend a larger share of revenue; contenders stretch and
    rebuilders save. A floor raises the budget to it; a hard cap limits it.
    """
    r = rules(league)
    index = revenue_index(team.abbr)
    budget = projected_revenue(league, team)["net"] * (0.30 + 0.12 * index) * STRATEGY_BUDGET.get(team.strategy, 1.0)
    if r.has_floor:
        budget = max(budget, r.floor_amount * 1.05)
    if r.has_cap:
        budget = min(budget, r.cap_amount)
    return round(budget, 2)


def spending_room(league: League, team: Team) -> float:
    """Budget left after payroll and projected luxury tax / soft-cap penalty."""
    r = rules(league)
    used = payroll(league, team) + projected_tax(league, team).total
    used += soft_cap_penalty(r, season_payroll(league, team))
    return round(payroll_budget(league, team) - used, 4)


def fits_budget(league: League, team: Team, added: float, kind: str = KIND_FREE_AGENT) -> bool:
    """CPU clubs only take on payroll that their owners' budget covers, including tax."""
    if added <= EPS or kind in MANDATORY_KINDS:
        return True
    r = rules(league)
    after = payroll(league, team) + added
    if r.has_floor and after <= r.floor_amount + EPS:
        return True  # reaching the floor is always worth it
    cost = after + projected_tax(league, team, added).total + soft_cap_penalty(r, season_payroll(league, team, added))
    return cost <= payroll_budget(league, team) + EPS


def cpu_can_add(league: League, team: Team, kind: str, added: float, *, salary: float = 0.0,
                outgoing: float = 0.0, incoming: float = 0.0) -> bool:
    """Legal under the rules *and* within the club's budget."""
    check = check_transaction(league, team, kind, added, salary=salary, outgoing=outgoing, incoming=incoming)
    return check.allowed and fits_budget(league, team, added, kind)


def below_floor(league: League, team: Team) -> float:
    """How far the club's payroll is below the floor (0 when none or above)."""
    r = rules(league)
    if not r.has_floor:
        return 0.0
    return round(max(0.0, r.floor_amount - payroll(league, team)), 4)


# ----------------------------------------------------------------------------- season cycle


def start_season(league: League) -> list[str]:
    """New season: reset accruals and lift salaries to the league minimum."""
    state = league.finance.state
    state.accrued_cash = {abbr: 0.0 for abbr in league.teams}
    state.accrued_aav = {abbr: 0.0 for abbr in league.teams}
    state.accrued_days = 0
    league.finance._cache.clear()
    r = rules(league, league.year)
    raised = 0
    for player in league.players.values():
        if player.team is not None and player.contract_years > 0 and player.salary < r.min_salary - EPS:
            contracts.raise_to_minimum(player, r.min_salary)
            raised += 1
    return [f"{raised} players were raised to the new {format_amount(r.min_salary)} league minimum."] if raised else []


def accrue_day(league: League) -> None:
    """Credit one regular-season day of payroll to every club (CBT proration)."""
    state = league.finance.state
    for abbr, team in league.teams.items():
        state.accrued_cash[abbr] = state.accrued_cash.get(abbr, 0.0) + payroll(league, team)
        state.accrued_aav[abbr] = state.accrued_aav.get(abbr, 0.0) + aav_payroll(league, team)
    state.accrued_days += 1


def initialise_accruals(league: League) -> None:
    """Approximate accruals for a save that predates them (days so far at today's payroll)."""
    state = league.finance.state
    played = league.day if league.phase == "regular" else (season_days(league) if league.phase in (
        "postseason", "complete") else 0)
    state.accrued_days = played
    state.accrued_cash = {a: payroll(league, t) * played for a, t in league.teams.items()}
    state.accrued_aav = {a: aav_payroll(league, t) * played for a, t in league.teams.items()}


def settle_season(league: League, postseason_rounds: dict[str, int] | None = None) -> list[str]:
    """End of season: revenue, revenue sharing, CBT, soft-cap penalty and floor shortfall.

    Returns news items (the user's club and notable league events).
    """
    r = rules(league, league.year)
    state = league.finance.state
    records = league.records()
    rounds = postseason_rounds or {}
    rows: dict[str, dict[str, Any]] = {}
    for abbr in sorted(league.teams):
        rec = records[abbr]
        games = rec.wins + rec.losses
        win_pct = rec.wins / games if games else 0.5
        rows[abbr] = {"year": league.year, "team": abbr, "win_pct": round(win_pct, 4),
                      "local": local_revenue(abbr, win_pct, rounds.get(abbr, 0))}
    pool = sum(row["local"] for row in rows.values()) * r.revenue_sharing_rate if r.revenue_sharing else 0.0
    share_in = pool / len(rows) if rows else 0.0
    news: list[str] = []
    payors: list[str] = []
    for abbr, row in rows.items():
        team = league.teams[abbr]
        season_cash = (state.accrued_cash.get(abbr, 0.0) / state.accrued_days if state.accrued_days
                       else payroll(league, team))
        cbt_value = (state.accrued_aav.get(abbr, 0.0) / state.accrued_days if state.accrued_days
                     else aav_payroll(league, team)) + r.player_benefits
        prior = state.streaks.get(abbr, 0)
        bill = cbt_bill(r, cbt_value, prior)
        state.streaks[abbr] = bill.offence if bill.over > 0 else 0
        if bill.draft_penalty:
            state.draft_penalties.setdefault(league.draft_year, []).append(abbr)
        soft = soft_cap_penalty(r, season_cash)
        floor = floor_penalty(r, season_cash)
        shared_out = row["local"] * r.revenue_sharing_rate if r.revenue_sharing else 0.0
        revenue = row["local"] - shared_out + (share_in if r.revenue_sharing else 0.0) + CENTRAL_REVENUE
        row.update({
            "central": CENTRAL_REVENUE, "shared_out": round(shared_out, 4),
            "shared_in": round(share_in if r.revenue_sharing else 0.0, 4), "revenue": round(revenue, 4),
            "payroll": round(season_cash, 4), "cbt_payroll": round(cbt_value, 4), "cbt_over": bill.over,
            "cbt_offence": bill.offence, "cbt_rate": bill.base_rate, "cbt_tax": bill.total,
            "draft_penalty": bill.draft_penalty, "soft_cap_penalty": soft, "floor_penalty": floor,
            "net": round(revenue - season_cash - bill.total - soft - floor, 4),
        })
        state.ledger.append(row)
        if bill.total > 0:
            payors.append(f"{abbr} {format_amount(bill.total)}")
        if abbr == league.user_team:
            news.extend(_user_settlement_news(league, r, row, bill))
        elif soft or floor:
            parts = ([f"{format_amount(soft)} soft-cap penalty"] if soft else []) + (
                [f"{format_amount(floor)} floor shortfall"] if floor else [])
            league.log_transaction(f"{abbr} charged {' and '.join(parts)} for {league.year}.")
    if payors:
        news.append(f"{league.year} Competitive Balance Tax: " + ", ".join(payors) + ".")
    years = sorted({row["year"] for row in state.ledger})
    if len(years) > LEDGER_SEASONS:
        keep = set(years[-LEDGER_SEASONS:])
        state.ledger = [row for row in state.ledger if row["year"] in keep]
    league.finance._cache.clear()
    return news


def _user_settlement_news(league: League, r: FinancialRules, row: dict[str, Any], bill: TaxBill) -> list[str]:
    news = [f"{league.year} finances: revenue {format_amount(row['revenue'])}, payroll "
            f"{format_amount(row['payroll'])}, CBT payroll {format_amount(row['cbt_payroll'])}."]
    if bill.total > 0:
        nth = {1: "first", 2: "second"}.get(bill.offence, f"{bill.offence}th")
        text = (f"You owe {format_amount(bill.total)} in Competitive Balance Tax ({nth} consecutive season over, "
                f"{format_rate(bill.base_rate)} base rate on {format_amount(bill.over)} over"
                + (f" plus {format_amount(bill.surcharge)} in surcharges" if bill.surcharge else "") + ").")
        news.append(text)
        if bill.draft_penalty:
            news.append(f"More than {format_amount(r.cbt_draft_penalty_over)} over the threshold: your top "
                        f"{league.draft_year} draft pick moves back 10 places.")
    elif r.cbt_enabled and league.finance.state.streaks.get(league.user_team, 0) == 0 and row["cbt_payroll"] > 0:
        news.append("You finished under the CBT threshold: any repeat-offender rate resets.")
    if row["soft_cap_penalty"]:
        news.append(f"Soft-cap penalty: {format_amount(row['soft_cap_penalty'])}.")
    if row["floor_penalty"]:
        news.append(f"Salary floor shortfall charge: {format_amount(row['floor_penalty'])}.")
    return news


def penalised_first_round(order: list[str], clubs: list[str]) -> list[str] | None:
    """Move each penalised club's first-round pick back 10 places (top-6 picks are protected)."""
    if not clubs:
        return None
    first = list(order)
    for abbr in sorted(set(clubs), key=order.index):
        index = first.index(abbr)
        if index < 6:
            continue
        first.pop(index)
        first.insert(min(len(first), index + 10), abbr)
    return first


# ----------------------------------------------------------------------------- editing rules


def migration_notes(league: League) -> list[str]:
    """What happened to a pre-v0.9 save's finances when it was loaded."""
    r = rules(league)
    legacy = league.finance.config.legacy_salary_cap
    notes = [f"league financial rules are now configurable; {r.source} for {r.season} apply "
             f"(no salary cap, {format_amount(r.cbt_threshold)} CBT threshold, revenue sharing)."]
    if legacy is not None:
        notes.append(f"The previous {format_amount(legacy)} hard cap is no longer enforced; it can be restored "
                     "with Edit financial rules.")
    notes.append("Contracts, payrolls and history are unchanged.")
    return notes


def restore_legacy_cap_rules(league: League) -> FinancialRules | None:
    """Custom rules that bring back a migrated save's old hard cap (None if there was none)."""
    legacy = league.finance.config.legacy_salary_cap
    if legacy is None:
        return None
    return replace(default_custom_rules(contract_season(league)), cap_type=CAP_HARD, cap_amount=legacy)


def describe_change(league: League, old: FinancialRules, new: FinancialRules) -> list[str]:
    """Plain-language consequences of switching a running league from ``old`` to ``new``."""
    lines: list[str] = []
    user = league.user
    pay = payroll(league, user)
    others = [t for t in league.teams.values() if t.abbr != league.user_team]
    if (old.cap_type, old.cap_amount) != (new.cap_type, new.cap_amount):
        if not new.has_cap:
            lines.append("Salary cap removed: payroll is unlimited (subject to the CBT and budgets).")
        else:
            over = [t.abbr for t in others if payroll(league, t) > new.cap_amount + EPS]
            lines.append(f"{CAP_LABELS[new.cap_type]} of {format_amount(new.cap_amount)}: "
                         f"{len(over)} other club(s) are above it today.")
            if pay > new.cap_amount + EPS:
                verb = "you cannot add payroll" if new.hard_cap else "you can add payroll only through exceptions"
                lines.append(f"Your payroll {format_amount(pay)} is above the new cap. Existing contracts stay "
                             f"valid, but {verb} until you are under it.")
    if (old.floor_enabled, old.floor_amount) != (new.floor_enabled, new.floor_amount):
        if not new.has_floor:
            lines.append("Salary floor removed.")
        else:
            under = [t.abbr for t in league.teams.values() if payroll(league, t) < new.floor_amount - EPS]
            lines.append(f"Salary floor of {format_amount(new.floor_amount)}: {len(under)} club(s) are below it "
                         "today and will be charged the season-average shortfall at season end.")
    if (old.cbt_enabled, old.cbt_threshold, old.cbt_rates) != (new.cbt_enabled, new.cbt_threshold, new.cbt_rates):
        if not new.cbt_enabled:
            lines.append("Competitive Balance Tax disabled: no tax for this season's assessment.")
        else:
            streak = league.finance.state.streaks.get(league.user_team, 0)
            before = cbt_bill(old, cbt_payroll(league, user), streak).total if old.cbt_enabled else 0.0
            after = cbt_bill(new, cbt_payroll(league, user), streak).total
            lines.append(f"CBT threshold {format_amount(new.cbt_threshold)}: your projected tax "
                         f"{format_amount(before)} -> {format_amount(after)}.")
    if (old.revenue_sharing, old.revenue_sharing_rate) != (new.revenue_sharing, new.revenue_sharing_rate):
        lines.append("Revenue sharing " + (f"set to {format_rate(new.revenue_sharing_rate)}" if new.revenue_sharing
                                           else "disabled") + ": changes club revenues and CPU payroll budgets.")
    if new.min_salary > old.min_salary + EPS:
        count = sum(1 for p in league.players.values()
                    if p.team and p.contract_years > 0 and p.salary < new.min_salary - EPS)
        lines.append(f"League minimum {format_amount(new.min_salary)}: {count} contracts below it are raised "
                     "when the next season starts.")
    elif new.min_salary < old.min_salary - EPS:
        lines.append(f"League minimum lowered to {format_amount(new.min_salary)} (existing contracts unchanged).")
    if (old.soft_cap_exceptions, old.soft_cap_tax_rate) != (new.soft_cap_exceptions, new.soft_cap_tax_rate) \
            and new.soft_cap:
        lines.append("Soft-cap exceptions/penalty updated.")
    if not lines:
        lines.append("No rule changes.")
        return lines
    lines.append("Contracts, payrolls, team finances and league history are preserved; the new rules apply to "
                 f"transactions from now on and to the {contract_season(league)} season's assessments.")
    return lines


def _candidate(league: League, preset: str, custom: FinancialRules | None) -> FinanceConfig:
    if preset == PRESET_CUSTOM:
        if custom is None:
            raise ValueError("Custom rules need a rule set.")
        errors = validate_rules(custom)
        if errors:
            raise ValueError(" ".join(errors))
    config = league.finance.config
    candidate = FinanceConfig([RuleEra(e.start, e.preset, e.custom, e.note) for e in config.eras],
                              config.legacy_salary_cap)
    candidate.set_rules(contract_season(league), preset, custom)
    return candidate


def preview_change(league: League, preset: str, custom: FinancialRules | None = None) -> list[str]:
    """What :func:`change_rules` would report, without changing anything."""
    season = contract_season(league)
    return describe_change(league, rules(league), _candidate(league, preset, custom).rules_for(season))


def change_rules(league: League, preset: str, custom: FinancialRules | None = None) -> list[str]:
    """Switch a running league to new rules from the current contract season on.

    Earlier seasons keep their rules (eras); contracts, payrolls, CBT streaks,
    ledgers and history are never modified.
    """
    season = contract_season(league)
    old = rules(league)
    candidate = _candidate(league, preset, custom)
    new = candidate.rules_for(season)
    if old == new and preset == league.finance.config.preset_for(season):
        return ["No rule changes."]
    lines = describe_change(league, old, new)
    league.finance.config = candidate
    league.finance.state.changes.append({"year": league.year, "phase": league.phase, "season": season,
                                         "preset": preset, "summary": lines[:-1] or lines})
    league.finance._cache.clear()
    league.finance.migrated = False
    league.add_news(f"League financial rules changed ({PRESET_LABELS[preset]}, from {season}).")
    return lines


# ----------------------------------------------------------------------------- initial payrolls


def fit_initial_payrolls(league: League) -> None:
    """Shape generated contracts to the league's rules and owners' budgets (new leagues only).

    Clubs above a cap (or far above their budget) scale their veteran salaries
    down; clubs below a floor or far below their budget scale them up.
    Minimum-salary players are untouched.
    """
    r = rules(league, league.year)
    for abbr in sorted(league.teams):
        team = league.teams[abbr]
        total = payroll(league, team)
        budget = payroll_budget(league, team)
        target = None
        if r.has_cap and total > r.cap_amount * 0.97:
            target = r.cap_amount * 0.97
        elif total > budget * 1.05:
            target = budget
        elif r.has_floor and total < r.floor_amount * 1.02:
            target = r.floor_amount * 1.02
        elif total < budget * 0.7:
            target = budget * 0.7  # rich clubs carry rich veteran contracts
        if target is None:
            continue
        big = [league.players[pid] for pid in team.all_players if league.players[pid].salary > r.min_salary + EPS]
        big_total = sum(p.salary for p in big)
        if big_total <= 0:
            continue
        scale = max(0.1, (big_total + target - total) / big_total)
        for player in big:
            contracts.set_flat_salary(player, contracts.round_salary(player.salary * scale, r.min_salary))
