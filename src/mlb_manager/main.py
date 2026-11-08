"""Application entry point (console script ``mlb-manager``)."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from mlb_manager import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mlb-manager",
        description="Manage an MLB franchise from the terminal.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    build_parser().parse_args(argv)
    print(f"MLB Manager {__version__}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
