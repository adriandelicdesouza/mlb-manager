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
        self.assertEqual(data["format_version"], 2)
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

    def test_v1_save_is_migrated(self) -> None:
        league = League.create(seed=36, user_team="CIN")
        league.sim_days(4)
        data = league.to_dict()
        # Strip everything version 2 introduced to reproduce a version-1 save.
        for key in ("salary_cap", "draft_picks", "draft", "pending_draft_order", "trade_offers",
                    "next_offer_id", "transactions"):
            data.pop(key)
        v2_player_keys = ("potential", "development", "salary", "contract_years", "contract_length",
                          "draft_year", "draft_round", "draft_pick")
        data["players"] = [p for p in data["players"] if p["team"] is not None]
        for p in data["players"]:
            for key in v2_player_keys:
                p.pop(key)
        for t in data["teams"]:
            t.pop("strategy")
            t.pop("dead_money")
        path = self.dir / "v1.json"
        path.write_text(json.dumps({"format_version": 1, "league": data}))
        migrated = load_league(path)
        self.assertEqual(migrated.day, league.day)
        self.assertEqual(len(migrated.draft_picks), 450)
        for team in migrated.teams.values():
            for pid in team.all_players:
                self.assertGreater(migrated.players[pid].salary, 0)
                self.assertGreater(migrated.players[pid].potential, 0)
        self.assertTrue(any(p.team is None for p in migrated.players.values()))
        again = load_league(path)
        self.assertEqual(again.to_dict(), migrated.to_dict())
        save_league(migrated, self.dir / "v2.json")
        self.assertEqual(json.loads((self.dir / "v2.json").read_text())["format_version"], 2)
        migrated.sim_days(2)

    def test_mid_draft_round_trip(self) -> None:
        from mlb_manager.league import PHASE_COMPLETE

        league = League.create(seed=37, user_team="ARI")
        league.phase = PHASE_COMPLETE
        league.begin_offseason()
        league.advance_offseason()
        league.draft_until_user()
        path = save_league(league, self.dir / "draft.json")
        loaded = load_league(path)
        self.assertEqual(loaded.to_dict(), league.to_dict())
        self.assertEqual(loaded.draft_on_clock(), league.draft_on_clock())
        for lg in (league, loaded):
            lg.draft_auto_rest()
            lg.advance_offseason()
            lg.advance_offseason()
        self.assertEqual(loaded.to_dict(), league.to_dict())

    def test_resolve_save_path(self) -> None:
        self.assertEqual(resolve_save_path("my club", self.dir), self.dir / "my_club.json")
        self.assertEqual(resolve_save_path("x.json", self.dir), self.dir / "x.json")
        nested = self.dir / "sub" / "y"
        self.assertEqual(resolve_save_path(str(nested), self.dir), nested.with_suffix(".json"))
        with self.assertRaises(SaveError):
            resolve_save_path("  ", self.dir)
