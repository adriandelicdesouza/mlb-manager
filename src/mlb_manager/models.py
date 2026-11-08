"""Core domain dataclasses with JSON-friendly (de)serialisation."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any

from mlb_manager import ratings as rt


@dataclass(slots=True)
class BattingStats:
    g: int = 0
    pa: int = 0
    ab: int = 0
    r: int = 0
    h: int = 0
    doubles: int = 0
    triples: int = 0
    hr: int = 0
    rbi: int = 0
    bb: int = 0
    so: int = 0
    hbp: int = 0
    sf: int = 0
    sb: int = 0
    cs: int = 0

    @property
    def avg(self) -> float:
        return self.h / self.ab if self.ab else 0.0

    @property
    def obp(self) -> float:
        denom = self.ab + self.bb + self.hbp + self.sf
        return (self.h + self.bb + self.hbp) / denom if denom else 0.0

    @property
    def total_bases(self) -> int:
        singles = self.h - self.doubles - self.triples - self.hr
        return singles + 2 * self.doubles + 3 * self.triples + 4 * self.hr

    @property
    def slg(self) -> float:
        return self.total_bases / self.ab if self.ab else 0.0

    @property
    def ops(self) -> float:
        return self.obp + self.slg

    def add(self, other: BattingStats) -> None:
        for f in fields(self):
            setattr(self, f.name, getattr(self, f.name) + getattr(other, f.name))

    def to_dict(self) -> dict[str, int]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> BattingStats:
        return cls(**(data or {}))

    def to_list(self) -> list[int]:
        """Compact positional form used for career history."""
        return [getattr(self, f.name) for f in fields(self)]

    @classmethod
    def from_list(cls, values: list[int] | None) -> BattingStats:
        names = [f.name for f in fields(cls)]
        return cls(**dict(zip(names, (int(v) for v in (values or [])), strict=False)))

    def copy(self) -> BattingStats:
        return type(self).from_list(self.to_list())

    def minus(self, other: BattingStats) -> BattingStats:
        out = self.copy()
        for f in fields(out):
            setattr(out, f.name, getattr(out, f.name) - getattr(other, f.name))
        return out

    @property
    def is_empty(self) -> bool:
        return not any(self.to_list())


@dataclass(slots=True)
class PitchingStats:
    g: int = 0
    gs: int = 0
    w: int = 0
    l: int = 0  # noqa: E741 - standard baseball abbreviation
    sv: int = 0
    outs: int = 0
    h: int = 0
    r: int = 0
    er: int = 0
    bb: int = 0
    so: int = 0
    hr: int = 0
    pitches: int = 0

    @property
    def innings(self) -> float:
        return self.outs / 3

    @property
    def ip_display(self) -> str:
        return f"{self.outs // 3}.{self.outs % 3}"

    @property
    def era(self) -> float:
        return self.er * 27 / self.outs if self.outs else 0.0

    @property
    def whip(self) -> float:
        return (self.bb + self.h) * 3 / self.outs if self.outs else 0.0

    def add(self, other: PitchingStats) -> None:
        for f in fields(self):
            setattr(self, f.name, getattr(self, f.name) + getattr(other, f.name))

    def to_dict(self) -> dict[str, int]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> PitchingStats:
        return cls(**(data or {}))

    def to_list(self) -> list[int]:
        """Compact positional form used for career history."""
        return [getattr(self, f.name) for f in fields(self)]

    @classmethod
    def from_list(cls, values: list[int] | None) -> PitchingStats:
        names = [f.name for f in fields(cls)]
        return cls(**dict(zip(names, (int(v) for v in (values or [])), strict=False)))

    def copy(self) -> PitchingStats:
        return type(self).from_list(self.to_list())

    def minus(self, other: PitchingStats) -> PitchingStats:
        out = self.copy()
        for f in fields(out):
            setattr(out, f.name, getattr(out, f.name) - getattr(other, f.name))
        return out

    @property
    def is_empty(self) -> bool:
        return not any(self.to_list())


@dataclass(slots=True)
class Player:
    id: int
    first_name: str
    last_name: str
    age: int
    bats: str  # "L", "R" or "S"
    throws: str  # "L" or "R"
    position: str  # primary position, one of ratings.ALL_POSITIONS
    secondary_positions: list[str] = field(default_factory=list)
    ratings: dict[str, int] = field(default_factory=dict)
    team: str | None = None
    fatigue: int = 0  # pitch-count style fatigue, recovers daily
    batting: BattingStats = field(default_factory=BattingStats)
    pitching: PitchingStats = field(default_factory=PitchingStats)
    # Front office data -------------------------------------------------------
    potential: int = 0  # true ceiling for overall (0 = not yet initialised)
    development: str = "normal"  # "fast", "normal" or "slow" growth trajectory
    salary: float = 0.0  # annual salary in $ millions
    contract_years: int = 0  # seasons remaining, including the current one
    contract_length: int = 0  # length of the contract when signed
    draft_year: int | None = None
    draft_round: int | None = None
    draft_pick: int | None = None
    # In-season data -----------------------------------------------------------
    injury: str | None = None  # description of the current injury
    injury_days: int = 0  # days until healthy (0 = healthy)
    service_years: int = 0  # completed MLB seasons (rookie = 0)
    games_by_pos: dict[str, int] = field(default_factory=dict)  # season games started per position
    # Careers, injuries and morale (v0.4) -----------------------------------------
    # Postseason lines for the current season, keyed by round code (WC/DS/CS/WS);
    # never mixed into ``batting``/``pitching``.
    post_batting: dict[str, BattingStats] = field(default_factory=dict)
    post_pitching: dict[str, PitchingStats] = field(default_factory=dict)
    # Regular-season stats accumulated with earlier clubs this season (closed at a
    # trade/release): [{"t": team, "b": [...], "p": [...]}].
    stints: list[dict[str, Any]] = field(default_factory=list)
    # Archived seasons, one entry per (year, club): {"y", "t", "b", "p", "post"?}.
    history: list[dict[str, Any]] = field(default_factory=list)
    injury_total: int = 0  # length of the current injury when it happened
    injury_history: list[dict[str, Any]] = field(default_factory=list)  # {"y", "d", "i", "n"}
    rehab: bool = False  # on a rehab assignment with the minor league club
    rust_days: int = 0  # returned from a long injury without rehab: reduced form
    morale: int = 50  # 0-100, 50 = neutral

    @property
    def is_injured(self) -> bool:
        return self.injury_days > 0

    @property
    def name(self) -> str:
        return f"{self.first_name} {self.last_name}"

    @property
    def short_name(self) -> str:
        return f"{self.first_name[0]}. {self.last_name}"

    @property
    def is_pitcher(self) -> bool:
        return self.position in rt.PITCHER_POSITIONS

    @property
    def eligible_positions(self) -> list[str]:
        """Positions the player can field without an out-of-position penalty."""
        if self.is_pitcher:
            return [self.position]
        return [self.position, *self.secondary_positions]

    def can_play(self, position: str) -> bool:
        if position == "DH":
            return not self.is_pitcher
        return position in self.eligible_positions

    @property
    def overall(self) -> int:
        return rt.overall(self.ratings, self.position)

    def overall_at(self, position: str) -> int:
        if position in rt.PITCHER_POSITIONS:
            return rt.overall(self.ratings, position)
        bat = rt.batting_rating(self.ratings)
        if position == "DH":
            return rt.clamp_rating(bat)
        share = rt.DEFENSE_SHARE[position]
        d = rt.defense_rating(self.ratings, position, self.can_play(position))
        return rt.clamp_rating(bat * (1 - share) + d * share)

    def rating(self, attr: str) -> int:
        return self.ratings.get(attr, rt.RATING_MIN)

    def reset_season_stats(self) -> None:
        self.batting = BattingStats()
        self.pitching = PitchingStats()
        self.games_by_pos = {}
        self.post_batting = {}
        self.post_pitching = {}
        self.stints = []

    @property
    def main_position(self) -> str:
        """Position played most this season (primary position if none)."""
        if not self.games_by_pos:
            return self.position
        return max(sorted(self.games_by_pos), key=lambda pos: self.games_by_pos[pos])

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "age": self.age,
            "bats": self.bats,
            "throws": self.throws,
            "position": self.position,
            "secondary_positions": list(self.secondary_positions),
            "ratings": dict(self.ratings),
            "team": self.team,
            "fatigue": self.fatigue,
            "batting": self.batting.to_dict(),
            "pitching": self.pitching.to_dict(),
            "potential": self.potential,
            "development": self.development,
            "salary": self.salary,
            "contract_years": self.contract_years,
            "contract_length": self.contract_length,
            "draft_year": self.draft_year,
            "draft_round": self.draft_round,
            "draft_pick": self.draft_pick,
            "injury": self.injury,
            "injury_days": self.injury_days,
            "service_years": self.service_years,
            "games_by_pos": dict(self.games_by_pos),
            "post_batting": {k: v.to_list() for k, v in self.post_batting.items()},
            "post_pitching": {k: v.to_list() for k, v in self.post_pitching.items()},
            "stints": [dict(x) for x in self.stints],
            "history": [dict(x) for x in self.history],
            "injury_total": self.injury_total,
            "injury_history": [dict(x) for x in self.injury_history],
            "rehab": self.rehab,
            "rust_days": self.rust_days,
            "morale": self.morale,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Player:
        return cls(
            id=int(data["id"]),
            first_name=data["first_name"],
            last_name=data["last_name"],
            age=int(data["age"]),
            bats=data["bats"],
            throws=data["throws"],
            position=data["position"],
            secondary_positions=list(data.get("secondary_positions", [])),
            ratings={k: int(v) for k, v in data["ratings"].items()},
            team=data.get("team"),
            fatigue=int(data.get("fatigue", 0)),
            batting=BattingStats.from_dict(data.get("batting")),
            pitching=PitchingStats.from_dict(data.get("pitching")),
            potential=int(data.get("potential", 0)),
            development=data.get("development", "normal"),
            salary=float(data.get("salary", 0.0)),
            contract_years=int(data.get("contract_years", 0)),
            contract_length=int(data.get("contract_length", 0)),
            draft_year=data.get("draft_year"),
            draft_round=data.get("draft_round"),
            draft_pick=data.get("draft_pick"),
            injury=data.get("injury"),
            injury_days=int(data.get("injury_days", 0)),
            service_years=int(data.get("service_years", 0)),
            games_by_pos={k: int(v) for k, v in data.get("games_by_pos", {}).items()},
            post_batting={k: BattingStats.from_list(v) for k, v in data.get("post_batting", {}).items()},
            post_pitching={k: PitchingStats.from_list(v) for k, v in data.get("post_pitching", {}).items()},
            stints=[dict(x) for x in data.get("stints", [])],
            history=[dict(x) for x in data.get("history", [])],
            injury_total=int(data.get("injury_total", 0)),
            injury_history=[dict(x) for x in data.get("injury_history", [])],
            rehab=bool(data.get("rehab", False)),
            rust_days=int(data.get("rust_days", 0)),
            morale=int(data.get("morale", 50)),
        )


@dataclass(slots=True)
class RetiredPlayer:
    """A retired player's permanent record (career history survives retirement)."""

    id: int
    first_name: str
    last_name: str
    position: str
    age: int  # at retirement
    retired_year: int
    last_team: str | None
    history: list[dict[str, Any]] = field(default_factory=list)
    draft_year: int | None = None
    injuries: int = 0  # career injury count

    @property
    def name(self) -> str:
        return f"{self.first_name} {self.last_name}"

    @property
    def is_pitcher(self) -> bool:
        return self.position in rt.PITCHER_POSITIONS

    @classmethod
    def from_player(cls, player: Player, year: int) -> RetiredPlayer:
        return cls(
            id=player.id, first_name=player.first_name, last_name=player.last_name,
            position=player.position, age=player.age, retired_year=year, last_team=player.team,
            history=[dict(x) for x in player.history], draft_year=player.draft_year,
            injuries=len(player.injury_history),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "first_name": self.first_name, "last_name": self.last_name,
            "position": self.position, "age": self.age, "retired_year": self.retired_year,
            "last_team": self.last_team, "history": [dict(x) for x in self.history],
            "draft_year": self.draft_year, "injuries": self.injuries,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RetiredPlayer:
        return cls(
            id=int(data["id"]), first_name=data["first_name"], last_name=data["last_name"],
            position=data["position"], age=int(data["age"]), retired_year=int(data["retired_year"]),
            last_team=data.get("last_team"), history=[dict(x) for x in data.get("history", [])],
            draft_year=data.get("draft_year"), injuries=int(data.get("injuries", 0)),
        )


@dataclass(slots=True)
class LineupSlot:
    player_id: int
    position: str

    def to_dict(self) -> dict[str, Any]:
        return {"player_id": self.player_id, "position": self.position}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LineupSlot:
        return cls(int(data["player_id"]), data["position"])


@dataclass(slots=True)
class Team:
    abbr: str
    city: str
    name: str
    league: str
    division: str
    active: list[int] = field(default_factory=list)
    reserves: list[int] = field(default_factory=list)
    lineup: list[LineupSlot] = field(default_factory=list)
    rotation: list[int] = field(default_factory=list)
    closer: int | None = None
    rotation_index: int = 0
    strategy: str = "balanced"  # "contending", "balanced" or "rebuilding"
    dead_money: float = 0.0  # salary owed to released players this season
    injured_list: list[int] = field(default_factory=list)  # IL: in the org, off the 26

    @property
    def full_name(self) -> str:
        return f"{self.city} {self.name}"

    @property
    def division_key(self) -> str:
        return f"{self.league} {self.division}"

    @property
    def all_players(self) -> list[int]:
        return [*self.active, *self.reserves, *self.injured_list]

    def remove_player(self, player_id: int) -> None:
        """Remove a player from whichever roster list holds him."""
        for group in (self.active, self.reserves, self.injured_list):
            if player_id in group:
                group.remove(player_id)
                return
        raise ValueError(f"Player #{player_id} is not in {self.abbr}'s organisation")

    def to_dict(self) -> dict[str, Any]:
        return {
            "abbr": self.abbr,
            "city": self.city,
            "name": self.name,
            "league": self.league,
            "division": self.division,
            "active": list(self.active),
            "reserves": list(self.reserves),
            "lineup": [s.to_dict() for s in self.lineup],
            "rotation": list(self.rotation),
            "closer": self.closer,
            "rotation_index": self.rotation_index,
            "strategy": self.strategy,
            "dead_money": self.dead_money,
            "injured_list": list(self.injured_list),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Team:
        return cls(
            abbr=data["abbr"],
            city=data["city"],
            name=data["name"],
            league=data["league"],
            division=data["division"],
            active=[int(x) for x in data["active"]],
            reserves=[int(x) for x in data["reserves"]],
            lineup=[LineupSlot.from_dict(s) for s in data.get("lineup", [])],
            rotation=[int(x) for x in data.get("rotation", [])],
            closer=data.get("closer"),
            rotation_index=int(data.get("rotation_index", 0)),
            strategy=data.get("strategy", "balanced"),
            dead_money=float(data.get("dead_money", 0.0)),
            injured_list=[int(x) for x in data.get("injured_list", [])],
        )
