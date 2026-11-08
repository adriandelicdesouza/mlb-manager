"""The dugout: live, manager-controlled games on top of the pitch-by-pitch engine.

A :class:`LiveGame` wraps a pitch-by-pitch :class:`~mlb_manager.game.GameSimulator`
for the user's game of the day. It owns

* the user's **instructions** (persistent batting, baserunning, pitching and
  defensive plans, matchup-specific overrides) and **one-shot calls** for the
  next pitch (pitch type, location, take, bunt, hit-and-run, steal...);
* the **automation level** of each decision category (manual / assisted /
  auto). Automated categories use :class:`CpuBrain`, the same in-game
  intelligence that manages the opposing club; assisted categories let the CPU
  handle routine work and pause with a recommendation at key moments;
* **progression** by pitch, play, plate appearance, half-inning, inning or to
  the end of the game, pausing for assisted-mode prompts;
* the **decision log** (who decided what, in which situation, previous and new
  instruction, resulting play) with an *ex-ante* assessment computed from the
  information available at the time - never from the outcome;
* the **post-game managerial report**.

UIs (GUI and CLI) only call this API: :meth:`LiveGame.controls` describes the
context-sensitive controls, :meth:`LiveGame.apply` performs one, and
:meth:`LiveGame.advance` moves the game on. Nothing here prints.
"""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from mlb_manager import tactics as tx
from mlb_manager.game import (
    MIN_BATTERS_FACED,
    PHASE_FINAL,
    PHASE_HALF,
    PHASE_PA,
    PHASE_PITCH,
    REGULATION_INNINGS,
    GameResult,
    GameSimulator,
    RuleError,
    _Side,
    order_defense,
    steal_attempt_rate,
)
from mlb_manager.models import Player

CATEGORIES = ("pitching", "batting", "baserunning", "defense", "substitutions")
CATEGORY_LABELS = {"pitching": "Pitching", "batting": "Hitting", "baserunning": "Baserunning",
                   "defense": "Defense", "substitutions": "Substitutions"}
MODES = ("manual", "assisted", "auto")
MODE_LABELS = {"manual": "Manual", "assisted": "Assisted", "auto": "Automatic"}
UNITS = ("pitch", "play", "pa", "half", "inning", "game")
UNIT_LABELS = {"pitch": "Next pitch", "play": "Next play", "pa": "Next batter", "half": "End of half-inning",
               "inning": "End of inning", "game": "To the final"}
PRESETS = {
    "manual": dict.fromkeys(CATEGORIES, "manual"),
    "assisted": dict.fromkeys(CATEGORIES, "assisted"),
    "auto": dict.fromkeys(CATEGORIES, "auto"),
}
EXHAUSTION_MARGIN = 35  # a manual pitcher is pulled automatically this far past his limit
HOOK_SHIFT = {"quick": -12, "normal": 0, "long": 12}
RESULT_KINDS = ("PA", "SB", "CS", "PK", "TO")
STEP_LIMIT = 20000
LIVE_STEAL_FACTOR = 1.5


# ----------------------------------------------------------------------------- data


@dataclass(slots=True)
class NextPitch:
    """One-shot calls for the next pitch (cleared once the pitch is thrown)."""

    pitch_type: str | None = None
    location: str | None = None
    intentional_ball: bool = False
    pitchout: bool = False
    take: bool = False
    bunt: bool = False
    hit_and_run: bool = False
    steal: str | None = None
    send: str | None = None  # applies to the next ball in play

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> NextPitch:
        return cls(**dict(data or {}))


@dataclass(slots=True)
class Instructions:
    """A club's persistent game plan."""

    batting: tx.BattingPlan = field(default_factory=tx.BattingPlan)
    per_batter: dict[int, tx.BattingPlan] = field(default_factory=dict)  # matchup-specific overrides
    pitching: tx.PitchingPlan = field(default_factory=tx.PitchingPlan)
    pitch_vs: dict[int, str] = field(default_factory=dict)  # opposing batter id -> pitcher approach
    alignment: tx.Alignment = field(default_factory=tx.Alignment)

    def plan_for(self, batter_id: int) -> tx.BattingPlan:
        return self.per_batter.get(batter_id, self.batting)

    def to_dict(self) -> dict[str, Any]:
        return {"batting": self.batting.to_dict(),
                "per_batter": {str(k): v.to_dict() for k, v in self.per_batter.items()},
                "pitching": self.pitching.to_dict(), "pitch_vs": {str(k): v for k, v in self.pitch_vs.items()},
                "alignment": self.alignment.to_dict()}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> Instructions:
        data = data or {}
        return cls(batting=tx.BattingPlan.from_dict(data.get("batting")),
                   per_batter={int(k): tx.BattingPlan.from_dict(v) for k, v in data.get("per_batter", {}).items()},
                   pitching=tx.PitchingPlan.from_dict(data.get("pitching")),
                   pitch_vs={int(k): str(v) for k, v in data.get("pitch_vs", {}).items()},
                   alignment=tx.Alignment.from_dict(data.get("alignment")))


@dataclass(slots=True)
class Decision:
    """One managerial intervention, with the situation it was made in."""

    seq: int
    event: int  # number of game events when the decision was made
    inning: int
    top: bool
    outs: int
    count: tuple[int, int]
    bases: list[int]
    score: tuple[int, int]  # (user club, opponent)
    category: str
    action: str
    text: str
    player: int | None
    previous: str
    new: str
    source: str  # manual / auto / assisted / forced
    assessment: tx.Assessment | None = None
    result: int | None = None  # index of the event that resolved the decision
    result_kinds: tuple[str, ...] = RESULT_KINDS
    team: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "event": self.event, "inning": self.inning, "top": self.top, "outs": self.outs,
                "count": list(self.count), "bases": list(self.bases), "score": list(self.score),
                "category": self.category, "action": self.action, "text": self.text, "player": self.player,
                "previous": self.previous, "new": self.new, "source": self.source,
                "assessment": self.assessment.to_dict() if self.assessment else None, "result": self.result,
                "result_kinds": list(self.result_kinds), "team": self.team}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Decision:
        return cls(seq=int(d["seq"]), event=int(d["event"]), inning=int(d["inning"]), top=bool(d["top"]),
                   outs=int(d["outs"]), count=(int(d["count"][0]), int(d["count"][1])),
                   bases=[int(x) for x in d["bases"]], score=(int(d["score"][0]), int(d["score"][1])),
                   category=str(d["category"]), action=str(d["action"]), text=str(d["text"]),
                   player=d.get("player"), previous=str(d["previous"]), new=str(d["new"]), source=str(d["source"]),
                   assessment=tx.Assessment.from_dict(d.get("assessment")), result=d.get("result"),
                   result_kinds=tuple(d.get("result_kinds", RESULT_KINDS)), team=str(d.get("team", "")))


@dataclass(slots=True)
class Prompt:
    """A key decision presented to the user in assisted mode."""

    key: str
    category: str
    question: str
    options: list[tuple[str, str]]  # (value, label); the first option is the CPU recommendation
    basis: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "category": self.category, "question": self.question,
                "options": [list(o) for o in self.options], "basis": self.basis}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any] | None) -> Prompt | None:
        if not d:
            return None
        return cls(str(d["key"]), str(d["category"]), str(d["question"]),
                   [(str(a), str(b)) for a, b in d["options"]], str(d.get("basis", "")))


@dataclass(slots=True)
class Control:
    """A context-sensitive managerial control for the UI."""

    key: str
    group: str
    label: str
    kind: str  # "choice" (pick a value), "action" (do it now), "toggle"
    options: list[tuple[str, str]] = field(default_factory=list)
    value: str | None = None
    enabled: bool = True
    reason: str = ""  # why it is unavailable
    scope: str = ""  # "next pitch", "plan", "batter", "now"
    hotkey: str = ""


@dataclass(slots=True)
class AdvanceReport:
    events: list[dict[str, Any]]
    stopped: str  # "unit", "prompt", "final", "limit"


# ----------------------------------------------------------------------------- run expectancy helpers


def transition(outs: int, bases: tuple[bool, bool, bool], outcome: str) -> tuple[int, tuple[bool, bool, bool], int]:
    """Typical result of an outcome from a base/out state: (outs, bases, runs)."""
    b1, b2, b3 = bases
    runs = 0
    if outcome in ("BB", "HBP", "IBB"):
        if b1 and b2 and b3:
            runs = 1
        nb3 = b3 or (b1 and b2)
        nb2 = b2 or b1
        return outs, (True, nb2, nb3), runs
    if outcome == "HR":
        return outs, (False, False, False), 1 + b1 + b2 + b3
    if outcome == "3B":
        return outs, (False, False, True), b1 + b2 + b3
    if outcome == "2B":
        return outs, (False, True, b1), b2 + b3
    if outcome in ("1B", "E"):
        return outs, (True, b1, False), b2 + b3
    if outcome == "SAC":
        if outs >= 2:
            return outs + 1, bases, 0
        return outs + 1, (False, b1, b2), int(b3)
    if outcome == "GO" and b1 and outs < 2:
        return outs + 2, (False, False, b2 or b3), 0  # double play, lead runners hold-ish
    if outcome in ("GO", "FO") and b3 and outs < 2:
        return outs + 1, (b1, b2, False), 1
    return outs + 1, bases, 0


def expected_runs_after(outs: int, bases: tuple[bool, bool, bool], probs: Mapping[str, float]) -> float:
    """Expected runs (scored now + expectancy after) of a plate-appearance distribution."""
    total = sum(probs.values())
    value = 0.0
    for outcome, p in probs.items():
        n_outs, n_bases, runs = transition(outs, bases, outcome)
        value += p / total * (runs + tx.run_expectancy(n_outs, n_bases))
    return value


# ----------------------------------------------------------------------------- analysis


def _rep_effects(pitcher: Player, sequencing: str) -> tuple[float, float, float, float]:
    rep = tx.repertoire(pitcher)
    weights = tx.pitch_weights(rep, sequencing, 1, 1, None)
    total = sum(weights)
    acc = [0.0, 0.0, 0.0, 0.0]
    for p, w in zip(rep, weights, strict=True):
        for i, v in enumerate(tx.pitch_effects(rep, p.code)):
            acc[i] += w / total * v
    return acc[0], acc[1], acc[2], acc[3]


def _mix_bip(approach: str, exec_p: float) -> tuple[float, float, float]:
    mix = tx.location_mix(1, 1, approach)
    hr = hit = gb = 0.0
    total = 0.0
    for name, w in zip(tx.LOCATIONS, mix, strict=True):
        for loc_name, p in ((name, exec_p if name != "heart" else 0.96),
                            ("mistake", (1 - exec_p) * tx.MISTAKE_SHARE),
                            ("wild", (1 - exec_p) * (1 - tx.MISTAKE_SHARE))):
            h, t, g = tx.location_bip(loc_name)
            loc = tx.LOCATION_MODEL[loc_name]
            share = w * p * (loc.zone + (1 - loc.zone) * 0.35)
            hr += share * h
            hit += share * t
            gb += share * g
            total += share
    return hr / total, hit / total, gb / total


def expected_pa(game: GameSimulator, batter: Player, pitcher: Player, fielding: _Side, swing: tx.SwingCall,
                call: tx.PitchCall, align: tx.Alignment, tired: float | None = None,
                defense: float | None = None) -> dict[str, float]:
    """Outcome distribution of a plate appearance under the given instructions (analytical)."""
    probs = game.outcome_probabilities(batter, pitcher, fielding, tired, defense)
    tired_now = game.tiredness(fielding, pitcher.id) if tired is None else tired
    exec_neutral = tx.execution_chance(pitcher, "edge", 50, "normal", tired_now)
    cal = tx.calibrate(probs["K"], probs["BB"], probs["HBP"], exec_neutral)
    exec_p = tx.execution_chance(pitcher, "edge", 50, call.effort, tired_now)
    d_c, d_z, d_gb, d_hr = _rep_effects(pitcher, call.sequencing)
    mods = tx.PitchMods(zone=d_z, contact=d_c + tx.effort_contact(call.effort, pitcher))
    if call.hold_runners and any(game.runners()[:2]):
        mods.contact += 0.04
    tx.swing_mods(swing, batter, mods)
    mods.bunt = False
    mods.no_swing = False

    def dist(count: tuple[int, int]) -> tuple[float, float, float, float, float]:
        return tx.pitch_distribution(count, cal, mods, tx.location_mix(*count, call.approach), exec_p)

    k, bb, hbp, inplay, _ = tx.terminal(dist)
    shares = tx.bip_shares(probs)
    hr_m, hit_m, gb = _mix_bip(call.approach, exec_p)
    tx._apply_bip(shares, hr=hr_m * 2.718281828 ** d_hr, hit=hit_m, gb=gb + d_gb)
    tx.batter_bip_mods(shares, batter, swing)
    tx.alignment_bip_mods(shares, align, batter, game.runners(), game.outs)
    shares = tx.normalise(shares)
    out = {"K": k, "BB": bb, "HBP": hbp}
    out.update({o: inplay * v for o, v in shares.items()})
    return out


def pitch_value(game: GameSimulator, batter: Player, pitcher: Player, fielding: _Side, call: tx.PitchCall,
                swing: tx.SwingCall, location: str | None, code: str | None) -> float:
    """Expected batter run value of the next pitch under a pitch call (analytical)."""
    probs, cal = game.calibration(batter, pitcher, fielding)
    tired = game.tiredness(fielding, pitcher.id)
    rep = tx.repertoire(pitcher)
    if call.intentional_ball or call.pitchout:
        location, code = "waste", rep[0].code
    if code is None:
        weights = tx.pitch_weights(rep, call.sequencing, game.balls, game.strikes, None)
        total = sum(weights)
        effects = [0.0, 0.0, 0.0, 0.0]
        grade = 0.0
        for p, w in zip(rep, weights, strict=True):
            for i, v in enumerate(tx.pitch_effects(rep, p.code)):
                effects[i] += w / total * v
            grade += w / total * p.grade
    else:
        effects = list(tx.pitch_effects(rep, code))
        grade = next(p.grade for p in rep if p.code == code)
    exec_p = tx.execution_chance(pitcher, "edge", int(grade), call.effort, tired)
    mods = tx.PitchMods(zone=effects[1], contact=effects[0] + tx.effort_contact(call.effort, pitcher))
    tx.swing_mods(swing, batter, mods)
    mods.bunt = False
    if location == "waste":
        mods.no_swing = True
    count = (game.balls, game.strikes)
    mix = tx.location_mix(*count, call.approach)
    dist = tx.pitch_distribution(count, cal, mods, mix, exec_p, location)
    shares = tx.bip_shares(probs)
    if location and location != "waste":
        h, t, g = tx.location_bip(location)
    else:
        h, t, g = _mix_bip(call.approach, exec_p)
    tx._apply_bip(shares, hr=h * 2.718281828 ** effects[3], hit=t, gb=g + effects[2])
    tx.batter_bip_mods(shares, batter, swing)
    return tx.pitch_run_value(count, dist, tx.bip_value(shares))


def bunt_expectation(game: GameSimulator, batter: Player, corners: bool) -> float:
    """Expected runs (now + expectancy) of a sacrifice/bunt attempt from the current state."""
    outs, bases = game.outs, game.runners()
    speed = batter.rating("speed")
    skill = (batter.rating("contact") - 50) * 0.004 + (speed - 50) * 0.002
    if (bases[0] or bases[1]) and outs < 2:
        hit = max(0.0, 0.05 + (speed - 50) * 0.003 - (0.03 if corners else 0.0))
        sac = 0.70 + skill - (0.10 if corners else 0.0)
        fc = 0.15 + (0.06 if corners else 0.0)
        pop = max(0.0, 1 - hit - sac - fc)
        dist = {"BB": hit, "SAC": sac, "K": pop}
        value = expected_runs_after(outs, bases, dist) * (hit + sac + pop)
        fc_outs, fc_bases = outs + 1, (True, bases[0] and bases[1], bases[2])
        value += fc * tx.run_expectancy(fc_outs, fc_bases)
        return value
    hit = max(0.03, 0.20 + (speed - 50) * 0.008 + skill - (0.08 if corners else 0.0))
    return expected_runs_after(outs, bases, {"1B": hit, "K": 1 - hit})


# ----------------------------------------------------------------------------- CPU brain


class CpuBrain:
    """In-game CPU intelligence: pitching changes, steals, bunts, walks, alignment, subs."""

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng

    def pitching_change(self, game: GameSimulator, side: _Side, inning_start: bool,
                        hook: str = "normal") -> Player | None:
        if game.can_change_pitcher(side) is not None:
            return None
        other = game.away if side is game.home else game.home
        shift = HOOK_SHIFT.get(hook, 0)
        original = side.pitch_limit
        side.pitch_limit = max(15, original + shift)
        try:
            return game.auto_pitching_change(side, other, inning_start)
        finally:
            side.pitch_limit = original

    def want_ibb(self, game: GameSimulator, side: _Side) -> bool:
        b1, b2, b3 = game.runners()
        if b1 or not (b2 or b3) or game.outs >= 2 and not b3:
            return False
        if game.inning < 8:
            return False
        lead = side.box.runs - (game.away if side is game.home else game.home).box.runs
        if not -1 <= lead <= 1:
            return False
        batter = game.current_batter()
        batting = game.batting_side
        nxt = game.players[batting.order[(batting.batter_index + 1) % 9][0]]
        pitcher = game.current_pitcher()
        rv_now = tx.pa_run_value(game.outcome_probabilities(batter, pitcher, side))
        rv_next = tx.pa_run_value(game.outcome_probabilities(nxt, pitcher, side))
        return rv_now - rv_next > 0.07

    def steal_plan(self, game: GameSimulator, side: _Side) -> int | None:
        """Pitch number (0-based) to run on, using the classic attempt rate; or None."""
        runner = game.bases[0]
        if runner is None or game.bases[1] is not None or game.outs >= 2:
            return None
        speed = game.players[runner.player_id].rating("speed")
        # Runners also break on pitches that are fouled off or put in play, so a
        # planned attempt yields a steal attempt about two times in three.
        if self.rng.random() >= min(0.4, steal_attempt_rate(speed) * LIVE_STEAL_FACTOR):
            return None
        return 0 if self.rng.random() < 0.7 else 1

    def want_bunt(self, game: GameSimulator, side: _Side) -> bool:
        b1, b2, b3 = game.runners()
        if game.outs != 0 or not (b1 or b2) or b3 or game.inning < 7:
            return False
        diff = side.box.runs - (game.away if side is game.home else game.home).box.runs
        if not -1 <= diff <= 0:
            return False
        batter = game.current_batter()
        return batter.rating("power") < 45 and batter.rating("contact") >= 45

    def alignment(self, game: GameSimulator, side: _Side, current: tx.Alignment) -> tx.Alignment:
        b1, b2, b3 = game.runners()
        other = game.away if side is game.home else game.home
        diff = side.box.runs - other.box.runs
        infield = "normal"
        if b3 and game.outs < 2 and game.inning >= 7 and -1 <= diff <= 1:
            infield = "in"
        elif b1 and game.outs < 2:
            infield = "dp"
        outfield = "deep" if game.inning >= 9 and 1 <= diff <= 2 else "normal"
        return tx.Alignment(infield=infield, outfield=outfield, shift=current.shift, aggression=current.aggression)

    def pinch_hitter(self, game: GameSimulator, side: _Side) -> Player | None:
        if game.inning < 7:
            return None
        other = game.away if side is game.home else game.home
        if not -2 <= side.box.runs - other.box.runs <= 1:
            return None
        batter = game.current_batter()
        pitcher = game.current_pitcher()
        current = tx.pa_run_value(game.outcome_probabilities(batter, pitcher, other))
        best: tuple[float, Player] | None = None
        for p in game.bench(side):
            value = tx.pa_run_value(game.outcome_probabilities(p, pitcher, other))
            if best is None or value > best[0]:
                best = (value, p)
        if best is not None and best[0] - current > 0.04:
            return best[1]
        return None


class CpuManager:
    """Manages a club entirely by CPU logic (the opponent in a live game)."""

    def __init__(self, brain: CpuBrain, instructions: Instructions) -> None:
        self.brain = brain
        self.instructions = instructions
        self.steal_at: int | None = None
        self.bunt = False

    def on_half_start(self, game: GameSimulator, side: _Side) -> None:
        if side is game.fielding_side:
            reliever = self.brain.pitching_change(game, side, True)
            if reliever is not None:
                game.change_pitcher(side.key, reliever.id)

    def on_pa_start(self, game: GameSimulator, side: _Side) -> None:
        self.steal_at = None
        self.bunt = False
        if side is game.batting_side:
            ph = self.brain.pinch_hitter(game, side)
            if ph is not None:
                game.pinch_hit(side.key, ph.id)
            self.steal_at = self.brain.steal_plan(game, side)
            self.bunt = self.steal_at is None and self.brain.want_bunt(game, side) and self.brain.rng.random() < 0.5
            return
        reliever = self.brain.pitching_change(game, side, False)
        if reliever is not None:
            game.change_pitcher(side.key, reliever.id)
        self.instructions.alignment = self.brain.alignment(game, side, self.instructions.alignment)
        if self.brain.want_ibb(game, side):
            game.note(side.team.abbr, f"{side.team.abbr} intentionally walks {game.current_batter().short_name}.")
            game.intentional_walk(side.key)

    def pitch_call(self, game: GameSimulator, side: _Side) -> tx.PitchCall:
        plan = self.instructions.pitching
        return tx.PitchCall(effort=plan.effort, approach=plan.approach, sequencing=plan.sequencing,
                            hold_runners=plan.hold_runners)

    def swing_call(self, game: GameSimulator, side: _Side) -> tx.SwingCall:
        plan = self.instructions.plan_for(game.current_batter().id)
        team = self.instructions.batting
        steal = "steal" if self.steal_at is not None and len(game.pa_pitches) == self.steal_at else None
        return tx.SwingCall(approach=plan.approach, aggression=plan.aggression, discipline=plan.discipline,
                            effort=plan.effort, lead=team.lead, advance=team.advance, steal=steal,
                            bunt=self.bunt and game.strikes < 2)

    def alignment(self, game: GameSimulator, side: _Side) -> tx.Alignment:
        return self.instructions.alignment


class UserManager:
    """The user's club: instructions and one-shot calls, with CPU help per automation level."""

    def __init__(self, session: LiveGame) -> None:
        self.session = session

    def _auto(self, category: str) -> bool:
        return self.session.is_automatic(category)

    def on_half_start(self, game: GameSimulator, side: _Side) -> None:
        s = self.session
        if side is not game.fielding_side:
            return
        if self._auto("pitching"):
            reliever = s.brain.pitching_change(game, side, True, s.instructions.pitching.hook)
            if reliever is not None:
                s._auto_pitching_change(reliever, "auto")
        else:
            s._forced_change_if_exhausted()

    def on_pa_start(self, game: GameSimulator, side: _Side) -> None:
        s = self.session
        s.steal_at = None
        if side is game.batting_side:
            if self._auto("substitutions"):
                ph = s.brain.pinch_hitter(game, side)
                if ph is not None:
                    s._do_pinch_hit(ph.id, "auto")
            if self._auto("baserunning"):
                s.steal_at = s.brain.steal_plan(game, side)
                if s.steal_at is not None:
                    s._log("baserunning", "steal", f"Steal planned for {game.players[game.bases[0].player_id].short_name}"
                           if game.bases[0] else "Steal planned", game.bases[0].player_id if game.bases[0] else None,
                           "-", "steal", "auto", s._steal_assessment(), result_kinds=("SB", "CS", "PA"))
            if self._auto("batting") and s.steal_at is None and s.brain.want_bunt(game, side) \
                    and s.brain.rng.random() < 0.5:
                s.next_pitch.bunt = True
                s._log("batting", "bunt", f"Sacrifice bunt with {game.current_batter().short_name}",
                       game.current_batter().id, "swing away", "bunt", "auto", s._bunt_assessment())
            return
        if self._auto("pitching"):
            reliever = s.brain.pitching_change(game, side, False, s.instructions.pitching.hook)
            if reliever is not None:
                s._auto_pitching_change(reliever, "auto")
        else:
            s._forced_change_if_exhausted()
        if self._auto("defense"):
            new = s.brain.alignment(game, side, s.instructions.alignment)
            if new != s.instructions.alignment:
                old = s.instructions.alignment
                s.instructions.alignment = new
                s._log("defense", "alignment", f"Defense: infield {tx.LABELS[new.infield].lower()}, "
                       f"outfield {tx.LABELS[new.outfield].lower()}", None,
                       f"{old.infield}/{old.outfield}", f"{new.infield}/{new.outfield}", "auto",
                       s._alignment_assessment(old, new))
        if self._auto("pitching") and game.phase == PHASE_PA and s.brain.want_ibb(game, side):
            s._do_ibb("auto")

    def pitch_call(self, game: GameSimulator, side: _Side) -> tx.PitchCall:
        s = self.session
        plan = s.instructions.pitching
        batter = game.current_batter()
        nxt = s.next_pitch
        call = tx.PitchCall(pitch_type=nxt.pitch_type, location=nxt.location, effort=plan.effort,
                            approach=s.instructions.pitch_vs.get(batter.id, plan.approach),
                            sequencing=plan.sequencing, intentional_ball=nxt.intentional_ball,
                            pitchout=nxt.pitchout, hold_runners=plan.hold_runners)
        nxt.pitch_type = nxt.location = None
        nxt.intentional_ball = nxt.pitchout = False
        return call

    def swing_call(self, game: GameSimulator, side: _Side) -> tx.SwingCall:
        s = self.session
        batter = game.current_batter()
        plan = s.instructions.plan_for(batter.id)
        nxt = s.next_pitch
        steal = nxt.steal
        if steal is None and s.steal_at is not None and len(game.pa_pitches) == s.steal_at:
            steal = "steal"
        team = s.instructions.batting
        call = tx.SwingCall(approach=plan.approach, aggression=plan.aggression, discipline=plan.discipline,
                            effort=plan.effort, take=nxt.take, bunt=nxt.bunt, hit_and_run=nxt.hit_and_run,
                            steal=steal, lead=team.lead, advance=team.advance, send=nxt.send)
        nxt.take = nxt.hit_and_run = False
        nxt.steal = None
        if not nxt.bunt or game.strikes >= 2:
            nxt.bunt = False
        return call

    def alignment(self, game: GameSimulator, side: _Side) -> tx.Alignment:
        return self.session.instructions.alignment


# ----------------------------------------------------------------------------- the live game


class LiveGame:
    """The user's game in progress, managed from the dugout."""

    def __init__(self, sim: GameSimulator, user_key: str, key: str, label: str, record_stats: bool,
                 round_code: str | None, cpu_rng: random.Random) -> None:
        self.sim = sim
        self.user_key = user_key
        self.key = key
        self.label = label
        self.record_stats = record_stats
        self.round_code = round_code
        self.settings: dict[str, str] = dict(PRESETS["assisted"])
        self.instructions = Instructions()
        self.opponent_instructions = Instructions()
        self.next_pitch = NextPitch()
        self.decisions: list[Decision] = []
        self.prompt: Prompt | None = None
        self.answered: set[str] = set()
        self.suppress_prompts = False
        self.steal_at: int | None = None
        self.brain = CpuBrain(cpu_rng)
        self.user_manager = UserManager(self)
        self.cpu_manager = CpuManager(self.brain, self.opponent_instructions)
        self.user_side.manager = self.user_manager
        self.opp_side.manager = self.cpu_manager

    # -- accessors -------------------------------------------------------------------

    @property
    def game(self) -> GameSimulator:
        return self.sim

    @property
    def user_side(self) -> _Side:
        return self.sim.side(self.user_key)

    @property
    def opp_side(self) -> _Side:
        return self.sim.side("home" if self.user_key == "away" else "away")

    @property
    def user_batting(self) -> bool:
        return self.sim.batting_side is self.user_side

    @property
    def is_over(self) -> bool:
        return self.sim.is_over

    @property
    def result(self) -> GameResult | None:
        return self.sim.result

    def is_automatic(self, category: str) -> bool:
        mode = self.settings.get(category, "auto")
        return mode == "auto" or (mode == "assisted" and self.suppress_prompts)

    def set_mode(self, category: str, mode: str) -> None:
        if category not in CATEGORIES or mode not in MODES:
            raise ValueError(f"Unknown automation setting {category}={mode}.")
        self.settings[category] = mode

    def set_preset(self, preset: str) -> None:
        if preset not in PRESETS:
            raise ValueError(f"Unknown preset {preset}.")
        self.settings = dict(PRESETS[preset])

    def batter_plan(self) -> tx.BattingPlan:
        return self.instructions.plan_for(self.sim.current_batter().id)

    # -- logging ---------------------------------------------------------------------

    def _situation(self) -> dict[str, Any]:
        g = self.sim
        us, them = self.user_side.box.runs, self.opp_side.box.runs
        return {"inning": g.inning, "top": g.top, "outs": g.outs, "count": (g.balls, g.strikes),
                "bases": [r.player_id if r else 0 for r in g.bases], "score": (us, them)}

    def _log(self, category: str, action: str, text: str, player: int | None, previous: str, new: str,
             source: str, assessment: tx.Assessment | None = None,
             result_kinds: tuple[str, ...] = RESULT_KINDS) -> Decision:
        events = self.sim.events or []
        decision = Decision(seq=len(self.decisions), event=len(events), category=category, action=action,
                            text=text, player=player, previous=previous, new=new, source=source,
                            assessment=assessment, result_kinds=result_kinds, team=self.user_side.team.abbr,
                            **self._situation())
        self.decisions.append(decision)
        tag = {"manual": "", "auto": " (auto)", "assisted": " (assisted)", "forced": " (forced)"}.get(source, "")
        self.sim.note(self.user_side.team.abbr, f"{self.user_side.team.abbr}: {text}{tag}")
        decision.event = len(self.sim.events or [])
        return decision

    def _resolve_results(self) -> None:
        events = self.sim.events or []
        for d in self.decisions:
            if d.result is not None:
                continue
            for index in range(d.event, len(events)):
                if events[index]["k"] in d.result_kinds:
                    d.result = index
                    break

    # -- assessments -----------------------------------------------------------------

    def _swing_for(self, batter: Player) -> tx.SwingCall:
        ins = self.instructions if self.sim.batting_side is self.user_side else self.opponent_instructions
        plan = ins.plan_for(batter.id)
        return tx.SwingCall(approach=plan.approach, aggression=plan.aggression, discipline=plan.discipline,
                            effort=plan.effort, lead=ins.batting.lead, advance=ins.batting.advance)

    def _call_for(self, batter: Player) -> tx.PitchCall:
        ins = self.instructions if self.sim.fielding_side is self.user_side else self.opponent_instructions
        plan = ins.pitching
        return tx.PitchCall(effort=plan.effort, approach=ins.pitch_vs.get(batter.id, plan.approach),
                            sequencing=plan.sequencing, hold_runners=plan.hold_runners)

    def _align_for(self) -> tx.Alignment:
        ins = self.instructions if self.sim.fielding_side is self.user_side else self.opponent_instructions
        return ins.alignment

    def _plan_assessment(self, swing_old: tx.SwingCall, swing_new: tx.SwingCall, call_old: tx.PitchCall,
                         call_new: tx.PitchCall, align_old: tx.Alignment, align_new: tx.Alignment) -> tx.Assessment:
        g = self.sim
        batter, pitcher, fielding = g.current_batter(), g.current_pitcher(), g.fielding_side
        old = expected_pa(g, batter, pitcher, fielding, swing_old, call_old, align_old)
        new = expected_pa(g, batter, pitcher, fielding, swing_new, call_new, align_new)
        rv_old, rv_new = tx.pa_run_value(old), tx.pa_run_value(new)
        sign = 1 if self.user_batting else -1
        value = sign * (rv_new - rv_old)
        basis = (f"vs {batter.short_name}: expected runs/PA {rv_old:+.3f} -> {rv_new:+.3f}; "
                 f"K {old['K']:.0%}->{new['K']:.0%}, BB {old['BB']:.0%}->{new['BB']:.0%}, "
                 f"HR {old['HR']:.1%}->{new['HR']:.1%}")
        return tx.Assessment(tx.grade_value(value, 0.004), basis, value)

    def _alignment_assessment(self, old: tx.Alignment, new: tx.Alignment) -> tx.Assessment:
        g = self.sim
        batter = g.current_batter()
        call, swing = self._call_for(batter), self._swing_for(batter)
        base = self._plan_assessment(swing, swing, call, call, old, new)
        value = base.value
        notes = []
        b1, b2, b3 = g.runners()
        if g.outs < 2:
            probs = expected_pa(g, batter, g.current_pitcher(), g.fielding_side, swing, call, new)
            if b3 and (new.infield == "in") != (old.infield == "in"):
                runs_saved = probs["GO"] * (0.5 - 0.18)
                value += runs_saved if new.infield == "in" else -runs_saved
                notes.append(f"run on a grounder {50 if new.infield != 'in' else 18}% likely")
            if b1 and (new.infield == "dp") != (old.infield == "dp"):
                dp_value = probs["GO"] * 0.08 * 0.6
                value += dp_value if new.infield == "dp" else -dp_value
                notes.append("double-play depth")
        basis = base.basis + (f"; {', '.join(notes)}" if notes else "")
        return tx.Assessment(tx.grade_value(value, 0.004), basis, value)

    def _steal_assessment(self, delayed: bool = False) -> tx.Assessment | None:
        g = self.sim
        targets = g.steal_targets()
        if not targets:
            return None
        batter = g.current_batter()
        swing = self._swing_for(batter)
        call = self._call_for(batter)
        p = g.steal_probability(targets[0], swing, call, None, delayed)
        need = tx.steal_break_even(g.outs, g.runners(), targets[0])
        value = (p - need) * 0.6
        basis = f"success chance ~{p:.0%} vs break-even {need:.0%} ({'third' if targets[0] == 3 else 'second'})"
        return tx.Assessment(tx.grade_value(value, 0.02), basis, value)

    def _bunt_assessment(self) -> tx.Assessment:
        g = self.sim
        batter = g.current_batter()
        corners = self.opponent_instructions.alignment.infield == "corners"
        bunt = bunt_expectation(g, batter, corners)
        probs = expected_pa(g, batter, g.current_pitcher(), g.fielding_side, self._swing_for(batter),
                            self._call_for(batter), self.opponent_instructions.alignment)
        swing_away = expected_runs_after(g.outs, g.runners(), probs)
        value = bunt - swing_away
        basis = f"expected runs this inning: bunt {bunt:.2f} vs swing away {swing_away:.2f}"
        if g.inning >= 8 and abs(self.user_side.box.runs - self.opp_side.box.runs) <= 1:
            basis += " (late and close: one run matters more than the average)"
        return tx.Assessment(tx.grade_value(value, 0.02), basis, value)

    def _ibb_assessment(self) -> tx.Assessment:
        g = self.sim
        batter = g.current_batter()
        before = tx.run_expectancy(g.outs, g.runners())
        outs, bases, runs = transition(g.outs, g.runners(), "IBB")
        walk = runs + tx.run_expectancy(outs, bases)
        probs = expected_pa(g, batter, g.current_pitcher(), g.fielding_side, self._swing_for(batter),
                            self._call_for(batter), self._align_for())
        pitch_to = expected_runs_after(g.outs, g.runners(), probs)
        value = pitch_to - walk
        basis = (f"expected runs: walk {walk:.2f} vs pitching to {batter.short_name} {pitch_to:.2f} "
                 f"(state now {before:.2f}); ignores the next hitter's quality")
        return tx.Assessment(tx.grade_value(value, 0.03), basis, value)

    def _pitching_change_assessment(self, new_id: int) -> tx.Assessment:
        g = self.sim
        side = self.user_side
        batting = g.batting_side if g.fielding_side is side else g.side(self.opp_side.key)
        current = g.players[side.pitcher_id]
        new = g.players[new_id]
        upcoming = [g.players[batting.order[(batting.batter_index + i) % 9][0]] for i in range(3)]
        old_rv = new_rv = 0.0
        new_tired = min(25.0, new.fatigue * 0.05)
        for b in upcoming:
            old_rv += tx.pa_run_value(g.outcome_probabilities(b, current, side))
            new_rv += tx.pa_run_value(g.outcome_probabilities(b, new, side, tired=new_tired))
        value = old_rv - new_rv
        load = g.tiredness(side)
        basis = (f"next 3 hitters: {current.short_name} {old_rv:+.2f} runs (fatigue {load:.0f}) vs "
                 f"{new.short_name} {new_rv:+.2f} runs")
        return tx.Assessment(tx.grade_value(value, 0.03), basis, value)

    def _sub_assessment(self, out_id: int, in_id: int, kind: str) -> tx.Assessment:
        g = self.sim
        old, new = g.players[out_id], g.players[in_id]
        if kind == "PH":
            pitcher = g.current_pitcher()
            fielding = g.fielding_side
            a = tx.pa_run_value(g.outcome_probabilities(old, pitcher, fielding))
            b = tx.pa_run_value(g.outcome_probabilities(new, pitcher, fielding))
            value = b - a
            basis = f"vs {pitcher.short_name}: {old.short_name} {a:+.3f} vs {new.short_name} {b:+.3f} runs/PA"
            pos = self.user_side.position_of(out_id) or ""
            if pos not in ("DH", "") and not new.can_play(pos):
                basis += f"; {new.short_name} is out of position at {pos}"
            return tx.Assessment(tx.grade_value(value, 0.01), basis, value)
        if kind == "PR":
            delta = new.rating("speed") - old.rating("speed")
            value = delta * 0.002
            return tx.Assessment(tx.grade_value(value, 0.01),
                                 f"speed {old.rating('speed')} -> {new.rating('speed')}", value)
        side = self.user_side
        pos = side.position_of(out_id) or "DH"
        order = [(in_id if pid == out_id else pid, p) for pid, p in side.order]
        new_def = order_defense(order, g.players)
        value = (new_def - side.defense) * 0.004 * 3
        basis = (f"team defense {side.defense:.1f} -> {new_def:.1f} at {pos}; "
                 f"bat {old.overall_at('DH')} -> {new.overall_at('DH')} OVR")
        return tx.Assessment(tx.grade_value(value, 0.01), basis, value)

    def _pitch_call_assessment(self, location: str | None, code: str | None,
                               ib: bool = False, pitchout: bool = False) -> tx.Assessment:
        g = self.sim
        batter, pitcher, fielding = g.current_batter(), g.current_pitcher(), g.fielding_side
        base = self._call_for(batter)
        swing = self._swing_for(batter)
        auto_rv = pitch_value(g, batter, pitcher, fielding, base, swing, None, None)
        chosen = tx.PitchCall(effort=base.effort, approach=base.approach, sequencing=base.sequencing,
                              intentional_ball=ib, pitchout=pitchout, hold_runners=base.hold_runners)
        rv = pitch_value(g, batter, pitcher, fielding, chosen, swing, location, code)
        value = auto_rv - rv  # fielding perspective
        basis = f"count {g.balls}-{g.strikes}: batter value {rv:+.3f} vs {auto_rv:+.3f} for the catcher's mix"
        if pitchout and g.steal_targets():
            t = g.steal_targets()[0]
            p0 = g.steal_probability(t, swing, base)
            p1 = g.steal_probability(t, swing, chosen)
            basis += f"; a steal would succeed {p1:.0%} instead of {p0:.0%}"
            return tx.Assessment("informational", basis, value)
        return tx.Assessment(tx.grade_value(value, 0.006), basis, value)

    def _swing_one_shot_assessment(self, take: bool) -> tx.Assessment:
        g = self.sim
        batter, pitcher, fielding = g.current_batter(), g.current_pitcher(), g.fielding_side
        call = self._call_for(batter)
        swing = self._swing_for(batter)
        normal = pitch_value(g, batter, pitcher, fielding, call, swing, None, None)
        swing.take = take
        chosen = pitch_value(g, batter, pitcher, fielding, call, swing, None, None)
        value = chosen - normal
        basis = f"count {g.balls}-{g.strikes}: pitch value {chosen:+.3f} vs {normal:+.3f} swinging normally"
        return tx.Assessment(tx.grade_value(value, 0.006), basis, value)

    # -- internal moves --------------------------------------------------------------

    def _auto_pitching_change(self, reliever: Player, source: str) -> None:
        g = self.sim
        side = self.user_side
        current = g.players[side.pitcher_id]
        assessment = self._pitching_change_assessment(reliever.id)
        g.change_pitcher(side.key, reliever.id)
        self._log("pitching", "change", f"Pitching change: {reliever.short_name} replaces {current.short_name}",
                  reliever.id, current.short_name, reliever.short_name, source, assessment)

    def _forced_change_if_exhausted(self) -> None:
        g = self.sim
        side = self.user_side
        if side is not g.fielding_side:
            return
        if g._load(side, side.pitcher_id) < side.pitch_limit + EXHAUSTION_MARGIN:
            return
        reliever = g._choose_reliever(side, side.box.runs - self.opp_side.box.runs)
        if reliever is None or g.can_change_pitcher(side) is not None:
            return
        self._auto_pitching_change(reliever, "forced")

    def _do_pinch_hit(self, pid: int, source: str) -> None:
        g = self.sim
        old = g.current_batter()
        assessment = self._sub_assessment(old.id, pid, "PH")
        g.pinch_hit(self.user_key, pid)
        self._log("substitutions", "pinch_hit", f"{g.players[pid].short_name} pinch-hits for {old.short_name}",
                  pid, old.short_name, g.players[pid].short_name, source, assessment)

    def _do_ibb(self, source: str) -> None:
        g = self.sim
        batter = g.current_batter()
        assessment = self._ibb_assessment()
        self._log("pitching", "ibb", f"Intentional walk to {batter.short_name}", batter.id, "pitch to him",
                  "walk", source, assessment)
        g.intentional_walk(self.user_key)

    # -- prompts (assisted mode) -----------------------------------------------------

    def _assisted(self, category: str) -> bool:
        return self.settings.get(category) == "assisted" and not self.suppress_prompts

    def _check_prompt(self) -> Prompt | None:
        g = self.sim
        if g.phase not in (PHASE_HALF, PHASE_PA) or g.is_over:
            return None
        side = self.user_side
        tag = f"{g.inning}:{g.top}:{len(g.events or [])}"
        fielding_next = g.fielding_side is side
        if fielding_next and self._assisted("pitching"):
            reliever = self.brain.pitching_change(g, side, g.phase == PHASE_HALF, self.instructions.pitching.hook)
            if reliever is not None:
                # Asked again only after 15 more pitches (or a new inning) once declined.
                pitches = side.box.pitching[side.pitcher_id].pitches
                key = f"pc:{side.pitcher_id}:{g.inning}:{g.top}:{pitches // 15}"
                if key not in self.answered:
                    cur = g.players[side.pitcher_id]
                    line = side.box.pitching[cur.id]
                    a = self._pitching_change_assessment(reliever.id)
                    return Prompt(key, "pitching",
                                  f"{cur.short_name} has thrown {line.pitches} pitches ({line.ip_display} IP, "
                                  f"{line.r} R). Bring in {reliever.short_name}?",
                                  [(f"change:{reliever.id}", f"Bring in {reliever.short_name}"),
                                   ("stay", f"Stay with {cur.short_name}")], a.basis)
        if g.phase != PHASE_PA:
            return None
        if fielding_next:
            if self._assisted("pitching") and self.brain.want_ibb(g, side):
                key = f"ibb:{tag}"
                if key not in self.answered:
                    a = self._ibb_assessment()
                    b = g.current_batter()
                    return Prompt(key, "pitching", f"First base is open. Intentionally walk {b.short_name}?",
                                  [("ibb", "Walk him"), ("pitch", "Pitch to him")], a.basis)
            if self._assisted("defense"):
                new = self.brain.alignment(g, side, self.instructions.alignment)
                if new.infield == "in" and self.instructions.alignment.infield != "in":
                    key = f"inf:{g.inning}:{g.top}:{g.outs}:{tuple(g.runners())}"
                    if key not in self.answered:
                        a = self._alignment_assessment(self.instructions.alignment, new)
                        return Prompt(key, "defense", "Runner on third, less than two outs. Bring the infield in?",
                                      [("infield:in", "Infield in"), ("keep", "Keep current depth")], a.basis)
            return None
        # User batting.
        if self._assisted("substitutions"):
            ph = self.brain.pinch_hitter(g, side)
            if ph is not None:
                key = f"ph:{tag}"
                if key not in self.answered:
                    a = self._sub_assessment(g.current_batter().id, ph.id, "PH")
                    return Prompt(key, "substitutions",
                                  f"Pinch-hit {ph.short_name} for {g.current_batter().short_name}?",
                                  [(f"ph:{ph.id}", f"Send up {ph.short_name}"), ("keep", "Let him hit")], a.basis)
        if self._assisted("baserunning") and g.steal_targets() == [2] and g.outs < 2:
            runner = g.players[g.bases[0].player_id]  # type: ignore[union-attr]
            a = self._steal_assessment()
            if runner.rating("speed") >= 65 and a is not None and a.value > 0.0:
                key = f"st:{tag}"
                if key not in self.answered:
                    return Prompt(key, "baserunning", f"{runner.short_name} has the speed to run. Send him?",
                                  [("steal", "Steal on the first pitch"), ("hold", "Hold him")], a.basis)
        if self._assisted("batting") and self.brain.want_bunt(g, side):
            key = f"bu:{tag}"
            if key not in self.answered:
                a = self._bunt_assessment()
                return Prompt(key, "batting", f"Sacrifice bunt with {g.current_batter().short_name}?",
                              [("bunt", "Bunt") if a.value >= 0 else ("swing", "Swing away"),
                               ("swing", "Swing away") if a.value >= 0 else ("bunt", "Bunt")], a.basis)
        return None

    def answer_prompt(self, value: str) -> str:
        prompt = self.prompt
        if prompt is None:
            raise RuleError("There is no decision waiting.")
        self.answered.add(prompt.key)
        self.prompt = None
        recommended = prompt.options[0][0]
        source = "assisted" if value == recommended else "manual"
        g = self.sim
        if value.startswith("change:"):
            pid = int(value.split(":")[1])
            self._auto_pitching_change(g.players[pid], source)
            return f"{g.players[pid].short_name} is coming in."
        if value.startswith("ph:"):
            self._do_pinch_hit(int(value.split(":")[1]), source)
            return "Pinch hitter announced."
        if value == "ibb":
            self._do_ibb(source)
            return "Intentional walk."
        if value == "infield:in":
            return self.apply("def.infield", "in", source=source)
        if value == "steal":
            return self.apply("run.steal", "steal", source=source)
        if value == "bunt":
            return self.apply("bat.bunt", "on", source=source)
        self._log(prompt.category, "declined", f"Declined: {prompt.question}", None, "-", value, "manual",
                  tx.Assessment("informational", prompt.basis))
        return "Decision noted."

    # -- progression -----------------------------------------------------------------

    def advance(self, unit: str = "pitch") -> AdvanceReport:
        """Move the game on by ``unit``; stops early for assisted-mode prompts."""
        if unit not in UNITS:
            raise ValueError(f"Unknown progression unit {unit}.")
        g = self.sim
        events = g.events
        assert events is not None  # live games always record events
        start = len(events)
        if self.prompt is not None:
            return AdvanceReport([], "prompt")
        start_half = (g.inning, g.top)
        for _ in range(STEP_LIMIT):
            if g.is_over:
                break
            prompt = self._check_prompt()
            if prompt is not None:
                self.prompt = prompt
                self._resolve_results()
                return AdvanceReport(events[start:], "prompt")
            before = len(events)
            phase_before = g.phase
            g.step()
            new = events[before:]
            if any(e["k"] == "PA" or e["k"] == "HALF" for e in new):
                # Signs that last for a plate appearance end with it.
                self.next_pitch.bunt = False
                self.next_pitch.send = None
            self._resolve_results()
            if self._unit_done(unit, new, phase_before, start_half):
                break
        else:
            return AdvanceReport(events[start:], "limit")
        if g.is_over:
            self._finalize()
            return AdvanceReport(events[start:], "final")
        return AdvanceReport(events[start:], "unit")

    def _unit_done(self, unit: str, new: list[dict[str, Any]], phase_before: str,
                   start_half: tuple[int, bool]) -> bool:
        g = self.sim
        kinds = {e["k"] for e in new}
        if g.is_over:
            return True
        if unit == "pitch":
            return bool(kinds & {"P", "PA", "SB", "CS", "PK"})
        if unit == "play":
            return bool(kinds & {"PA", "SB", "CS", "TO"}) or any(e["k"] == "PK" and e["r"] == "out" for e in new)
        if unit == "pa":
            return "PA" in kinds or (g.phase == PHASE_HALF and phase_before != PHASE_HALF)
        if unit == "half":
            return g.phase == PHASE_HALF and phase_before != PHASE_HALF
        if unit == "inning":
            return g.phase == PHASE_HALF and phase_before != PHASE_HALF and g.top
        return False

    def finish(self) -> AdvanceReport:
        """Play the rest of the game without stopping (assisted decisions are automated)."""
        self.suppress_prompts = True
        self.prompt = None
        try:
            return self.advance("game")
        finally:
            self.suppress_prompts = False

    def _finalize(self) -> None:
        result = self.sim.result
        if result is not None and result.manager_report is None:
            result.manager_report = self.summary()

    # -- controls --------------------------------------------------------------------

    def controls(self) -> list[Control]:
        """Context-sensitive controls for the current decision point."""
        g = self.sim
        out: list[Control] = []
        if g.is_over:
            return out
        side = self.user_side
        phase = g.phase
        fielding_next = g.fielding_side is side
        batting_now = not fielding_next

        def choice(key: str, group: str, label: str, values: tuple[str, ...] | list[tuple[str, str]],
                   value: str | None, scope: str, enabled: bool = True, reason: str = "", hotkey: str = "") -> None:
            opts = [(v, tx.LABELS.get(v, v)) for v in values] if values and isinstance(values[0], str) \
                else list(values)  # type: ignore[arg-type]
            out.append(Control(key, group, label, "choice", opts, value, enabled, reason, scope, hotkey))

        def action(key: str, group: str, label: str, scope: str, enabled: bool = True, reason: str = "",
                   value: str | None = None, hotkey: str = "") -> None:
            out.append(Control(key, group, label, "action", [], value, enabled, reason, scope, hotkey))

        runners = g.runners()
        manual_pitch = not self.is_automatic("pitching")
        auto_reason = "Automated - set Pitching to Manual or Assisted to call pitches."
        if fielding_next:
            plan = self.instructions.pitching
            pitcher = g.current_pitcher()
            if phase == PHASE_PITCH:
                rep = tx.repertoire(pitcher)
                choice("pitch.type", "Pitching", "Pitch type",
                       [("auto", "Catcher's call"), *[(p.code, f"{p.name} ({p.grade})") for p in rep]],
                       self.next_pitch.pitch_type or "auto", "next pitch", manual_pitch, "" if manual_pitch else
                       auto_reason, "t")
                choice("pitch.location", "Pitching", "Location", [("auto", "Catcher's call"),
                       *[(v, tx.LABELS[v]) for v in tx.LOCATIONS]], self.next_pitch.location or "auto",
                       "next pitch", manual_pitch, "" if manual_pitch else auto_reason, "o")
                action("pitch.intentional_ball", "Pitching", "Intentional ball", "next pitch", manual_pitch,
                       "" if manual_pitch else auto_reason, "on" if self.next_pitch.intentional_ball else None)
                if g.steal_targets():
                    action("pitch.pitchout", "Pitching", "Pitch out", "next pitch", manual_pitch,
                           "" if manual_pitch else auto_reason, "on" if self.next_pitch.pitchout else None)
            choice("pitch.effort", "Pitching", "Effort", tx.PITCH_EFFORT, plan.effort, "plan", hotkey="e")
            choice("pitch.sequencing", "Pitching", "Sequencing", tx.SEQUENCING, plan.sequencing, "plan")
            choice("pitch.approach", "Pitching", "Approach", tx.PITCH_APPROACH, plan.approach, "plan")
            if phase in (PHASE_PA, PHASE_PITCH):
                batter = g.current_batter()
                choice("match.approach", "Pitching", f"Approach vs {batter.short_name}",
                       [("plan", "Use game plan"), *[(v, tx.LABELS[v]) for v in tx.PITCH_APPROACH]],
                       self.instructions.pitch_vs.get(batter.id, "plan"), "batter")
            if phase == PHASE_HALF:
                choice("pitch.hook", "Pitching", "Workload", tx.HOOK, plan.hook, "plan")
            if runners[0] or runners[1]:
                choice("run.hold", "Baserunning", "Hold runners", [("off", "Normal"), ("on", "Hold close")],
                       "on" if plan.hold_runners else "off", "plan")
            reason = g.can_change_pitcher(side)
            pen = g.bullpen(side)
            choice("sub.pitcher", "Substitutions", "Change pitcher",
                   [(str(p.id), self._pen_label(p)) for p in pen], None, "now", reason is None,
                   reason or "", "c")
            if phase == PHASE_PA:
                action("def.ibb", "Pitching", "Intentional walk", "now", manual_pitch,
                       "" if manual_pitch else auto_reason, hotkey="i")
            align = self.instructions.alignment
            choice("def.infield", "Defense", "Infield", tx.INFIELD, align.infield, "plan")
            choice("def.outfield", "Defense", "Outfield", tx.OUTFIELD, align.outfield, "plan")
            choice("def.shift", "Defense", "Shading (no extreme shifts)", tx.SHIFT, align.shift, "plan")
            choice("def.aggression", "Defense", "Defensive aggressiveness", tx.DEF_AGGRESSION, align.aggression,
                   "plan")
            if phase in (PHASE_HALF, PHASE_PA):
                bench = g.bench(side)
                opts = [(f"{i}:{p.id}", f"{pos} {g.players[pid].short_name} -> {p.short_name}"
                         f" ({'can play' if p.can_play(pos) else 'out of position'})")
                        for i, (pid, pos) in enumerate(side.order) if pos != "DH" for p in bench]
                choice("sub.defense", "Substitutions", "Defensive replacement", opts, None, "now", bool(opts),
                       "" if opts else "No bench players left.")
        if batting_now:
            batter = g.current_batter()
            plan = self.instructions.plan_for(batter.id)
            manual_bat = not self.is_automatic("batting")
            manual_run = not self.is_automatic("baserunning")
            bat_reason = "Automated - set Hitting to Manual or Assisted."
            run_reason = "Automated - set Baserunning to Manual or Assisted."
            if phase in (PHASE_PA, PHASE_PITCH):
                choice("bat.approach", "Hitting", f"Approach ({batter.short_name})", tx.BAT_APPROACHES,
                       plan.approach, "batter", hotkey="a")
                choice("bat.aggression", "Hitting", "Swing aggression", tx.AGGRESSION, plan.aggression, "batter")
                choice("bat.discipline", "Hitting", "Plate discipline", tx.DISCIPLINE, plan.discipline, "batter")
                choice("bat.effort", "Hitting", "Contact effort", tx.SWING_EFFORT, plan.effort, "batter")
            if phase == PHASE_PITCH:
                action("bat.take", "Hitting", "Take the pitch", "next pitch", manual_bat,
                       "" if manual_bat else bat_reason, "on" if self.next_pitch.take else None, "k")
            if phase in (PHASE_PA, PHASE_PITCH):
                action("bat.bunt", "Hitting", "Bunt", "until two strikes" if g.strikes < 2 else "next pitch",
                       manual_bat, "" if manual_bat else bat_reason, "on" if self.next_pitch.bunt else None, "b")
                if runners[0] and g.outs < 2:
                    action("bat.hit_and_run", "Hitting", "Hit-and-run", "next pitch", manual_bat and manual_run,
                           "" if manual_bat and manual_run else run_reason,
                           "on" if self.next_pitch.hit_and_run else None, "h")
            if phase in (PHASE_HALF,):
                tb = self.instructions.batting
                choice("plan.approach", "Hitting", "Team approach", tx.BAT_APPROACHES, tb.approach, "plan")
                choice("plan.aggression", "Hitting", "Team aggression", tx.AGGRESSION, tb.aggression, "plan")
                choice("plan.discipline", "Hitting", "Team discipline", tx.DISCIPLINE, tb.discipline, "plan")
            team_plan = self.instructions.batting
            if any(runners):
                choice("run.advance", "Baserunning", "Advancement", tx.ADVANCE, team_plan.advance, "plan")
                choice("run.send", "Baserunning", "Next ball in play", [("auto", "Read the play"),
                       ("send", "Send runners"), ("hold", "Hold runners")], self.next_pitch.send or "auto",
                       "next play", manual_run, "" if manual_run else run_reason)
            if runners[0] or runners[1]:
                choice("run.lead", "Baserunning", "Lead", tx.LEADS, team_plan.lead, "plan")
            if g.steal_targets() and phase in (PHASE_PA, PHASE_PITCH):
                action("run.steal", "Baserunning", "Steal", "next pitch", manual_run, "" if manual_run else
                       run_reason, "on" if self.next_pitch.steal == "steal" else None, "s")
                action("run.delayed", "Baserunning", "Delayed steal", "next pitch", manual_run,
                       "" if manual_run else run_reason, "on" if self.next_pitch.steal == "delayed" else None)
            bench = g.bench(side)
            if phase == PHASE_PA:
                choice("sub.pinch_hit", "Substitutions", f"Pinch-hit for {batter.short_name}",
                       [(str(p.id), self._bench_label(p)) for p in bench], None, "now", bool(bench),
                       "" if bench else "No bench players left.", "p")
            if phase in (PHASE_PA, PHASE_PITCH) and any(runners):
                opts = [(f"{i + 1}:{p.id}", f"{('1st', '2nd', '3rd')[i]} {g.players[r.player_id].short_name}"
                         f" -> {p.short_name} (speed {p.rating('speed')})")
                        for i, r in enumerate(g.bases) if r for p in bench]
                choice("sub.pinch_run", "Substitutions", "Pinch runner", opts, None, "now", bool(opts),
                       "" if opts else "No bench players left.")
        if phase == PHASE_HALF:
            action("lineup.order", "Substitutions", "Reorder batting order", "now", False,
                   "The batting order is fixed once the game starts; use substitutions.")
        for category in CATEGORIES:
            choice(f"auto.{category}", "Automation", CATEGORY_LABELS[category],
                   [(m, MODE_LABELS[m]) for m in MODES], self.settings[category], "setting")
        return out

    def _pen_label(self, p: Player) -> str:
        role = "SP" if p.id in self.user_side.team.rotation else ("CL" if p.id == self.user_side.team.closer
                                                                    else "RP")
        rest = "rested" if p.fatigue <= 15 else "tired" if p.fatigue <= 45 else "gassed"
        return f"{p.short_name} {role} {p.throws}HP  OVR {p.overall}  {rest}"

    def _bench_label(self, p: Player) -> str:
        return (f"{p.short_name} {p.position} {p.bats}  CON {p.rating('contact')} POW {p.rating('power')} "
                f"SPD {p.rating('speed')}")

    def control(self, key: str) -> Control | None:
        return next((c for c in self.controls() if c.key == key), None)

    # -- applying controls -----------------------------------------------------------

    def apply(self, key: str, value: str | None = None, source: str = "manual") -> str:
        """Perform one control. Returns a short confirmation; raises RuleError when not allowed."""
        g = self.sim
        if g.is_over:
            raise RuleError("The game is over.")
        ctrl = self.control(key)
        if ctrl is None:
            raise RuleError("That control is not available right now.")
        if not ctrl.enabled:
            raise RuleError(ctrl.reason or "Not available right now.")
        if ctrl.kind == "choice" and value not in {v for v, _ in ctrl.options}:
            raise RuleError(f"Invalid choice {value!r}.")
        group, name = key.split(".", 1)
        if group == "auto":
            previous = self.settings[name]
            self.set_mode(name, str(value))
            return f"{CATEGORY_LABELS[name]}: {MODE_LABELS[str(value)]} (was {MODE_LABELS[previous]})."
        handler = getattr(self, f"_apply_{group}_{name}", None)
        if handler is None:
            raise RuleError("Unknown control.")
        if self.prompt is not None and group not in ("auto",):
            # Acting directly resolves the pending prompt as a manual decision.
            self.answered.add(self.prompt.key)
            self.prompt = None
        return handler(value, source)

    # pitching ---------------------------------------------------------------------

    def _apply_pitch_type(self, value: str | None, source: str) -> str:
        code = None if value in (None, "auto") else value
        prev = self.next_pitch.pitch_type or "auto"
        self.next_pitch.pitch_type = code
        if code is None:
            return "Pitch type back to the catcher's call."
        a = self._pitch_call_assessment(self.next_pitch.location, code)
        self._log("pitching", "pitch_type", f"Call: {tx.PITCH_TYPES[code].name}", self.sim.current_pitcher().id,
                  prev, code, source, a, ("P",))
        return f"Next pitch: {tx.PITCH_TYPES[code].name}."

    def _apply_pitch_location(self, value: str | None, source: str) -> str:
        loc = None if value in (None, "auto") else value
        prev = self.next_pitch.location or "auto"
        self.next_pitch.location = loc
        if loc is None:
            return "Location back to the catcher's call."
        a = self._pitch_call_assessment(loc, self.next_pitch.pitch_type)
        self._log("pitching", "location", f"Location: {tx.LABELS[loc].lower()}", self.sim.current_pitcher().id,
                  prev, loc, source, a, ("P",))
        return f"Next pitch: {tx.LABELS[loc].lower()}."

    def _apply_pitch_intentional_ball(self, value: str | None, source: str) -> str:
        self.next_pitch.intentional_ball = not self.next_pitch.intentional_ball
        if self.next_pitch.intentional_ball:
            self.next_pitch.pitchout = False
            a = self._pitch_call_assessment("waste", None, ib=True)
            self._log("pitching", "intentional_ball", "Intentional ball", self.sim.current_pitcher().id, "pitch",
                      "intentional ball", source, a, ("P",))
            return "Next pitch: intentional ball."
        return "Intentional ball cancelled."

    def _apply_pitch_pitchout(self, value: str | None, source: str) -> str:
        self.next_pitch.pitchout = not self.next_pitch.pitchout
        if self.next_pitch.pitchout:
            self.next_pitch.intentional_ball = False
            a = self._pitch_call_assessment("waste", None, pitchout=True)
            self._log("pitching", "pitchout", "Pitchout", self.sim.current_pitcher().id, "pitch", "pitchout", source,
                      a, ("P",))
            return "Next pitch: pitchout."
        return "Pitchout cancelled."

    def _plan_change(self, category: str, action: str, label: str, attr_owner: Any, attr: str, value: str,
                     source: str, player: int | None = None) -> str:
        previous = getattr(attr_owner, attr)
        if previous == value:
            return f"{label} already {tx.LABELS.get(str(value), value)}."
        g = self.sim
        batter = g.current_batter()
        swing_old = self._swing_for(batter)
        call_old = self._call_for(batter)
        align_old = tx.Alignment.from_dict(self._align_for().to_dict())
        setattr(attr_owner, attr, value)
        swing_new = self._swing_for(batter)
        call_new = self._call_for(batter)
        align_new = self._align_for()
        assessment: tx.Assessment | None
        if category == "defense":
            assessment = self._alignment_assessment(align_old, align_new)
        elif category in ("batting", "pitching") and attr != "hook":
            assessment = self._plan_assessment(swing_old, swing_new, call_old, call_new, align_old, align_new)
            if attr == "effort" and category == "pitching":
                stress = tx.EFFORT_STRESS[value]
                assessment.basis += f"; fatigue cost x{stress:.2f} per pitch"
                assessment.grade = "informational" if value != "normal" else assessment.grade
        else:
            assessment = tx.Assessment("informational", f"{label}: {previous} -> {value}")
        self._log(category, action, f"{label}: {tx.LABELS.get(str(value), value)}", player,
                  str(previous), str(value), source, assessment)
        return f"{label}: {tx.LABELS.get(str(value), value)}."

    def _apply_pitch_effort(self, value: str | None, source: str) -> str:
        return self._plan_change("pitching", "effort", "Pitching effort", self.instructions.pitching, "effort",
                                 str(value), source, self.sim.current_pitcher().id)

    def _apply_pitch_sequencing(self, value: str | None, source: str) -> str:
        return self._plan_change("pitching", "sequencing", "Sequencing", self.instructions.pitching, "sequencing",
                                 str(value), source, self.sim.current_pitcher().id)

    def _apply_pitch_approach(self, value: str | None, source: str) -> str:
        return self._plan_change("pitching", "approach", "Pitching approach", self.instructions.pitching,
                                 "approach", str(value), source, self.sim.current_pitcher().id)

    def _apply_pitch_hook(self, value: str | None, source: str) -> str:
        return self._plan_change("pitching", "workload", "Workload", self.instructions.pitching, "hook",
                                 str(value), source)

    def _apply_match_approach(self, value: str | None, source: str) -> str:
        batter = self.sim.current_batter()
        previous = self.instructions.pitch_vs.get(batter.id, "plan")
        if value == "plan":
            self.instructions.pitch_vs.pop(batter.id, None)
            self._log("pitching", "matchup", f"Use the game plan vs {batter.short_name}", batter.id, previous,
                      "plan", source)
            return f"Game plan approach vs {batter.short_name}."
        swing = self._swing_for(batter)
        call_old = self._call_for(batter)
        self.instructions.pitch_vs[batter.id] = str(value)
        call_new = self._call_for(batter)
        align = self._align_for()
        a = self._plan_assessment(swing, swing, call_old, call_new, align, align)
        self._log("pitching", "matchup", f"Approach vs {batter.short_name}: {tx.LABELS[str(value)]}", batter.id,
                  previous, str(value), source, a)
        return f"Approach vs {batter.short_name}: {tx.LABELS[str(value)]}."

    def _apply_run_hold(self, value: str | None, source: str) -> str:
        plan = self.instructions.pitching
        new = value == "on"
        if plan.hold_runners == new:
            return "No change."
        plan.hold_runners = new
        swing = self._swing_for(self.sim.current_batter())
        targets = self.sim.steal_targets()
        basis = "Pitcher pays more attention to runners (slightly easier for the hitter)."
        if targets:
            off = self.sim.steal_probability(targets[0], swing, tx.PitchCall(hold_runners=False))
            on = self.sim.steal_probability(targets[0], swing, tx.PitchCall(hold_runners=True))
            basis += f" Steal success {off:.0%} -> {on:.0%}."
        self._log("baserunning", "hold", "Hold runners close" if new else "Stop holding runners", None,
                  "off" if new else "on", "on" if new else "off", source, tx.Assessment("informational", basis))
        return "Holding runners." if new else "Normal attention to runners."

    def _apply_sub_pitcher(self, value: str | None, source: str) -> str:
        pid = int(str(value))
        self._auto_pitching_change(self.sim.players[pid], source)
        return f"{self.sim.players[pid].short_name} is coming in."

    def _apply_def_ibb(self, value: str | None, source: str) -> str:
        self._do_ibb(source)
        return "Intentional walk."

    def _apply_def_infield(self, value: str | None, source: str) -> str:
        return self._plan_change("defense", "infield", "Infield", self.instructions.alignment, "infield",
                                 str(value), source)

    def _apply_def_outfield(self, value: str | None, source: str) -> str:
        return self._plan_change("defense", "outfield", "Outfield", self.instructions.alignment, "outfield",
                                 str(value), source)

    def _apply_def_shift(self, value: str | None, source: str) -> str:
        return self._plan_change("defense", "shift", "Shading", self.instructions.alignment, "shift",
                                 str(value), source)

    def _apply_def_aggression(self, value: str | None, source: str) -> str:
        return self._plan_change("defense", "aggression", "Defensive aggressiveness", self.instructions.alignment,
                                 "aggression", str(value), source)

    def _apply_sub_defense(self, value: str | None, source: str) -> str:
        slot_text, pid_text = str(value).split(":")
        slot, pid = int(slot_text), int(pid_text)
        out_id, pos = self.user_side.order[slot]
        a = self._sub_assessment(out_id, pid, "DEF")
        self.sim.defensive_sub(self.user_key, slot, pid)
        self._log("substitutions", "defense", f"{self.sim.players[pid].short_name} replaces "
                  f"{self.sim.players[out_id].short_name} at {pos}", pid, self.sim.players[out_id].short_name,
                  self.sim.players[pid].short_name, source, a)
        return f"{self.sim.players[pid].short_name} takes over at {pos}."

    # batting / baserunning -------------------------------------------------------------

    def _batter_override(self) -> tx.BattingPlan:
        batter = self.sim.current_batter()
        if batter.id not in self.instructions.per_batter:
            self.instructions.per_batter[batter.id] = tx.BattingPlan.from_dict(self.instructions.batting.to_dict())
        return self.instructions.per_batter[batter.id]

    def _apply_bat_approach(self, value: str | None, source: str) -> str:
        return self._plan_change("batting", "approach", "Approach", self._batter_override(), "approach",
                                 str(value), source, self.sim.current_batter().id)

    def _apply_bat_aggression(self, value: str | None, source: str) -> str:
        return self._plan_change("batting", "aggression", "Swing aggression", self._batter_override(),
                                 "aggression", str(value), source, self.sim.current_batter().id)

    def _apply_bat_discipline(self, value: str | None, source: str) -> str:
        return self._plan_change("batting", "discipline", "Plate discipline", self._batter_override(),
                                 "discipline", str(value), source, self.sim.current_batter().id)

    def _apply_bat_effort(self, value: str | None, source: str) -> str:
        return self._plan_change("batting", "effort", "Contact effort", self._batter_override(), "effort",
                                 str(value), source, self.sim.current_batter().id)

    def _apply_plan_approach(self, value: str | None, source: str) -> str:
        return self._team_plan("approach", "Team approach", str(value), source)

    def _apply_plan_aggression(self, value: str | None, source: str) -> str:
        return self._team_plan("aggression", "Team aggression", str(value), source)

    def _apply_plan_discipline(self, value: str | None, source: str) -> str:
        return self._team_plan("discipline", "Team discipline", str(value), source)

    def _team_plan(self, attr: str, label: str, value: str, source: str) -> str:
        plan = self.instructions.batting
        previous = getattr(plan, attr)
        setattr(plan, attr, value)
        for override in self.instructions.per_batter.values():
            if getattr(override, attr) == previous:
                setattr(override, attr, value)
        self._log("batting", f"team_{attr}", f"{label}: {tx.LABELS.get(value, value)}", None, previous, value,
                  source, tx.Assessment("informational", "Applies to every hitter without a specific instruction."))
        return f"{label}: {tx.LABELS.get(value, value)}."

    def _apply_bat_take(self, value: str | None, source: str) -> str:
        self.next_pitch.take = not self.next_pitch.take
        if self.next_pitch.take:
            self.next_pitch.bunt = self.next_pitch.hit_and_run = False
            a = self._swing_one_shot_assessment(True)
            self._log("batting", "take", f"Take sign for {self.sim.current_batter().short_name}",
                      self.sim.current_batter().id, "swing", "take", source, a, ("P",))
            return "Take sign is on."
        return "Take sign off."

    def _apply_bat_bunt(self, value: str | None, source: str) -> str:
        self.next_pitch.bunt = not self.next_pitch.bunt if value is None else value == "on"
        if self.next_pitch.bunt:
            self.next_pitch.take = self.next_pitch.hit_and_run = False
            a = self._bunt_assessment()
            if self.sim.strikes == 2:
                a.basis += "; with two strikes a foul bunt is a strikeout"
            self._log("batting", "bunt", f"Bunt sign for {self.sim.current_batter().short_name}",
                      self.sim.current_batter().id, "swing away", "bunt", source, a)
            return "Bunt sign is on."
        return "Bunt sign off."

    def _apply_bat_hit_and_run(self, value: str | None, source: str) -> str:
        self.next_pitch.hit_and_run = not self.next_pitch.hit_and_run
        if self.next_pitch.hit_and_run:
            self.next_pitch.take = self.next_pitch.bunt = False
            self.next_pitch.steal = None
            g = self.sim
            swing = self._swing_for(g.current_batter())
            p = g.steal_probability(2, swing, self._call_for(g.current_batter())) if g.steal_targets() else 0.0
            a = tx.Assessment("informational", f"batter must swing to protect the runner; if he misses, "
                              f"the runner is a straight steal (~{max(0.0, p - 0.03):.0%})")
            self._log("batting", "hit_and_run", "Hit-and-run is on", g.current_batter().id, "-", "hit-and-run",
                      source, a)
            return "Hit-and-run is on."
        return "Hit-and-run off."

    def _apply_run_steal(self, value: str | None, source: str) -> str:
        return self._steal("steal", source)

    def _apply_run_delayed(self, value: str | None, source: str) -> str:
        return self._steal("delayed", source)

    def _steal(self, kind: str, source: str) -> str:
        if self.next_pitch.steal == kind:
            self.next_pitch.steal = None
            return "Steal sign off."
        self.next_pitch.steal = kind
        self.next_pitch.hit_and_run = False
        g = self.sim
        runner = g.bases[g.steal_targets()[0] - 2]
        a = self._steal_assessment(kind == "delayed")
        label = "Delayed steal" if kind == "delayed" else "Steal"
        self._log("baserunning", kind, f"{label}: {g.players[runner.player_id].short_name}"  # type: ignore[union-attr]
                  f" goes on the next pitch", runner.player_id if runner else None, "-", kind, source, a,
                  ("SB", "CS", "PA"))
        return f"{label} sign is on."

    def _apply_run_advance(self, value: str | None, source: str) -> str:
        return self._plan_change("baserunning", "advance", "Advancement", self.instructions.batting, "advance",
                                 str(value), source)

    def _apply_run_lead(self, value: str | None, source: str) -> str:
        previous = self.instructions.batting.lead
        self.instructions.batting.lead = str(value)
        if previous == value:
            return "No change."
        basis = "Extended leads improve steals and extra bases but invite pickoff throws."
        self._log("baserunning", "lead", f"Lead: {tx.LABELS[str(value)]}", None, previous, str(value), source,
                  tx.Assessment("informational", basis))
        return f"Lead: {tx.LABELS[str(value)]}."

    def _apply_run_send(self, value: str | None, source: str) -> str:
        new = None if value in (None, "auto") else value
        previous = self.next_pitch.send or "auto"
        self.next_pitch.send = new
        if new:
            self._log("baserunning", "send", "Send the runners on the next ball in play" if new == "send"
                      else "Hold the runners on the next ball in play", None, previous, str(new), source,
                      tx.Assessment("informational", "Sending adds extra bases but risks outs on the bases."),
                      ("PA",))
        return "Runner instruction set."

    def _apply_sub_pinch_hit(self, value: str | None, source: str) -> str:
        pid = int(str(value))
        self._do_pinch_hit(pid, source)
        return f"{self.sim.players[pid].short_name} will pinch-hit."

    def _apply_sub_pinch_run(self, value: str | None, source: str) -> str:
        base_text, pid_text = str(value).split(":")
        base, pid = int(base_text), int(pid_text)
        runner = self.sim.bases[base - 1]
        assert runner is not None
        old = self.sim.players[runner.player_id]
        a = self._sub_assessment(old.id, pid, "PR")
        self.sim.pinch_run(self.user_key, base, pid)
        self._log("substitutions", "pinch_run", f"{self.sim.players[pid].short_name} pinch-runs for "
                  f"{old.short_name}", pid, old.short_name, self.sim.players[pid].short_name, source, a)
        return f"{self.sim.players[pid].short_name} is running."

    # -- reviews ---------------------------------------------------------------------

    def upcoming_hitters(self, count: int = 4) -> list[dict[str, Any]]:
        """Next hitters of the batting club with the matchup against the current pitcher."""
        g = self.sim
        batting, fielding = g.batting_side, g.fielding_side
        pitcher = g.current_pitcher()
        rows = []
        start = batting.batter_index
        for i in range(count):
            pid, pos = batting.order[(start + i) % 9]
            b = g.players[pid]
            probs = g.outcome_probabilities(b, pitcher, fielding)
            line = batting.box.batting[pid]
            rows.append({"player": b, "pos": pos, "slot": (start + i) % 9 + 1, "today": f"{line.h}-{line.ab}",
                         "obp": sum(probs[o] for o in ("BB", "HBP", "1B", "2B", "3B", "HR")),
                         "k": probs["K"], "hr": probs["HR"], "rv": tx.pa_run_value(probs)})
        return rows

    def bullpen_report(self) -> list[dict[str, Any]]:
        g = self.sim
        side = self.user_side
        rows = []
        for p in g.bullpen(side):
            rows.append({"player": p, "role": "SP" if p.id in side.team.rotation else
                         "CL" if p.id == side.team.closer else "RP", "fatigue": p.fatigue,
                         "rating": p.overall, "throws": p.throws, "rest": "rested" if p.fatigue <= 15
                         else "tired" if p.fatigue <= 45 else "gassed"})
        return rows

    def pitcher_status(self, side: _Side | None = None) -> dict[str, Any]:
        g = self.sim
        side = side or g.fielding_side
        pid = side.pitcher_id
        line = side.box.pitching[pid]
        load = g._load(side, pid)
        p = g.players[pid]
        limit = side.pitch_limit
        stamina = max(0.0, min(1.0, 1 - load / (limit + EXHAUSTION_MARGIN)))
        tired = g.tiredness(side, pid)
        condition = ("fresh" if load < limit * 0.6 else "steady" if load < limit - 10 else
                     "tiring" if load < limit + 10 else "exhausted")
        return {"player": p, "pitches": line.pitches, "load": load, "limit": limit, "stamina": stamina,
                "tired": tired, "condition": condition, "line": line, "faced": side.faced.get(pid, 0),
                "repertoire": tx.repertoire(p)}

    def opponent_tendencies(self) -> list[dict[str, Any]]:
        g = self.sim
        opp = self.opp_side
        rows = []
        for pid, pos in opp.order:
            b = g.players[pid]
            pull = tx.pull_tendency(b)
            rows.append({"player": b, "pos": pos, "pull": pull, "speed": b.rating("speed"),
                         "power": b.rating("power"), "contact": b.rating("contact"), "eye": b.rating("eye"),
                         "note": ("pull hitter - shade pull" if pull > 0.62 else
                                  "sprays the ball - stay straight up" if pull < 0.4 else "balanced spray"),
                         "threat": "base stealer" if b.rating("speed") >= 65 else ""})
        return rows

    def fatigue_report(self) -> list[dict[str, Any]]:
        g = self.sim
        side = self.user_side
        rows = []
        for pid, pos in side.order:
            p = g.players[pid]
            rows.append({"player": p, "pos": pos, "fatigue": p.fatigue, "morale": p.morale,
                         "injury": p.injury or "", "rust": p.rust_days})
        return rows

    # -- post-game report ------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        """Managerial report: decisions per category with ex-ante assessments and results."""
        events = self.sim.events or []
        by_cat: dict[str, dict[str, Any]] = {}
        for c in CATEGORIES:
            by_cat[c] = {"count": 0, "manual": 0, "auto": 0, "assisted": 0, "forced": 0,
                         "favourable": 0, "neutral": 0, "unfavourable": 0, "informational": 0,
                         "expected_runs": 0.0}
        for d in self.decisions:
            if d.category not in by_cat:
                continue
            row = by_cat[d.category]
            row["count"] += 1
            row[d.source] = row.get(d.source, 0) + 1
            if d.assessment is not None:
                row[d.assessment.grade] = row.get(d.assessment.grade, 0) + 1
                if d.assessment.grade != "informational":
                    row["expected_runs"] += d.assessment.value
        pitch_calls = [d for d in self.decisions if d.action in ("pitch_type", "location")]
        call_results = Counter(events[d.result]["r"] for d in pitch_calls if d.result is not None)
        steals = [d for d in self.decisions if d.action in ("steal", "delayed")]
        steal_results = Counter(events[d.result]["k"] for d in steals if d.result is not None)
        notes = []
        if pitch_calls:
            strikes = sum(call_results[r] for r in ("called", "swinging", "foul"))
            notes.append(f"Pitch calls: {len(pitch_calls)}; {strikes} strikes/fouls, "
                         f"{call_results['ball']} balls, {call_results['inplay']} put in play.")
        if steals:
            notes.append(f"Steal attempts sent: {len(steals)} ({steal_results['SB']} safe, "
                         f"{steal_results['CS']} caught).")
        changes = [d for d in self.decisions if d.action == "change"]
        if changes:
            notes.append(f"Pitching changes: {len(changes)} "
                         f"({sum(1 for d in changes if d.source == 'manual')} by you).")
        return {
            "team": self.user_side.team.abbr,
            "categories": by_cat,
            "decisions": len(self.decisions),
            "manual": sum(1 for d in self.decisions if d.source == "manual"),
            "notes": notes,
            "log": [d.to_dict() for d in self.decisions[-200:]],
            "method": "Grades use the information available when each decision was made (matchup model, run "
                      "expectancy, break-even rates), not the result of the play.",
        }

    # -- serialisation ---------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        state = self.brain.rng.getstate()
        return {
            "key": self.key, "label": self.label, "record_stats": self.record_stats, "round_code": self.round_code,
            "user_key": self.user_key, "settings": dict(self.settings),
            "instructions": self.instructions.to_dict(), "opponent": self.opponent_instructions.to_dict(),
            "next_pitch": self.next_pitch.to_dict(), "decisions": [d.to_dict() for d in self.decisions],
            "prompt": self.prompt.to_dict() if self.prompt else None, "answered": sorted(self.answered),
            "steal_at": self.steal_at, "cpu": {"steal_at": self.cpu_manager.steal_at, "bunt": self.cpu_manager.bunt},
            "cpu_rng": [state[0], list(state[1]), state[2]], "sim": self.sim.snapshot(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], players: Mapping[int, Player]) -> LiveGame:
        sim = GameSimulator.restore(dict(data["sim"]), players)
        rng = random.Random()
        version, internal, gauss = data["cpu_rng"]
        rng.setstate((version, tuple(internal), gauss))
        live = cls(sim, str(data["user_key"]), str(data["key"]), str(data["label"]), bool(data["record_stats"]),
                   data.get("round_code"), rng)
        live.settings = {c: str(data["settings"].get(c, "assisted")) for c in CATEGORIES}
        live.instructions = Instructions.from_dict(data.get("instructions"))
        live.opponent_instructions = Instructions.from_dict(data.get("opponent"))
        live.cpu_manager.instructions = live.opponent_instructions
        live.next_pitch = NextPitch.from_dict(data.get("next_pitch"))
        live.decisions = [Decision.from_dict(d) for d in data.get("decisions", [])]
        live.prompt = Prompt.from_dict(data.get("prompt"))
        live.answered = set(data.get("answered", []))
        live.steal_at = data.get("steal_at")
        cpu = data.get("cpu", {})
        live.cpu_manager.steal_at = cpu.get("steal_at")
        live.cpu_manager.bunt = bool(cpu.get("bunt"))
        if sim.is_over:
            live._finalize()
        return live


def describe_phase(game: GameSimulator) -> str:
    if game.phase == PHASE_FINAL:
        return "Final"
    if game.phase == PHASE_HALF:
        return "Between innings"
    if game.phase == PHASE_PA:
        return "Between batters"
    return "Between pitches"


def late_and_close(game: GameSimulator) -> bool:
    return game.inning >= REGULATION_INNINGS - 2 and abs(game.home.box.runs - game.away.box.runs) <= 1


__all__ = ["AdvanceReport", "CpuBrain", "Control", "Decision", "Instructions", "LiveGame", "MIN_BATTERS_FACED",
           "NextPitch", "Prompt", "describe_phase", "late_and_close"]
