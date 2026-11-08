"""Text rendering for the terminal UI.

Every function returns a string; nothing here reads input or mutates state.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING

from mlb_manager import ratings as rt
from mlb_manager import roster
from mlb_manager.franchises import DIVISIONS, FRANCHISES, LEAGUE_NAMES, LEAGUES
from mlb_manager.game import GameResult, TeamBox
from mlb_manager import careers, contracts, development, finance, freeagency, frontoffice, injuries, morale, trades
from mlb_manager.draft import DraftPick
from mlb_manager.league import (
    PHASE_COMPLETE,
    PHASE_DRAFT,
    PHASE_LABELS,
    PHASE_POSTSEASON,
    PHASE_REGULAR,
    DayReport,
    League,
)
from mlb_manager.models import BattingStats, PitchingStats, Player, RetiredPlayer, Team
from mlb_manager.playoffs import ROUND_NAMES
from mlb_manager.standings import (
    division_standings,
    games_back,
    wild_card_standings,
)

if TYPE_CHECKING:
    from mlb_manager.dugout import Decision, LiveGame


def rule(width: int = 72, char: str = "-") -> str:
    return char * width


def title(text: str, width: int = 72) -> str:
    return f"{rule(width, '=')}\n{text.center(width)}\n{rule(width, '=')}"


def table(headers: Sequence[str], rows: Iterable[Sequence[object]], align: str | None = None) -> str:
    """Render a simple fixed-width table. ``align`` holds 'l'/'r' per column."""
    rows = [[str(c) for c in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    align = align or "l" * len(headers)

    def fmt(cells: Sequence[str]) -> str:
        out = []
        for i, cell in enumerate(cells):
            out.append(cell.rjust(widths[i]) if align[i] == "r" else cell.ljust(widths[i]))
        return "  ".join(out).rstrip()

    lines = [fmt(list(headers)), fmt(["-" * w for w in widths])]
    lines.extend(fmt(row) for row in rows)
    return "\n".join(lines)


def pct(value: float) -> str:
    text = f"{value:.3f}"
    return text[1:] if text.startswith("0") else text


def gb(value: float) -> str:
    if value == 0:
        return "-"
    return f"{value:.1f}"


def phase_label(league: League) -> str:
    if league.phase == PHASE_REGULAR:
        return f"Regular season, day {league.day + 1}"
    return PHASE_LABELS.get(league.phase, league.phase)


def team_list() -> str:
    lines = []
    for lg in LEAGUES:
        lines.append(LEAGUE_NAMES[lg])
        for div in DIVISIONS:
            clubs = [f for f in FRANCHISES if f.league == lg and f.division == div]
            entries = ", ".join(f"{f.abbr} ({f.full_name})" for f in clubs)
            lines.append(f"  {div:<8} {entries}")
    return "\n".join(lines)


# ----------------------------------------------------------------------------- header


def status_line(league: League) -> str:
    team = league.user
    records = league.records()
    rec = records[team.abbr]
    order = division_standings(league.teams, records, team.league, team.division)
    place = order.index(team.abbr) + 1
    behind = games_back(records[order[0]], rec)
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(place, "th")
    standing = f"{place}{suffix} {team.division_key}" + (f", {gb(behind)} GB" if behind else "")
    return (
        f"{team.full_name} ({team.abbr})  |  {league.year}  |  {phase_label(league)}  |  "
        f"{rec.wins}-{rec.losses}, {standing}"
    )


# ----------------------------------------------------------------------------- standings


def standings(league: League, lg: str) -> str:
    records = league.records()
    out = [LEAGUE_NAMES[lg]]
    for div in DIVISIONS:
        order = division_standings(league.teams, records, lg, div)
        leader = records[order[0]]
        rows = []
        for abbr in order:
            r = records[abbr]
            marker = "*" if abbr == league.user_team else " "
            rows.append([
                f"{marker}{abbr}", r.wins, r.losses, pct(r.pct), gb(games_back(leader, r)),
                f"{r.home_wins}-{r.home_losses}", f"{r.away_wins}-{r.away_losses}",
                r.last10, r.streak, f"{r.run_diff:+d}",
            ])
        out.append("")
        out.append(table(
            [f"{lg} {div}", "W", "L", "PCT", "GB", "HOME", "AWAY", "L10", "STRK", "RD"],
            rows, "lrrrrrrrrr",
        ))
    out.append("")
    out.append(wild_card(league, lg))
    return "\n".join(out)


def wild_card(league: League, lg: str) -> str:
    records = league.records()
    order = wild_card_standings(league.teams, records, lg)
    cutoff = records[order[2]] if len(order) >= 3 else None
    rows = []
    for i, abbr in enumerate(order[:8], start=1):
        r = records[abbr]
        behind = games_back(cutoff, r) if cutoff and i > 3 else 0.0
        marker = "*" if abbr == league.user_team else " "
        rows.append([i, f"{marker}{abbr}", r.wins, r.losses, pct(r.pct), gb(behind) if i > 3 else "+"])
    return table([f"{lg} Wild Card", "Team", "W", "L", "PCT", "WCGB"], rows, "rlrrrr")


# ----------------------------------------------------------------------------- roster


def _hand(p: Player) -> str:
    return f"{p.bats}/{p.throws}"


def _positions(p: Player) -> str:
    return "/".join(p.eligible_positions)


def hitters_table(players: Sequence[Player], positions: dict[int, str] | None = None) -> str:
    rows = []
    for p in players:
        pos = (positions or {}).get(p.id, p.position)
        rows.append([
            p.id, display_name(p), pos, _positions(p), p.age, _hand(p), p.overall_at(pos) if pos != "-" else p.overall,
            p.rating("contact"), p.rating("power"), p.rating("eye"), p.rating("speed"),
            p.rating("fielding"), p.rating("arm"), p.rating("range"),
            p.ratings.get("framing", "-"),
            pct(p.batting.avg), p.batting.hr, f"{p.batting.ops:.3f}",
        ])
    return table(
        ["ID", "Name", "Pos", "Elig", "Age", "B/T", "OVR", "CON", "POW", "EYE", "SPD",
         "FLD", "ARM", "RNG", "FRM", "AVG", "HR", "OPS"],
        rows, "rllllrrrrrrrrrrrrr",
    )


def pitchers_table(players: Sequence[Player], roles: dict[int, str] | None = None) -> str:
    rows = []
    for p in players:
        s = p.pitching
        rows.append([
            p.id, display_name(p), (roles or {}).get(p.id, p.position), p.age, p.throws, p.overall,
            p.rating("stuff"), p.rating("control"), p.rating("movement"), p.rating("stamina"),
            p.fatigue, f"{s.w}-{s.l}", s.sv, f"{s.era:.2f}", s.ip_display, s.so,
        ])
    return table(
        ["ID", "Name", "Role", "Age", "T", "OVR", "STF", "CTL", "MOV", "STA", "FAT",
         "W-L", "SV", "ERA", "IP", "SO"],
        rows, "rlllrrrrrrrrrrrr",
    )


def staff_roles(team: Team) -> dict[int, str]:
    roles = {pid: f"SP{i}" for i, pid in enumerate(team.rotation, start=1)}
    if team.closer is not None:
        roles[team.closer] = "CL"
    return roles


def roster_view(league: League, team: Team) -> str:
    players = league.players
    hitters = sorted(roster.active_hitters(team, players), key=lambda p: rt.LINEUP_POSITIONS.index(p.position))
    pitchers = roster.active_pitchers(team, players)
    roles = staff_roles(team)
    rank_of = {pid: i for i, pid in enumerate(team.rotation)}
    pitchers.sort(key=lambda p: (rank_of.get(p.id, 10 if p.id == team.closer else 11), p.id))
    reserves = roster.reserve_players(team, players)
    out = [
        f"{team.full_name} - active roster ({len(team.active)}/{roster.MAX_ACTIVE})",
        "",
        "Position players",
        hitters_table(hitters),
        "",
        "Pitchers",
        pitchers_table(pitchers, roles),
        "",
        f"Reserves ({len(reserves)})",
        hitters_table([p for p in reserves if not p.is_pitcher]),
        "",
        pitchers_table([p for p in reserves if p.is_pitcher]),
    ]
    return "\n".join(out)


def lineup_view(league: League, team: Team) -> str:
    players = league.players
    rows = []
    for i, slot in enumerate(team.lineup, start=1):
        p = players[slot.player_id]
        flag = "" if p.can_play(slot.position) else " (!)"
        defense = (
            "-" if slot.position == "DH"
            else round(rt.defense_rating(p.ratings, slot.position, p.can_play(slot.position)))
        )
        rows.append([
            i, slot.position + flag, p.id, p.name, _positions(p), p.bats,
            p.rating("contact"), p.rating("power"), p.rating("eye"), p.rating("speed"), defense,
            pct(p.batting.avg), p.batting.hr, f"{p.batting.ops:.3f}",
        ])
    out = [
        f"{team.full_name} - starting lineup",
        table(["#", "Pos", "ID", "Name", "Elig", "B", "CON", "POW", "EYE", "SPD", "DEF",
               "AVG", "HR", "OPS"], rows, "rlrllcrrrrrrrr"),
        "(!) = playing out of position (defensive penalty);  [INJ] = injured: a healthy "
        "bench player starts in his place until he returns",
        "",
        "Bench",
        hitters_table(roster.bench(team, players), {p.id: p.position for p in roster.bench(team, players)}),
    ]
    problems = roster.validate(team, players)
    if problems:
        out.append("")
        out.append("WARNINGS:")
        out.extend(f"  - {p}" for p in problems)
    return "\n".join(out)


def staff_view(league: League, team: Team) -> str:
    players = league.players
    rotation = [players[pid] for pid in team.rotation]
    nxt = team.rotation_index % max(1, len(team.rotation))
    roles = {pid: f"SP{i + 1}{' >' if i == nxt else ''}" for i, pid in enumerate(team.rotation)}
    pen = roster.bullpen(team, players)
    pen.sort(key=lambda p: (p.id != team.closer, -rt.pitching_rating(p.ratings, "RP")))
    pen_roles = {p.id: ("CL" if p.id == team.closer else "RP") for p in pen}
    return "\n".join([
        f"{team.full_name} - pitching staff   ('>' = next scheduled starter, FAT = fatigue)",
        "",
        "Rotation",
        pitchers_table(rotation, roles),
        "",
        "Bullpen",
        pitchers_table(pen, pen_roles),
    ])


def player_card(league: League, p: Player) -> str:
    team = league.teams.get(p.team or "")
    status = "-"
    if team:
        status = "Active" if p.id in team.active else "Reserve"
    lines = [
        f"#{p.id} {p.name}  ({p.position}{', also ' + '/'.join(p.secondary_positions) if p.secondary_positions else ''})",
        f"Team: {team.full_name if team else 'Free agent'} [{status}]   Age: {p.age}   "
        f"Bats/Throws: {p.bats}/{p.throws}   Overall: {p.overall}",
        f"Potential (scouted): {pot_text(league, p)}"
        + (f"   Development: {p.development}" if p.team == league.user_team else ""),
        contract_line(p),
    ]
    if p.draft_year is not None:
        lines.append(f"Drafted: {p.draft_year}, round {p.draft_round}, pick #{p.draft_pick}")
    lines += ["", "Ratings"]
    groups = [rt.PITCHING_ATTRS] if p.is_pitcher else []
    groups += [rt.HITTING_ATTRS, rt.FIELDING_ATTRS]
    if "framing" in p.ratings:
        groups.append(rt.CATCHER_ATTRS)
    for group in groups:
        for attr in group:
            value = p.rating(attr)
            bar = "#" * (value // 5)
            lines.append(f"  {rt.ATTRIBUTE_LABELS[attr]:<9} {value:>3}  {bar}")
    if not p.is_pitcher:
        lines.append("")
        lines.append("Defensive value by position (1-99):")
        lines.append("  " + "  ".join(
            f"{pos}:{round(rt.defense_rating(p.ratings, pos, p.can_play(pos)))}"
            for pos in rt.FIELD_POSITIONS
        ))
    b, s = p.batting, p.pitching
    lines.append("")
    lines.append(f"{league.year} regular season")
    if b.pa or s.g:
        lines.append(stat_table(p.is_pitcher, [("", b, s)]))
    else:
        lines.append("  No games played yet.")
    post_b, post_s = careers.current_postseason(p)
    if not (post_b.is_empty and post_s.is_empty):
        lines += ["", f"{league.year} postseason"]
        lines.append(stat_table(p.is_pitcher, [("", post_b, post_s)]))
    lines += ["", career_section(league, p)]
    if p.is_pitcher:
        lines.append(f"Current fatigue: {p.fatigue}")
    lines += ["", health_section(league, p)]
    if p.team:
        lines.append(morale_section(league, p, detailed=p.team == league.user_team))
    lines.append(f"MLB service: {p.service_years} season(s)" + (" (rookie)" if p.service_years == 0 else ""))
    return "\n".join(lines)


BATTING_HEADERS = ["G", "AB", "R", "H", "2B", "3B", "HR", "RBI", "BB", "SO", "SB", "AVG", "OBP", "SLG", "OPS"]
PITCHING_HEADERS = ["G", "GS", "W", "L", "SV", "IP", "H", "ER", "BB", "SO", "HR", "ERA", "WHIP"]


def batting_cells(b: BattingStats) -> list[object]:
    return [b.g, b.ab, b.r, b.h, b.doubles, b.triples, b.hr, b.rbi, b.bb, b.so, b.sb,
            pct(b.avg), pct(b.obp), pct(b.slg), pct(b.ops)]


def pitching_cells(s: PitchingStats) -> list[object]:
    return [s.g, s.gs, s.w, s.l, s.sv, s.ip_display, s.h, s.er, s.bb, s.so, s.hr,
            f"{s.era:.2f}", f"{s.whip:.2f}"]


def stat_table(pitcher: bool, rows: Sequence[tuple[str, BattingStats, PitchingStats]],
               label: str = "") -> str:
    """Batting or pitching table; the first column is a free-form label (year/team)."""
    headers = PITCHING_HEADERS if pitcher else BATTING_HEADERS
    body = [[name, *(pitching_cells(s) if pitcher else batting_cells(b))] for name, b, s in rows]
    if not label and all(not name for name, _, _ in rows):
        return table(headers, [r[1:] for r in body], "r" * len(headers))
    return table([label, *headers], body, "l" + "r" * len(headers))


def career_section(league: League, p: Player | RetiredPlayer) -> str:
    """Year-by-year regular-season career, plus postseason and World Series totals."""
    rows = careers.season_rows(p, league.year)
    lines = ["Career (regular season)"]
    if not rows:
        lines.append("  No MLB statistics yet (careers are tracked from the league's first season).")
        return "\n".join(lines)
    body = [(f"{r.year} {r.team}", r.batting, r.pitching) for r in rows]
    bat, pit = careers.career_totals(p, league.year)
    body.append(("Total", bat, pit))
    lines.append(stat_table(p.is_pitcher, body, "Season"))
    post_rows = careers.postseason_rows(p, league.year)
    if post_rows:
        pb, ps = careers.postseason_totals(p, league.year)
        wb, ws = careers.world_series_totals(p, league.year)
        lines += ["", "Postseason (never combined with the regular season)"]
        body = [(f"{r.year} {r.team}", r.batting, r.pitching) for r in post_rows]
        body.append(("Total", pb, ps))
        if not (wb.is_empty and ws.is_empty):
            body.append(("World Series", wb, ws))
        lines.append(stat_table(p.is_pitcher, body, "Postseason"))
    honours = [f"{e['year'] if 'year' in e else ''} {e['award']}".strip() for e in player_awards(league, p.id)]
    if honours:
        lines += ["", "Awards: " + ", ".join(honours)]
    return "\n".join(lines)


def player_awards(league: League, player_id: int) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for item in league.awards_history:
        for entry in item["entries"]:
            if entry["player_id"] == player_id:
                out.append({"year": item["year"], "award": entry["award"]})
    for entry in league.postseason_awards:
        if entry["player_id"] == player_id:
            out.append({"year": entry["year"], "award": entry["award"]})
    return sorted(out, key=lambda e: (e["year"], str(e["award"])))


def health_section(league: League, p: Player) -> str:
    trait = injuries.durability(p, league.seed)
    lines = [f"Health: {'injured' if p.is_injured else 'healthy'}   Durability: {trait}"]
    if p.is_injured:
        where = " (rehab assignment)" if p.rehab else ""
        lines.append(f"  INJURED: {p.injury}, about {p.injury_days} day(s) remaining{where}")
    if p.rust_days:
        lines.append(f"  Rusty after injury: reduced form for ~{p.rust_days} more day(s)")
    if p.injury_history:
        lines.append("  Injury history: " + "; ".join(
            f"{h['y']} {h['i']} ({h['n']}d)" for h in reversed(p.injury_history[-6:])))
    return "\n".join(lines)


def morale_section(league: League, p: Player, detailed: bool = True) -> str:
    text = f"Morale: {p.morale} ({morale.label(p.morale)})"
    mod = morale.performance_modifier(p)
    if mod:
        text += f"   in-game effect {mod:+d}"
    if not detailed:
        return text
    reasons = morale.factors(league, p)
    if reasons:
        text += "\n  " + ", ".join(f"{r} {d:+d}" for r, d in reasons)
    return text


# ----------------------------------------------------------------------------- games


def line_score(league: League, result: GameResult) -> str:
    innings = max(len(result.away_box.line), len(result.home_box.line))
    headers = [""] + [str(i) for i in range(1, innings + 1)] + ["R", "H", "E"]
    rows = []
    for box in (result.away_box, result.home_box):
        cells = [str(x) for x in box.line] + ["x"] * (innings - len(box.line))
        rows.append([box.abbr, *cells, box.runs, box.hits, box.errors])
    return table(headers, rows, "l" + "r" * (len(headers) - 1))


def box_score(league: League, result: GameResult) -> str:
    players = league.players

    def name(pid: int) -> str:
        return players[pid].short_name if pid in players else f"#{pid}"

    out = [
        f"{league.teams[result.away].full_name} {result.away_runs} @ "
        f"{league.teams[result.home].full_name} {result.home_runs}"
        + (f"  (F/{result.innings})" if result.innings != 9 else ""),
        "",
        line_score(league, result),
    ]
    for box in (result.away_box, result.home_box):
        out.append("")
        out.append(_batting_box(box, name))
        out.append("")
        out.append(_pitching_box(box, name))
    decisions = [f"W: {name(result.winning_pitcher)}", f"L: {name(result.losing_pitcher)}"]
    if result.save_pitcher:
        decisions.append(f"S: {name(result.save_pitcher)}")
    out.append("")
    out.append("   ".join(decisions))
    if result.log:
        out.append("")
        out.append("Game log")
        out.extend(f"  {line}" for line in result.log)
    return "\n".join(out)


def box_order(box: TeamBox) -> list[tuple[int, str, bool]]:
    """Batting order for display: each substitute right after the player he replaced."""
    entries = list(box.batting_order)
    out: list[tuple[int, str, bool]] = []

    def add(pid: int, pos: str, sub: bool) -> None:
        out.append((pid, pos, sub))
        for other, opos in entries:
            if box.subs.get(other) == pid:
                add(other, opos, True)

    for pid, pos in entries:
        if pid not in box.subs:
            add(pid, pos, False)
    return out


def _batting_box(box: TeamBox, name) -> str:  # noqa: ANN001
    rows = []
    for pid, pos, sub in box_order(box):
        s = box.batting[pid]
        rows.append([f"{'  ' if sub else ''}{name(pid)} {pos}", s.ab, s.r, s.h, s.rbi, s.bb, s.so, s.hr])
    return table([f"{box.abbr} batting", "AB", "R", "H", "RBI", "BB", "SO", "HR"], rows, "lrrrrrrr")


def _pitching_box(box: TeamBox, name) -> str:  # noqa: ANN001
    rows = []
    for pid, s in box.pitching.items():
        rows.append([name(pid), s.ip_display, s.h, s.r, s.er, s.bb, s.so, s.hr, s.pitches])
    return table([f"{box.abbr} pitching", "IP", "H", "R", "ER", "BB", "SO", "HR", "NP"], rows, "lrrrrrrrr")


def result_line(league: League, result: GameResult, perspective: str | None = None) -> str:
    text = f"{result.away} {result.away_runs} @ {result.home} {result.home_runs}"
    if result.innings != 9:
        text += f" ({result.innings})"
    if perspective in (result.away, result.home):
        text = ("W  " if result.winner == perspective else "L  ") + text
    return text


def day_summary(league: League, reports: Sequence[DayReport]) -> str:
    user = league.user_team
    lines: list[str] = []
    played = 0
    wins = 0
    for report in reports:
        for g in report.games:
            r = g.result
            if user in (r.away, r.home):
                played += 1
                wins += r.winner == user
                prefix = "" if g.label == "Regular season" else f"[{g.label}] "
                lines.append(f"  {prefix}{result_line(league, r, user)}")
    header = f"Simulated {len(reports)} day(s)."
    if played:
        header += f" {user} went {wins}-{played - wins}."
    else:
        header += f" {user} did not play."
    news = [n for report in reports for n in report.news]
    out = [header]
    if len(lines) > 15:
        out.append(f"  ... {len(lines) - 15} earlier games omitted ...")
        lines = lines[-15:]
    out.extend(lines)
    if news:
        out.append("")
        out.append("News:")
        out.extend(f"  * {n}" for n in news)
    return "\n".join(out)


def schedule_view(league: League, abbr: str, recent: int = 10, upcoming: int = 10) -> str:
    games = league.team_games(abbr)
    played = [g for g in games if g.played][-recent:]
    future = [g for g in games if not g.played][:upcoming]
    out = [f"{league.teams[abbr].full_name} - schedule", "", "Recent results"]
    if played:
        rows = []
        for g in played:
            home = g.home == abbr
            opp = g.away if home else g.home
            us, them = (g.home_runs, g.away_runs) if home else (g.away_runs, g.home_runs)
            rows.append([g.day + 1, ("vs " if home else "@ ") + opp, "W" if g.winner == abbr else "L",
                         f"{us}-{them}" + (f" ({g.innings})" if g.innings != 9 else "")])
        out.append(table(["Day", "Opponent", "", "Score"], rows, "rlll"))
    else:
        out.append("  None yet.")
    out.append("")
    out.append("Upcoming")
    if future:
        out.append(table(["Day", "Opponent"], [
            [g.day + 1, ("vs " if g.home == abbr else "@ ") + (g.away if g.home == abbr else g.home)]
            for g in future
        ], "rl"))
    else:
        out.append("  Regular season schedule complete.")
    return "\n".join(out)


def leaders_view(league: League, limit: int = 5) -> str:
    out = [f"{league.year} league leaders"]
    batting = [("AVG", "avg", pct), ("HR", "hr", lambda v: str(int(v))),
               ("RBI", "rbi", lambda v: str(int(v))), ("OPS", "ops", lambda v: f"{v:.3f}"),
               ("SB", "sb", lambda v: str(int(v)))]
    pitching = [("ERA", "era", lambda v: f"{v:.2f}"), ("W", "w", lambda v: str(int(v))),
                ("SO", "so", lambda v: str(int(v))), ("SV", "sv", lambda v: str(int(v))),
                ("WHIP", "whip", lambda v: f"{v:.2f}")]
    for label, stat, fmt in batting:
        rows = [[i, p.name, p.team, fmt(v)] for i, (p, v) in enumerate(league.batting_leaders(stat, limit), 1)]
        out.append("")
        out.append(table(["#", label, "Team", ""], rows, "rllr") if rows else f"{label}: no qualifiers yet")
    for label, stat, fmt in pitching:
        rows = [[i, p.name, p.team, fmt(v)] for i, (p, v) in enumerate(league.pitching_leaders(stat, limit), 1)]
        out.append("")
        out.append(table(["#", label, "Team", ""], rows, "rllr") if rows else f"{label}: no qualifiers yet")
    return "\n".join(out)


def bracket_view(league: League) -> str:
    p = league.playoffs
    if p is None:
        return "The postseason has not started. Top 6 per league qualify (3 division winners + 3 wild cards)."
    out = ["Postseason bracket"]
    for lg in sorted(p.seeds):
        out.append(f"  {lg} seeds: " + ", ".join(f"{i}. {a}" for i, a in enumerate(p.seeds[lg], 1)))
    for index, rnd in enumerate(p.rounds):
        out.append("")
        out.append(ROUND_NAMES[index])
        for s in rnd:
            out.append(f"  {s.league:<3} {s.summary()}  (best of {s.best_of})")
    if p.champion:
        out.append("")
        out.append(f"CHAMPION: {league.teams[p.champion].full_name}")
    return "\n".join(out)


def dashboard(league: League) -> str:
    team = league.user
    out = [status_line(league), ""]
    if league.last_user_game:
        out.append("Last game: " + result_line(league, league.last_user_game, team.abbr))
    nxt = next((g for g in league.team_games(team.abbr) if not g.played), None)
    if league.phase == PHASE_POSTSEASON:
        series = league.user_series()
        if series and not series.is_over:
            out.append(f"Postseason: {series.round_name} - {series.summary()}")
        else:
            out.append(f"Postseason: {league.postseason_finish(team.abbr)}")
    elif nxt:
        where = f"vs {nxt.away}" if nxt.home == team.abbr else f"@ {nxt.home}"
        out.append(f"Next game: day {nxt.day + 1} {where}")
    if league.phase in (PHASE_COMPLETE, *OFFSEASON) and league.history:
        h = league.history[-1]
        out.append(f"{h['year']} champion: {h['champion']}. {team.abbr}: {h['user_result']}.")
    if league.in_offseason:
        out.append(f"Offseason phase: {PHASE_LABELS[league.phase]}  (continue via SIMULATE)")
    hurt = league.injured_players(team)
    if hurt:
        out.append(f"Injuries: {len(hurt)} ({len(team.injured_list)} on the IL) - see Injuries (15)")
    out.append(f"{payroll_status(league, team)}   Strategy: {team.strategy}   "
               f"Trade offers: {len(league.trade_offers)}")
    out.append("")
    out.append(lineup_compact(league, team))
    out.append("")
    out.append("Recent news:")
    out.extend(f"  * {n}" for n in league.news[-6:])
    return "\n".join(out)


def lineup_compact(league: League, team: Team) -> str:
    parts = []
    for i, slot in enumerate(team.lineup, start=1):
        parts.append(f"{i}. {league.players[slot.player_id].short_name} {slot.position}")
    starter = league.players[team.rotation[team.rotation_index % len(team.rotation)]]
    return "Lineup: " + ", ".join(parts) + f"\nNext starter: {starter.name}"


def history_view(league: League) -> str:
    if not league.history:
        return "No completed seasons yet."
    rows = [[h["year"], h["champion"], h["runner_up"], f"{h['user_wins']}-{h['user_losses']}", h["user_result"]]
            for h in league.history]
    return table(["Year", "Champion", "Runner-up", league.user_team, "Result"], rows, "rllrl")


# ----------------------------------------------------------------------------- front office

OFFSEASON = ("resign", "draft", "free_agency")


def pot_text(league: League, p: Player) -> str:
    pot = development.scouted_potential(p, league.user_team, league.seed, league.year)
    return f"{pot} ({development.potential_grade(pot)})"


def contract_line(p: Player) -> str:
    if p.team is None:
        return "Contract: none (free agent)"
    years = "pending free agent" if p.contract_years <= 0 else f"{p.contract_years} yr left"
    return f"Contract: {contracts.money(p.salary)}/yr, {years} [{contracts.contract_status(p)}]"


def org_table(league: League, members: Sequence[Player], show_role: Team | None = None) -> str:
    rows = []
    for p in members:
        where = ""
        if show_role is not None:
            where = "MLB" if p.id in show_role.active else "MiLB"
        rows.append([
            p.id, p.name, p.position, p.age, p.overall,
            development.scouted_potential(p, league.user_team, league.seed, league.year),
            contracts.money(p.salary), p.contract_years, contracts.contract_status(p), where,
        ])
    return table(["ID", "Name", "Pos", "Age", "OVR", "POT", "Salary", "Yrs", "Status", "Lvl"],
                 rows, "rllrrrrrll")


def cap_text(league: League, team: Team) -> str:
    """``No salary cap`` or ``Hard cap $200M (room $12.5M)`` / ``... OVER by ...``."""
    r = finance.rules(league)
    room = finance.cap_room(league, team)
    if room is None:
        return "No salary cap"
    kind = "Hard cap" if r.hard_cap else "Soft cap"
    state = f"room {fmt(room)}" if room >= 0 else f"OVER by {fmt(-room)}"
    return f"{kind} {fmt(r.cap_amount)} ({state})"


def fmt(value: float | None) -> str:
    return finance.format_amount(value)


def payroll_status(league: League, team: Team) -> str:
    """One line: payroll, cap, floor and CBT position for ``team``."""
    r = finance.rules(league)
    parts = [f"Payroll {fmt(finance.payroll(league, team))}", cap_text(league, team)]
    if r.has_floor:
        short = finance.below_floor(league, team)
        parts.append(f"Floor {fmt(r.floor_amount)}" + (f" (short {fmt(short)})" if short else ""))
    if r.cbt_enabled:
        bill = finance.projected_tax(league, team)
        parts.append(f"CBT payroll {fmt(bill.cbt_payroll)} / {fmt(r.cbt_threshold)}"
                     + (f" (proj. tax {fmt(bill.total)})" if bill.total else ""))
    else:
        parts.append("No CBT")
    return "  |  ".join(parts)


def front_office_overview(league: League) -> str:
    team = league.user
    players = league.players
    open_, reason = trades.trade_window(league)
    picks = [p for p in league.draft_picks if p.owner == team.abbr]
    lines = [
        f"FRONT OFFICE - {team.full_name}",
        f"Phase: {phase_label(league)}   Club strategy (league view): {team.strategy}",
        payroll_status(league, team),
        f"Owners' payroll budget (payroll + taxes): {fmt(finance.payroll_budget(league, team))}   "
        f"Rules: {finance.rules(league).source} ({finance.rules(league).agreement})",
        f"Organisation: {len(team.all_players)}/{frontoffice.MAX_ORG} players "
        f"({len(team.active)} active, {len(team.reserves)} minor league)",
        f"Trading: {reason}" + ("" if open_ else " (closed)"),
        f"Pending trade offers: {len(league.trade_offers)}   Draft picks owned: {len(picks)}",
    ]
    return "\n".join(lines)


def payroll_view(league: League, team: Team) -> str:
    players = league.players
    members = sorted(frontoffice.org(league, team), key=lambda p: (-p.salary, p.id))
    commits = contracts.commitments(team, players)
    years = [str(league.year + i + (0 if league.in_season or league.is_complete else 1)) for i in range(4)]
    lines = [
        f"{team.full_name} - contracts & payroll",
        payroll_status(league, team),
        f"Dead money {fmt(team.dead_money)}  |  Contract AAV total {fmt(finance.aav_payroll(league, team))}  |  "
        f"Owners' budget {fmt(finance.payroll_budget(league, team))}",
        "",
        "Future commitments (cash)",
        table(years, [[contracts.money(c) for c in commits]], "rrrr"),
        "",
        org_table(league, members, team),
    ]
    expiring = [p for p in members if p.contract_years <= 1]
    if expiring:
        lines += ["", "Approaching free agency: " + ", ".join(
            f"{p.name} ({contracts.contract_status(p)})" for p in expiring[:12]
        )]
    return "\n".join(lines)


def picks_view(league: League, owner: str) -> str:
    picks = sorted((p for p in league.draft_picks if p.owner == owner), key=lambda p: (p.year, p.round, p.original))
    if not picks:
        return f"{owner} owns no draft picks."
    return table(["Pick code", "Year", "Rd", "Original club"],
                 [[p.pick_id, p.year, p.round, p.original] for p in picks], "lrrl")


def trade_partner_view(league: League, abbr: str) -> str:
    team = league.teams[abbr]
    members = sorted(trades.tradable_players(league, team), key=lambda p: (-p.overall, p.id))
    return "\n".join([
        f"{team.full_name} ({abbr}) - strategy: {team.strategy}, payroll "
        f"{contracts.money(contracts.payroll(team, league.players))}",
        org_table(league, members, team),
        "",
        "Draft picks",
        picks_view(league, abbr),
    ])


def proposal_view(league: League, prop: trades.TradeProposal) -> str:
    lines = []
    for abbr in (prop.team_a, prop.team_b):
        pids, picks = prop.assets_of(abbr)
        lines.append(f"{abbr} gives:")
        for pid in pids:
            p = league.players[pid]
            lines.append(f"   {p.name} ({p.position}, age {p.age}, {p.overall} OVR, POT "
                         f"{pot_text(league, p)}, {contracts.money(p.salary)} x {p.contract_years})")
        for pick_id in picks:
            pick = trades.get_pick(league, pick_id)
            lines.append(f"   {pick.label() if pick else pick_id} pick ({pick_id})")
        if not pids and not picks:
            lines.append("   nothing")
    return "\n".join(lines)


def contract_impact_view(league: League, prop: trades.TradeProposal) -> str:
    """Payroll consequences for both clubs (public information)."""
    rows = []
    notes: list[str] = []
    impacts = trades.contract_impact(league, prop)
    for impact in impacts:
        future = " / ".join(f"{v:+.2f}" for v in impact.future_change)
        room = "no cap" if impact.cap_space_after is None else fmt(impact.cap_space_after)
        tax = (f"{fmt(impact.tax_before)} -> {fmt(impact.tax_after)}" if finance.rules(league).cbt_enabled
               else "no CBT")
        rows.append([impact.team, contracts.money(impact.payroll_before), contracts.money(impact.payroll_after),
                     f"{impact.change:+.2f}", room, tax, future, f"{impact.years_in} in / {impact.years_out} out"])
        notes.extend(f"  {impact.team}: {m}" for m in impact.messages)
    text = "Contract implications\n" + table(
        ["Club", "Payroll now", "After", "Change", "Cap room after", "Proj. CBT tax", "Next 3 yrs ($M)",
         "Contract yrs"], rows, "lrrrrrrl")
    return text + ("\n" + "\n".join(notes) if notes else "")


def offers_view(league: League) -> str:
    if not league.trade_offers:
        return "No pending trade offers."
    out = []
    for offer in league.trade_offers:
        reason = f" - {offer.reason}" if offer.reason else ""
        out.append(f"Offer #{offer.offer_id} from {offer.proposal.team_a} (expires day {offer.expires_day + 1}){reason}")
        out.append(proposal_view(league, offer.proposal))
        out.append("")
    return "\n".join(out).rstrip()


def free_agents_view(league: League, position: str | None = None, limit: int = 25) -> str:
    pool = freeagency.free_agents(league)
    if position:
        pool = [p for p in pool if p.position == position]
    rows = []
    for p in pool[:limit]:
        salary, years = freeagency.asking(league, p)
        rows.append([p.id, p.name, p.position, p.age, p.overall,
                     development.scouted_potential(p, league.user_team, league.seed, league.year),
                     contracts.money(salary), years])
    header = (f"Free agents ({len(pool)} available{' at ' + position if position else ''})  |  "
              f"{cap_text(league, league.user)}  |  Org "
              f"{len(league.user.all_players)}/{frontoffice.MAX_ORG}")
    if not rows:
        return header + "\nNo free agents match."
    return header + "\n" + table(["ID", "Name", "Pos", "Age", "OVR", "POT", "Asking", "Yrs"], rows, "rllrrrrr")


def draft_board(league: League, limit: int = 20) -> str:
    draft = league.draft
    if draft is None or not draft.pool:
        return "No prospects available."
    prospects = [league.players[pid] for pid in draft.pool]
    def scouted(p: Player) -> int:
        return development.scouted_potential(p, league.user_team, league.seed, league.year)
    prospects.sort(key=lambda p: (-(scouted(p) + p.overall * 0.3), p.id))
    rows = [[i, p.id, p.name, p.position, p.age, p.overall, scouted(p), development.potential_grade(scouted(p))]
            for i, p in enumerate(prospects[:limit], 1)]
    return table(["Rank", "ID", "Name", "Pos", "Age", "OVR", "POT", "Grade"], rows, "rrllrrrl")


def draft_room_view(league: League) -> str:
    draft = league.draft
    if draft is None or league.phase != PHASE_DRAFT:
        mine = sorted(
            (p for p in league.draft_picks if p.owner == league.user_team), key=lambda p: (p.year, p.round)
        )
        lines = ["The amateur draft is held during the offseason (after the re-signing period).",
                 "Draft order: worst regular-season record first; playoff clubs pick last.", "",
                 "Your picks:"]
        lines += [f"  {p.pick_id}  {p.label()}" for p in mine] or ["  none"]
        if draft is not None and draft.selections:
            lines += ["", f"Your {draft.year} draft class:"]
            for sel in draft.selections:
                if sel.team == league.user_team and sel.player_id in league.players:
                    p = league.players[sel.player_id]
                    lines.append(f"  #{sel.overall_pick} R{sel.round} {p.name} {p.position} ({p.overall} OVR)")
        return "\n".join(lines)
    clock = league.draft_on_clock()
    lines = [f"{draft.year} AMATEUR DRAFT - {len(draft.selections)}/{draft.total_picks} picks made"]
    if clock:
        number, rnd, owner = clock
        flag = "  <== YOU ARE ON THE CLOCK" if owner == league.user_team else ""
        lines.append(f"On the clock: pick #{number} (round {rnd}) - {owner}{flag}")
    upcoming = []
    for index in range(len(draft.selections), draft.total_picks):
        rnd, original = draft.slot(index)
        if league.draft_owner(rnd, original) == league.user_team:
            upcoming.append(f"#{index + 1} (R{rnd})")
    lines.append("Your remaining picks: " + (", ".join(upcoming) or "none"))
    recent = draft.selections[-5:]
    if recent:
        lines.append("")
        lines.append("Recent picks:")
        for sel in recent:
            p = league.players.get(sel.player_id)
            if p:
                lines.append(f"  #{sel.overall_pick} {sel.team}: {p.name}, {p.position}, age {p.age}")
    lines += ["", "Your scouts' board (POT is a scouting estimate):", draft_board(league)]
    return "\n".join(lines)


def prospects_view(league: League, team: Team) -> str:
    players = league.players
    minors = sorted(roster.reserve_players(team, players), key=lambda p: (-p.overall, p.id))
    young = sorted((p for p in roster.active_players(team, players) if p.age <= 24), key=lambda p: (-p.overall, p.id))
    rows = []
    for p in minors:
        pot = development.scouted_potential(p, league.user_team, league.seed, league.year)
        rows.append([p.id, p.name, p.position, p.age, p.overall, pot, development.potential_grade(pot),
                     p.development, contracts.money(p.salary), p.contract_years])
    lines = [
        f"{team.full_name} - minor league system ({len(minors)} players; organisation "
        f"{len(team.all_players)}/{frontoffice.MAX_ORG}; active {len(team.active)}/{roster.MAX_ACTIVE})",
        table(["ID", "Name", "Pos", "Age", "OVR", "POT", "Grd", "Dev", "Salary", "Yrs"], rows, "rllrrrllrr")
        if rows else "No minor league players.",
    ]
    if young:
        lines += ["", "Young players on the active roster: " + ", ".join(
            f"{p.name} ({p.position}, {p.age}, {p.overall}/{pot_text(league, p)})" for p in young
        )]
    if len(team.all_players) > frontoffice.MAX_ORG:
        lines += ["", f"WARNING: organisation is over the {frontoffice.MAX_ORG}-player limit. "
                      "The lowest-value players will be released before next season unless you act."]
    return "\n".join(lines)


def transactions_view(league: League, limit: int = 25) -> str:
    if not league.transactions:
        return "No transactions yet."
    return "Recent league transactions\n" + "\n".join(f"  {t}" for t in league.transactions[-limit:])


def pick_label(pick: DraftPick) -> str:
    return f"{pick.pick_id} ({pick.label()})"


# ----------------------------------------------------------------------------- season experience


def display_name(p: Player) -> str:
    return f"{p.name} [INJ]" if p.is_injured else p.name


def injury_report(league: League, team: Team) -> str:
    hurt = league.injured_players(team)
    lines = [f"{team.full_name} - injury report   (IL players do not count against the 26-man roster)"]
    if not hurt:
        lines.append("No injuries. Everyone is healthy.")
        return "\n".join(lines)
    rows = []
    for p in hurt:
        if p.id in team.injured_list:
            if not p.is_injured:
                status = "IL - healthy, ready to activate"
            elif injuries.can_rehab(team, p):
                status = "IL - can start rehab"
            else:
                status = "IL"
        elif p.rehab:
            status = "Rehab assignment"
        elif p.id in team.active:
            status = "Active (sits until healthy)" if p.is_injured else f"Active - rusty ({p.rust_days}d)"
        else:
            status = "Minors"
        rows.append([p.id, p.name, p.position, p.overall, p.injury or "-", p.injury_days or "-",
                     injuries.durability(p, league.seed), status])
    lines.append(table(["ID", "Name", "Pos", "OVR", "Injury", "Days", "Durability", "Status"], rows, "rllrlrll"))
    lines.append(f"Active roster {len(team.active)}/{roster.MAX_ACTIVE}; injured list {len(team.injured_list)}. "
                 f"Rehab assignments start {injuries.REHAB_WINDOW_DAYS} days before a return and avoid "
                 "post-injury rust.")
    return "\n".join(lines)


def milestones_view(league: League, limit: int = 40) -> str:
    if not league.milestones:
        return f"No {league.year} milestones yet."
    return f"{league.year} season milestones\n" + "\n".join(f"  * {m}" for m in league.milestones[-limit:])


def awards_view(league: League, year: int | None = None) -> str:
    from mlb_manager import awards as awards_mod

    entries = league.season_awards(year)
    year = league.year if year is None else year
    if not entries:
        return f"No awards for {year} yet (awards are announced at the end of the regular season)."
    lines = [f"{year} MLB awards"]
    for lg in ("AL", "NL"):
        lines.append("")
        lines.append(LEAGUE_NAMES[lg])
        mine = [e for e in entries if e["award"].startswith(lg)]
        rows = []
        for e in awards_mod.headline_awards(mine):
            rows.append([e["award"][3:], e["name"], e["team"], e["line"]])
        lines.append(table(["Award", "Player", "Team", "Season"], rows, "llll"))
        gg = [e for e in mine if "Gold Glove" in e["award"]]
        ss = [e for e in mine if "Silver Slugger" in e["award"]]
        lines.append("Gold Gloves:     " + ", ".join(f"{e['position']} {e['name']} ({e['team']})" for e in gg))
        lines.append("Silver Sluggers: " + ", ".join(f"{e['position']} {e['name']} ({e['team']})" for e in ss))
    user = [e for e in entries if e["team"] == league.user_team]
    if user:
        lines += ["", f"{league.user_team} winners: " + ", ".join(f"{e['name']} ({e['award']})" for e in user)]
    return "\n".join(lines)


def season_summary(league: League) -> str:
    records = league.records()
    team = league.user
    rec = records[team.abbr]
    players = [league.players[pid] for pid in team.all_players]
    hitters = [p for p in players if not p.is_pitcher and p.batting.pa]
    pitchers = [p for p in players if p.is_pitcher and p.pitching.outs]
    ab = sum(p.batting.ab for p in hitters)
    hits = sum(p.batting.h for p in hitters)
    outs = sum(p.pitching.outs for p in pitchers)
    er = sum(p.pitching.er for p in pitchers)
    lines = [
        title(f"{league.year} season summary - {team.full_name}"),
        f"Record {rec.wins}-{rec.losses} ({pct(rec.pct)}), runs {rec.runs_for} scored / "
        f"{rec.runs_against} allowed ({rec.run_diff:+d})",
        f"Team AVG {pct(hits / ab) if ab else '.000'}, team ERA {er * 27 / outs:.2f}" if outs else "",
        f"Postseason: {league.postseason_finish(team.abbr)}" if league.playoffs else
        "Postseason: not started",
    ]
    best_hitters = sorted((p for p in hitters if p.batting.pa >= 150), key=lambda p: -p.batting.ops)[:3]
    best_pitchers = sorted((p for p in pitchers if p.pitching.outs >= 120), key=lambda p: p.pitching.era)[:3]
    if best_hitters:
        lines.append("Top hitters:   " + "; ".join(
            f"{p.name} {pct(p.batting.avg)}/{pct(p.batting.obp)}/{pct(p.batting.slg)}, {p.batting.hr} HR"
            for p in best_hitters))
    if best_pitchers:
        lines.append("Top pitchers:  " + "; ".join(
            f"{p.name} {p.pitching.w}-{p.pitching.l}, {p.pitching.era:.2f} ERA" for p in best_pitchers))
    lines += ["", "Division leaders:" if league.phase == PHASE_REGULAR else "Division winners:"]
    for lg in LEAGUES:
        for div in DIVISIONS:
            leader = division_standings(league.teams, records, lg, div)[0]
            r = records[leader]
            lines.append(f"  {lg} {div:<8} {league.teams[leader].full_name} ({r.wins}-{r.losses})")
    if league.playoffs and league.playoffs.champion:
        lines.append(f"World Series champion: {league.teams[league.playoffs.champion].full_name}")
    lines += ["", leaders_view(league, limit=3)]
    if league.season_awards():
        lines += ["", awards_view(league)]
    return "\n".join(line for line in lines if line is not None)


def franchise_history(league: League) -> str:
    team = league.user
    lines = [title(f"{team.full_name} - franchise history")]
    if league.history:
        rows = [[h["year"], f"{h['user_wins']}-{h['user_losses']}", h["user_result"], h["champion"],
                 ", ".join(f"{a['award'][3:]}: {a['name']}" for a in h.get("awards", []) if a["team"] == team.abbr) or "-"]
                for h in league.history]
        lines.append(table(["Year", "Record", "Result", "Champion", "Our award winners"], rows, "rrlll"))
        titles = [h["year"] for h in league.history if h["champion"] == team.abbr]
        playoffs = [h for h in league.history if h["user_result"] != "Missed playoffs"]
        wins = sum(h["user_wins"] for h in league.history)
        losses = sum(h["user_losses"] for h in league.history)
        lines.append("")
        lines.append(f"Seasons: {len(league.history)}   All-time record: {wins}-{losses}   "
                     f"Playoff appearances: {len(playoffs)}   Championships: "
                     f"{len(titles)}{' (' + ', '.join(map(str, titles)) + ')' if titles else ''}")
    else:
        lines.append("No completed seasons yet.")
    for kind, heading in (("championship", "Championships"), ("award", "Awards"),
                          ("postseason", "Postseason"), ("transaction", "Major transactions")):
        events = [e for e in league.franchise_log if e["kind"] == kind]
        if events:
            lines += ["", heading]
            lines.extend(f"  {e['year']}: {e['text']}" for e in events[-15:])
    if league.history:
        lines += ["", "League champions: " + ", ".join(f"{h['year']} {h['champion']}" for h in league.history)]
    return "\n".join(lines)


# ----------------------------------------------------------------------------- careers & history


def career_leaders_view(league: League, limit: int = 10, team: str | None = None,
                        postseason: bool = False) -> str:
    scope = f"{team} franchise" if team else "All-time"
    kind = "postseason" if postseason else "career"
    out = [f"{scope} {kind} leaders (active players and retired)"]
    batting = ("hr", "h", "rbi", "sb", "avg", "ops") if not postseason else ("hr", "h", "rbi", "avg")
    pitching = ("w", "so", "sv", "era") if not postseason else ("w", "so", "sv", "era")
    any_rows = False
    for stats, pitcher in ((batting, False), (pitching, True)):
        for stat in stats:
            rows = careers.career_leaders(league, stat, pitching=pitcher, limit=limit, team=team,
                                          postseason=postseason)
            if not rows:
                continue
            any_rows = True
            label = (careers.PITCHING_STATS if pitcher else careers.BATTING_STATS)[stat][0]
            fmt = (lambda v: f"{v:.2f}") if stat in ("era", "whip") else (
                (lambda v: pct(v)) if stat in ("avg", "obp", "slg", "ops") else (lambda v: f"{v:.0f}"))
            out.append("")
            out.append(label + ":  " + "  ".join(
                f"{i}. {person_label(league, person)} {fmt(v)}" for i, (person, v) in enumerate(rows[:5], 1)))
    if not any_rows:
        out.append("No statistics recorded yet.")
    return "\n".join(out)


def person_label(league: League, person: Player | RetiredPlayer | object) -> str:
    if isinstance(person, RetiredPlayer):
        return f"{person.name} (ret.)"
    assert isinstance(person, Player)
    return f"{person.name} ({person.team or 'FA'})"


def records_view(league: League, team: str | None = None) -> str:
    scope = f"{league.teams[team].full_name} franchise" if team else "League"
    records = careers.season_records(league, team)
    out = [f"{scope} single-season records (since {first_year(league)})"]
    if not records:
        out.append("No qualifying seasons yet.")
        return "\n".join(out)
    out.append(table(["Record", "Value", "Player", "Year"],
                     [[r.label, r.value, r.holder, r.year] for r in records], "lrlr"))
    out.append("Rate records need 502 PA or 162 IP in a season.")
    return "\n".join(out)


def first_year(league: League) -> int:
    return league.history[0]["year"] if league.history else league.year


def retired_view(league: League, limit: int = 30) -> str:
    retired = careers.retired_list(league)
    if not retired:
        return "No players with MLB careers have retired yet."
    rows = [[r.retired_year, r.name, r.position, r.age, r.last_team or "FA",
             careers.seasons_played(r), careers.career_summary(r)] for r in retired[:limit]]
    return ("Retired players (most recent first)\n"
            + table(["Year", "Player", "Pos", "Age", "Last", "Yrs", "Career"], rows, "rllrlrl")
            + f"\n{len(retired)} retired players on record.")


def career_milestones_view(league: League, limit: int = 40) -> str:
    if not league.career_milestones:
        return "No career milestones yet (100 HR, 1,000 hits, 100 wins, 1,000 strikeouts, 100 saves, ...)."
    return "Career milestones\n" + "\n".join(f"  * {m}" for m in league.career_milestones[-limit:])


def postseason_history_view(league: League) -> str:
    if not league.history:
        return "No completed postseasons yet."
    out = ["Postseason history"]
    for h in reversed(league.history):
        out.append("")
        mvp = f"   WS MVP: {h['ws_mvp']}" if h.get("ws_mvp") else ""
        out.append(f"{h['year']}: {h['champion']} def. {h['runner_up']}{mvp}")
        for line in h.get("series", []):
            if not line.startswith("World Series"):
                out.append(f"    {line}")
        for award in league.postseason_award_list(h["year"]):
            if award["award"] != "World Series MVP":
                out.append(f"    {award['award']}: {award['name']} ({award['team']}) - {award['line']}")
    return "\n".join(out)


def championships_view(league: League) -> str:
    if not league.history:
        return "No champions crowned yet."
    counts: dict[str, int] = {}
    for h in league.history:
        counts[h["champion"]] = counts.get(h["champion"], 0) + 1
    rows = [[h["year"], league.teams[h["champion"]].full_name, h["runner_up"],
             h.get("ws_mvp") or "-"] for h in reversed(league.history)]
    out = ["Championship history", table(["Year", "Champion", "Runner-up", "WS MVP"], rows, "rlll")]
    ranking = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    out.append("Titles: " + ", ".join(f"{abbr} {n}" for abbr, n in ranking))
    mine = counts.get(league.user_team, 0)
    out.append(f"{league.user_team}: {mine} championship{'s' if mine != 1 else ''}.")
    return "\n".join(out)


def morale_view(league: League, team: Team) -> str:
    rows = []
    for pid in team.all_players:
        p = league.players[pid]
        reasons = [r for r, d in morale.factors(league, p) if d < 0]
        rows.append((p.morale, [p.id, p.name, p.position, p.overall, p.morale, morale.label(p.morale),
                                ", ".join(reasons[:2]) or "-"]))
    rows.sort(key=lambda r: (r[0], r[1][0]))
    return (f"{team.full_name} - clubhouse morale (lowest first)\n"
            + table(["ID", "Name", "Pos", "OVR", "Mor", "Mood", "Concerns"], [r for _, r in rows[:20]],
                    "rllrrll"))


# ----------------------------------------------------------------------------- GUI helpers (pure)


def recent_results(league: League, abbr: str, count: int = 5) -> list[str]:
    """Most recent regular-season results for a club, newest first."""
    played = [g for g in league.team_games(abbr) if g.played][-count:]
    out = []
    for g in reversed(played):
        home = g.home == abbr
        mine, theirs = (g.home_runs, g.away_runs) if home else (g.away_runs, g.home_runs)
        opp = g.away if home else g.home
        out.append(f"Day {g.day + 1:>3}  {'W' if mine > theirs else 'L'} {mine}-{theirs}  {'vs' if home else '@ '} {opp}")
    return out


def upcoming_games(league: League, abbr: str, count: int = 3) -> list[str]:
    if league.phase == PHASE_POSTSEASON:
        series = league.user_series() if abbr == league.user_team else None
        if series and not series.is_over:
            return [f"{series.round_name}: {series.summary()}"]
        return [f"Postseason: {league.postseason_finish(abbr)}"]
    games = [g for g in league.team_games(abbr) if not g.played][:count]
    return [f"Day {g.day + 1:>3}  {'vs' if g.home == abbr else '@ '} {g.away if g.home == abbr else g.home}"
            for g in games]


def division_position(league: League, abbr: str) -> str:
    team = league.teams[abbr]
    records = league.records()
    order = division_standings(league.teams, records, team.league, team.division)
    place = order.index(abbr) + 1
    behind = games_back(records[order[0]], records[abbr])
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(place, "th")
    return f"{place}{suffix} {team.league} {team.division}" + (f", {gb(behind)} GB" if behind else "")


OUTCOME_TEXT: dict[str, str] = {
    "K": "strikes out", "BB": "walks", "HBP": "is hit by a pitch", "1B": "singles", "2B": "doubles",
    "3B": "triples", "HR": "homers", "E": "reaches on an error", "GO": "grounds out", "DP": "grounds into a double play",
    "FO": "flies out", "LO": "lines out", "IBB": "is intentionally walked", "SAC": "lays down a sacrifice bunt",
    "BH": "beats out a bunt for a hit", "FC": "bunts into a fielder's choice", "POP": "pops up a bunt",
    "BO": "is thrown out on a bunt",
}
PITCH_RESULT_TEXT: dict[str, str] = {
    "ball": "ball", "called": "called strike", "swinging": "swinging strike", "foul": "foul",
    "inplay": "in play", "hbp": "hit by pitch", "bunt": "bunted fair", "bunt_foul": "foul bunt",
    "bunt_miss": "missed bunt",
}
LOCATION_TEXT: dict[str, str] = {
    "heart": "over the heart of the plate", "edge": "on the edge", "up": "up in the zone", "down": "down",
    "chase": "off the plate", "waste": "well outside", "mistake": "left over the plate", "wild": "way off target",
}


def event_text(league: League, event: dict[str, object]) -> str:
    """One play-by-play line for a recorded game event."""
    def name(pid: object) -> str:
        player = league.players.get(int(pid)) if pid else None  # type: ignore[arg-type]
        return player.short_name if player else f"#{pid}"

    half = "Top" if event["top"] else "Bot"
    prefix = f"{half} {event['i']}"
    kind = event["k"]
    base = {2: "second", 3: "third"}.get(int(event.get("base", 2)), "second")  # type: ignore[call-overload]
    if kind == "PC":
        return f"{prefix}: pitching change - {name(event['pit'])} comes in."
    if kind == "SB":
        return f"{prefix}: {name(event['p'])} steals {base}."
    if kind == "CS":
        return f"{prefix}: {name(event['p'])} caught stealing {base}."
    if kind == "P":
        return f"{prefix}: {pitch_text(event)}"
    if kind in ("DEC", "SUB"):
        return f"{prefix}: {event.get('t', '')}"
    if kind == "HALF":
        return f"{'Top' if event['top'] else 'Bottom'} of the {ordinal(int(event['i']))}"  # type: ignore[call-overload]
    if kind == "PK":
        bag = ("first", "second", "third")[int(event.get("base", 1)) - 1]  # type: ignore[call-overload]
        if event["r"] == "out":
            return f"{prefix}: {name(event['p'])} is picked off {bag}!"
        return f"{prefix}: pickoff throw to {bag}, {name(event['p'])} gets back."
    if kind == "TO":
        return f"{prefix}: {name(event['p'])} is thrown out on the bases."
    outcome = str(event["r"])
    if outcome == "FO" and event["n"]:
        text = "hits a sacrifice fly"
    else:
        text = OUTCOME_TEXT.get(outcome, "puts the ball in play")
    runs = int(event["n"])  # type: ignore[arg-type]
    scored = f" {runs} run{'s' if runs > 1 else ''} score{'s' if runs == 1 else ''}." if runs else ""
    score = f" ({event['a']}-{event['h']})" if runs else ""
    return f"{prefix}: {name(event['p'])} {text}.{scored}{score}"


def bases_diagram(event: dict[str, object] | None) -> str:
    """Three-line diamond: filled markers for occupied bases."""
    occupied = [bool(x) for x in (event["b"] if event and int(event["o"]) < 3 else [0, 0, 0])]  # type: ignore[index]
    mark = ["●" if o else "○" for o in occupied]
    return f"    {mark[1]}\n  {mark[2]}   {mark[0]}\n    ◇"


# ----------------------------------------------------------------------------- dugout (live games)


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def pitch_text(event: Mapping[str, object]) -> str:
    """'95.4 mph slider, down - swinging strike (1-2)'."""
    from mlb_manager.tactics import PITCH_TYPES

    code = str(event.get("pt", "FF"))
    pitch = PITCH_TYPES[code].name if code in PITCH_TYPES else code
    actual = str(event.get("x", ""))
    in_zone = bool(event.get("z"))
    if in_zone and actual in ("chase", "wild", "waste"):
        where = "catching the corner"
    elif not in_zone and actual in ("heart", "mistake", "edge", "up", "down"):
        where = "just off the plate"
    else:
        where = LOCATION_TEXT.get(actual, "")
    result = PITCH_RESULT_TEXT.get(str(event["r"]), str(event["r"]))
    balls, strikes = (event.get("c") or [0, 0])  # type: ignore[misc]
    if strikes >= 3:
        result = {"called": "called strike three", "swinging": "swinging strike three",
                  "bunt_foul": "foul bunt, strike three", "bunt_miss": "missed bunt, strike three"}.get(
                      str(event["r"]), "strike three")
        tail = ""
    elif balls >= 4:
        result, tail = "ball four", ""
    elif event["r"] in ("inplay", "bunt", "hbp"):
        tail = ""
    else:
        tail = f" ({balls}-{strikes})"
    return f"{float(event.get('v', 0)):.0f} mph {pitch} {where} - {result}{tail}"  # type: ignore[arg-type]


def diamond(league: League, bases: Sequence[int]) -> str:
    """Diamond with the runners' names."""
    def runner(pid: int) -> str:
        return league.players[pid].last_name[:10] if pid and pid in league.players else ""

    mark = ["◆" if pid else "◇" for pid in bases]
    first, second, third = (runner(pid) for pid in bases)
    return (f"{'':>11}{mark[1]} {second}\n"
            f"{third:>10} {mark[2]}   {mark[0]} {first}\n"
            f"{'':>11}⌂")


def dots(filled: int, total: int) -> str:
    return "●" * filled + "○" * (total - filled)


def live_line_score(league: League, live: LiveGame) -> str:
    sim = live.sim
    innings = max(9, len(sim.away.box.line), len(sim.home.box.line))
    headers = [""] + [str(i) for i in range(1, innings + 1)] + ["R", "H", "E"]
    rows = []
    for side in (sim.away, sim.home):
        box = side.box
        cells = [str(x) for x in box.line] + [""] * (innings - len(box.line))
        rows.append([box.abbr, *cells, box.runs, box.hits, box.errors])
    return table(headers, rows, "l" + "r" * (len(headers) - 1))


def live_situation(league: League, live: LiveGame) -> str:
    from mlb_manager.dugout import describe_phase

    sim = live.sim
    if sim.is_over:
        result = sim.result
        assert result is not None
        return f"FINAL{f' ({result.innings})' if result.innings != 9 else ''}\n" + result_line(
            league, result, league.user_team)
    half = "Top" if sim.top else "Bot"
    outs = min(sim.outs, 3)
    bases = [r.player_id if r else 0 for r in sim.bases]
    score = f"{sim.away.team.abbr} {sim.away.box.runs}  {sim.home.team.abbr} {sim.home.box.runs}"
    return (f"{score}   {half} {ordinal(sim.inning)}\n"
            f"{describe_phase(sim)}\n"
            f"Outs {dots(outs, 3)}  B {dots(sim.balls, 4)} S {dots(sim.strikes, 3)}  {sim.balls}-{sim.strikes}\n"
            f"{diamond(league, bases)}")


def matchup_card(league: League, live: LiveGame) -> str:
    sim = live.sim
    if sim.is_over:
        return ""
    batter = sim.current_batter()
    batting = sim.batting_side
    pos = batting.order[batting.batter_index][1]
    line = batting.box.batting[batter.id]
    season = batter.batting
    probs = sim.outcome_probabilities(batter, sim.current_pitcher(), sim.fielding_side)
    obp = sum(probs[o] for o in ("BB", "HBP", "1B", "2B", "3B", "HR"))
    status = live.pitcher_status()
    p = status["player"]
    pl = status["line"]
    bar_len = 10
    filled = int(round(status["stamina"] * bar_len))
    rep = " ".join(f"{r.code}{r.grade}" for r in status["repertoire"])
    pitches = status["pitches"]
    return (f"AB {batter.short_name} ({batter.bats}) {pos} #{batting.batter_index + 1}  today {line.h}-{line.ab}\n"
            f"   {fmt_avg(season.avg)}/{fmt_avg(season.obp)}/{fmt_avg(season.slg)} {season.hr} HR · "
            f"xOBP {obp:.3f} K {probs['K']:.0%}\n"
            f"P  {p.short_name} ({p.throws}) {pl.ip_display} IP {pl.h}H {pl.r}R {pl.bb}BB {pl.so}K\n"
            f"   {pitches} pitch{'es' if pitches != 1 else ''} [{'█' * filled}{'░' * (bar_len - filled)}] "
            f"{status['condition']}\n"
            f"   {rep}")


def fmt_avg(value: float) -> str:
    return f"{value:.3f}".lstrip("0") if value < 1 else f"{value:.3f}"


def live_feed(league: League, live: LiveGame, limit: int = 60) -> str:
    events = live.sim.events or []
    lines = [event_text(league, e) for e in events[-limit:]]
    return "\n".join(reversed(lines)) or "Play ball!"


def live_lineups(league: League, live: LiveGame) -> str:
    sim = live.sim
    out = []
    for side in (live.user_side, live.opp_side):
        batting_now = sim.batting_side is side and not sim.is_over
        rows = []
        for i, (pid, pos) in enumerate(side.order):
            p = league.players[pid]
            line = side.box.batting[pid]
            mark = "▶" if batting_now and i == side.batter_index else " "
            rows.append([f"{mark}{i + 1}", p.short_name, pos, p.bats, f"{line.h}-{line.ab}",
                         p.rating("contact"), p.rating("power"), p.rating("speed")])
        out.append(table([side.team.abbr, "Batter", "Pos", "B", "Today", "CON", "POW", "SPD"], rows,
                         "rllllrrr"))
        pitcher = league.players[side.pitcher_id]
        defense = f"Defense {side.defense:.0f}   Catcher {side.catcher.short_name} (arm {side.catcher.rating('arm')})"
        out.append(f"Pitching: {pitcher.short_name}   {defense}")
        out.append("")
    return "\n".join(out)


def live_instructions(live: LiveGame) -> str:
    from mlb_manager import tactics as tx
    from mlb_manager.dugout import CATEGORIES, CATEGORY_LABELS, MODE_LABELS

    ins = live.instructions
    tb, pp, al = ins.batting, ins.pitching, ins.alignment
    L = tx.LABELS
    lines = [
        "AUTOMATION  " + "  ".join(f"{CATEGORY_LABELS[c]}: {MODE_LABELS[live.settings[c]]}" for c in CATEGORIES),
        f"HITTING     {L[tb.approach]} approach, {L[tb.aggression].lower()} swings, {L[tb.discipline].lower()} "
        f"discipline, {L[tb.effort].lower()} effort ({len(ins.per_batter)} batter-specific)",
        f"BASES       {L[tb.lead]} leads, {L[tb.advance].lower()} advancing",
        f"PITCHING    {L[pp.effort]} effort, {L[pp.approach]}, {L[pp.sequencing]}, {L[pp.hook]}"
        f"{', holding runners' if pp.hold_runners else ''} ({len(ins.pitch_vs)} matchup plans)",
        f"DEFENSE     Infield {L[al.infield].lower()}, outfield {L[al.outfield].lower()}, {L[al.shift].lower()}, "
        f"{L[al.aggression].lower()}",
    ]
    nxt = live.next_pitch
    calls = []
    if nxt.pitch_type:
        calls.append(tx.PITCH_TYPES[nxt.pitch_type].name)
    if nxt.location:
        calls.append(L[nxt.location].lower())
    for flag, label in ((nxt.intentional_ball, "intentional ball"), (nxt.pitchout, "pitchout"),
                        (nxt.take, "take"), (nxt.bunt, "bunt"), (nxt.hit_and_run, "hit-and-run")):
        if flag:
            calls.append(label)
    if nxt.steal:
        calls.append("delayed steal" if nxt.steal == "delayed" else "steal")
    if nxt.send:
        calls.append(f"{nxt.send} runners")
    lines.append("NEXT PITCH  " + (", ".join(calls) if calls else "-"))
    return "\n".join(lines)


def bullpen_text(league: League, live: LiveGame) -> str:
    rows = [[r["player"].short_name, r["role"], r["throws"], r["rating"], r["fatigue"], r["rest"]]
            for r in live.bullpen_report()]
    status = live.pitcher_status(live.user_side)
    head = (f"On the mound: {status['player'].short_name}  {status['pitches']} pitches, load "
            f"{status['load']:.0f}/{status['limit']}, {status['condition']}, faced {status['faced']}")
    return head + "\n\n" + table(["Bullpen", "Role", "T", "OVR", "Fatigue", "Status"], rows, "llrrrl")


def scouting_text(league: League, live: LiveGame) -> str:
    rows = [[r["player"].short_name, r["pos"], f"{r['pull']:.2f}", r["power"], r["contact"], r["eye"], r["speed"],
             (r["note"] + (f"; {r['threat']}" if r["threat"] else ""))] for r in live.opponent_tendencies()]
    out = [table([f"{live.opp_side.team.abbr} hitters", "Pos", "Pull", "POW", "CON", "EYE", "SPD", "Tendency"],
                 rows, "llrrrrrl")]
    pitcher = league.players[live.opp_side.pitcher_id]
    from mlb_manager import tactics as tx

    rep = ", ".join(f"{r.name} {r.grade} ({r.usage:.0%})" for r in tx.repertoire(pitcher))
    out.append("")
    out.append(f"Their pitcher {pitcher.short_name}: {rep}")
    up = live.upcoming_hitters(4)
    out.append("")
    out.append(table(["Due up", "Pos", "Today", "OBP*", "K*", "HR*"],
                     [[f"{r['slot']}. {r['player'].short_name}", r["pos"], r["today"], f"{r['obp']:.3f}",
                       f"{r['k']:.0%}", f"{r['hr']:.1%}"] for r in up], "llrrrr"))
    out.append("* expected vs the current pitcher")
    fat = live.fatigue_report()
    out.append("")
    out.append(table(["Your lineup", "Pos", "Fatigue", "Morale", "Health"],
                     [[r["player"].short_name, r["pos"], r["fatigue"], r["morale"],
                       r["injury"] or ("rusty" if r["rust"] else "ok")] for r in fat], "llrrl"))
    return "\n".join(out)


def decision_text(league: League, live: LiveGame, decision: Decision) -> str:
    events = live.sim.events or []
    half = "T" if decision.top else "B"
    a = decision.assessment
    grade = f"[{a.grade}] " if a else ""
    result = ""
    if decision.result is not None and decision.result < len(events):
        result = " => " + event_text(league, events[decision.result]).split(": ", 1)[-1]
    return (f"{half}{decision.inning} {decision.outs} out, {decision.count[0]}-{decision.count[1]}, "
            f"{decision.score[0]}-{decision.score[1]}  {decision.text}  ({decision.source}) {grade}"
            f"{(a.basis if a else '')}{result}")


def decision_log(league: League, live: LiveGame, limit: int = 60) -> str:
    rows = [decision_text(league, live, d) for d in live.decisions[-limit:]]
    return "\n".join(reversed(rows)) or "No managerial decisions yet."


def manager_report(report: Mapping[str, object] | None) -> str:
    """Post-game managerial summary."""
    from mlb_manager.dugout import CATEGORIES, CATEGORY_LABELS

    if not report:
        return "No managerial report (the game was simulated automatically)."
    cats = report["categories"]  # type: ignore[index]
    rows = []
    for c in CATEGORIES:
        r = cats[c]  # type: ignore[index]
        if not r["count"]:
            continue
        rows.append([CATEGORY_LABELS[c], r["count"], r["manual"], r["auto"] + r["forced"], r["assisted"],
                     r["favourable"], r["neutral"], r["unfavourable"], f"{r['expected_runs']:+.2f}"])
    out = [f"Managerial report: {report['decisions']} decisions, {report['manual']} made by you."]
    if rows:
        out.append(table(["Category", "Dec", "You", "CPU", "Asst", "Fav", "Neu", "Unfav", "xRuns"], rows,
                         "lrrrrrrrr"))
    out.extend(str(n) for n in report.get("notes", []))  # type: ignore[attr-defined]
    out.append(str(report.get("method", "")))
    log = report.get("log", [])  # type: ignore[attr-defined]
    if log:
        out.append("")
        out.append("Decision log")
        for d in log[-25:]:
            a = d.get("assessment") or {}
            out.append(f"  {'T' if d['top'] else 'B'}{d['inning']} {d['outs']} out  {d['text']} ({d['source']})"
                       + (f" [{a.get('grade')}] {a.get('basis')}" if a else ""))
    return "\n".join(out)


# ----------------------------------------------------------------------------- financial rules


def rules_rows(r: finance.FinancialRules) -> list[tuple[str, str]]:
    """``(label, value)`` rows describing a rule set (shared by CLI and GUI)."""
    if r.cap_type == finance.CAP_NONE:
        cap = "None - unlimited payroll"
    elif r.hard_cap:
        cap = f"Hard cap {fmt(r.cap_amount)} - no move may take payroll above it"
    else:
        exc = ", ".join(finance.SOFT_CAP_EXCEPTIONS[e].lower() for e in r.soft_cap_exceptions) or "none"
        cap = (f"Soft cap {fmt(r.cap_amount)} - exceeding it only through exceptions ({exc}); penalty "
               f"{finance.format_rate(r.soft_cap_tax_rate)} of the season payroll above it")
    floor = (f"{fmt(r.floor_amount)} - shortfall charged at {finance.format_rate(r.floor_penalty_rate)}"
             if r.has_floor else "None")
    if r.cbt_enabled:
        rates = " / ".join(finance.format_rate(x) for x in r.cbt_rates)
        surcharges = ", ".join(
            f"+{finance.format_rate(t.rate)}"
            + (f" (repeat {finance.format_rate(t.repeat_rate)})" if t.repeat_rate != t.rate else "")
            + f" over {fmt(r.cbt_threshold + t.over)}" for t in r.cbt_surcharges) or "none"
        cbt = (f"Threshold {fmt(r.cbt_threshold)}; {rates} for the 1st / 2nd / 3rd+ consecutive season over "
               f"(resets after a season under); surcharges {surcharges}")
        if r.cbt_draft_penalty_over > 0:
            cbt += f"; {fmt(r.cbt_draft_penalty_over)}+ over moves the top draft pick back 10 places"
        measure = f"Contract AAV, prorated by days on the roster, plus {fmt(r.player_benefits)} player benefits"
    else:
        cbt, measure = "Disabled", "-"
    sharing = (f"{finance.format_rate(r.revenue_sharing_rate)} of local revenue pooled and split equally"
               if r.revenue_sharing else "Disabled")
    return [
        ("Rules", r.source + (f" - {r.agreement}" if r.agreement != "Custom" else "")
         + (" (provisional)" if r.provisional else "")),
        ("Season", str(r.season)),
        ("Salary cap", cap),
        ("Salary floor", floor),
        ("Competitive Balance Tax", cbt),
        ("CBT payroll", measure),
        ("Revenue sharing", sharing),
        ("League minimum salary", fmt(r.min_salary)),
    ]


def rules_view(r: finance.FinancialRules, title: str | None = None) -> str:
    rows = rules_rows(r)
    width = max(len(label) for label, _ in rows)
    lines = [title] if title else []
    lines += [f"  {label:<{width}}  {value}" for label, value in rows]
    lines += [f"  Note: {note}" for note in r.notes]
    proposals = finance.proposed_terms(r.season)
    if proposals and r.source == finance.PRESET_LABELS[finance.PRESET_MLB]:
        lines.append("  Not in effect (proposals are never applied as league rules):")
        lines += [f"    - {t.party}: {t.summary} [{t.status}]" for t in proposals]
    return "\n".join(lines)


def league_finances_view(league: League) -> str:
    """Every club's payroll position under the current rules, plus the user's past seasons."""
    r = finance.rules(league)
    streaks = league.finance.state.streaks
    rows = []
    for team in sorted(league.teams.values(), key=lambda t: -finance.payroll(league, t)):
        bill = finance.projected_tax(league, team)
        rows.append([team.abbr + (" *" if team.abbr == league.user_team else ""), fmt(finance.payroll(league, team)),
                     fmt(bill.cbt_payroll), fmt(bill.total) if bill.total else "-",
                     streaks.get(team.abbr, 0) or "-", fmt(finance.payroll_budget(league, team))])
    lines = [rules_view(r, f"League financial rules ({finance.contract_season(league)})"), "",
             "Club payrolls (projected for the season)",
             table(["Club", "Payroll", "CBT payroll", "Proj. tax", "Yrs over", "Budget"], rows, "lrrrrr")]
    past = [row for row in league.finance.state.ledger if row["team"] == league.user_team]
    if past:
        lines += ["", f"{league.user_team} finances by season"]
        lines.append(table(
            ["Year", "Revenue", "Shared out", "Shared in", "Payroll", "CBT payroll", "CBT tax", "Penalties", "Net"],
            [[row["year"], fmt(row["revenue"]), fmt(row["shared_out"]), fmt(row["shared_in"]), fmt(row["payroll"]),
              fmt(row["cbt_payroll"]), fmt(row["cbt_tax"]), fmt(row["soft_cap_penalty"] + row["floor_penalty"]),
              fmt(row["net"])] for row in past], "rrrrrrrrr"))
    if league.finance.state.changes:
        lines += ["", "Rule changes made to this league"]
        lines += [f"  {c['year']} ({c['phase']}): {finance.PRESET_LABELS.get(c['preset'], c['preset'])} from "
                  f"{c['season']}" for c in league.finance.state.changes]
    return "\n".join(lines)
