"""Application entry point (console script ``mlb-manager``)."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from mlb_manager import __version__, views
from mlb_manager.cli import App, QuitApp
from mlb_manager.franchises import FRANCHISES_BY_ABBR, LEAGUES
from mlb_manager.league import League
from mlb_manager.persistence import SaveError, load_league


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mlb-manager",
        description="Manage an MLB franchise of fictional players from the terminal.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--new", action="store_true", help="start a new franchise immediately")
    parser.add_argument("--team", type=str.upper, metavar="ABBR",
                        help="club abbreviation for --new / --simulate-season (e.g. NYY)")
    parser.add_argument("--seed", type=int, help="league seed for reproducible results")
    parser.add_argument("--load", type=Path, metavar="FILE", help="load a saved franchise")
    parser.add_argument("--save-dir", type=Path, metavar="DIR",
                        help="directory for saves (default: ./saves or $MLB_MANAGER_SAVE_DIR)")
    parser.add_argument("--salary-cap", type=float, metavar="MILLIONS",
                        help="salary cap in $M for a new league (default: 125)")
    parser.add_argument("--simulate-season", action="store_true",
                        help="non-interactive: simulate a full season and print a summary")
    parser.add_argument("--cli", action="store_true",
                        help="use the classic numbered-menu interface instead of the interactive GUI")
    return parser


def use_tui(args: argparse.Namespace) -> bool:
    """The Textual GUI runs on an interactive terminal unless --cli is given."""
    from mlb_manager import tui

    return not args.cli and tui.available() and sys.stdin.isatty() and sys.stdout.isatty()


def simulate_season(seed: int, team: str) -> str:
    league = League.create(seed=seed, user_team=team)
    league.sim_to_completion()
    parts = [views.title(f"{league.year} season summary (seed {seed})")]
    for lg in LEAGUES:
        parts.append(views.standings(league, lg))
        parts.append("")
    parts.append(views.bracket_view(league))
    parts.append("")
    parts.append(views.leaders_view(league, limit=3))
    parts.append("")
    rec = league.records()[team]
    parts.append(f"{team}: {rec.wins}-{rec.losses}, {league.postseason_finish(team)}")
    return "\n".join(parts)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.team and args.team not in FRANCHISES_BY_ABBR:
        parser.error(f"unknown team {args.team!r}; choose from {', '.join(sorted(FRANCHISES_BY_ABBR))}")

    if args.simulate_season:
        print(simulate_season(args.seed if args.seed is not None else 1, args.team or "NYY"))
        return 0

    if args.salary_cap is not None and args.salary_cap <= 0:
        parser.error("--salary-cap must be positive")
    if use_tui(args):
        from mlb_manager import tui

        league, save_path = None, None
        if args.load:
            try:
                league = load_league(args.load)
            except SaveError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1
            save_path = args.load
        return tui.run(league=league, save_path=save_path, save_dir=args.save_dir, salary_cap=args.salary_cap,
                       new_team=args.team if (args.new or args.team) and league is None else None,
                       new_seed=args.seed)
    app = App(save_dir=args.save_dir, salary_cap=args.salary_cap)
    if args.load:
        try:
            league = load_league(args.load)
        except SaveError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        return app.run(league, save_path=args.load)
    if args.new or args.team:
        app.say(views.title(f"MLB MANAGER {__version__}"))
        try:
            started = app.new_franchise(team=args.team, seed=args.seed)
        except QuitApp:
            return 0
        return app.run(app.league if started else None)
    return app.run()


if __name__ == "__main__":
    sys.exit(main())
