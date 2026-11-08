import copy
import random
import statistics
import unittest

from mlb_manager import awards, development, roster, seasonlog, views
from mlb_manager.game import GameResult, TeamBox
from mlb_manager.generation import PlayerFactory
from mlb_manager.league import PHASE_COMPLETE, League
from mlb_manager.models import BattingStats, PitchingStats


class FullSeasonTests(unittest.TestCase):
    """One complete season (regular + postseason) shared by these tests."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.league = League.create(seed=300, user_team="CIN")
        lg = cls.league
        cls.young = {p.id: p.overall for p in lg.players.values()
                     if p.team and p.age <= 23 and p.potential > p.overall + 5}
        lg.sim_days(31)
        cls.after_month = {pid: lg.players[pid].overall for pid in cls.young if pid in lg.players}
        lg.sim_to_completion()
        cls.entries = lg.season_awards()

    def awards_for(self, suffix: str) -> list[dict]:
        return [e for e in self.entries if e["award"].endswith(suffix)]

    def test_headline_awards_each_league(self) -> None:
        for label in ("MVP", "Cy Young", "Rookie of the Year"):
            winners = self.awards_for(label)
            self.assertEqual(sorted(e["award"][:2] for e in winners), ["AL", "NL"], label)
        for e in self.awards_for("Cy Young"):
            self.assertTrue(self.league.players[e["player_id"]].is_pitcher)
        for e in self.awards_for("Rookie of the Year"):
            self.assertEqual(self.league.players[e["player_id"]].service_years, 0)

    def test_award_winners_play_in_the_right_league(self) -> None:
        for e in self.entries:
            self.assertEqual(self.league.teams[e["team"]].league, e["award"][:2])

    def test_mvp_is_best_value_qualifier(self) -> None:
        lg = self.league
        games = 162
        for e in self.awards_for("MVP"):
            lg_code = e["award"][:2]
            pool = [p for p in lg.players.values() if p.team and lg.teams[p.team].league == lg_code
                    and ((not p.is_pitcher and p.batting.pa >= 3.1 * games)
                         or (p.is_pitcher and p.pitching.outs >= 3 * games))]
            best = max(pool, key=lambda p: (awards.player_value(p), -p.id))
            self.assertEqual(best.id, e["player_id"])

    def test_gold_gloves_and_silver_sluggers(self) -> None:
        lg = self.league
        gold = [e for e in self.entries if "Gold Glove" in e["award"]]
        silver = [e for e in self.entries if "Silver Slugger" in e["award"]]
        self.assertGreaterEqual(len(gold), 14)
        self.assertGreaterEqual(len(silver), 14)
        for e in gold:
            if e["position"] != "P":
                self.assertGreaterEqual(lg.players[e["player_id"]].games_by_pos.get(e["position"], 0), 81)
        for e in silver:
            self.assertEqual(lg.players[e["player_id"]].main_position, e["position"])

    def test_history_records_season(self) -> None:
        h = self.league.history[-1]
        self.assertEqual(h["year"], 2026)
        self.assertEqual(len(h["awards"]), 6)
        self.assertIn("HR", h["leaders"])
        self.assertIn("user_runs", h)

    def test_milestones_recorded(self) -> None:
        lg = self.league
        self.assertTrue(lg.milestones)
        self.assertEqual(len(lg.milestone_keys), len(set(lg.milestone_keys)))
        self.assertTrue(any("clinch" in m for m in lg.milestones))

    def test_in_season_development(self) -> None:
        changes = [self.after_month[pid] - self.young[pid] for pid in self.after_month]
        self.assertGreater(statistics.mean(changes), 0)
        self.assertLessEqual(max(changes), 4)  # capped, no wild swings

    def test_views(self) -> None:
        lg = self.league
        self.assertIn("season summary", views.season_summary(lg))
        self.assertIn("MLB awards", views.awards_view(lg))
        self.assertIn("franchise history", views.franchise_history(lg))
        self.assertIn("milestones", views.milestones_view(lg))

    def test_offseason_increments_service_and_keeps_awards(self) -> None:
        lg = copy.deepcopy(self.league)
        regular = next(p for p in lg.players.values() if p.team and p.batting.g > 100)
        before = regular.service_years
        lg.start_new_season()
        self.assertEqual(regular.service_years, before + 1)
        self.assertEqual(lg.milestones, [])
        self.assertEqual(regular.games_by_pos, {})
        self.assertEqual(lg.season_awards(2026), self.entries)
        self.assertIn("2026", views.franchise_history(lg))


class MilestoneUnitTests(unittest.TestCase):
    def test_stat_milestone_crossing(self) -> None:
        factory = PlayerFactory(random.Random(1))
        p = factory.create("LF", 0, 27, 0)
        p.team = "NYY"
        p.batting = BattingStats(hr=30, rbi=99)
        line = BattingStats(hr=1, rbi=1)
        messages = seasonlog.stat_milestones(p, line, None)
        self.assertEqual(len(messages), 1)
        self.assertIn("30 home runs", messages[0])
        p.batting = BattingStats(hr=31, rbi=100)
        self.assertEqual(len(seasonlog.stat_milestones(p, line, None)), 1)  # 100 RBI only
        sp = factory.create("SP", 0, 27, 0)
        sp.team = "NYY"
        sp.pitching = PitchingStats(so=201)
        self.assertTrue(seasonlog.stat_milestones(sp, None, PitchingStats(so=5)))

    def test_no_hitter_detection(self) -> None:
        factory = PlayerFactory(random.Random(2))
        ace = factory.create("SP", 0, 27, 0)
        players = {ace.id: ace}
        home = TeamBox("NYY", pitching={ace.id: PitchingStats(outs=27)}, line=[0] * 9, hits=5)
        away = TeamBox("BOS", line=[0] * 9, hits=0)
        result = GameResult("BOS", "NYY", away, home, 9, ace.id, ace.id, None)
        found = seasonlog.no_hitters(result, players)
        self.assertEqual(len(found), 1)
        self.assertIn("throws a no-hitter against BOS", found[0])

    def test_milestone_dedupe(self) -> None:
        lg = League.create(seed=301, user_team="NYY")
        news: list[str] = []
        lg.add_milestone("k", "hello", news)
        lg.add_milestone("k", "hello", news)
        self.assertEqual(news, ["hello"])
        self.assertEqual(len(lg.milestones), 1)


class InSeasonDevelopmentUnitTests(unittest.TestCase):
    def make(self, age: int, pot_gap: int):
        factory = PlayerFactory(random.Random(age))
        out = []
        for _ in range(200):
            p = factory.create("SS", -8, age, 0)
            p.age = age
            p.potential = p.overall + pot_gap
            out.append(p)
        return out

    def test_playing_time_matters(self) -> None:
        regular = statistics.mean(development.in_season_development(p, random.Random(p.id), 1.2)
                                  for p in self.make(21, 20))
        idle = statistics.mean(development.in_season_development(p, random.Random(p.id), 0.3)
                               for p in self.make(21, 20))
        self.assertGreater(regular, idle)

    def test_changes_are_capped(self) -> None:
        for p in self.make(20, 40):
            self.assertLessEqual(development.in_season_development(p, random.Random(p.id), 1.2), 4)
        for p in self.make(38, 0):
            self.assertGreaterEqual(development.in_season_development(p, random.Random(p.id), 1.0), -4)

    def test_veterans_slip(self) -> None:
        old = statistics.mean(development.in_season_development(p, random.Random(p.id), 1.0)
                              for p in self.make(37, 0))
        self.assertLess(old, 0)


class SmarterRosterTests(unittest.TestCase):
    def test_hitting_value_blends_performance(self) -> None:
        factory = PlayerFactory(random.Random(3))
        p = factory.create("1B", 0, 27, 0)
        base = roster.hitting_value(p, use_performance=True)
        p.batting = BattingStats(pa=400, ab=350, h=140, hr=35, bb=50)
        self.assertGreater(roster.hitting_value(p, use_performance=True), base)
        self.assertEqual(roster.hitting_value(p, use_performance=False), base)

    def test_rotation_prefers_effective_starters(self) -> None:
        lg = League.create(seed=302, user_team="TB")
        team = lg.teams["TB"]
        fifth = lg.players[team.rotation[-1]]
        candidates = [p for p in roster.bullpen(team, lg.players) if p.id != team.closer]
        sub = max(candidates, key=lambda p: p.rating("stamina"))
        fifth.pitching = PitchingStats(outs=300, er=90)  # 8.10 ERA
        sub.pitching = PitchingStats(outs=300, er=20)
        sub.ratings["stamina"] = 70
        sub.ratings.update({k: fifth.ratings[k] for k in ("stuff", "control", "movement")})
        roster.auto_pitching(team, lg.players, use_performance=True)
        self.assertIn(sub.id, team.rotation)
        self.assertNotIn(fifth.id, team.rotation)

    def test_cpu_promotes_better_prospects(self) -> None:
        lg = League.create(seed=303, user_team="NYY")
        team = lg.teams["KC"]
        prospect = lg.players[team.reserves[0]]
        for attr in prospect.ratings:
            if attr != "stamina":
                prospect.ratings[attr] = 85
        from mlb_manager import frontoffice
        frontoffice.optimize_roster(lg, team, use_performance=True)
        self.assertIn(prospect.id, team.active)
        self.assertEqual(roster.validate(team, lg.players), [])


class MigrationV2Tests(unittest.TestCase):
    def test_v2_save_upgrades(self) -> None:
        import json
        import tempfile
        from pathlib import Path

        from mlb_manager.persistence import load_league

        lg = League.create(seed=304, user_team="SEA")
        lg.sim_days(10)
        data = lg.to_dict()
        for key in ("milestones", "milestone_keys", "awards_history", "franchise_log"):
            data.pop(key)
        for p in data["players"]:
            for key in ("injury", "injury_days", "service_years", "games_by_pos"):
                p.pop(key)
        for t in data["teams"]:
            t.pop("injured_list")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "v2.json"
            path.write_text(json.dumps({"format_version": 2, "league": data}))
            migrated = load_league(path)
        self.assertTrue(all(not p.is_injured for p in migrated.players.values()))
        vets = [p for p in migrated.players.values() if p.team and p.age >= 30]
        self.assertTrue(all(p.service_years > 0 for p in vets))
        regulars = [migrated.players[s.player_id] for s in migrated.user.lineup]
        self.assertTrue(all(p.games_by_pos for p in regulars if p.batting.g))
        migrated.sim_days(3)


class HistoryWithoutGamesTests(unittest.TestCase):
    def test_franchise_log_captures_user_transactions(self) -> None:
        lg = League.create(seed=305, user_team="NYY")
        lg.log_transaction("TRADE: NYY sends A to BOS for B.")
        lg.log_transaction("BOS signed free agent X for 3 yr / $12.00M.")
        lg.log_transaction("NYY signed free agent Y (RP, 50 OVR) for 1 yr / $0.75M.")
        lg.log_transaction("NYY signed free agent Z (SP, 66 OVR) for 3 yr / $11.50M.")
        texts = [e["text"] for e in lg.franchise_log]
        self.assertEqual(len(texts), 2)
        self.assertTrue(texts[0].startswith("TRADE"))
        self.assertIn("$11.50M", texts[1])

    def test_completed_phase_summary_without_postseason(self) -> None:
        lg = League.create(seed=306, user_team="NYY")
        lg.phase = PHASE_COMPLETE
        self.assertIn("season summary", views.season_summary(lg))
