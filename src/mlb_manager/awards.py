"""End-of-regular-season awards, decided from simulated statistics.

Per league: MVP, Cy Young, Rookie of the Year, a Gold Glove at each fielding
position (plus pitcher) and a Silver Slugger at each lineup position.
Scores are simple, transparent value formulas (no voting simulation):

* Hitter value  = batting runs from OPS over PA + positional adjustment +
  a small defensive bonus at the position actually played.
* Pitcher value = runs saved versus a 4.40 ERA over the innings pitched, plus
  small strikeout / save credits.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from mlb_manager import ratings as rt
from mlb_manager.models import Player, Team

if TYPE_CHECKING:
    from mlb_manager.league import League

LEAGUE_ERA = 4.40
POSITION_ADJUSTMENT: dict[str, float] = {
    "C": 9.0, "SS": 7.0, "CF": 5.0, "2B": 4.0, "3B": 3.0,
    "RF": 1.0, "LF": 0.0, "1B": -4.0, "DH": -8.0,
}
GOLD_GLOVE_POSITIONS: tuple[str, ...] = (*rt.FIELD_POSITIONS, "P")
SILVER_SLUGGER_POSITIONS: tuple[str, ...] = rt.LINEUP_POSITIONS
AWARD_ORDER: tuple[str, ...] = ("MVP", "Cy Young", "Rookie of the Year")


def hitter_value(player: Player) -> float:
    b = player.batting
    if b.pa == 0:
        return 0.0
    batting_runs = (b.ops - 0.710) * b.pa * 0.28 + b.sb * 0.2 - b.cs * 0.4
    pos = player.main_position
    adjustment = POSITION_ADJUSTMENT.get(pos, 0.0) * b.pa / 650
    defense = 0.0
    if pos in rt.FIELD_POSITIONS:
        defense = (rt.defense_rating(player.ratings, pos, player.can_play(pos)) - 50) * 0.08 * b.pa / 650
    return batting_runs + adjustment + defense


def pitcher_value(player: Player) -> float:
    s = player.pitching
    if s.outs == 0:
        return 0.0
    runs_saved = (LEAGUE_ERA - s.era) * s.innings / 9
    return runs_saved + s.so * 0.02 + s.sv * 0.15 + s.w * 0.2


def player_value(player: Player) -> float:
    # Voters favour everyday players for MVP; pitchers need a dominant season.
    return pitcher_value(player) * 0.6 if player.is_pitcher else hitter_value(player)


def _stat_line(player: Player) -> str:
    if player.is_pitcher:
        s = player.pitching
        line = f"{s.w}-{s.l}, {s.era:.2f} ERA, {s.ip_display} IP, {s.so} K"
        return line + (f", {s.sv} SV" if s.sv else "")
    b = player.batting
    return (f"{b.avg:.3f}/{b.obp:.3f}/{b.slg:.3f}, {b.hr} HR, {b.rbi} RBI".replace("0.", ".")
            + (f", {b.sb} SB" if b.sb >= 15 else ""))


def _entry(award: str, player: Player) -> dict[str, Any]:
    return {
        "award": award,
        "player_id": player.id,
        "name": player.name,
        "team": player.team,
        "position": player.main_position if not player.is_pitcher else player.position,
        "line": _stat_line(player),
    }


def compute_awards(league: League) -> list[dict[str, Any]]:
    """Return award entries for the current (just finished) regular season."""
    games = max((r.games for r in league.records().values()), default=162) or 162
    min_pa = 3.1 * games
    min_outs = 3 * games
    teams: Mapping[str, Team] = league.teams
    awards: list[dict[str, Any]] = []
    for lg in ("AL", "NL"):
        members = [
            league.players[pid]
            for team in teams.values() if team.league == lg
            for pid in team.all_players
        ]
        hitters = [p for p in members if not p.is_pitcher and p.batting.pa >= min_pa]
        starters = [p for p in members if p.is_pitcher and p.pitching.outs >= min_outs]
        relievers = [p for p in members if p.is_pitcher and p.pitching.g >= games * 0.3]

        def best(pool: list[Player], score) -> Player | None:  # noqa: ANN001
            return max(pool, key=lambda p: (score(p), -p.id)) if pool else None

        mvp = best(hitters + starters, player_value)
        if mvp:
            awards.append(_entry(f"{lg} MVP", mvp))
        cy = best(starters + [r for r in relievers if r.pitching.sv >= 30], pitcher_value)
        if cy:
            awards.append(_entry(f"{lg} Cy Young", cy))
        rookies = [
            p for p in members if p.service_years == 0
            and (p.batting.pa >= 150 or p.pitching.outs >= 120)
        ]
        roy = best(rookies, player_value)
        if roy:
            awards.append(_entry(f"{lg} Rookie of the Year", roy))

        regulars = [p for p in members if not p.is_pitcher]
        for pos in GOLD_GLOVE_POSITIONS:
            if pos == "P":
                pool = starters
                winner = best(pool, lambda p: p.rating("fielding") + p.pitching.outs / 300)
            else:
                pool = [p for p in regulars if p.games_by_pos.get(pos, 0) >= games * 0.5]
                winner = best(pool, lambda p, pos=pos: rt.defense_rating(p.ratings, pos, p.can_play(pos))
                              + p.games_by_pos.get(pos, 0) / 100)
            if winner:
                awards.append(_entry(f"{lg} Gold Glove {pos}", winner) | {"position": pos})
        for pos in SILVER_SLUGGER_POSITIONS:
            pool = [p for p in regulars if p.main_position == pos and p.batting.pa >= games * 2.5]
            winner = best(pool, lambda p: p.batting.ops * 100 + p.batting.hr * 0.2)
            if winner:
                awards.append(_entry(f"{lg} Silver Slugger {pos}", winner) | {"position": pos})
    return awards


def headline_awards(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in entries if any(e["award"].endswith(a) for a in AWARD_ORDER)]
