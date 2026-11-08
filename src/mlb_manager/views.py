"""Text rendering for the terminal UI.

Every function returns a string; nothing here reads input or mutates state.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from mlb_manager import ratings as rt
from mlb_manager import roster
from mlb_manager.franchises import DIVISIONS, FRANCHISES, LEAGUE_NAMES, LEAGUES
from mlb_manager.game import GameResult, TeamBox
from mlb_manager.league import PHASE_COMPLETE, PHASE_POSTSEASON, DayReport, League
from mlb_manager.models import Player, Team
from mlb_manager.playoffs import ROUND_NAMES
from mlb_manager.standings import (
    division_standings,
    games_back,
    wild_card_standings,
)


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
    if league.phase == PHASE_POSTSEASON:
        return "Postseason"
    if league.phase == PHASE_COMPLETE:
        return "Season complete"
    return f"Regular season, day {league.day + 1}"


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
            p.id, p.name, pos, _positions(p), p.age, _hand(p), p.overall_at(pos) if pos != "-" else p.overall,
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
            p.id, p.name, (roles or {}).get(p.id, p.position), p.age, p.throws, p.overall,
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
        "(!) = playing out of position (defensive penalty)",
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
        "",
        "Ratings",
    ]
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
    lines.append(f"{league.year} season")
    if b.pa:
        lines.append(table(
            ["G", "PA", "AB", "R", "H", "2B", "3B", "HR", "RBI", "BB", "SO", "SB", "AVG", "OBP", "SLG", "OPS"],
            [[b.g, b.pa, b.ab, b.r, b.h, b.doubles, b.triples, b.hr, b.rbi, b.bb, b.so, b.sb,
              pct(b.avg), pct(b.obp), pct(b.slg), f"{b.ops:.3f}"]], "r" * 16,
        ))
    if s.g:
        lines.append(table(
            ["G", "GS", "W", "L", "SV", "IP", "H", "R", "ER", "BB", "SO", "HR", "ERA", "WHIP"],
            [[s.g, s.gs, s.w, s.l, s.sv, s.ip_display, s.h, s.r, s.er, s.bb, s.so, s.hr,
              f"{s.era:.2f}", f"{s.whip:.2f}"]], "r" * 14,
        ))
    if not b.pa and not s.g:
        lines.append("  No games played yet.")
    if p.is_pitcher:
        lines.append(f"Current fatigue: {p.fatigue}")
    return "\n".join(lines)


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


def _batting_box(box: TeamBox, name) -> str:  # noqa: ANN001
    rows = []
    for pid, pos in box.batting_order:
        s = box.batting[pid]
        rows.append([f"{name(pid)} {pos}", s.ab, s.r, s.h, s.rbi, s.bb, s.so, s.hr])
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
    if league.phase == PHASE_COMPLETE and league.history:
        h = league.history[-1]
        out.append(f"{h['year']} champion: {h['champion']}. {team.abbr}: {h['user_result']}.")
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
