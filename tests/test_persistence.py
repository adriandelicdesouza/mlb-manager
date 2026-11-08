import json
import tempfile
import unittest
from pathlib import Path

from mlb_manager import roster
from mlb_manager.league import League
from mlb_manager.persistence import (
    SaveError,
    list_saves,
    load_league,
    resolve_save_path,
    save_league,
)


class PersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_round_trip_preserves_state(self) -> None:
        league = League.create(seed=31, user_team="PHI")
        league.sim_days(10)
        path = save_league(league, self.dir / "phi.json")
        loaded = load_league(path)
        self.assertEqual(loaded.to_dict(), league.to_dict())
        self.assertIsNotNone(loaded.last_user_game)

    def test_save_load_continuation_is_deterministic(self) -> None:
        a = League.create(seed=32, user_team="KC")
        a.sim_days(6)
        save_league(a, self.dir / "kc.json")
        b = load_league(self.dir / "kc.json")
        a.sim_days(8)
        b.sim_days(8)
        self.assertEqual(a.to_dict(), b.to_dict())

    def test_manager_changes_persist(self) -> None:
        league = League.create(seed=33, user_team="DET")
        team = league.user
        roster.swap_batting_order(team, 0, 8)
        order = [s.player_id for s in team.lineup]
        save_league(league, self.dir / "det.json")
        loaded = load_league(self.dir / "det.json")
        self.assertEqual([s.player_id for s in loaded.user.lineup], order)

    def test_postseason_round_trip(self) -> None:
        league = League.create(seed=34, user_team="SD")
        league.sim_regular_season()
        league.sim_days(2)
        path = save_league(league, self.dir / "sd.json")
        loaded = load_league(path)
        self.assertEqual(loaded.playoffs.to_dict(), league.playoffs.to_dict())
        loaded.sim_to_completion()
        league.sim_to_completion()
        self.assertEqual(loaded.playoffs.champion, league.playoffs.champion)

    def test_save_is_plain_json_with_version(self) -> None:
        league = League.create(seed=35, user_team="MIA")
        path = save_league(league, self.dir / "nested" / "mia.json")
        data = json.loads(path.read_text())
        self.assertEqual(data["format_version"], 1)
        self.assertEqual(len(data["league"]["teams"]), 30)
        self.assertEqual(list_saves(self.dir / "nested"), [path])
        self.assertEqual([p.name for p in (self.dir / "nested").iterdir()], ["mia.json"])

    def test_errors(self) -> None:
        with self.assertRaises(SaveError):
            load_league(self.dir / "missing.json")
        bad = self.dir / "bad.json"
        bad.write_text("{not json")
        with self.assertRaises(SaveError):
            load_league(bad)
        wrong = self.dir / "wrong.json"
        wrong.write_text(json.dumps({"format_version": 99, "league": {}}))
        with self.assertRaises(SaveError):
            load_league(wrong)
        corrupt = self.dir / "corrupt.json"
        corrupt.write_text(json.dumps({"format_version": 1, "league": {"seed": 1}}))
        with self.assertRaises(SaveError):
            load_league(corrupt)

    def test_resolve_save_path(self) -> None:
        self.assertEqual(resolve_save_path("my club", self.dir), self.dir / "my_club.json")
        self.assertEqual(resolve_save_path("x.json", self.dir), self.dir / "x.json")
        nested = self.dir / "sub" / "y"
        self.assertEqual(resolve_save_path(str(nested), self.dir), nested.with_suffix(".json"))
        with self.assertRaises(SaveError):
            resolve_save_path("  ", self.dir)
