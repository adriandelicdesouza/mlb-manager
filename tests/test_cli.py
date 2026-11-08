import io
import tempfile
import unittest
import unittest.mock
from collections.abc import Iterable
from contextlib import redirect_stdout
from pathlib import Path

from mlb_manager.cli import App
from mlb_manager.league import League
from mlb_manager.main import main, simulate_season
from mlb_manager.persistence import load_league, save_league


class ScriptedIO:
    """Feeds scripted answers to the App and captures everything it prints."""

    def __init__(self, answers: Iterable[str]) -> None:
        self.answers = list(answers)
        self.lines: list[str] = []

    def input(self, prompt: str) -> str:
        self.lines.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)

    def output(self, text: str = "") -> None:
        self.lines.append(text)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_app(self, answers: list[str], league: League | None = None, save_path: Path | None = None) -> ScriptedIO:
        io_ = ScriptedIO(answers)
        app = App(io_.input, io_.output, save_dir=self.dir)
        self.assertEqual(app.run(league, save_path), 0)
        return io_

    def test_new_franchise_browse_sim_and_save(self) -> None:
        answers = [
            "1", "xyz", "bos", "99",      # new franchise: bad abbr, then BOS, seed 99
            "1",                           # dashboard
            "2",                           # roster
            "3", "1", "1", "9", "4", "0",  # lineup: swap 1<->9, auto, back
            "4", "3", "0",                 # staff: auto, back
            "6", "1",                      # player card #1
            "12", "2",                     # simulate 7 days
            "10",                          # box score
            "7", "3",                      # standings, both leagues
            "8", "9", "11",                # schedule, leaders, bracket
            "13", "bos_test",              # save
            "0",                           # back to main menu (no unsaved changes)
            "0",                           # quit
        ]
        io_ = self.run_app(answers)
        text = io_.text
        self.assertIn("Unknown club 'XYZ'", text)
        self.assertIn("You are now the manager of the Boston Red Sox", text)
        self.assertIn("starting lineup", text)
        self.assertIn("Simulated 7 day(s).", text)
        self.assertIn("W: ", text)  # box score decisions
        self.assertIn("AL East", text)
        self.assertIn("NL West", text)
        self.assertIn("league leaders", text)
        self.assertIn("Goodbye!", text)
        saved = load_league(self.dir / "bos_test.json")
        self.assertEqual(saved.user_team, "BOS")
        self.assertEqual(saved.seed, 99)
        self.assertGreater(saved.day, 0)

    def test_load_roster_move_and_eof_autosave(self) -> None:
        league = League.create(seed=5, user_team="SEA")
        path = save_league(league, self.dir / "sea.json")
        team = league.user
        out_id = team.lineup[0].player_id
        in_id = next(pid for pid in team.reserves if not league.players[pid].is_pitcher)
        answers = ["2", "1", "5", str(out_id), str(in_id), "12", "1"]  # then EOF
        io_ = self.run_app(answers)
        text = io_.text
        self.assertIn("Loaded sea.json", text)
        self.assertIn("Called up", text)
        self.assertIn("Simulated 1 day(s).", text)
        self.assertIn("Unsaved progress written to", text)
        # The manual save is untouched; progress goes to a separate autosave.
        self.assertEqual(load_league(path).day, 0)
        auto = load_league(self.dir / "autosave_sea_2026.json")
        self.assertIn(in_id, auto.user.active)
        self.assertEqual(auto.day, 1)

    def test_invalid_roster_move_rejected(self) -> None:
        league = League.create(seed=6, user_team="TEX")
        hitter = league.user.lineup[0].player_id
        pitcher = next(pid for pid in league.user.reserves if league.players[pid].is_pitcher)
        io_ = self.run_app(["5", str(hitter), str(pitcher), "0", "n", "0"], league=league)
        self.assertIn("Not allowed", io_.text)

    def test_full_season_and_new_season_flow(self) -> None:
        league = League.create(seed=8, user_team="ATL")
        io_ = self.run_app(["12", "5", "12", "y", "0", "n", "0"], league=league)
        text = io_.text
        self.assertIn("CHAMPION:", text)
        self.assertIn("Welcome to 2027!", text)
        self.assertEqual(league.year, 2027)

    def test_load_bad_path(self) -> None:
        io_ = self.run_app(["2", "missing_save", "0"])
        self.assertIn("Save file not found", io_.text)


class MainTests(unittest.TestCase):
    def test_simulate_season_summary(self) -> None:
        text = simulate_season(seed=3, team="HOU")
        self.assertIn("CHAMPION:", text)
        self.assertIn("HOU: ", text)

    def test_unknown_team_rejected(self) -> None:
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            with unittest.mock.patch("sys.stderr", new_callable=io.StringIO):
                main(["--team", "ZZZ", "--simulate-season"])
        self.assertEqual(ctx.exception.code, 2)

    def test_load_missing_file_returns_error(self) -> None:
        with unittest.mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(main(["--load", "/nonexistent/x.json"]), 1)
        self.assertIn("not found", err.getvalue())



