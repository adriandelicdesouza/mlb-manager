"""Baseball game simulation: an incremental state machine with two resolutions.

``GameSimulator`` holds the complete state of a game (inning, outs, bases,
count, lineups, pitchers, box scores) and advances it in small, immutable
steps. Completed events are appended to ``events`` and never rewritten.

* **Classic resolution** (``simulate_game``; every CPU-vs-CPU game): each plate
  appearance is resolved with one draw from the plate-appearance model and the
  built-in CPU logic handles steals and pitching changes. Its random-number
  sequence is identical to the v0.6 engine, so automated results are unchanged.
* **Pitch-by-pitch resolution** (live games managed from the dugout): the game
  pauses at decision points (between half-innings, between plate appearances,
  between pitches). A *manager* object per side supplies the instructions for
  each pitch (see :mod:`mlb_manager.tactics` and :mod:`mlb_manager.dugout`);
  the engine validates and resolves them. Neutral instructions reproduce the
  classic model's rates (see ``tactics.calibrate``).

The engine is pure with respect to the league: it reads player ratings and
team configuration, uses the supplied ``random.Random`` and never mutates
players or teams. The caller (``league.py``) applies stats and fatigue.
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from mlb_manager import ratings as rt
from mlb_manager import tactics as tx
from mlb_manager.models import BattingStats, PitchingStats, Player, Team

REGULATION_INNINGS = 9
MAX_INNINGS = 30  # safety valve; extra-inning runner makes this practically unreachable

# League-average per-plate-appearance outcome rates.
BASE_K = 0.222
BASE_BB = 0.077
BASE_HBP = 0.011
BASE_HR = 0.026
BASE_BABIP = 0.292

RELIEVER_FRESH_FATIGUE = 15  # relievers above this fatigue are avoided
SAVE_MAX_LEAD = 3
MIN_BATTERS_FACED = 3  # a reliever must face three batters or finish the half-inning

# Decision points of a live game.
PHASE_HALF = "half"  # before a half-inning starts (between innings)
PHASE_PA = "pa"  # before a plate appearance (between batters)
PHASE_PITCH = "pitch"  # during a plate appearance (between pitches)
PHASE_FINAL = "final"


# ----------------------------------------------------------------------------- results


@dataclass(slots=True)
class TeamBox:
    abbr: str
    batting_order: list[tuple[int, str]] = field(default_factory=list)
    batting: dict[int, BattingStats] = field(default_factory=dict)
    pitching: dict[int, PitchingStats] = field(default_factory=dict)
    line: list[int] = field(default_factory=list)  # runs per inning
    hits: int = 0
    errors: int = 0
    # Substitutes who entered the game: player id -> player id he replaced.
    subs: dict[int, int] = field(default_factory=dict)

    @property
    def runs(self) -> int:
        return sum(self.line)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "abbr": self.abbr,
            "batting_order": [list(x) for x in self.batting_order],
            "batting": {str(k): v.to_dict() for k, v in self.batting.items()},
            "pitching": {str(k): v.to_dict() for k, v in self.pitching.items()},
            "line": list(self.line),
            "hits": self.hits,
            "errors": self.errors,
        }
        if self.subs:
            data["subs"] = {str(k): v for k, v in self.subs.items()}
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TeamBox:
        return cls(
            abbr=data["abbr"],
            batting_order=[(int(pid), pos) for pid, pos in data["batting_order"]],
            batting={int(k): BattingStats.from_dict(v) for k, v in data["batting"].items()},
            pitching={int(k): PitchingStats.from_dict(v) for k, v in data["pitching"].items()},
            line=list(data["line"]),
            hits=int(data["hits"]),
            errors=int(data["errors"]),
            subs={int(k): int(v) for k, v in data.get("subs", {}).items()},
        )


@dataclass(slots=True)
class GameResult:
    away: str
    home: str
    away_box: TeamBox
    home_box: TeamBox
    innings: int
    winning_pitcher: int
    losing_pitcher: int
    save_pitcher: int | None
    log: list[str] = field(default_factory=list)
    # Play-by-play snapshots (only recorded for the user's games; see GameSimulator).
    events: list[dict[str, Any]] = field(default_factory=list)
    # Effective pitch load per pitcher when it differs from the pitch count
    # (pitching effort in managed games); drives post-game fatigue.
    workload: dict[int, int] = field(default_factory=dict)
    # Post-game managerial report of a live game (see dugout.summary).
    manager_report: dict[str, Any] | None = None

    @property
    def away_runs(self) -> int:
        return self.away_box.runs

    @property
    def home_runs(self) -> int:
        return self.home_box.runs

    @property
    def winner(self) -> str:
        return self.home if self.home_runs > self.away_runs else self.away

    @property
    def loser(self) -> str:
        return self.away if self.winner == self.home else self.home

    def box_for(self, abbr: str) -> TeamBox:
        if abbr == self.home:
            return self.home_box
        if abbr == self.away:
            return self.away_box
        raise KeyError(abbr)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "away": self.away,
            "home": self.home,
            "away_box": self.away_box.to_dict(),
            "home_box": self.home_box.to_dict(),
            "innings": self.innings,
            "winning_pitcher": self.winning_pitcher,
            "losing_pitcher": self.losing_pitcher,
            "save_pitcher": self.save_pitcher,
            "log": list(self.log),
            "events": [dict(e) for e in self.events],
        }
        if self.workload:
            data["workload"] = {str(k): v for k, v in self.workload.items()}
        if self.manager_report is not None:
            data["manager_report"] = self.manager_report
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GameResult:
        return cls(
            away=data["away"],
            home=data["home"],
            away_box=TeamBox.from_dict(data["away_box"]),
            home_box=TeamBox.from_dict(data["home_box"]),
            innings=int(data["innings"]),
            winning_pitcher=int(data["winning_pitcher"]),
            losing_pitcher=int(data["losing_pitcher"]),
            save_pitcher=data.get("save_pitcher"),
            log=list(data.get("log", [])),
            events=[dict(e) for e in data.get("events", [])],
            workload={int(k): int(v) for k, v in data.get("workload", {}).items()},
            manager_report=data.get("manager_report"),
        )


# ----------------------------------------------------------------------------- helpers


def order_defense(order: list[tuple[int, str]], players: Mapping[int, Player]) -> float:
    """Weighted defensive rating (1-99) of the fielders in a batting order."""
    total = weight = 0.0
    for pid, position in order:
        if position == "DH":
            continue
        player = players[pid]
        importance = rt.POSITION_DEFENSE_IMPORTANCE[position]
        total += rt.defense_rating(player.ratings, position, player.can_play(position)) * importance
        weight += importance
    return total / weight if weight else 50.0


def team_defense(team: Team, players: Mapping[int, Player]) -> float:
    """Weighted defensive rating (1-99) of a lineup's fielders."""
    return order_defense([(s.player_id, s.position) for s in team.lineup], players)


def starter_pitch_limit(pitcher: Player) -> int:
    limit = 93 + (pitcher.rating("stamina") - 60) * 0.8 - pitcher.fatigue * 0.5
    return int(max(45, min(115, limit)))


def reliever_pitch_limit(pitcher: Player) -> int:
    return int(max(15, min(60, 12 + pitcher.rating("stamina") * 0.45 - pitcher.fatigue * 0.3)))


class SideManager(Protocol):
    """Supplies one club's decisions in a pitch-by-pitch game (see dugout.py)."""

    def on_half_start(self, game: GameSimulator, side: _Side) -> None: ...

    def on_pa_start(self, game: GameSimulator, side: _Side) -> None: ...

    def pitch_call(self, game: GameSimulator, side: _Side) -> tx.PitchCall: ...

    def swing_call(self, game: GameSimulator, side: _Side) -> tx.SwingCall: ...

    def alignment(self, game: GameSimulator, side: _Side) -> tx.Alignment: ...


class RuleError(ValueError):
    """A managerial move that the rules or the game situation do not allow."""


@dataclass(slots=True)
class _Runner:
    player_id: int
    pitcher_id: int  # pitcher charged if this runner scores
    earned: bool = True


@dataclass(slots=True)
class _RunContext:
    """Situational modifiers for runner advancement in pitch-by-pitch games."""

    advance: str = "normal"
    send: str | None = None
    lead: str = "normal"
    moving: bool = False
    infield: str = "normal"
    outfield: str = "normal"
    def_aggression: str = "normal"
    of_arm: float = 50.0


@dataclass(slots=True)
class _Side:
    key: str  # "away" or "home"
    team: Team
    box: TeamBox
    defense: float
    catcher: Player
    pitcher_id: int
    starter_id: int
    pitchers_used: list[int]
    pitch_limit: int
    batter_index: int = 0
    lead_at_entry: dict[int, int] = field(default_factory=dict)
    # Current batting order (changes with substitutions; box.batting_order keeps everyone).
    order: list[tuple[int, str]] = field(default_factory=list)
    removed: set[int] = field(default_factory=set)  # left the game, may not return
    stress: dict[int, float] = field(default_factory=dict)  # effective pitch load (live games)
    faced: dict[int, int] = field(default_factory=dict)  # batters faced per pitcher
    entry: dict[int, tuple[int, bool]] = field(default_factory=dict)  # (inning, top) a pitcher entered
    manager: SideManager | None = None

    def current_pitch_count(self) -> int:
        return self.box.pitching[self.pitcher_id].pitches

    @property
    def lineup_ids(self) -> list[int]:
        return [pid for pid, _ in self.order]

    def position_of(self, player_id: int) -> str | None:
        return next((pos for pid, pos in self.order if pid == player_id), None)


# ----------------------------------------------------------------------------- engine


class GameSimulator:
    def __init__(
        self,
        away: Team,
        home: Team,
        players: Mapping[int, Player],
        rng: random.Random,
        away_starter: int,
        home_starter: int,
        record_events: bool = False,
        pitch_by_pitch: bool = False,
    ) -> None:
        self.players = players
        # Event recording only appends snapshots; it never touches the RNG, so the
        # same game plays out identically with or without it.
        self.events: list[dict[str, Any]] | None = [] if (record_events or pitch_by_pitch) else None
        self.rng = rng
        self.pitch_by_pitch = pitch_by_pitch
        self.away = self._make_side("away", away, away_starter)
        self.home = self._make_side("home", home, home_starter)
        self.log: list[str] = []
        self.outs = 0
        self.inning = 1
        self.top = True
        self.bases: list[_Runner | None] = [None, None, None]
        self.leader: str | None = None
        self.go_ahead: tuple[int, int] | None = None  # (winning pitcher, losing pitcher)
        # Incremental (live) state.
        self.phase = PHASE_HALF
        self.balls = 0
        self.strikes = 0
        self.pa_pitches: list[str] = []  # pitch types thrown in the current plate appearance
        self.result: GameResult | None = None

    # -- setup -------------------------------------------------------------------------

    def _make_side(self, key: str, team: Team, starter_id: int) -> _Side:
        box = TeamBox(abbr=team.abbr)
        for slot in team.lineup:
            box.batting_order.append((slot.player_id, slot.position))
            box.batting[slot.player_id] = BattingStats(g=1)
        catcher_id = next(s.player_id for s in team.lineup if s.position == "C")
        starter = self.players[starter_id]
        box.pitching[starter_id] = PitchingStats(g=1, gs=1)
        return _Side(
            key=key,
            team=team,
            box=box,
            defense=team_defense(team, self.players),
            catcher=self.players[catcher_id],
            pitcher_id=starter_id,
            starter_id=starter_id,
            pitchers_used=[starter_id],
            pitch_limit=starter_pitch_limit(starter),
            lead_at_entry={starter_id: 0},
            order=list(box.batting_order),
            stress={starter_id: 0.0},
            faced={starter_id: 0},
            entry={starter_id: (1, True)},
        )

    # -- public ------------------------------------------------------------------------

    @property
    def is_over(self) -> bool:
        return self.phase == PHASE_FINAL

    def side(self, key: str) -> _Side:
        return self.away if key == "away" else self.home

    @property
    def batting_side(self) -> _Side:
        return self.away if self.top else self.home

    @property
    def fielding_side(self) -> _Side:
        return self.home if self.top else self.away

    def play(self) -> GameResult:
        """Play the whole game (classic resolution, or the live loop with managers)."""
        if self.pitch_by_pitch:
            while not self.is_over:
                self.step()
            assert self.result is not None
            return self.result
        while True:
            self._play_half(self.away, self.home)
            if self.inning >= REGULATION_INNINGS and self.home.box.runs > self.away.box.runs:
                break  # home team does not need its last at-bat
            self.top = False
            self._play_half(self.home, self.away)
            if self.inning >= REGULATION_INNINGS and self.home.box.runs != self.away.box.runs:
                break
            if self.inning >= MAX_INNINGS:
                break
            self.inning += 1
            self.top = True
        self.phase = PHASE_FINAL
        return self._finish()

    # -- innings -----------------------------------------------------------------------

    def _play_half(self, batting: _Side, fielding: _Side) -> None:
        self._begin_half(batting, fielding)
        self._consider_pitching_change(fielding, batting, inning_start=True)
        self._place_extra_runner(batting, fielding)
        while self.outs < 3:
            if self._is_walk_off(batting):
                return
            self._maybe_steal(batting, fielding)
            if self.outs >= 3:
                break
            self._consider_pitching_change(fielding, batting, inning_start=False)
            self._plate_appearance(batting, fielding)
            if self._is_walk_off(batting):
                return

    def _begin_half(self, batting: _Side, fielding: _Side) -> None:
        self.outs = 0
        self.bases = [None, None, None]
        batting.box.line.append(0)

    def _place_extra_runner(self, batting: _Side, fielding: _Side) -> None:
        if self.inning > REGULATION_INNINGS:
            # Extra-inning rule: the previous batter starts on second base.
            prev = batting.order[(batting.batter_index - 1) % 9][0]
            self.bases[1] = _Runner(prev, fielding.pitcher_id, earned=False)

    def _is_walk_off(self, batting: _Side) -> bool:
        return (
            batting is self.home
            and self.inning >= REGULATION_INNINGS
            and self.home.box.runs > self.away.box.runs
        )

    def _half_complete(self) -> bool:
        return self.outs >= 3 or self._is_walk_off(self.batting_side)

    def _end_half(self) -> None:
        """Advance to the next half-inning or finish the game (live resolution)."""
        if self.top:
            if self.inning >= REGULATION_INNINGS and self.home.box.runs > self.away.box.runs:
                self._final()
                return
            self.top = False
        else:
            if self.inning >= REGULATION_INNINGS and self.home.box.runs != self.away.box.runs:
                self._final()
                return
            if self.inning >= MAX_INNINGS:
                self._final()
                return
            self.inning += 1
            self.top = True
        self.phase = PHASE_HALF
        self.balls = self.strikes = 0

    def _final(self) -> None:
        self.phase = PHASE_FINAL
        self.result = self._finish()

    # -- pitching changes --------------------------------------------------------------

    def _consider_pitching_change(self, fielding: _Side, batting: _Side, inning_start: bool) -> None:
        reliever = self.auto_pitching_change(fielding, batting, inning_start)
        if reliever is None:
            return
        self._install_pitcher(fielding, batting, reliever.id)

    def auto_pitching_change(self, fielding: _Side, batting: _Side, inning_start: bool) -> Player | None:
        """The built-in CPU rule: the reliever to bring in now, or None."""
        pid = fielding.pitcher_id
        line = fielding.box.pitching[pid]
        pitches = self._load(fielding, pid)
        is_starter = pid == fielding.starter_id
        lead = fielding.box.runs - batting.box.runs
        need = False
        if is_starter:
            if pitches >= fielding.pitch_limit or line.r >= 7:
                need = True
            elif inning_start and pitches >= fielding.pitch_limit - 12:
                need = True
            elif inning_start and self.inning >= 9 and 0 < lead <= SAVE_MAX_LEAD and pitches > 85:
                need = True
        else:
            if pitches >= fielding.pitch_limit + 8:
                need = True
            elif inning_start and (line.outs >= 3 or pitches >= fielding.pitch_limit):
                need = True
            elif (
                inning_start
                and self.inning >= 9
                and 0 < lead <= SAVE_MAX_LEAD
                and fielding.team.closer not in fielding.pitchers_used
                and pid != fielding.team.closer
            ):
                need = True
        if not need:
            return None
        return self._choose_reliever(fielding, lead)

    def _install_pitcher(self, fielding: _Side, batting: _Side, reliever_id: int) -> None:
        reliever = self.players[reliever_id]
        lead = fielding.box.runs - batting.box.runs
        fielding.pitcher_id = reliever.id
        fielding.pitchers_used.append(reliever.id)
        fielding.pitch_limit = reliever_pitch_limit(reliever)
        fielding.lead_at_entry[reliever.id] = lead
        fielding.box.pitching[reliever.id] = PitchingStats(g=1)
        fielding.stress[reliever.id] = 0.0
        fielding.faced[reliever.id] = 0
        fielding.entry[reliever.id] = (self.inning, self.top)
        self._event("PC", batting, fielding, None, reliever.id, "PC", 0)
        half = "Top" if self.top else "Bot"
        self.log.append(f"{half} {self.inning}: {fielding.team.abbr} brings in {reliever.short_name}.")

    def bullpen(self, side: _Side) -> list[Player]:
        """Active pitchers who have not appeared yet (starters included, listed last)."""
        used = set(side.pitchers_used)
        rotation = set(side.team.rotation)
        pen = [self.players[pid] for pid in side.team.active
               if pid not in used and self.players[pid].is_pitcher]
        return sorted(pen, key=lambda p: (p.id in rotation, p.id))

    def _choose_reliever(self, fielding: _Side, lead: int) -> Player | None:
        used = set(fielding.pitchers_used)
        rotation = set(fielding.team.rotation)
        pen = [
            self.players[pid]
            for pid in fielding.team.active
            if pid not in used and pid not in rotation and self.players[pid].is_pitcher
        ]
        if not pen:
            return None
        fresh = [p for p in pen if p.fatigue <= RELIEVER_FRESH_FATIGUE]
        pool = fresh or pen
        closer_id = fielding.team.closer

        def quality(p: Player) -> float:
            return rt.pitching_rating(p.ratings, "RP") - p.fatigue * 0.3

        save_spot = self.inning >= 9 and 0 < lead <= SAVE_MAX_LEAD
        if save_spot or (self.inning >= 10 and lead >= 0):
            closer = next((p for p in pool if p.id == closer_id), None)
            if closer is not None:
                return closer
        non_closers = [p for p in pool if p.id != closer_id] or pool
        if self.inning >= 7 and -1 <= lead <= SAVE_MAX_LEAD:
            return max(non_closers, key=lambda p: (quality(p), -p.id))
        if self.inning <= 5:
            # Long relief: favour stamina.
            return max(non_closers, key=lambda p: (p.rating("stamina") - p.fatigue, -p.id))
        return min(non_closers, key=lambda p: (quality(p), p.id))

    # -- plate appearances (classic resolution) ----------------------------------------

    def _maybe_steal(self, batting: _Side, fielding: _Side) -> None:
        runner = self.bases[0]
        if runner is None or self.bases[1] is not None or self.outs >= 2:
            return
        speed = self.players[runner.player_id].rating("speed")
        attempt = steal_attempt_rate(speed)
        if self.rng.random() >= attempt:
            return
        success = steal_success(speed, fielding.catcher.rating("arm"))
        line = batting.box.batting[runner.player_id]
        if self.rng.random() < success:
            line.sb += 1
            self.bases[1], self.bases[0] = runner, None
            self._event("SB", batting, fielding, runner.player_id, fielding.pitcher_id, "SB", 0)
        else:
            line.cs += 1
            self.bases[0] = None
            self._record_out(fielding)
            self._event("CS", batting, fielding, runner.player_id, fielding.pitcher_id, "CS", 0)

    def _load(self, side: _Side, pid: int) -> float:
        """Pitch load used for fatigue: effective (effort-weighted) in live games."""
        if self.pitch_by_pitch:
            return side.stress.get(pid, 0.0)
        return side.box.pitching[pid].pitches

    def tiredness(self, side: _Side, pid: int | None = None) -> float:
        pid = side.pitcher_id if pid is None else pid
        pitcher = self.players[pid]
        tired = max(0, self._load(side, pid) - side.pitch_limit) * 0.5 + pitcher.fatigue * 0.05
        return min(tired, 25)

    def outcome_probabilities(self, batter: Player, pitcher: Player, fielding: _Side,
                              tired: float | None = None, defense: float | None = None) -> dict[str, float]:
        """The plate-appearance model; ``tired``/``defense`` override the live values (what-if analysis)."""
        return self._outcome_probabilities(batter, pitcher, fielding, tired, defense)

    def _outcome_probabilities(self, batter: Player, pitcher: Player, fielding: _Side,
                               tired: float | None = None, defense: float | None = None) -> dict[str, float]:
        if tired is None:
            tired = self.tiredness(fielding, pitcher.id)
        team_def = fielding.defense if defense is None else defense
        stuff = pitcher.rating("stuff") - tired
        control = pitcher.rating("control") - tired
        movement = pitcher.rating("movement") - tired

        contact = batter.rating("contact")
        power = batter.rating("power")
        eye = batter.rating("eye")
        speed = batter.rating("speed")

        bats = batter.bats
        if bats == "S":
            bats = "L" if pitcher.throws == "R" else "R"
        platoon = 3 if bats != pitcher.throws else -3
        contact += platoon
        power += platoon

        framing = fielding.catcher.ratings.get("framing", rt.UNTRAINED_CATCHER_FRAMING) - 50
        k = BASE_K * math.exp(0.016 * (stuff - contact) + 0.004 * framing)
        bb = BASE_BB * math.exp(0.016 * (eye - control) - 0.004 * framing)
        hbp = BASE_HBP * math.exp(-0.01 * (control - 50))
        hr = BASE_HR * math.exp(0.020 * (power - 50) - 0.02 * (movement - 50))
        k, bb, hbp, hr = min(k, 0.5), min(bb, 0.25), min(hbp, 0.03), min(hr, 0.12)
        in_play = max(0.15, 1.0 - k - bb - hbp - hr)

        babip = (
            BASE_BABIP
            + 0.0022 * (contact - 50)
            + 0.0010 * (speed - 50)
            - 0.0018 * (team_def - 50)
            - 0.0012 * (movement - 50)
        )
        babip = min(0.40, max(0.20, babip))
        hits = in_play * babip
        triple_share = min(0.08, 0.025 * math.exp(0.04 * (speed - 50)))
        double_share = min(0.35, 0.21 * math.exp(0.015 * (power - 50)))
        outs = in_play - hits
        error_rate = min(0.07, 0.03 * math.exp(-0.03 * (team_def - 50)))
        ground_share = min(0.65, max(0.30, 0.46 - 0.003 * (power - 50)))
        errors = outs * error_rate
        outs -= errors
        return {
            "K": k,
            "BB": bb,
            "HBP": hbp,
            "HR": hr,
            "3B": hits * triple_share,
            "2B": hits * double_share,
            "1B": hits * (1 - triple_share - double_share),
            "E": errors,
            "GO": outs * ground_share,
            "FO": outs * (1 - ground_share) * 0.72,
            "LO": outs * (1 - ground_share) * 0.28,
        }

    def _plate_appearance(self, batting: _Side, fielding: _Side) -> None:
        batter_id, _ = batting.order[batting.batter_index]
        batting.batter_index = (batting.batter_index + 1) % 9
        batter = self.players[batter_id]
        pitcher = self.players[fielding.pitcher_id]
        probs = self._outcome_probabilities(batter, pitcher, fielding)
        outcome = self.rng.choices(list(probs), weights=list(probs.values()))[0]
        self._apply_outcome(batting, fielding, batter_id, pitcher, outcome, self._pitch_count(outcome))

    def _apply_outcome(self, batting: _Side, fielding: _Side, batter_id: int, pitcher: Player, outcome: str,
                       pitches: int, ctx: _RunContext | None = None) -> None:
        """Apply a plate-appearance outcome: stats, runner advancement, runs, event.

        ``ctx`` is only given in pitch-by-pitch games; without it the random
        draws are exactly those of the classic engine."""
        batter = self.players[batter_id]
        bline = batting.box.batting[batter_id]
        pline = fielding.box.pitching[pitcher.id]
        outs_before = self.outs
        bline.pa += 1
        pline.pitches += pitches
        fielding.faced[pitcher.id] = fielding.faced.get(pitcher.id, 0) + 1

        new_runner = _Runner(batter_id, pitcher.id)
        rbi_eligible = True
        scored: list[_Runner] = []
        rng = self.rng
        b1, b2, b3 = self.bases
        extra_outs: list[_Runner] = []  # runners thrown out on the bases (live games)

        def spd(runner: _Runner | None) -> int:
            return self.players[runner.player_id].rating("speed") if runner else 50

        def decide(p: float, runner: _Runner, kind: str) -> str:
            if ctx is None:
                return "go" if rng.random() < p else "stay"
            return self._advance_decision(p, runner, kind, ctx)

        if outcome == "K":
            bline.ab += 1
            bline.so += 1
            pline.so += 1
            self._record_out(fielding)
        elif outcome in ("BB", "HBP", "IBB"):
            if outcome in ("BB", "IBB"):
                bline.bb += 1
                pline.bb += 1
            else:
                bline.hbp += 1
            if b1:
                if b2:
                    if b3:
                        scored.append(b3)
                    b3 = b2
                b2 = b1
            b1 = new_runner
        elif outcome == "HR":
            bline.ab += 1
            bline.h += 1
            bline.hr += 1
            pline.h += 1
            pline.hr += 1
            scored.extend(r for r in (b3, b2, b1) if r)
            scored.append(new_runner)
            b1 = b2 = b3 = None
        elif outcome == "3B":
            bline.ab += 1
            bline.h += 1
            bline.triples += 1
            pline.h += 1
            scored.extend(r for r in (b3, b2, b1) if r)
            b1, b2, b3 = None, None, new_runner
        elif outcome == "2B":
            bline.ab += 1
            bline.h += 1
            bline.doubles += 1
            pline.h += 1
            scored.extend(r for r in (b3, b2) if r)
            new3 = None
            if b1:
                p_score = 0.40 + (spd(b1) - 50) * 0.008 + (0.2 if self.outs == 2 else 0)
                choice = decide(p_score, b1, "home")
                if choice == "go":
                    scored.append(b1)
                elif choice == "out":
                    extra_outs.append(b1)
                else:
                    new3 = b1
            b1, b2, b3 = None, new_runner, new3
        elif outcome == "1B":
            bline.ab += 1
            bline.h += 1
            pline.h += 1
            if b3:
                scored.append(b3)
            new3 = None
            new2 = None
            if b2:
                p_score = 0.58 + (spd(b2) - 50) * 0.008 + (0.25 if self.outs == 2 else 0)
                choice = decide(p_score, b2, "home")
                if choice == "go":
                    scored.append(b2)
                elif choice == "out":
                    extra_outs.append(b2)
                else:
                    new3 = b2
            if b1:
                p_third = 0.27 + (spd(b1) - 50) * 0.008
                if new3 is None:
                    choice = decide(p_third, b1, "third")
                    if choice == "go":
                        new3 = b1
                    elif choice == "out":
                        extra_outs.append(b1)
                    else:
                        new2 = b1
                else:
                    new2 = b1
            b1, b2, b3 = new_runner, new2, new3
        elif outcome == "E":
            bline.ab += 1
            fielding.box.errors += 1
            new_runner.earned = False
            if b3:
                scored.append(b3)
            b3, b2, b1 = b2, b1, new_runner
            rbi_eligible = False
        elif outcome == "GO":
            bline.ab += 1
            if b1 and self.outs < 2 and ctx is not None and ctx.moving:
                # Runners going on contact: no double play, everyone moves up.
                self._record_out(fielding)
                if self.outs < 3:
                    if b3:
                        scored.append(b3)
                    b3, b2, b1 = b2, b1, None
            elif b1 and self.outs < 2:
                dp_chance = 0.55 - (batter.rating("speed") - 50) * 0.006 + (fielding.defense - 50) * 0.004
                if ctx is not None:
                    dp_chance += {"dp": 0.08, "in": -0.04}.get(ctx.infield, 0.0)
                    dp_chance += {"aggressive": 0.03, "conservative": -0.03}.get(ctx.def_aggression, 0.0)
                if rng.random() < dp_chance:
                    rbi_eligible = False
                    self._record_out(fielding, 2)
                    b1 = None
                    if self.outs < 3:
                        if b3:
                            scored.append(b3)
                        b3, b2 = b2, None
                else:
                    # Fielder's choice: lead runner forced, batter safe.
                    self._record_out(fielding)
                    if self.outs < 3:
                        if b3 and b2:
                            scored.append(b3)
                            b3 = b2
                        elif b3 is None and b2:
                            b3 = b2
                        b2 = None
                    b1 = new_runner
            else:
                self._record_out(fielding)
                if self.outs < 3:
                    if b3:
                        p_home = 0.5 if ctx is None else (0.18 if ctx.infield == "in" else 0.5)
                        choice = decide(p_home, b3, "home")
                        if choice == "go":
                            scored.append(b3)
                            b3 = None
                        elif choice == "out":
                            extra_outs.append(b3)
                            b3 = None
                    if b2 and b3 is None and rng.random() < 0.6:
                        b3, b2 = b2, None
                    if b1 and b2 is None:
                        b2, b1 = b1, None
        elif outcome == "FO":
            self._record_out(fielding)
            sac_chance = 0.62 + (spd(b3) - 50) * 0.006
            if ctx is not None:
                sac_chance += {"shallow": -0.12, "deep": 0.08}.get(ctx.outfield, 0.0)
            choice = decide(sac_chance, b3, "tag") if self.outs < 3 and b3 else "stay"
            if choice == "go":
                scored.append(b3)  # type: ignore[arg-type]
                b3 = None
                bline.sf += 1
            else:
                bline.ab += 1
                if choice == "out":
                    extra_outs.append(b3)  # type: ignore[arg-type]
                    b3 = None
            if self.outs < 3 and b2 and b3 is None and rng.random() < 0.3:
                b3, b2 = b2, None
        else:  # "LO" line drive / pop up: nobody advances
            bline.ab += 1
            self._record_out(fielding)
            if ctx is not None and ctx.moving and b1 and self.outs < 3 and rng.random() < 0.45:
                extra_outs.append(b1)  # doubled off first
                b1 = None

        for runner in extra_outs:
            if self.outs < 3:
                self._record_out(fielding)
            self._event("TO", batting, fielding, runner.player_id, pitcher.id, "TO", 0)
        if self.outs >= 3:
            # Runs on the final out (e.g. a forced third out) do not count.
            scored = [r for r in scored if outcome in ("HR", "1B", "2B", "3B", "BB", "HBP", "IBB", "E")]
        self.bases = [b1, b2, b3]
        if outcome in ("1B", "2B", "3B", "HR"):
            batting.box.hits += 1
        if scored:
            self._score_runs(batting, fielding, scored, batter_id, rbi_eligible, outcome)
        if self.events is not None:
            double_play = outcome == "GO" and self.outs - outs_before >= 2
            self._event("PA", batting, fielding, batter_id, pitcher.id, "DP" if double_play else outcome,
                        len(scored))

    def _advance_decision(self, p: float, runner: _Runner, kind: str, ctx: _RunContext) -> str:
        """Send or hold a runner (live games): extra aggression can get him thrown out."""
        bonus = {"aggressive": 0.15, "conservative": -0.15}.get(ctx.advance, 0.0)
        if ctx.send == "send":
            bonus += 0.30
        elif ctx.send == "hold":
            bonus -= 0.50
        if ctx.lead == "extended" and kind != "tag":
            bonus += 0.05
        if ctx.moving and kind in ("home", "third"):
            bonus += 0.25
        if ctx.outfield == "shallow" and kind in ("home", "third"):
            bonus += 0.06
        elif ctx.outfield == "deep" and kind in ("home", "third"):
            bonus -= 0.04
        adjusted = max(0.0, min(0.98, p + bonus))
        roll = self.rng.random()
        if roll < min(p, adjusted):
            return "go"
        if roll < adjusted:
            speed = self.players[runner.player_id].rating("speed")
            safe = 0.62 + (speed - 50) * 0.01 - (ctx.of_arm - 50) * 0.008
            return "go" if self.rng.random() < max(0.2, min(0.95, safe)) else "out"
        return "stay"

    def _event(self, kind: str, batting: _Side, fielding: _Side, player_id: int | None,
               pitcher_id: int, outcome: str, runs: int, **extra: Any) -> None:
        """Snapshot of the game state after a play (no-op unless recording)."""
        if self.events is None:
            return
        event = {
            "k": kind, "i": self.inning, "top": self.top, "o": self.outs,
            "b": [r.player_id if r else 0 for r in self.bases],
            "a": self.away.box.runs, "h": self.home.box.runs,
            "p": player_id or 0, "pit": pitcher_id, "r": outcome, "n": runs, "team": batting.team.abbr,
        }
        if self.pitch_by_pitch:
            event["c"] = [self.balls, self.strikes]
        event.update(extra)
        self.events.append(event)

    def note(self, team: str, text: str, kind: str = "DEC") -> None:
        """Record a managerial note (decision, substitution) in the live feed."""
        if self.events is None:
            return
        batting, fielding = self.batting_side, self.fielding_side
        self._event(kind, batting, fielding, None, fielding.pitcher_id, kind, 0, team=team, t=text)

    def _pitch_count(self, outcome: str) -> int:
        base = {"K": 4.8, "BB": 5.7, "HBP": 3.2}.get(outcome, 3.4)
        return max(1, int(round(self.rng.gauss(base, 1.2))))

    def _record_out(self, fielding: _Side, count: int = 1) -> None:
        count = min(count, 3 - self.outs)
        self.outs += count
        fielding.box.pitching[fielding.pitcher_id].outs += count

    def _score_runs(
        self,
        batting: _Side,
        fielding: _Side,
        scored: list[_Runner],
        batter_id: int,
        rbi_eligible: bool,
        outcome: str,
    ) -> None:
        for runner in scored:
            batting.box.batting[runner.player_id].r += 1
            line = fielding.box.pitching[runner.pitcher_id]
            line.r += 1
            if runner.earned:
                line.er += 1
            batting.box.line[-1] += 1
            if rbi_eligible:
                batting.box.batting[batter_id].rbi += 1
            self._update_lead(batting, fielding, runner)
        batter = self.players[batter_id]
        desc = {
            "HR": "homers",
            "3B": "triples",
            "2B": "doubles",
            "1B": "singles",
            "BB": "walks",
            "IBB": "is intentionally walked",
            "HBP": "is hit by a pitch",
            "E": "reaches on an error",
            "GO": "grounds into a play",
            "FO": "hits a sacrifice fly",
            "SAC": "lays down a sacrifice bunt",
            "BH": "beats out a bunt",
            "FC": "reaches on a fielder's choice",
        }.get(outcome, "puts the ball in play")
        half = "Top" if self.top else "Bot"
        n = len(scored)
        self.log.append(
            f"{half} {self.inning}: {batter.short_name} {desc}, {n} run{'s' if n > 1 else ''} score"
            f"{'s' if n == 1 else ''}. {self.away.team.abbr} {self.away.box.runs}, "
            f"{self.home.team.abbr} {self.home.box.runs}"
        )

    def _update_lead(self, batting: _Side, fielding: _Side, runner: _Runner) -> None:
        diff = self.home.box.runs - self.away.box.runs
        leader = "home" if diff > 0 else "away" if diff < 0 else None
        if leader is not None and leader != self.leader:
            self.go_ahead = (batting.pitcher_id, runner.pitcher_id)
        self.leader = leader

    # -- live resolution: stepping -----------------------------------------------------

    def step(self) -> list[dict[str, Any]]:
        """Resolve the next atomic unit of a live game and return the new events.

        HALF: start the half-inning; PA: start the plate appearance (managers'
        pre-pitch decisions); PITCH: throw one pitch (and resolve the play)."""
        if not self.pitch_by_pitch:
            raise RuleError("Only pitch-by-pitch games can be stepped.")
        assert self.events is not None
        start = len(self.events)
        if self.phase == PHASE_HALF:
            self._live_begin_half()
        elif self.phase == PHASE_PA:
            self._live_begin_pa()
        elif self.phase == PHASE_PITCH:
            self._live_pitch()
        return self.events[start:]

    def _manager(self, side: _Side) -> SideManager:
        if side.manager is None:
            raise RuleError(f"No manager attached for {side.team.abbr}.")
        return side.manager

    def _live_begin_half(self) -> None:
        batting, fielding = self.batting_side, self.fielding_side
        self._begin_half(batting, fielding)
        self._event("HALF", batting, fielding, None, fielding.pitcher_id, "HALF", 0)
        self._manager(fielding).on_half_start(self, fielding)
        self._manager(batting).on_half_start(self, batting)
        self._place_extra_runner(batting, fielding)
        self.phase = PHASE_PA

    def _live_begin_pa(self) -> None:
        batting, fielding = self.batting_side, self.fielding_side
        self._manager(batting).on_pa_start(self, batting)
        if self.phase != PHASE_PA:
            return
        self._manager(fielding).on_pa_start(self, fielding)
        if self.phase != PHASE_PA:  # e.g. an intentional walk ended the plate appearance
            return
        self.balls = self.strikes = 0
        self.pa_pitches = []
        self.phase = PHASE_PITCH

    def _after_play(self) -> None:
        """After anything that can end the half (plate appearance, steal, pickoff)."""
        if self._half_complete():
            self._end_half()
        elif self.phase == PHASE_PA:
            self.balls = self.strikes = 0

    def current_batter(self) -> Player:
        batting = self.batting_side
        return self.players[batting.order[batting.batter_index][0]]

    def current_pitcher(self) -> Player:
        return self.players[self.fielding_side.pitcher_id]

    def runners(self) -> tuple[bool, bool, bool]:
        return (self.bases[0] is not None, self.bases[1] is not None, self.bases[2] is not None)

    def calibration(self, batter: Player, pitcher: Player, fielding: _Side) -> tuple[dict[str, float], tx.Calibration]:
        probs = self._outcome_probabilities(batter, pitcher, fielding)
        exec_p = tx.execution_chance(pitcher, "edge", 50, "normal", self.tiredness(fielding, pitcher.id))
        return probs, tx.calibrate(probs["K"], probs["BB"], probs["HBP"], exec_p)

    def _run_context(self, swing: tx.SwingCall, align: tx.Alignment, fielding: _Side, moving: bool) -> _RunContext:
        return _RunContext(advance=swing.advance, send=swing.send, lead=swing.lead, moving=moving,
                           infield=align.infield, outfield=align.outfield, def_aggression=align.aggression,
                           of_arm=self._outfield_arm(fielding))

    def _outfield_arm(self, side: _Side) -> float:
        arms = [self.players[pid].rating("arm") for pid, pos in side.order if pos in ("LF", "CF", "RF")]
        return sum(arms) / len(arms) if arms else 50.0

    def steal_targets(self) -> list[int]:
        """Bases (2, 3) a steal attempt would go for right now; lead runner first."""
        b1, b2, b3 = self.runners()
        if b2 and not b3:
            return [3, 2] if b1 else [3]
        if b1 and not b2:
            return [2]
        return []

    def steal_probability(self, target: int, swing: tx.SwingCall, pitch: tx.PitchCall,
                          pitch_code: str | None = None, delayed: bool = False) -> float:
        runner = self.bases[target - 2]
        if runner is None:
            return 0.0
        fielding = self.fielding_side
        speed = self.players[runner.player_id].rating("speed")
        arm = fielding.catcher.rating("arm")
        if delayed:
            p = 0.60 + (speed - 60) * 0.004 - (fielding.defense - 50) * 0.006
        else:
            p = steal_success(speed, arm)
        if target == 3:
            p -= 0.06
        if swing.lead == "extended":
            p += 0.06
        if pitch.hold_runners:
            p -= 0.07
        if pitch_code is not None:
            p += 0.04 if tx.PITCH_TYPES[pitch_code].breaking else -0.02
        if pitch.pitchout:
            p *= 0.45
        return max(0.05, min(0.97, p))

    def _live_pitch(self) -> None:
        batting, fielding = self.batting_side, self.fielding_side
        batter = self.current_batter()
        pitcher = self.players[fielding.pitcher_id]
        call = self._manager(fielding).pitch_call(self, fielding)
        swing = self._manager(batting).swing_call(self, batting)
        align = self._manager(fielding).alignment(self, fielding)
        rng = self.rng
        targets = self.steal_targets()
        if not targets:
            swing.steal = None
        if swing.hit_and_run and not (self.bases[0] is not None and self.outs < 2):
            swing.hit_and_run = False
        runners_on = any(self.bases[:2])

        # Pickoff throw (only when the runner takes an extended lead).
        if swing.lead == "extended" and (self.bases[0] is not None or self.bases[1] is not None):
            chance = 0.06 if call.hold_runners else 0.025
            if rng.random() < chance:
                base = 0 if self.bases[0] is not None else 1
                runner = self.bases[base]
                assert runner is not None
                if rng.random() < 0.14:
                    self.bases[base] = None
                    self._record_out(fielding)
                    self._event("PK", batting, fielding, runner.player_id, pitcher.id, "out", 0, base=base + 1)
                    self._after_play()
                else:
                    self._event("PK", batting, fielding, runner.player_id, pitcher.id, "safe", 0, base=base + 1)
                return

        probs, cal = self.calibration(batter, pitcher, fielding)
        tired = self.tiredness(fielding, pitcher.id)
        rep = tx.repertoire(pitcher)
        waste = call.intentional_ball or call.pitchout
        # Pitch type and location.
        codes = [p.code for p in rep]
        if waste:
            code = codes[0]
            location = "waste"
        else:
            if call.pitch_type in codes:
                code = call.pitch_type
            else:
                weights = tx.pitch_weights(rep, call.sequencing, self.balls, self.strikes,
                                           self.pa_pitches[-1] if self.pa_pitches else None)
                code = rng.choices(codes, weights=weights)[0]
            if call.location in tx.LOCATIONS:
                location = call.location
            else:
                location = rng.choices(tx.LOCATIONS, weights=tx.location_mix(self.balls, self.strikes,
                                                                              call.approach))[0]
        grade = next(p.grade for p in rep if p.code == code)
        d_contact, d_zone, d_gb, d_hr = tx.pitch_effects(rep, code)
        # Hitters adjust to the same pitch over and over.
        repeats = sum(1 for c in self.pa_pitches[-3:] if c == code)
        if repeats >= 2:
            d_contact += 0.12 * (repeats - 1)
        # Execution.
        actual = location
        if not waste:
            if rng.random() >= tx.execution_chance(pitcher, location, grade, call.effort, tired):
                actual = "mistake" if rng.random() < tx.MISTAKE_SHARE else "wild"
        loc = tx.LOCATION_MODEL[actual]
        velo = (tx.fastball_velocity(pitcher) + tx.PITCH_TYPES[code].velo + tx.EFFORT_VELO.get(call.effort, 0.0)
                - tired * 0.08 + rng.gauss(0, 0.7))
        if waste:
            velo -= 4.0
        mods = tx.PitchMods(zone=d_zone, contact=d_contact + tx.effort_contact(call.effort, pitcher))
        if call.hold_runners and runners_on:
            mods.contact += 0.04
        tx.swing_mods(swing, batter, mods)
        if waste:
            mods.no_swing = True
        # Zone, swing, contact.
        if loc.zone <= 0:
            in_zone = False
        else:
            in_zone = rng.random() < tx.sigmoid(tx.logit(loc.zone) + cal.dz + mods.zone)
        count = (self.balls, self.strikes)
        result: str
        if mods.no_swing:
            swing_p = 0.0
        elif mods.bunt:
            swing_p = 0.95 if in_zone else 0.35
        elif in_zone:
            swing_p = tx.sigmoid(tx.logit(tx.ZONE_SWING[count]) + loc.swing + mods.zone_swing)
        else:
            swing_p = tx.sigmoid(tx.logit(tx.CHASE_SWING[count]) + loc.swing + mods.chase_swing)
        swung = swing_p > 0 and rng.random() < swing_p
        if swung and mods.bunt:
            bunt_contact = tx.sigmoid(tx.logit(0.74) + (batter.rating("contact") - 50) * 0.02
                                      - (0.0 if in_zone else 0.7))
            if rng.random() < bunt_contact:
                result = "bunt_foul" if rng.random() < 0.42 else "bunt"
            else:
                result = "bunt_miss"
        elif swung:
            base = tx.ZONE_CONTACT if in_zone else tx.CHASE_CONTACT
            if rng.random() < tx.sigmoid(tx.logit(base) + cal.dc + loc.contact + mods.contact):
                foul = tx.FOUL_ZONE if in_zone else tx.FOUL_CHASE
                result = "foul" if rng.random() < tx.sigmoid(tx.logit(foul) + mods.foul) else "inplay"
            else:
                result = "swinging"
        elif in_zone:
            result = "called"
        else:
            h = min(0.2, cal.hbp * mods.hbp * (3.0 if actual == "wild" else 1.0))
            result = "hbp" if (not waste and rng.random() < h) else "ball"

        # Bookkeeping for the pitch itself.
        pline = fielding.box.pitching[pitcher.id]
        pline.pitches += 1
        effort_load = 0.5 if waste else tx.EFFORT_STRESS.get(call.effort, 1.0)
        fielding.stress[pitcher.id] = fielding.stress.get(pitcher.id, 0.0) + effort_load
        self.pa_pitches.append(code)
        outcome: str | None = None
        if result in ("ball",):
            self.balls += 1
            if self.balls >= 4:
                outcome = "IBB" if call.intentional_ball else "BB"
        elif result in ("called", "swinging", "bunt_miss"):
            self.strikes += 1
            if self.strikes >= 3:
                outcome = "K"
        elif result == "foul":
            if self.strikes < 2:
                self.strikes += 1
        elif result == "bunt_foul":
            self.strikes += 1
            if self.strikes >= 3:
                outcome = "K"  # foul bunt with two strikes
        elif result == "hbp":
            outcome = "HBP"
        self._event("P", batting, fielding, batter.id, pitcher.id, result, 0, pt=code, v=round(velo, 1),
                    loc=location, x=actual, z=in_zone)

        moving = swing.steal is not None or swing.hit_and_run
        # Runners in motion on a pitch that is not put in play.
        if moving and result in ("ball", "called", "swinging", "bunt_miss") and outcome not in ("BB", "IBB"):
            strikeout = outcome == "K"
            if strikeout:
                self._finish_pa(batting, fielding, batter.id, pitcher, "K", swing, align, False)
                if self.phase == PHASE_FINAL:
                    return
            if self.outs < 3 and self.phase != PHASE_HALF:
                self._resolve_steal(batting, fielding, swing, call, code, delayed=swing.steal == "delayed",
                                    hit_and_run=swing.hit_and_run and swing.steal is None)
                if self._half_complete():
                    self._end_half()
            return
        if result == "inplay":
            outcome = self._batted_ball(probs, batter, swing, align, actual, d_gb, d_hr)
        elif result == "bunt":
            self._resolve_bunt(batting, fielding, batter, pitcher, swing, align)
            return
        if outcome is not None:
            self._finish_pa(batting, fielding, batter.id, pitcher, outcome, swing, align, moving)

    def _batted_ball(self, probs: dict[str, float], batter: Player, swing: tx.SwingCall, align: tx.Alignment,
                     location: str, d_gb: float, d_hr: float) -> str:
        shares = tx.bip_shares(probs)
        hr_mult, hit_mult, gb = tx.location_bip(location)
        tx._apply_bip(shares, hr=hr_mult * math.exp(d_hr), hit=hit_mult, gb=gb + d_gb)
        tx.batter_bip_mods(shares, batter, swing)
        tx.alignment_bip_mods(shares, align, batter, self.runners(), self.outs)
        shares = tx.normalise(shares)
        return self.rng.choices(list(shares), weights=list(shares.values()))[0]

    def _finish_pa(self, batting: _Side, fielding: _Side, batter_id: int, pitcher: Player, outcome: str,
                   swing: tx.SwingCall, align: tx.Alignment, moving: bool) -> None:
        ctx = self._run_context(swing, align, fielding, moving)
        self._apply_outcome(batting, fielding, batter_id, pitcher, outcome, 0, ctx)
        batting.batter_index = (batting.batter_index + 1) % 9
        self.phase = PHASE_PA
        self._after_play()

    def _resolve_steal(self, batting: _Side, fielding: _Side, swing: tx.SwingCall, call: tx.PitchCall,
                       code: str, delayed: bool, hit_and_run: bool) -> None:
        targets = self.steal_targets()
        if not targets:
            return
        lead_target = targets[0]
        p = self.steal_probability(lead_target, swing, call, code, delayed)
        if hit_and_run:
            p -= 0.03  # the batter swung through it: a straight steal with a decent jump
        runner = self.bases[lead_target - 2]
        assert runner is not None
        line = batting.box.batting[runner.player_id]
        if self.rng.random() < p:
            line.sb += 1
            self.bases[lead_target - 1] = runner
            self.bases[lead_target - 2] = None
            if len(targets) > 1:  # double steal: the trailing runner moves up too
                trailing = self.bases[0]
                if trailing is not None:
                    self.bases[1], self.bases[0] = trailing, None
                    batting.box.batting[trailing.player_id].sb += 1
            self._event("SB", batting, fielding, runner.player_id, fielding.pitcher_id, "SB", 0,
                        base=lead_target)
        else:
            line.cs += 1
            self.bases[lead_target - 2] = None
            if len(targets) > 1 and self.bases[0] is not None:
                self.bases[1], self.bases[0] = self.bases[0], None
            self._record_out(fielding)
            self._event("CS", batting, fielding, runner.player_id, fielding.pitcher_id, "CS", 0,
                        base=lead_target)

    def _resolve_bunt(self, batting: _Side, fielding: _Side, batter: Player, pitcher: Player,
                      swing: tx.SwingCall, align: tx.Alignment) -> None:
        """A fair bunt: sacrifice, bunt hit, fielder's choice or pop-up."""
        rng = self.rng
        b1, b2, b3 = self.bases
        speed = batter.rating("speed")
        skill = (batter.rating("contact") - 50) * 0.004 + (speed - 50) * 0.002
        corners = align.infield == "corners"
        runners = b1 is not None or b2 is not None
        bline = batting.box.batting[batter.id]
        pline = fielding.box.pitching[pitcher.id]
        if runners and self.outs < 2:
            sac = 0.70 + skill - (0.10 if corners else 0.0)
            hit = 0.05 + (speed - 50) * 0.003 - (0.03 if corners else 0.0)
            fc = 0.15 + (0.06 if corners else 0.0)
            roll = rng.random()
            if roll < hit:
                kind = "BH"
            elif roll < hit + sac:
                kind = "SAC"
            elif roll < hit + sac + fc:
                kind = "FC"
            else:
                kind = "POP"
        else:
            hit = 0.20 + (speed - 50) * 0.008 + skill - (0.08 if corners else 0.0)
            kind = "BH" if rng.random() < max(0.03, hit) else "BO"
        bline.pa += 1
        fielding.faced[pitcher.id] = fielding.faced.get(pitcher.id, 0) + 1
        new_runner = _Runner(batter.id, pitcher.id)
        scored: list[_Runner] = []
        if kind == "BH":
            bline.ab += 1
            bline.h += 1
            pline.h += 1
            batting.box.hits += 1
            self.bases = self._force_advance(new_runner, scored)
        elif kind == "SAC":
            # Sacrifice: not an at-bat; every runner moves up one base.
            self._record_out(fielding)
            if self.outs < 3:
                if b3:
                    scored.append(b3)
                self.bases = [None, b1, b2]
        elif kind == "FC":
            bline.ab += 1
            self._record_out(fielding)  # the lead runner is thrown out
            if self.outs < 3:
                self.bases = [new_runner, b1 if b2 else None, b3]
        elif kind == "POP":
            bline.ab += 1
            self._record_out(fielding)
        else:  # "BO": bunted out, runners hold
            bline.ab += 1
            self._record_out(fielding)
        if self.outs >= 3:
            scored = [r for r in scored if kind == "BH"]
        if scored:
            self._score_runs(batting, fielding, scored, batter.id, kind != "FC", kind)
        self._event("PA", batting, fielding, batter.id, pitcher.id, kind, len(scored))
        batting.batter_index = (batting.batter_index + 1) % 9
        self.phase = PHASE_PA
        self._after_play()

    def _force_advance(self, new_runner: _Runner, scored: list[_Runner]) -> list[_Runner | None]:
        """Bases after the batter reaches first and only forced runners move up."""
        b1, b2, b3 = self.bases
        if b1:
            if b2:
                if b3:
                    scored.append(b3)
                b3 = b2
            b2 = b1
        b1 = new_runner
        return [b1, b2, b3]

    # -- live resolution: managerial moves ---------------------------------------------

    def _require_phase(self, *phases: str) -> None:
        if self.phase not in phases:
            names = {PHASE_HALF: "between innings", PHASE_PA: "between batters", PHASE_PITCH: "between pitches",
                     PHASE_FINAL: "after the game"}
            raise RuleError("Only allowed " + " or ".join(names[p] for p in phases) + ".")

    def can_change_pitcher(self, side: _Side) -> str | None:
        """None if a pitching change is allowed now, otherwise the reason."""
        if self.phase not in (PHASE_HALF, PHASE_PA):
            return "Pitching changes are made between batters."
        if self.phase == PHASE_PA and side is not self.fielding_side:
            return "Your club is batting."
        pid = side.pitcher_id
        if pid != side.starter_id and self.phase == PHASE_PA:
            entered = side.entry.get(pid)
            if entered == (self.inning, self.top) and side.faced.get(pid, 0) < MIN_BATTERS_FACED:
                return (f"{self.players[pid].short_name} must face {MIN_BATTERS_FACED} batters or finish the "
                        f"inning ({side.faced.get(pid, 0)} so far).")
        if not self.bullpen(side):
            return "No pitchers left in the bullpen."
        return None

    def change_pitcher(self, key: str, pitcher_id: int) -> None:
        side = self.side(key)
        reason = self.can_change_pitcher(side)
        if reason:
            raise RuleError(reason)
        if pitcher_id not in {p.id for p in self.bullpen(side)}:
            raise RuleError("That pitcher is not available.")
        other = self.away if side is self.home else self.home
        self._install_pitcher(side, other, pitcher_id)

    def bench(self, side: _Side) -> list[Player]:
        """Position players available to enter the game."""
        in_game = set(side.lineup_ids)
        return sorted((self.players[pid] for pid in side.team.active
                       if pid not in in_game and pid not in side.removed and not self.players[pid].is_pitcher),
                      key=lambda p: p.id)

    def _substitute(self, side: _Side, slot: int, new_id: int, position: str, kind: str) -> None:
        old_id, _ = side.order[slot]
        if new_id not in {p.id for p in self.bench(side)}:
            raise RuleError("That player is not available on the bench.")
        side.order[slot] = (new_id, position)
        side.removed.add(old_id)
        side.box.batting_order.append((new_id, position))
        side.box.batting[new_id] = BattingStats(g=1)
        side.box.subs[new_id] = old_id
        if position == "C":
            side.catcher = self.players[new_id]
        side.defense = order_defense(side.order, self.players)
        new, old = self.players[new_id], self.players[old_id]
        labels = {"PH": "pinch-hits for", "PR": "pinch-runs for", "DEF": "replaces"}
        text = f"{side.team.abbr}: {new.short_name} {labels[kind]} {old.short_name}"
        if kind == "DEF":
            text += f" at {position}"
        self.note(side.team.abbr, text + ".", kind="SUB")
        self.events[-1].update({"p": new_id, "x": old_id, "pos": position, "sub": kind})  # type: ignore[index]

    def pinch_hit(self, key: str, bench_id: int) -> None:
        self._require_phase(PHASE_PA)
        side = self.side(key)
        if side is not self.batting_side:
            raise RuleError("Pinch hitters are used when your club bats.")
        slot = side.batter_index
        _, position = side.order[slot]
        self._substitute(side, slot, bench_id, position, "PH")

    def pinch_run(self, key: str, base: int, bench_id: int) -> None:
        self._require_phase(PHASE_PA, PHASE_PITCH)
        side = self.side(key)
        if side is not self.batting_side:
            raise RuleError("Pinch runners are used when your club bats.")
        runner = self.bases[base - 1] if 1 <= base <= 3 else None
        if runner is None:
            raise RuleError(f"There is no runner on base {base}.")
        slot = next((i for i, (pid, _) in enumerate(side.order) if pid == runner.player_id), None)
        if slot is None:
            raise RuleError("That runner is not in the lineup.")
        _, position = side.order[slot]
        self._substitute(side, slot, bench_id, position, "PR")
        self.bases[base - 1] = _Runner(bench_id, runner.pitcher_id, runner.earned)

    def defensive_sub(self, key: str, slot: int, bench_id: int) -> None:
        self._require_phase(PHASE_HALF, PHASE_PA)
        side = self.side(key)
        if self.phase == PHASE_PA and side is self.batting_side:
            raise RuleError("Defensive changes are made when your club takes the field.")
        _, position = side.order[slot]
        if position == "DH":
            raise RuleError("The designated hitter does not play the field.")
        self._substitute(side, slot, bench_id, position, "DEF")

    def intentional_walk(self, key: str) -> None:
        self._require_phase(PHASE_PA)
        side = self.side(key)
        if side is not self.fielding_side:
            raise RuleError("Only the fielding club can issue an intentional walk.")
        batting = self.batting_side
        batter = self.current_batter()
        pitcher = self.current_pitcher()
        self.balls = self.strikes = 0
        self._apply_outcome(batting, side, batter.id, pitcher, "IBB", 0, _RunContext())
        batting.batter_index = (batting.batter_index + 1) % 9
        self.phase = PHASE_PA
        self._after_play()

    # -- live state serialisation ------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Complete live state (for saving a game in progress)."""
        def runner(r: _Runner | None) -> list[Any] | None:
            return [r.player_id, r.pitcher_id, r.earned] if r else None

        def side(s: _Side) -> dict[str, Any]:
            return {
                "team": s.team.to_dict(), "box": s.box.to_dict(), "defense": s.defense, "catcher": s.catcher.id,
                "pitcher": s.pitcher_id, "starter": s.starter_id, "used": list(s.pitchers_used),
                "limit": s.pitch_limit, "bi": s.batter_index,
                "lead": {str(k): v for k, v in s.lead_at_entry.items()},
                "order": [list(x) for x in s.order], "removed": sorted(s.removed),
                "stress": {str(k): v for k, v in s.stress.items()},
                "faced": {str(k): v for k, v in s.faced.items()},
                "entry": {str(k): list(v) for k, v in s.entry.items()},
            }

        state = self.rng.getstate()
        return {
            "away": side(self.away), "home": side(self.home), "outs": self.outs, "inning": self.inning,
            "top": self.top, "bases": [runner(r) for r in self.bases], "leader": self.leader,
            "go_ahead": list(self.go_ahead) if self.go_ahead else None, "phase": self.phase,
            "balls": self.balls, "strikes": self.strikes, "pa_pitches": list(self.pa_pitches),
            "log": list(self.log), "events": [dict(e) for e in self.events or []],
            "rng": [state[0], list(state[1]), state[2]],
        }

    @classmethod
    def restore(cls, data: dict[str, Any], players: Mapping[int, Player]) -> GameSimulator:
        game = cls.__new__(cls)
        game.players = players
        game.pitch_by_pitch = True
        game.events = [dict(e) for e in data["events"]]
        game.rng = random.Random()
        version, internal, gauss = data["rng"]
        game.rng.setstate((version, tuple(internal), gauss))

        def side(key: str, d: dict[str, Any]) -> _Side:
            return _Side(
                key=key, team=Team.from_dict(d["team"]), box=TeamBox.from_dict(d["box"]), defense=float(d["defense"]),
                catcher=players[int(d["catcher"])], pitcher_id=int(d["pitcher"]), starter_id=int(d["starter"]),
                pitchers_used=[int(x) for x in d["used"]], pitch_limit=int(d["limit"]), batter_index=int(d["bi"]),
                lead_at_entry={int(k): int(v) for k, v in d["lead"].items()},
                order=[(int(pid), pos) for pid, pos in d["order"]], removed={int(x) for x in d["removed"]},
                stress={int(k): float(v) for k, v in d["stress"].items()},
                faced={int(k): int(v) for k, v in d["faced"].items()},
                entry={int(k): (int(v[0]), bool(v[1])) for k, v in d["entry"].items()},
            )

        game.away = side("away", data["away"])
        game.home = side("home", data["home"])
        game.log = list(data["log"])
        game.outs = int(data["outs"])
        game.inning = int(data["inning"])
        game.top = bool(data["top"])
        game.bases = [_Runner(int(r[0]), int(r[1]), bool(r[2])) if r else None for r in data["bases"]]
        game.leader = data.get("leader")
        game.go_ahead = (int(data["go_ahead"][0]), int(data["go_ahead"][1])) if data.get("go_ahead") else None
        game.phase = data["phase"]
        game.balls = int(data["balls"])
        game.strikes = int(data["strikes"])
        game.pa_pitches = list(data["pa_pitches"])
        game.result = None
        if game.phase == PHASE_FINAL:
            game.result = game._finish()
        return game

    # -- wrap-up -----------------------------------------------------------------------

    def _finish(self) -> GameResult:
        home_won = self.home.box.runs > self.away.box.runs
        win_side, lose_side = (self.home, self.away) if home_won else (self.away, self.home)
        if self.go_ahead is None:  # pragma: no cover - a game cannot end tied
            self.go_ahead = (win_side.pitcher_id, lose_side.pitcher_id)
        winner_pid, loser_pid = self.go_ahead

        if winner_pid == win_side.starter_id and win_side.box.pitching[winner_pid].outs < 15:
            relievers = [pid for pid in win_side.pitchers_used if pid != win_side.starter_id]
            if relievers:
                winner_pid = max(relievers, key=lambda pid: win_side.box.pitching[pid].outs)

        save_pid: int | None = None
        finisher = win_side.pitcher_id
        if (
            finisher != winner_pid
            and 0 < win_side.lead_at_entry.get(finisher, 0) <= SAVE_MAX_LEAD
            and win_side.box.pitching[finisher].outs >= 1
        ):
            save_pid = finisher

        away_box, home_box = self.away.box, self.home.box
        if self.pitch_by_pitch:
            # Copies: the live state stays decision-free, so a restored game is never double-counted.
            away_box = TeamBox.from_dict(away_box.to_dict())
            home_box = TeamBox.from_dict(home_box.to_dict())
        win_box, lose_box = (home_box, away_box) if home_won else (away_box, home_box)
        win_box.pitching[winner_pid].w += 1
        lose_box.pitching[loser_pid].l += 1
        if save_pid is not None:
            win_box.pitching[save_pid].sv += 1

        workload: dict[int, int] = {}
        if self.pitch_by_pitch:
            for s in (self.away, self.home):
                for pid, load in s.stress.items():
                    effective = int(round(load))
                    if pid in s.box.pitching and effective != s.box.pitching[pid].pitches:
                        workload[pid] = effective

        return GameResult(
            away=self.away.team.abbr,
            home=self.home.team.abbr,
            away_box=away_box,
            home_box=home_box,
            innings=self.inning,
            winning_pitcher=winner_pid,
            losing_pitcher=loser_pid,
            save_pitcher=save_pid,
            log=list(self.log),
            events=list(self.events or []),
            workload=workload,
        )


def steal_attempt_rate(speed: int) -> float:
    return 0.17 if speed >= 70 else 0.09 if speed >= 60 else 0.03 if speed >= 50 else 0.004


def steal_success(speed: int, catcher_arm: int) -> float:
    success = 0.72 + (speed - 60) * 0.01 - (catcher_arm - 50) * 0.006
    return min(0.95, max(0.4, success))


def simulate_game(
    away: Team,
    home: Team,
    players: Mapping[int, Player],
    rng: random.Random,
    away_starter: int,
    home_starter: int,
    record_events: bool = False,
) -> GameResult:
    """Simulate one complete game and return its result."""
    return GameSimulator(away, home, players, rng, away_starter, home_starter, record_events).play()
