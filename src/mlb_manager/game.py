"""Plate-appearance based simulation of a single baseball game.

The engine is pure: it reads player ratings and team configuration, uses the
supplied ``random.Random`` and returns a :class:`GameResult`. It never mutates
players or teams; the caller (``league.py``) applies stats and fatigue.
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from mlb_manager import ratings as rt
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

    @property
    def runs(self) -> int:
        return sum(self.line)

    def to_dict(self) -> dict[str, Any]:
        return {
            "abbr": self.abbr,
            "batting_order": [list(x) for x in self.batting_order],
            "batting": {str(k): v.to_dict() for k, v in self.batting.items()},
            "pitching": {str(k): v.to_dict() for k, v in self.pitching.items()},
            "line": list(self.line),
            "hits": self.hits,
            "errors": self.errors,
        }

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
        return {
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
        )


# ----------------------------------------------------------------------------- helpers


def team_defense(team: Team, players: Mapping[int, Player]) -> float:
    """Weighted defensive rating (1-99) of a lineup's fielders."""
    total = weight = 0.0
    for slot in team.lineup:
        if slot.position == "DH":
            continue
        player = players[slot.player_id]
        importance = rt.POSITION_DEFENSE_IMPORTANCE[slot.position]
        total += rt.defense_rating(player.ratings, slot.position, player.can_play(slot.position)) * importance
        weight += importance
    return total / weight if weight else 50.0


def starter_pitch_limit(pitcher: Player) -> int:
    limit = 93 + (pitcher.rating("stamina") - 60) * 0.8 - pitcher.fatigue * 0.5
    return int(max(45, min(115, limit)))


def reliever_pitch_limit(pitcher: Player) -> int:
    return int(max(15, min(60, 12 + pitcher.rating("stamina") * 0.45 - pitcher.fatigue * 0.3)))


@dataclass(slots=True)
class _Runner:
    player_id: int
    pitcher_id: int  # pitcher charged if this runner scores
    earned: bool = True


@dataclass(slots=True)
class _Side:
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

    def current_pitch_count(self) -> int:
        return self.box.pitching[self.pitcher_id].pitches


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
    ) -> None:
        self.players = players
        # Event recording only appends snapshots; it never touches the RNG, so the
        # same game plays out identically with or without it.
        self.events: list[dict[str, Any]] | None = [] if record_events else None
        self.rng = rng
        self.away = self._make_side(away, away_starter)
        self.home = self._make_side(home, home_starter)
        self.log: list[str] = []
        self.outs = 0
        self.inning = 1
        self.top = True
        self.bases: list[_Runner | None] = [None, None, None]
        self.leader: str | None = None
        self.go_ahead: tuple[int, int] | None = None  # (winning pitcher, losing pitcher)

    # -- setup -------------------------------------------------------------------------

    def _make_side(self, team: Team, starter_id: int) -> _Side:
        box = TeamBox(abbr=team.abbr)
        for slot in team.lineup:
            box.batting_order.append((slot.player_id, slot.position))
            box.batting[slot.player_id] = BattingStats(g=1)
        catcher_id = next(s.player_id for s in team.lineup if s.position == "C")
        starter = self.players[starter_id]
        box.pitching[starter_id] = PitchingStats(g=1, gs=1)
        return _Side(
            team=team,
            box=box,
            defense=team_defense(team, self.players),
            catcher=self.players[catcher_id],
            pitcher_id=starter_id,
            starter_id=starter_id,
            pitchers_used=[starter_id],
            pitch_limit=starter_pitch_limit(starter),
            lead_at_entry={starter_id: 0},
        )

    # -- public ------------------------------------------------------------------------

    def play(self) -> GameResult:
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
        return self._finish()

    # -- innings -----------------------------------------------------------------------

    def _play_half(self, batting: _Side, fielding: _Side) -> None:
        self.outs = 0
        self.bases = [None, None, None]
        batting.box.line.append(0)
        self._consider_pitching_change(fielding, batting, inning_start=True)
        if self.inning > REGULATION_INNINGS:
            # Extra-inning rule: the previous batter starts on second base.
            prev = batting.box.batting_order[(batting.batter_index - 1) % 9][0]
            self.bases[1] = _Runner(prev, fielding.pitcher_id, earned=False)
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

    def _is_walk_off(self, batting: _Side) -> bool:
        return (
            batting is self.home
            and self.inning >= REGULATION_INNINGS
            and self.home.box.runs > self.away.box.runs
        )

    # -- pitching changes --------------------------------------------------------------

    def _consider_pitching_change(self, fielding: _Side, batting: _Side, inning_start: bool) -> None:
        pid = fielding.pitcher_id
        line = fielding.box.pitching[pid]
        pitches = line.pitches
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
            return
        reliever = self._choose_reliever(fielding, lead)
        if reliever is None:
            return
        fielding.pitcher_id = reliever.id
        fielding.pitchers_used.append(reliever.id)
        fielding.pitch_limit = reliever_pitch_limit(reliever)
        fielding.lead_at_entry[reliever.id] = lead
        fielding.box.pitching[reliever.id] = PitchingStats(g=1)
        self._event("PC", batting, fielding, None, reliever.id, "PC", 0)
        half = "Top" if self.top else "Bot"
        self.log.append(f"{half} {self.inning}: {fielding.team.abbr} brings in {reliever.short_name}.")

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

    # -- plate appearances -------------------------------------------------------------

    def _maybe_steal(self, batting: _Side, fielding: _Side) -> None:
        runner = self.bases[0]
        if runner is None or self.bases[1] is not None or self.outs >= 2:
            return
        speed = self.players[runner.player_id].rating("speed")
        attempt = 0.17 if speed >= 70 else 0.09 if speed >= 60 else 0.03 if speed >= 50 else 0.004
        if self.rng.random() >= attempt:
            return
        success = 0.72 + (speed - 60) * 0.01 - (fielding.catcher.rating("arm") - 50) * 0.006
        success = min(0.95, max(0.4, success))
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

    def _outcome_probabilities(self, batter: Player, pitcher: Player, fielding: _Side) -> dict[str, float]:
        pitches = fielding.box.pitching[pitcher.id].pitches
        tired = max(0, pitches - fielding.pitch_limit) * 0.5 + pitcher.fatigue * 0.05
        tired = min(tired, 25)
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
            - 0.0018 * (fielding.defense - 50)
            - 0.0012 * (movement - 50)
        )
        babip = min(0.40, max(0.20, babip))
        hits = in_play * babip
        triple_share = min(0.08, 0.025 * math.exp(0.04 * (speed - 50)))
        double_share = min(0.35, 0.21 * math.exp(0.015 * (power - 50)))
        outs = in_play - hits
        error_rate = min(0.07, 0.03 * math.exp(-0.03 * (fielding.defense - 50)))
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
        batter_id, _ = batting.box.batting_order[batting.batter_index]
        batting.batter_index = (batting.batter_index + 1) % 9
        batter = self.players[batter_id]
        pitcher = self.players[fielding.pitcher_id]
        probs = self._outcome_probabilities(batter, pitcher, fielding)
        outcome = self.rng.choices(list(probs), weights=list(probs.values()))[0]

        bline = batting.box.batting[batter_id]
        pline = fielding.box.pitching[pitcher.id]
        outs_before = self.outs
        bline.pa += 1
        pline.pitches += self._pitch_count(outcome)

        new_runner = _Runner(batter_id, pitcher.id)
        rbi_eligible = True
        scored: list[_Runner] = []
        rng = self.rng
        b1, b2, b3 = self.bases

        def spd(runner: _Runner | None) -> int:
            return self.players[runner.player_id].rating("speed") if runner else 50

        if outcome == "K":
            bline.ab += 1
            bline.so += 1
            pline.so += 1
            self._record_out(fielding)
        elif outcome in ("BB", "HBP"):
            if outcome == "BB":
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
                if rng.random() < p_score:
                    scored.append(b1)
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
                if rng.random() < p_score:
                    scored.append(b2)
                else:
                    new3 = b2
            if b1:
                p_third = 0.27 + (spd(b1) - 50) * 0.008
                if new3 is None and rng.random() < p_third:
                    new3 = b1
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
            if b1 and self.outs < 2:
                dp_chance = 0.55 - (batter.rating("speed") - 50) * 0.006 + (fielding.defense - 50) * 0.004
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
                    if b3 and rng.random() < 0.5:
                        scored.append(b3)
                        b3 = None
                    if b2 and b3 is None and rng.random() < 0.6:
                        b3, b2 = b2, None
                    if b1 and b2 is None:
                        b2, b1 = b1, None
        elif outcome == "FO":
            self._record_out(fielding)
            if self.outs < 3 and b3 and rng.random() < 0.62 + (spd(b3) - 50) * 0.006:
                scored.append(b3)
                b3 = None
                bline.sf += 1
            else:
                bline.ab += 1
            if self.outs < 3 and b2 and b3 is None and rng.random() < 0.3:
                b3, b2 = b2, None
        else:  # "LO" line drive / pop up: nobody advances
            bline.ab += 1
            self._record_out(fielding)

        if self.outs >= 3:
            # Runs on the final out (e.g. a forced third out) do not count.
            scored = [r for r in scored if outcome in ("HR", "1B", "2B", "3B", "BB", "HBP", "E")]
        self.bases = [b1, b2, b3]
        if outcome in ("1B", "2B", "3B", "HR"):
            batting.box.hits += 1
        if scored:
            self._score_runs(batting, fielding, scored, batter_id, rbi_eligible, outcome)
        if self.events is not None:
            double_play = outcome == "GO" and self.outs - outs_before >= 2
            self._event("PA", batting, fielding, batter_id, pitcher.id, "DP" if double_play else outcome,
                        len(scored))

    def _event(self, kind: str, batting: _Side, fielding: _Side, player_id: int | None,
               pitcher_id: int, outcome: str, runs: int) -> None:
        """Snapshot of the game state after a play (no-op unless recording)."""
        if self.events is None:
            return
        self.events.append({
            "k": kind, "i": self.inning, "top": self.top, "o": self.outs,
            "b": [r.player_id if r else 0 for r in self.bases],
            "a": self.away.box.runs, "h": self.home.box.runs,
            "p": player_id or 0, "pit": pitcher_id, "r": outcome, "n": runs, "team": batting.team.abbr,
        })

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
            "HBP": "is hit by a pitch",
            "E": "reaches on an error",
            "GO": "grounds into a play",
            "FO": "hits a sacrifice fly",
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

        win_side.box.pitching[winner_pid].w += 1
        lose_side.box.pitching[loser_pid].l += 1
        if save_pid is not None:
            win_side.box.pitching[save_pid].sv += 1

        return GameResult(
            away=self.away.team.abbr,
            home=self.home.team.abbr,
            away_box=self.away.box,
            home_box=self.home.box,
            innings=self.inning,
            winning_pitcher=winner_pid,
            losing_pitcher=loser_pid,
            save_pitcher=save_pid,
            log=self.log,
            events=self.events or [],
        )


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
