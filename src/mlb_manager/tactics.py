"""Tactical model for pitch-by-pitch games: instructions, repertoires and probabilities.

The plate-appearance model in :mod:`mlb_manager.game` defines what a matchup
*should* produce on average (strikeout, walk, home-run rates...). This module
turns that into a pitch-level model that the dugout can influence:

* every pitch has a location (heart / edge / up / down / chase), a pitch type
  from the pitcher's repertoire, an execution roll, a zone/swing/contact
  sequence and, when the ball is put in play, a batted-ball distribution;
* per plate appearance, two calibration knobs (zone and contact offsets) and an
  HBP rate are solved so that *neutral* instructions reproduce the
  plate-appearance model's strikeout, walk and HBP rates exactly (in
  expectation); batted-ball shares are taken from the same model;
* instructions (approach, aggression, pitch calls, effort, alignment...) are
  deviations from that neutral baseline, scaled by player ability, so they
  shift probabilities without ever guaranteeing an outcome.

Everything here is pure: functions take ratings and instructions and return
numbers. Randomness is supplied by the caller's ``random.Random``.
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, fields
from functools import lru_cache
from typing import Any

from mlb_manager.models import Player

# ----------------------------------------------------------------------------- instructions

BAT_APPROACHES = ("contact", "balanced", "power")
AGGRESSION = ("patient", "normal", "aggressive")
DISCIPLINE = ("loose", "normal", "strict")
SWING_EFFORT = ("easy", "normal", "max")
LEADS = ("normal", "extended")
ADVANCE = ("conservative", "normal", "aggressive")
PITCH_EFFORT = ("conserve", "normal", "max")
PITCH_APPROACH = ("attack", "balanced", "nibble")
SEQUENCING = ("mix", "fastballs", "backwards", "best")
INFIELD = ("normal", "in", "dp", "corners")
OUTFIELD = ("normal", "shallow", "deep")
SHIFT = ("none", "pull", "oppo")
DEF_AGGRESSION = ("conservative", "normal", "aggressive")
HOOK = ("quick", "normal", "long")
LOCATIONS = ("heart", "edge", "up", "down", "chase")

LABELS: dict[str, str] = {
    "contact": "Contact", "balanced": "Balanced", "power": "Power",
    "patient": "Patient", "normal": "Normal", "aggressive": "Aggressive",
    "loose": "Loose", "strict": "Strict", "easy": "Easy", "max": "Max",
    "extended": "Extended", "conservative": "Conservative",
    "conserve": "Conserve", "attack": "Attack zone", "nibble": "Nibble corners",
    "mix": "Mix (auto)", "fastballs": "Establish fastball", "backwards": "Pitch backwards",
    "best": "Lean on best pitch",
    "in": "Drawn in", "dp": "Double-play depth", "corners": "Corners in",
    "shallow": "Shallow", "deep": "Deep / no doubles",
    "none": "Straight up", "pull": "Shade pull side", "oppo": "Shade opposite field",
    "quick": "Quick hook", "long": "Long leash",
    "heart": "Heart of the plate", "edge": "Paint the edges", "up": "Elevate", "down": "Keep it down",
    "chase": "Chase pitch (off the plate)",
}


@dataclass(slots=True)
class BattingPlan:
    """Persistent batting/baserunning instructions for one batter or a whole club."""

    approach: str = "balanced"
    aggression: str = "normal"
    discipline: str = "normal"
    effort: str = "normal"
    lead: str = "normal"
    advance: str = "normal"

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> BattingPlan:
        names = {f.name for f in fields(cls)}
        return cls(**{k: str(v) for k, v in (data or {}).items() if k in names})


@dataclass(slots=True)
class PitchingPlan:
    """Persistent instructions for the club's pitchers."""

    effort: str = "normal"
    approach: str = "balanced"
    sequencing: str = "mix"
    hold_runners: bool = False
    hook: str = "normal"  # workload preference for automatic changes: quick / normal / long

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> PitchingPlan:
        data = dict(data or {})
        return cls(effort=str(data.get("effort", "normal")), approach=str(data.get("approach", "balanced")),
                   sequencing=str(data.get("sequencing", "mix")), hold_runners=bool(data.get("hold_runners")),
                   hook=str(data.get("hook", "normal")))


@dataclass(slots=True)
class Alignment:
    """Defensive positioning of the fielding club."""

    infield: str = "normal"
    outfield: str = "normal"
    shift: str = "none"
    aggression: str = "normal"

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> Alignment:
        names = {f.name for f in fields(cls)}
        return cls(**{k: str(v) for k, v in (data or {}).items() if k in names})

    @property
    def is_default(self) -> bool:
        return self == Alignment()


@dataclass(slots=True)
class PitchCall:
    """What the fielding club does on the next pitch (resolved from plan + one-shot calls)."""

    pitch_type: str | None = None  # None = pitcher/catcher choose
    location: str | None = None  # None = choose by count and approach
    effort: str = "normal"
    approach: str = "balanced"
    sequencing: str = "mix"
    intentional_ball: bool = False
    pitchout: bool = False
    hold_runners: bool = False


@dataclass(slots=True)
class SwingCall:
    """What the batting club does on the next pitch."""

    approach: str = "balanced"
    aggression: str = "normal"
    discipline: str = "normal"
    effort: str = "normal"
    take: bool = False
    bunt: bool = False
    hit_and_run: bool = False
    steal: str | None = None  # None, "steal" or "delayed"
    lead: str = "normal"
    advance: str = "normal"
    send: str | None = None  # one-play override: "send" or "hold"

    @property
    def runners_moving(self) -> bool:
        return self.hit_and_run or self.steal is not None


# ----------------------------------------------------------------------------- repertoire


@dataclass(frozen=True, slots=True)
class PitchType:
    code: str
    name: str
    whiff: float  # contact logit reduction when thrown
    zone: float  # zone logit shift (command of the pitch)
    gb: float  # ground-ball tilt of contact
    hr: float  # log home-run multiplier
    velo: float  # mph relative to the fastball
    breaking: bool


PITCH_TYPES: dict[str, PitchType] = {
    "FF": PitchType("FF", "four-seam fastball", 0.00, 0.30, -0.08, 0.10, 0.0, False),
    "SI": PitchType("SI", "sinker", -0.12, 0.25, 0.30, -0.15, -1.2, False),
    "FC": PitchType("FC", "cutter", 0.10, 0.15, 0.05, -0.05, -4.5, False),
    "SL": PitchType("SL", "slider", 0.35, -0.20, 0.00, -0.05, -8.5, True),
    "CU": PitchType("CU", "curveball", 0.30, -0.30, 0.10, -0.10, -14.0, True),
    "CH": PitchType("CH", "changeup", 0.30, -0.25, 0.15, -0.05, -8.5, True),
    "FS": PitchType("FS", "splitter", 0.40, -0.35, 0.20, -0.10, -7.0, True),
}
SECONDARY = ("SL", "CU", "CH", "FC", "FS")
SECONDARY_WEIGHTS = (0.34, 0.22, 0.24, 0.12, 0.08)


@dataclass(frozen=True, slots=True)
class RepertoirePitch:
    code: str
    grade: int  # 20-80 scouting grade
    usage: float  # share of pitches under a neutral "mix"

    @property
    def name(self) -> str:
        return PITCH_TYPES[self.code].name


def repertoire(pitcher: Player) -> tuple[RepertoirePitch, ...]:
    """Deterministic pitch mix derived from the pitcher's id and ratings."""
    r = pitcher.ratings
    return _repertoire(pitcher.id, r.get("stuff", 50), r.get("movement", 50), r.get("control", 50),
                       r.get("stamina", 50), pitcher.position)


@lru_cache(maxsize=4096)
def _repertoire(pid: int, stuff: int, movement: int, control: int, stamina: int,
                position: str) -> tuple[RepertoirePitch, ...]:
    rng = random.Random(f"repertoire:{pid}")
    sinker = movement > stuff + 4 or rng.random() < 0.25
    primary = "SI" if sinker else "FF"
    count = 2 if position == "RP" else 3
    if position != "RP" and (stamina >= 60 or rng.random() < 0.35):
        count += 1
    if position == "RP" and rng.random() < 0.3:
        count -= 1
    pool = list(SECONDARY)
    weights = list(SECONDARY_WEIGHTS)
    chosen: list[str] = []
    for _ in range(count):
        pick = rng.choices(pool, weights=weights)[0]
        index = pool.index(pick)
        pool.pop(index)
        weights.pop(index)
        chosen.append(pick)
    base = 50 + (stuff - 50) * 0.55 + (movement - 50) * 0.25
    grades = {primary: base + (stuff - 50) * 0.15 + rng.gauss(0, 4)}
    for code in chosen:
        grades[code] = base + rng.gauss(0, 7)
    best = max(chosen, key=lambda c: grades[c])
    grades[best] += 4
    fb_usage = 0.62 - 0.05 * len(chosen) + rng.uniform(-0.04, 0.04)
    rest = 1.0 - fb_usage
    raw = {c: max(0.15, (grades[c] - 25) / 30) for c in chosen}
    total = sum(raw.values())
    pitches = [RepertoirePitch(primary, _grade(grades[primary]), round(fb_usage, 3))]
    pitches += [RepertoirePitch(c, _grade(grades[c]), round(rest * raw[c] / total, 3)) for c in chosen]
    return tuple(pitches)


def _grade(value: float) -> int:
    return int(max(20, min(80, round(value / 5) * 5)))


def fastball_velocity(pitcher: Player) -> float:
    return 89.0 + (pitcher.rating("stuff") - 50) * 0.13 + (2.0 if pitcher.position == "RP" else 0.0)


def _pitch_effects(pitch: RepertoirePitch) -> tuple[float, float, float, float]:
    """(contact, zone, gb, log-hr) shifts of one pitch, before centring."""
    pt = PITCH_TYPES[pitch.code]
    quality = (pitch.grade - 50) / 10
    return (-(pt.whiff + 0.10 * quality), pt.zone + 0.05 * quality, pt.gb, pt.hr - 0.06 * quality)


def pitch_effects(rep: tuple[RepertoirePitch, ...], code: str) -> tuple[float, float, float, float]:
    """Effects of throwing ``code`` relative to the pitcher's neutral mix (zero on average)."""
    mean = [0.0, 0.0, 0.0, 0.0]
    for p in rep:
        for i, v in enumerate(_pitch_effects(p)):
            mean[i] += p.usage * v
    pitch = next(p for p in rep if p.code == code)
    return tuple(v - m for v, m in zip(_pitch_effects(pitch), mean, strict=True))  # type: ignore[return-value]


def pitch_weights(rep: tuple[RepertoirePitch, ...], sequencing: str, balls: int, strikes: int,
                  previous: str | None) -> list[float]:
    """Selection weights for an automatic pitch call."""
    weights = []
    best = max(rep[1:] or rep, key=lambda p: p.grade).code
    for i, p in enumerate(rep):
        w = p.usage
        fastball = i == 0
        if balls >= 3 or (balls == 2 and strikes == 0):
            w *= 1.5 if fastball else 0.7
        elif strikes == 2 and balls < 2:
            w *= 0.75 if fastball else 1.25
        if sequencing == "fastballs":
            w *= 1.8 if fastball else 0.6
        elif sequencing == "backwards":
            w *= 0.55 if fastball else 1.5
        elif sequencing == "best":
            w *= 2.2 if p.code == best else 0.75
        if previous == p.code:
            w *= 0.8
        weights.append(w)
    return weights


# ----------------------------------------------------------------------------- locations


@dataclass(frozen=True, slots=True)
class Location:
    zone: float  # probability the executed pitch is a strike
    swing: float  # swing logit shift
    contact: float  # contact logit shift
    hr: float  # home-run multiplier on contact
    hit: float  # base-hit multiplier on contact
    gb: float  # ground-ball tilt


LOCATION_MODEL: dict[str, Location] = {
    "heart": Location(0.92, 0.40, 0.45, 1.55, 1.08, 0.00),
    "edge": Location(0.55, 0.00, -0.15, 0.85, 0.96, 0.00),
    "up": Location(0.50, 0.10, -0.25, 1.10, 0.97, -0.18),
    "down": Location(0.50, -0.05, -0.10, 0.72, 0.98, 0.18),
    "chase": Location(0.08, -0.10, -0.35, 0.40, 0.82, 0.05),
    # Not choosable: what a missed spot becomes.
    "mistake": Location(0.92, 0.50, 0.60, 1.85, 1.12, -0.05),
    "wild": Location(0.03, -1.50, -0.40, 0.30, 0.75, 0.00),
    "waste": Location(0.0, -9.0, -1.0, 0.30, 0.75, 0.00),
}

_MIX = {
    # (heart, edge, up, down, chase) by count situation, balanced approach
    "ahead": (0.09, 0.30, 0.16, 0.20, 0.25),
    "even": (0.17, 0.37, 0.15, 0.21, 0.10),
    "behind": (0.30, 0.38, 0.12, 0.15, 0.05),
    "3-0": (0.50, 0.40, 0.05, 0.05, 0.00),
    "0-2": (0.05, 0.25, 0.20, 0.20, 0.30),
}
_APPROACH_SHIFT = {
    "attack": (1.6, 1.15, 1.0, 1.0, 0.5),
    "balanced": (1.0, 1.0, 1.0, 1.0, 1.0),
    "nibble": (0.5, 1.15, 1.0, 1.05, 1.6),
}


def count_state(balls: int, strikes: int) -> str:
    if balls == 3 and strikes == 0:
        return "3-0"
    if balls == 0 and strikes == 2:
        return "0-2"
    if strikes > balls:
        return "ahead"
    if balls > strikes:
        return "behind"
    return "even"


def location_mix(balls: int, strikes: int, approach: str = "balanced") -> tuple[float, ...]:
    base = _MIX[count_state(balls, strikes)]
    shift = _APPROACH_SHIFT.get(approach, _APPROACH_SHIFT["balanced"])
    raw = [b * s for b, s in zip(base, shift, strict=True)]
    total = sum(raw)
    return tuple(r / total for r in raw)


def execution_chance(pitcher: Player, location: str, grade: int, effort: str, tired: float) -> float:
    if location == "heart":
        return 0.96
    if location == "waste":
        return 0.995
    chance = (0.64 + 0.007 * (pitcher.rating("control") - 50) + 0.004 * (grade - 50)
              - (0.035 if effort == "max" else -0.03 if effort == "conserve" else 0.0) - 0.008 * tired)
    return max(0.30, min(0.93, chance))


MISTAKE_SHARE = 0.45  # a missed spot catches the middle of the plate this often

# ----------------------------------------------------------------------------- base swing model

ZONE_SWING = {(0, 0): 0.55, (1, 0): 0.62, (2, 0): 0.55, (3, 0): 0.12, (0, 1): 0.68, (1, 1): 0.70,
              (2, 1): 0.72, (3, 1): 0.60, (0, 2): 0.84, (1, 2): 0.85, (2, 2): 0.86, (3, 2): 0.88}
CHASE_SWING = {(0, 0): 0.22, (1, 0): 0.26, (2, 0): 0.22, (3, 0): 0.03, (0, 1): 0.30, (1, 1): 0.31,
               (2, 1): 0.30, (3, 1): 0.20, (0, 2): 0.40, (1, 2): 0.40, (2, 2): 0.41, (3, 2): 0.42}
ZONE_CONTACT = 0.82
CHASE_CONTACT = 0.58
FOUL_ZONE = 0.57
FOUL_CHASE = 0.60
BASE_HBP_PER_PITCH = 0.006


def logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def sigmoid(x: float) -> float:
    if x < -60:
        return 0.0
    return 1.0 / (1.0 + math.exp(-x))


_ZS_LOGIT = {c: logit(v) for c, v in ZONE_SWING.items()}
_CS_LOGIT = {c: logit(v) for c, v in CHASE_SWING.items()}
_ZC_LOGIT = logit(ZONE_CONTACT)
_CC_LOGIT = logit(CHASE_CONTACT)
_FZ_LOGIT = logit(FOUL_ZONE)
_FC_LOGIT = logit(FOUL_CHASE)


@dataclass(slots=True)
class PitchMods:
    """Deviations from the neutral pitch model for one pitch (logit shifts)."""

    zone: float = 0.0  # added to the calibrated zone logit
    zone_swing: float = 0.0
    chase_swing: float = 0.0
    contact: float = 0.0
    foul: float = 0.0
    hbp: float = 1.0
    no_swing: bool = False  # take, intentional ball, pitchout
    bunt: bool = False


@dataclass(slots=True)
class Calibration:
    dz: float
    dc: float
    hbp: float
    pitches: float  # expected pitches per plate appearance (neutral)


def _outcome_split(z_logit: float, loc: Location, count: tuple[int, int], cal_dc: float, hbp: float,
                   mods: PitchMods, wild: bool = False) -> tuple[float, float, float, float, float]:
    """(ball, strike, foul, in play, hbp) for one *actual* location (strike = called or missed)."""
    z = 0.0 if loc.zone <= 0.0 else sigmoid(z_logit)
    if mods.no_swing:
        sz = so = 0.0
    else:
        sz = sigmoid(_ZS_LOGIT[count] + loc.swing + mods.zone_swing)
        so = sigmoid(_CS_LOGIT[count] + loc.swing + mods.chase_swing)
    shift = cal_dc + loc.contact + mods.contact
    cz = sigmoid(_ZC_LOGIT + shift)
    co = sigmoid(_CC_LOGIT + shift)
    fz = sigmoid(_FZ_LOGIT + mods.foul)
    fo = sigmoid(_FC_LOGIT + mods.foul)
    h = min(0.2, hbp * mods.hbp * (3.0 if wild else 1.0))
    zs, os_ = z * sz, (1 - z) * so
    called = z * (1 - sz)
    miss = zs * (1 - cz) + os_ * (1 - co)
    foul = zs * cz * fz + os_ * co * fo
    inplay = zs * cz * (1 - fz) + os_ * co * (1 - fo)
    take_ball = (1 - z) * (1 - so)
    return take_ball * (1 - h), called + miss, foul, inplay, take_ball * h


_ZONE_LOGIT = {name: (logit(loc.zone) if loc.zone > 0 else -50.0) for name, loc in LOCATION_MODEL.items()}


def pitch_distribution(count: tuple[int, int], cal: Calibration, mods: PitchMods, mix: tuple[float, ...],
                       exec_chance: float, location: str | None = None) -> tuple[float, float, float, float, float]:
    """Probability of (ball, strike, foul, in play, hbp) for the next pitch.

    ``location`` fixes the target; otherwise the target is drawn from ``mix``.
    A missed spot becomes a mistake over the plate or a wild pitch out of the zone.
    """
    targets = [(location, 1.0)] if location else list(zip(LOCATIONS, mix, strict=True))
    splits: dict[str, tuple[float, float, float, float, float]] = {}

    def split(name: str) -> tuple[float, float, float, float, float]:
        value = splits.get(name)
        if value is None:
            loc = LOCATION_MODEL[name]
            z_logit = _ZONE_LOGIT[name] + cal.dz + mods.zone if loc.zone > 0 else -50.0
            value = _outcome_split(z_logit, loc, count, cal.dc, cal.hbp, mods, name == "wild")
            splits[name] = value
        return value

    out = [0.0] * 5
    for name, weight in targets:
        if weight <= 0:
            continue
        if name == "waste":
            parts = [("waste", 1.0)]
        else:
            exec_p = 0.96 if name == "heart" else exec_chance
            parts = [(name, exec_p), ("mistake", (1 - exec_p) * MISTAKE_SHARE),
                     ("wild", (1 - exec_p) * (1 - MISTAKE_SHARE))]
        for part, p in parts:
            values = split(part)
            w = weight * p
            for i in range(5):
                out[i] += w * values[i]
    return out[0], out[1], out[2], out[3], out[4]


def terminal(dist: Any) -> tuple[float, float, float, float, float]:
    """(K, BB, HBP, in play, pitches) of a plate appearance by dynamic programming over counts.

    ``dist(count)`` returns the (ball, strike, foul, in play, hbp) probabilities of one pitch."""
    memo: dict[tuple[int, int], tuple[float, float, float, float, float]] = {}
    for balls in range(3, -1, -1):
        for strikes in range(2, -1, -1):
            ball, strike, foul, inplay, hbp = dist((balls, strikes))
            stay = foul if strikes == 2 else 0.0
            advance_strike = strike + (0.0 if strikes == 2 else foul)
            scale = 1.0 / max(1e-9, 1.0 - stay)
            k_next = (1.0, 0.0, 0.0, 0.0, 0.0) if strikes == 2 else memo[(balls, strikes + 1)]
            b_next = (0.0, 1.0, 0.0, 0.0, 0.0) if balls == 3 else memo[(balls + 1, strikes)]
            vals = []
            for i in range(4):
                v = advance_strike * k_next[i] + ball * b_next[i]
                if i == 2:
                    v += hbp
                if i == 3:
                    v += inplay
                vals.append(v * scale)
            pitches = (1.0 + advance_strike * k_next[4] + ball * b_next[4]) * scale
            memo[(balls, strikes)] = (vals[0], vals[1], vals[2], vals[3], pitches)
    return memo[(0, 0)]


def _neutral_terminal(cal: Calibration, exec_chance: float = 0.64) -> tuple[float, float, float, float, float]:
    """(K, BB, HBP, in play, pitches) of a neutral plate appearance."""
    mods = PitchMods()
    return terminal(lambda c: pitch_distribution(c, cal, mods, location_mix(*c), exec_chance))


@lru_cache(maxsize=8192)
def _calibrate_cached(k_key: int, bb_key: int, hbp_key: int, exec_key: int) -> Calibration:
    k_t, bb_t, hbp_t = k_key / 400, bb_key / 400, max(hbp_key / 2000, 0.0005)
    ex = exec_key / 50
    cal = Calibration(0.0, 0.0, BASE_HBP_PER_PITCH, 3.9)
    for _ in range(3):
        for _ in range(10):
            k, bb, _, _, _ = _neutral_terminal(cal, ex)
            fk, fb = k - k_t, bb - bb_t
            if abs(fk) < 2e-4 and abs(fb) < 2e-4:
                break
            eps = 0.02
            k1, bb1, *_ = _neutral_terminal(Calibration(cal.dz + eps, cal.dc, cal.hbp, 0), ex)
            k2, bb2, *_ = _neutral_terminal(Calibration(cal.dz, cal.dc + eps, cal.hbp, 0), ex)
            a, b = (k1 - k) / eps, (k2 - k) / eps
            c, d = (bb1 - bb) / eps, (bb2 - bb) / eps
            det = a * d - b * c
            if abs(det) < 1e-9:
                break
            step_z = (d * fk - b * fb) / det
            step_c = (-c * fk + a * fb) / det
            cal = Calibration(cal.dz - max(-1.0, min(1.0, step_z)), cal.dc - max(-1.0, min(1.0, step_c)),
                              cal.hbp, 0)
        _, _, hbp, _, _ = _neutral_terminal(cal, ex)
        if hbp > 0:
            cal = Calibration(cal.dz, cal.dc, min(0.2, cal.hbp * hbp_t / hbp), 0)
    *_, pitches = _neutral_terminal(cal, ex)
    return Calibration(cal.dz, cal.dc, cal.hbp, pitches)


def calibrate(k: float, bb: float, hbp: float, exec_chance: float = 0.64) -> Calibration:
    """Calibration knobs reproducing the plate-appearance rates under neutral play.

    ``exec_chance`` is the pitcher's neutral chance of hitting his spot, so that
    command is not counted twice (the walk rate already reflects control)."""
    return _calibrate_cached(round(k * 400), round(bb * 400), round(hbp * 2000), round(exec_chance * 50))


# ----------------------------------------------------------------------------- batted balls

BIP_OUTCOMES = ("HR", "3B", "2B", "1B", "E", "GO", "FO", "LO")
HITS = ("1B", "2B", "3B")


def bip_shares(probs: Mapping[str, float]) -> dict[str, float]:
    total = sum(probs[o] for o in BIP_OUTCOMES)
    return {o: probs[o] / total for o in BIP_OUTCOMES}


def _apply_bip(shares: dict[str, float], hr: float = 1.0, hit: float = 1.0, xbh: float = 1.0,
               gb: float = 0.0, error: float = 1.0, single: float = 1.0) -> None:
    shares["HR"] *= hr
    shares["1B"] *= hit * single
    shares["2B"] *= hit * xbh
    shares["3B"] *= hit * xbh
    shares["E"] *= error
    shares["GO"] *= 1 + gb
    shares["FO"] *= 1 - gb * 0.8
    shares["LO"] *= 1 - gb * 0.3


@lru_cache(maxsize=1)
def _location_centre() -> dict[str, float]:
    """Average BIP multipliers of neutral play, weighted by how often each location is put in play.

    Location effects are divided by these, so the neutral mix leaves batted-ball
    shares exactly as the plate-appearance model has them."""
    cal = calibrate(0.222, 0.077, 0.011)
    mods = PitchMods()
    exec_p = 0.64
    # Forward pass: probability of reaching each count.
    visit = {(b, s): 0.0 for b in range(4) for s in range(3)}
    visit[(0, 0)] = 1.0
    for balls in range(4):
        for strikes in range(3):
            v = visit[(balls, strikes)]
            if not v:
                continue
            ball, strike, foul, _, _ = pitch_distribution((balls, strikes), cal, mods, location_mix(balls, strikes),
                                                          exec_p)
            stay = foul if strikes == 2 else 0.0
            adv = strike + (0.0 if strikes == 2 else foul)
            v /= 1 - stay
            if balls < 3:
                visit[(balls + 1, strikes)] += v * ball
            if strikes < 2:
                visit[(balls, strikes + 1)] += v * adv
            visit[(balls, strikes)] = v
    acc = {"hr": 0.0, "hit": 0.0, "gb": 0.0}
    total = 0.0
    for count, v in visit.items():
        mix = location_mix(*count)
        for name, w in zip(LOCATIONS, mix, strict=True):
            p_exec = 0.96 if name == "heart" else exec_p
            for part, p in ((name, p_exec), ("mistake", (1 - p_exec) * MISTAKE_SHARE),
                            ("wild", (1 - p_exec) * (1 - MISTAKE_SHARE))):
                inplay = pitch_distribution(count, cal, mods, mix, exec_p, part)[3]
                share = v * w * p * inplay
                loc = LOCATION_MODEL[part]
                acc["hr"] += share * loc.hr
                acc["hit"] += share * loc.hit
                acc["gb"] += share * loc.gb
                total += share
    return {k: val / total for k, val in acc.items()}


def location_bip(location: str) -> tuple[float, float, float]:
    """(hr multiplier, hit multiplier, gb tilt) of contact at a location, centred on the neutral mix."""
    loc = LOCATION_MODEL[location]
    centre = _location_centre()
    return loc.hr / centre["hr"], loc.hit / centre["hit"], loc.gb - centre["gb"]


def pull_tendency(batter: Player) -> float:
    """0 (spray) .. 1 (dead pull); power hitters pull more."""
    rng = random.Random(f"pull:{batter.id}")
    return max(0.05, min(0.95, 0.45 + (batter.rating("power") - 50) * 0.008
                         - (batter.rating("contact") - 50) * 0.003 + rng.uniform(-0.12, 0.12)))


def batter_bip_mods(shares: dict[str, float], batter: Player, swing: SwingCall) -> None:
    power = batter.rating("power")
    contact = batter.rating("contact")
    if swing.approach == "power":
        lift = max(0.2, min(1.3, (power - 30) / 35))
        _apply_bip(shares, hr=1 + 0.42 * lift, xbh=1 + 0.12 * lift, hit=0.95, gb=-0.08)
    elif swing.approach == "contact":
        skill = max(0.4, min(1.3, (contact - 25) / 40))
        _apply_bip(shares, hr=0.76, xbh=0.92, hit=1 + 0.06 * skill, gb=0.05)
    if swing.effort == "max":
        _apply_bip(shares, hr=1.25, xbh=1.08, hit=0.97)
    elif swing.effort == "easy":
        _apply_bip(shares, hr=0.72, xbh=0.9, hit=1.03)
    if swing.hit_and_run:
        _apply_bip(shares, hr=0.8, hit=1.04, gb=0.10)


def alignment_bip_mods(shares: dict[str, float], align: Alignment, batter: Player, runners: tuple[bool, ...],
                       outs: int) -> None:
    if align.infield == "in":
        _apply_bip(shares, single=1.16)
    elif align.infield == "dp":
        _apply_bip(shares, single=1.04)
    elif align.infield == "corners":
        _apply_bip(shares, single=1.05, xbh=1.04)
    if align.outfield == "shallow":
        _apply_bip(shares, single=0.92, xbh=1.20)
    elif align.outfield == "deep":
        _apply_bip(shares, single=1.07, xbh=0.80)
    if align.shift != "none":
        pull = pull_tendency(batter)
        fit = (pull - 0.5) * 2 if align.shift == "pull" else (0.5 - pull) * 2
        _apply_bip(shares, single=1 - 0.10 * fit - 0.02, gb=0.0)
    if align.aggression == "aggressive":
        _apply_bip(shares, hit=0.97, error=1.30)
    elif align.aggression == "conservative":
        _apply_bip(shares, hit=1.02, error=0.80)


def normalise(shares: dict[str, float]) -> dict[str, float]:
    total = sum(shares.values())
    return {k: v / total for k, v in shares.items()}


# ----------------------------------------------------------------------------- swing / pitch mods


def swing_mods(swing: SwingCall, batter: Player, mods: PitchMods) -> None:
    eye = batter.rating("eye")
    contact = batter.rating("contact")
    power = batter.rating("power")
    if swing.approach == "power":
        mods.contact -= 0.30 * (1.25 - contact / 100)
        mods.zone_swing += 0.10
    elif swing.approach == "contact":
        mods.contact += 0.30 * (0.55 + contact / 110)
        mods.foul += 0.10
    if swing.aggression == "aggressive":
        mods.zone_swing += 0.55
        mods.chase_swing += 0.35
    elif swing.aggression == "patient":
        mods.zone_swing -= 0.55
        mods.chase_swing -= 0.30
    if swing.discipline == "strict":
        mods.chase_swing -= 0.65 * max(0.35, min(1.4, eye / 60))
        mods.zone_swing -= 0.15
    elif swing.discipline == "loose":
        mods.chase_swing += 0.45
    if swing.effort == "max":
        mods.contact -= 0.30 - (power - 50) * 0.002
    elif swing.effort == "easy":
        mods.contact += 0.25
    if swing.take:
        mods.no_swing = True
    if swing.hit_and_run:
        mods.zone_swing += 3.0
        mods.chase_swing += 2.2
        mods.contact += 0.20
    if swing.bunt:
        mods.bunt = True


def effort_contact(effort: str, pitcher: Player) -> float:
    if effort == "max":
        return -0.26 * (0.6 + pitcher.rating("stuff") / 125)
    if effort == "conserve":
        return 0.12
    return 0.0


EFFORT_STRESS = {"conserve": 0.75, "normal": 1.0, "max": 1.45}
EFFORT_VELO = {"conserve": -1.5, "normal": 0.0, "max": 1.6}

# ----------------------------------------------------------------------------- run values

# Run expectancy by (outs, bases bitmask 1st=1, 2nd=2, 3rd=4); MLB 2010s averages.
RE24: dict[tuple[int, int], float] = {}
for _outs, _row in enumerate((
    (0.481, 0.859, 1.100, 1.437, 1.350, 1.784, 1.964, 2.292),
    (0.254, 0.509, 0.664, 0.884, 0.950, 1.130, 1.376, 1.541),
    (0.098, 0.224, 0.319, 0.429, 0.353, 0.478, 0.580, 0.752),
)):
    for _mask, _value in enumerate(_row):
        RE24[(_outs, _mask)] = _value


def run_expectancy(outs: int, bases: tuple[bool, bool, bool]) -> float:
    if outs >= 3:
        return 0.0
    mask = (1 if bases[0] else 0) | (2 if bases[1] else 0) | (4 if bases[2] else 0)
    return RE24[(outs, mask)]


# Linear weights (runs above an average plate appearance).
LINEAR_WEIGHTS = {"BB": 0.31, "HBP": 0.33, "1B": 0.47, "2B": 0.77, "3B": 1.04, "HR": 1.40, "E": 0.45,
                  "GO": -0.28, "FO": -0.26, "LO": -0.26, "K": -0.28}
# Count values (runs relative to 0-0).
COUNT_VALUE = {(0, 0): 0.0, (1, 0): 0.038, (2, 0): 0.104, (3, 0): 0.200, (0, 1): -0.043, (1, 1): -0.015,
               (2, 1): 0.036, (3, 1): 0.168, (0, 2): -0.103, (1, 2): -0.080, (2, 2): -0.038, (3, 2): 0.089}


def bip_value(shares: Mapping[str, float]) -> float:
    """Expected linear-weight runs of a ball in play."""
    total = sum(shares.values())
    return sum(LINEAR_WEIGHTS[o] * v for o, v in shares.items()) / total if total else 0.0


def pa_run_value(probs: Mapping[str, float]) -> float:
    """Expected linear-weight runs of a plate appearance distribution."""
    return sum(LINEAR_WEIGHTS.get(o, 0.0) * p for o, p in probs.items())


def pitch_run_value(count: tuple[int, int], dist: tuple[float, float, float, float, float],
                    bip_value: float) -> float:
    """Expected change in batter run value from one pitch."""
    balls, strikes = count
    ball, strike, foul, inplay, hbp = dist
    now = COUNT_VALUE[count]
    after_ball = LINEAR_WEIGHTS["BB"] if balls == 3 else COUNT_VALUE[(balls + 1, strikes)]
    after_strike = LINEAR_WEIGHTS["K"] if strikes == 2 else COUNT_VALUE[(balls, strikes + 1)]
    after_foul = now if strikes == 2 else COUNT_VALUE[(balls, strikes + 1)]
    return (ball * after_ball + strike * after_strike + foul * after_foul + inplay * bip_value
            + hbp * LINEAR_WEIGHTS["HBP"] - now)


def steal_break_even(outs: int, bases: tuple[bool, bool, bool], target: int) -> float:
    """Success rate needed for a steal of ``target`` (2 or 3) to break even in run expectancy."""
    before = run_expectancy(outs, bases)
    b = list(bases)
    runner_from = target - 2
    b[runner_from] = False
    fail = run_expectancy(outs + 1, (b[0], b[1], b[2]))
    b[target - 1] = True
    success = run_expectancy(outs, (b[0], b[1], b[2]))
    if success <= fail:
        return 1.0
    return max(0.0, min(1.0, (before - fail) / (success - fail)))


@dataclass(slots=True)
class Assessment:
    """Ex-ante evaluation of a decision (never based on the play's outcome)."""

    grade: str  # "favourable", "neutral", "unfavourable" or "informational"
    basis: str
    value: float = 0.0  # expected runs gained for the deciding club (positive = good)

    def to_dict(self) -> dict[str, Any]:
        return {"grade": self.grade, "basis": self.basis, "value": self.value}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> Assessment | None:
        if not data:
            return None
        return cls(str(data["grade"]), str(data["basis"]), float(data.get("value", 0.0)))


def grade_value(value: float, band: float = 0.006) -> str:
    if value > band:
        return "favourable"
    if value < -band:
        return "unfavourable"
    return "neutral"


@dataclass(slots=True)
class Matchup:
    """Pre-computed matchup facts shown in the dugout."""

    probs: dict[str, float] = field(default_factory=dict)

    @property
    def obp(self) -> float:
        p = self.probs
        return sum(p.get(o, 0.0) for o in ("BB", "HBP", "1B", "2B", "3B", "HR"))
