"""Career, postseason and franchise statistics.

Storage model (all on :class:`~mlb_manager.models.Player`):

* ``batting`` / ``pitching`` hold the *current* regular season only.
* ``post_batting`` / ``post_pitching`` hold the current postseason, keyed by
  round code (``WC``, ``DS``, ``CS``, ``WS``). They are never merged into the
  regular-season lines.
* ``stints`` records the regular-season stats a player accumulated with
  earlier clubs this season (closed when he is traded or released), so
  statistics follow the player while still being credited to the right club.
* ``history`` holds archived seasons, one entry per (year, club):
  ``{"y": year, "t": club, "b": [...], "p": [...], "post": {round: {"b", "p"}}}``.
  Stats are stored as compact positional lists (see ``BattingStats.to_list``).

The live season is archived when the offseason begins. Until then career
totals are ``history + live season``; a season is never counted twice because
history entries for a year only exist once it has been archived.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from mlb_manager.models import BattingStats, PitchingStats, Player, RetiredPlayer

if TYPE_CHECKING:
    from mlb_manager.league import League

ROUND_CODES: tuple[str, ...] = ("WC", "DS", "CS", "WS")
ROUND_LABELS: dict[str, str] = {"WC": "Wild Card", "DS": "Division Series", "CS": "Championship Series",
                                "WS": "World Series"}

# Qualification for rate records (per 162 games, as in MLB).
QUALIFY_PA = 502
QUALIFY_OUTS = 162 * 3
CAREER_QUALIFY_PA = 1500
CAREER_QUALIFY_OUTS = 1000 * 3

CAREER_BATTING_MILESTONES: dict[str, tuple[int, ...]] = {
    "hr": (100, 200, 300, 400, 500, 600, 700),
    "h": (1000, 1500, 2000, 2500, 3000),
    "rbi": (1000, 1500, 2000),
    "sb": (300, 500),
}
CAREER_PITCHING_MILESTONES: dict[str, tuple[int, ...]] = {
    "w": (100, 150, 200, 250, 300),
    "so": (1000, 1500, 2000, 2500, 3000),
    "sv": (100, 200, 300, 400),
}
STAT_LABELS: dict[str, str] = {
    "hr": "home runs", "h": "hits", "rbi": "RBI", "sb": "stolen bases", "r": "runs", "bb": "walks",
    "w": "wins", "so": "strikeouts", "sv": "saves", "g": "games",
}


class HasHistory(Protocol):
    id: int
    history: list[dict[str, Any]]

    @property
    def name(self) -> str: ...

    @property
    def is_pitcher(self) -> bool: ...


# ----------------------------------------------------------------------------- live bookkeeping


def round_code(round_index: int) -> str:
    return ROUND_CODES[min(round_index, len(ROUND_CODES) - 1)]


def add_postseason(player: Player, code: str, batting: BattingStats | None,
                   pitching: PitchingStats | None) -> None:
    if batting is not None:
        player.post_batting.setdefault(code, BattingStats()).add(batting)
    if pitching is not None:
        player.post_pitching.setdefault(code, PitchingStats()).add(pitching)


def _closed(player: Player) -> tuple[BattingStats, PitchingStats]:
    bat, pit = BattingStats(), PitchingStats()
    for stint in player.stints:
        bat.add(BattingStats.from_list(stint.get("b")))
        pit.add(PitchingStats.from_list(stint.get("p")))
    return bat, pit


def close_stint(player: Player) -> None:
    """Credit the stats since the last club change to ``player.team``.

    Call *before* the player leaves his club (trade, release, free agency).
    """
    if player.team is None:
        return
    bat_done, pit_done = _closed(player)
    bat = player.batting.minus(bat_done)
    pit = player.pitching.minus(pit_done)
    if bat.is_empty and pit.is_empty:
        return
    player.stints.append({"t": player.team, "b": bat.to_list(), "p": pit.to_list()})


def archive_season(player: Player, year: int, last_team: str | None = None) -> None:
    """Move the live season (stints, remainder and postseason) into ``history``."""
    if any(h["y"] == year for h in player.history):
        return  # already archived
    entries: list[dict[str, Any]] = []
    for stint in player.stints:
        entries.append({"y": year, "t": stint["t"], "b": list(stint["b"]), "p": list(stint["p"])})
    bat_done, pit_done = _closed(player)
    bat = player.batting.minus(bat_done)
    pit = player.pitching.minus(pit_done)
    team = player.team or last_team
    post = {
        code: {"b": player.post_batting.get(code, BattingStats()).to_list(),
               "p": player.post_pitching.get(code, PitchingStats()).to_list()}
        for code in ROUND_CODES
        if code in player.post_batting or code in player.post_pitching
    }
    if not (bat.is_empty and pit.is_empty) or post:
        if team is None and entries:
            team = entries[-1]["t"]
        entry: dict[str, Any] = {"y": year, "t": team or "FA", "b": bat.to_list(), "p": pit.to_list()}
        if post:
            entry["post"] = post
        entries.append(entry)
    # Merge repeated stints with the same club in one season.
    merged: list[dict[str, Any]] = []
    for entry in entries:
        same = next((m for m in merged if m["t"] == entry["t"]), None)
        if same is None:
            merged.append(entry)
            continue
        b = BattingStats.from_list(same["b"])
        b.add(BattingStats.from_list(entry["b"]))
        p = PitchingStats.from_list(same["p"])
        p.add(PitchingStats.from_list(entry["p"]))
        same["b"], same["p"] = b.to_list(), p.to_list()
        if "post" in entry:
            same["post"] = entry["post"]
    player.history.extend(merged)
    player.stints = []


# ----------------------------------------------------------------------------- totals


@dataclass(slots=True)
class StatLine:
    """One row of a player's career table."""

    year: int
    team: str
    batting: BattingStats
    pitching: PitchingStats


def _is_archived(player: HasHistory, year: int | None) -> bool:
    return year is None or any(h["y"] == year for h in player.history)


def season_rows(player: HasHistory, year: int | None = None) -> list[StatLine]:
    """Regular-season rows per (year, club), including the live season if not archived."""
    rows = [
        StatLine(h["y"], h["t"], BattingStats.from_list(h.get("b")), PitchingStats.from_list(h.get("p")))
        for h in player.history
    ]
    if isinstance(player, Player) and not _is_archived(player, year):
        assert year is not None
        for stint in player.stints:
            rows.append(StatLine(year, stint["t"], BattingStats.from_list(stint["b"]),
                                 PitchingStats.from_list(stint["p"])))
        bat_done, pit_done = _closed(player)
        bat, pit = player.batting.minus(bat_done), player.pitching.minus(pit_done)
        if not (bat.is_empty and pit.is_empty):
            rows.append(StatLine(year, player.team or "FA", bat, pit))
    return rows


def postseason_rows(player: HasHistory, year: int | None = None,
                    rounds: Iterable[str] = ROUND_CODES) -> list[StatLine]:
    """Postseason rows per year (summed over the requested rounds)."""
    wanted = set(rounds)
    rows: list[StatLine] = []
    for h in player.history:
        post = h.get("post") or {}
        bat, pit = BattingStats(), PitchingStats()
        found = False
        for code, line in post.items():
            if code in wanted:
                bat.add(BattingStats.from_list(line.get("b")))
                pit.add(PitchingStats.from_list(line.get("p")))
                found = True
        if found:
            rows.append(StatLine(h["y"], h["t"], bat, pit))
    if isinstance(player, Player) and not _is_archived(player, year):
        assert year is not None
        bat, pit = BattingStats(), PitchingStats()
        found = False
        for code in wanted:
            if code in player.post_batting:
                bat.add(player.post_batting[code])
                found = True
            if code in player.post_pitching:
                pit.add(player.post_pitching[code])
                found = True
        if found:
            rows.append(StatLine(year, player.team or "FA", bat, pit))
    return rows


def _sum(rows: Iterable[StatLine]) -> tuple[BattingStats, PitchingStats]:
    bat, pit = BattingStats(), PitchingStats()
    for row in rows:
        bat.add(row.batting)
        pit.add(row.pitching)
    return bat, pit


def career_totals(player: HasHistory, year: int | None = None) -> tuple[BattingStats, PitchingStats]:
    """Career regular-season totals (``year`` = the league's current season)."""
    return _sum(season_rows(player, year))


def postseason_totals(player: HasHistory, year: int | None = None,
                      rounds: Iterable[str] = ROUND_CODES) -> tuple[BattingStats, PitchingStats]:
    return _sum(postseason_rows(player, year, rounds))


def world_series_totals(player: HasHistory, year: int | None = None) -> tuple[BattingStats, PitchingStats]:
    return postseason_totals(player, year, ("WS",))


def current_postseason(player: Player) -> tuple[BattingStats, PitchingStats]:
    bat, pit = BattingStats(), PitchingStats()
    for line in player.post_batting.values():
        bat.add(line)
    for pline in player.post_pitching.values():
        pit.add(pline)
    return bat, pit


def seasons_played(player: HasHistory, year: int | None = None) -> int:
    return len({row.year for row in season_rows(player, year)
                if row.batting.g or row.pitching.g})


# ----------------------------------------------------------------------------- milestones


def _crossed(after: int, gained: int, thresholds: tuple[int, ...]) -> list[int]:
    before = after - gained
    return [t for t in thresholds if before < t <= after]


def career_milestones(player: Player, base: tuple[BattingStats, PitchingStats],
                      batting: BattingStats | None, pitching: PitchingStats | None) -> list[str]:
    """Career thresholds crossed by today's line.

    ``base`` is the player's archived career total; the live season is added here.
    Call *after* today's line has been added to the season stats.
    """
    out: list[str] = []
    if batting is not None:
        for stat, marks in CAREER_BATTING_MILESTONES.items():
            gained = getattr(batting, stat)
            if not gained:
                continue
            total = getattr(base[0], stat) + getattr(player.batting, stat)
            for mark in _crossed(total, gained, marks):
                out.append(f"{player.name} ({player.team}) reaches {mark} career {STAT_LABELS[stat]}.")
    if pitching is not None:
        for stat, marks in CAREER_PITCHING_MILESTONES.items():
            gained = getattr(pitching, stat)
            if not gained:
                continue
            total = getattr(base[1], stat) + getattr(player.pitching, stat)
            for mark in _crossed(total, gained, marks):
                out.append(f"{player.name} ({player.team}) reaches {mark} career {STAT_LABELS[stat]}.")
    return out


def archived_totals(player: HasHistory) -> tuple[BattingStats, PitchingStats]:
    return career_totals(player, None)


# ----------------------------------------------------------------------------- leaderboards


Value = Callable[[BattingStats, PitchingStats], float]

BATTING_STATS: dict[str, tuple[str, bool]] = {
    # key: (label, is rate stat)
    "g": ("G", False), "h": ("H", False), "hr": ("HR", False), "rbi": ("RBI", False),
    "r": ("R", False), "bb": ("BB", False), "sb": ("SB", False),
    "avg": ("AVG", True), "obp": ("OBP", True), "slg": ("SLG", True), "ops": ("OPS", True),
}
PITCHING_STATS: dict[str, tuple[str, bool]] = {
    "w": ("W", False), "so": ("SO", False), "sv": ("SV", False), "g": ("G", False),
    "outs": ("IP", False), "era": ("ERA", True), "whip": ("WHIP", True),
}
ASCENDING = {"era", "whip"}


def all_people(league: League) -> list[HasHistory]:
    people: list[HasHistory] = list(league.players.values())
    people.extend(league.retired.values())
    return people


def _value(stats: BattingStats | PitchingStats, stat: str) -> float:
    return float(getattr(stats, stat))


def career_leaders(league: League, stat: str, pitching: bool = False, limit: int = 10,
                   team: str | None = None, postseason: bool = False) -> list[tuple[HasHistory, float]]:
    """All-time leaders (active and retired). ``team`` restricts to stats with that club."""
    rates = PITCHING_STATS if pitching else BATTING_STATS
    if stat not in rates:
        raise KeyError(stat)
    is_rate = rates[stat][1]
    out: list[tuple[HasHistory, float]] = []
    for person in all_people(league):
        if pitching != person.is_pitcher:
            continue
        rows = (postseason_rows(person, league.year) if postseason else season_rows(person, league.year))
        if team is not None:
            rows = [r for r in rows if r.team == team]
        if not rows:
            continue
        bat, pit = _sum(rows)
        line: BattingStats | PitchingStats = pit if pitching else bat
        if is_rate and not postseason:
            if pitching and pit.outs < CAREER_QUALIFY_OUTS // (3 if team else 1):
                continue
            if not pitching and bat.pa < CAREER_QUALIFY_PA // (3 if team else 1):
                continue
        if is_rate and postseason and ((pitching and pit.outs < 60) or (not pitching and bat.pa < 40)):
            continue
        value = _value(line, stat)
        if not is_rate and value <= 0:
            continue
        out.append((person, value))
    reverse = stat not in ASCENDING
    out.sort(key=lambda item: ((-item[1] if reverse else item[1]), item[0].id))
    return out[:limit]


@dataclass(slots=True)
class Record:
    label: str
    value: str
    holder: str
    year: int


SEASON_RECORD_STATS: tuple[tuple[str, str, bool, str], ...] = (
    # (stat, label, pitching, format)
    ("hr", "Home runs", False, "{:.0f}"),
    ("h", "Hits", False, "{:.0f}"),
    ("rbi", "RBI", False, "{:.0f}"),
    ("sb", "Stolen bases", False, "{:.0f}"),
    ("avg", "Batting average", False, "{:.3f}"),
    ("ops", "OPS", False, "{:.3f}"),
    ("w", "Wins", True, "{:.0f}"),
    ("so", "Strikeouts", True, "{:.0f}"),
    ("sv", "Saves", True, "{:.0f}"),
    ("era", "ERA", True, "{:.2f}"),
)


def season_records(league: League, team: str | None = None) -> list[Record]:
    """Best single seasons (franchise records when ``team`` is given)."""
    seasons: dict[tuple[int, int], tuple[HasHistory, BattingStats, PitchingStats]] = {}
    for person in all_people(league):
        per_year: dict[int, list[StatLine]] = defaultdict(list)
        for row in season_rows(person, league.year):
            if team is None or row.team == team:
                per_year[row.year].append(row)
        for year, rows in per_year.items():
            bat, pit = _sum(rows)
            seasons[(person.id, year)] = (person, bat, pit)
    records: list[Record] = []
    for stat, label, pitching, fmt in SEASON_RECORD_STATS:
        best: tuple[float, int, int, HasHistory] | None = None
        for (pid, year), (person, bat, pit) in seasons.items():
            if person.is_pitcher != pitching:
                continue
            line: BattingStats | PitchingStats = pit if pitching else bat
            if stat in ("avg", "ops") and bat.pa < QUALIFY_PA:
                continue
            if stat == "era" and pit.outs < QUALIFY_OUTS:
                continue
            value = _value(line, stat)
            if stat not in ASCENDING and value <= 0:
                continue
            key = -value if stat in ASCENDING else value
            if best is None or (key, -year, -pid) > (best[0], -best[1], -best[2]):
                best = (key, year, pid, person)
        if best is not None:
            value = -best[0] if stat in ASCENDING else best[0]
            text = fmt.format(value)
            records.append(Record(label, text[1:] if text.startswith("0.") else text, best[3].name, best[1]))
    return records


def retired_list(league: League) -> list[RetiredPlayer]:
    return sorted(league.retired.values(), key=lambda r: (-r.retired_year, r.last_name, r.id))


def career_summary(person: HasHistory, year: int | None = None) -> str:
    bat, pit = career_totals(person, year)
    if person.is_pitcher:
        return (f"{pit.w}-{pit.l}, {pit.era:.2f} ERA, {pit.ip_display} IP, {pit.so} SO, {pit.sv} SV"
                if pit.g else "no MLB appearances")
    if not bat.g:
        return "no MLB appearances"
    return (f"{bat.avg:.3f}/{bat.obp:.3f}/{bat.slg:.3f}, {bat.h} H, {bat.hr} HR, {bat.rbi} RBI"
            .replace("0.", "."))


# ----------------------------------------------------------------------------- postseason awards


def series_score(player: Player, code: str) -> float:
    """Simple, transparent series MVP score from one round's stats."""
    bat = player.post_batting.get(code)
    pit = player.post_pitching.get(code)
    score = 0.0
    if bat is not None and not player.is_pitcher:
        score += bat.total_bases + bat.bb + bat.hbp + bat.r + bat.rbi + bat.sb * 0.5 - bat.so * 0.2
    if pit is not None:
        score += pit.outs / 3 * 1.4 - pit.er * 2.2 + pit.so * 0.5 + pit.w * 2.5 + pit.sv * 2.0 - pit.bb * 0.4
    return score


def series_mvp(league: League, winner: str, code: str) -> Player | None:
    team = league.teams[winner]
    candidates = [league.players[pid] for pid in team.all_players
                  if code in league.players[pid].post_batting or code in league.players[pid].post_pitching]
    if not candidates:
        return None
    return max(candidates, key=lambda p: (series_score(p, code), -p.id))


def series_line(player: Player, code: str) -> str:
    if player.is_pitcher:
        s = player.post_pitching.get(code, PitchingStats())
        text = f"{s.w}-{s.l}, {s.era:.2f} ERA, {s.ip_display} IP, {s.so} K"
        return text + (f", {s.sv} SV" if s.sv else "")
    b = player.post_batting.get(code, BattingStats())
    return f"{b.h}-for-{b.ab}, {b.hr} HR, {b.rbi} RBI".replace("0.", ".")
