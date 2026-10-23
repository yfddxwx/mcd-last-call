"""Behaviour locks for the timing model, the parser and the demo replay.

Run:  python -m unittest discover -s tests -v
No token, no network required.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mcd_last_call as m  # noqa: E402

MODEL = m.load_model()

NORMAL = datetime(2026, 10, 23, 15, 0)
LUNCH = datetime(2026, 10, 23, 12, 20)


class TestParsing(unittest.TestCase):
    def test_extract_payload_from_markdown_blob(self):
        raw = (
            "# API Response Information\n\n## Response Structure\n\n- **data**: x\n\n"
            '## Original Response\n\n{"success":true,"code":200,"data":{"a":1}}'
        )
        self.assertEqual(m.extract_payload(raw)["data"], {"a": 1})

    def test_extract_payload_survives_trailing_instruction_block(self):
        raw = '前言文字{"success":true,"data":[1,2]}## 展示结果时，storeCode 必须完整展示'
        self.assertEqual(m.extract_payload(raw)["data"], [1, 2])

    def test_extract_payload_passthrough_dict(self):
        self.assertEqual(m.extract_payload({"success": True})["success"], True)

    def test_parse_nutrition_table(self):
        blob = (
            "[160]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:\n"
            "  巨无霸,null,2146,513,27,26,42,961,171\n"
            "  中薯条,null,1210,289,4,12,38,165,18"
        )
        table = m.parse_nutrition_table(blob)
        self.assertEqual(table["巨无霸"]["kcal"], 513)
        self.assertEqual(table["中薯条"]["kcal"], 289)
        self.assertEqual(len(table), 2)

    def test_match_kcal_falls_back_to_containment(self):
        table = {"可乐中杯": {"kcal": 147}, "巨无霸": {"kcal": 513}}
        self.assertEqual(m.match_kcal("可乐中杯", table), 147)
        self.assertEqual(m.match_kcal("巨无霸（经典）", table), 513)
        self.assertIsNone(m.match_kcal("双层脆鸡堡", table))


class TestTiming(unittest.TestCase):
    def test_busy_factor_windows(self):
        self.assertEqual(m.busy_factor(LUNCH, MODEL), 1.4)
        self.assertEqual(m.busy_factor(NORMAL, MODEL), 1.0)
        self.assertEqual(m.busy_factor(datetime(2026, 10, 23, 18, 0), MODEL), 1.3)

    def test_prep_scales_with_items_and_busy(self):
        one = m.prep_minutes("combo", 1, LUNCH, MODEL)
        three = m.prep_minutes("combo", 3, LUNCH, MODEL)
        quiet = m.prep_minutes("combo", 3, NORMAL, MODEL)
        self.assertEqual(one, 8)          # 5.5 * 1.4 -> 7.7 -> 8
        self.assertEqual(three, 11)       # 5.5 * 1.4 * 1.3 -> 10.01 -> 11
        self.assertLess(quiet, three)

    def test_eat_minutes_clamped(self):
        self.assertEqual(m.eat_minutes(0, MODEL), 6)
        self.assertEqual(m.eat_minutes(649, MODEL), 11)
        self.assertEqual(m.eat_minutes(5000, MODEL), 20)   # ceiling, never "eat for an hour"


class TestSolver(unittest.TestCase):
    def _plan(self, budget_min=40, walk=7, deadline_hour=13):
        dead = LUNCH + timedelta(minutes=budget_min)
        return m.Plan(1, None, LUNCH, dead, budget_min, walk, "test", 5, "", None, None)

    def _cand(self, name, kcal=650, price=2500, kind="combo", items=3):
        return m.Candidate(code=name, name=name, kind=kind, item_count=items,
                           kcal_mid=kcal, kcal_lo=kcal, kcal_hi=kcal,
                           kcal_coverage="3/3", price_cents=price)

    def test_feasible_marks_late_and_computes_lot(self):
        plan = self._plan()
        m.evaluate([self._cand("A")], plan, MODEL, None, None, 1)
        # wait = max(7, 11) = 11, eat = 11, back = 7, buffer = 5  -> 34
        self.assertEqual(plan.best.need_min, 34)
        self.assertEqual(plan.verdict, "来得及")
        self.assertEqual(plan.lot, LUNCH + timedelta(minutes=40 - 34))

    def test_exact_boundary_is_not_late(self):
        plan = self._plan(budget_min=34)
        m.evaluate([self._cand("A")], plan, MODEL, None, None, 1)
        self.assertEqual(plan.best.slack_min, 0)
        self.assertEqual(plan.verdict, "卡线")     # 0 <= slack < 3

    def test_one_minute_short_is_late(self):
        plan = self._plan(budget_min=33)
        m.evaluate([self._cand("A")], plan, MODEL, None, None, 1)
        self.assertEqual(plan.verdict, "来不及")
        self.assertIsNone(plan.lot)

    def test_walk_dominates_when_store_is_far(self):
        plan = self._plan(budget_min=60, walk=25)
        m.evaluate([self._cand("A")], plan, MODEL, None, None, 1)
        self.assertEqual(plan.best.need_min, 25 + 11 + 25 + 5)

    def test_unknown_calories_never_guessed(self):
        plan = self._plan()
        ghost = m.Candidate(code="X", name="双层脆鸡堡", kind="combo", item_count=1,
                            kcal_mid=45, kcal_lo=0, kcal_hi=90, kcal_coverage="0/1",
                            price_cents=2300)
        m.evaluate([ghost], plan, MODEL, None, None, 1)
        self.assertEqual(plan.verdict, "无可行选项")
        self.assertTrue(any("热量" in n and "查不到" in n for n in plan.notes))

    def test_no_solution_suggests_relaxation(self):
        plan = m.Plan(3, None, LUNCH, LUNCH + timedelta(minutes=40), 40, 7, "test", 5, "", None, None)
        m.evaluate([self._cand("套餐", kcal=649)], plan, MODEL, 600, 30, 3)
        self.assertEqual(plan.verdict, "无可行选项")
        self.assertTrue(any("649" in n for n in plan.notes), plan.notes)

    def test_kcal_limit_filters(self):
        plan = m.Plan(3, None, LUNCH, LUNCH + timedelta(minutes=40), 40, 7, "test", 5, "", None, None)
        m.evaluate([self._cand("轻", kcal=500), self._cand("重", kcal=900)], plan, MODEL, 600, 30, 3)
        self.assertEqual(len(plan.ranked), 1)
        self.assertEqual(plan.ranked[0].name, "轻")

    def test_snack_is_not_a_lunch(self):
        plan = self._plan()
        snack = self._cand("薯条", kcal=289, price=1200, kind="snack", items=1)
        m.evaluate([snack, self._cand("套餐")], plan, MODEL, None, None, 1)
        self.assertNotIn("薯条", [c.name for c in plan.ranked])


class TestDemoReplay(unittest.TestCase):
    """The offline snapshot must produce the numbers promised in the README."""

    def test_demo_fixtures_exist(self):
        for name in ("meta.json", "query-nearby-stores.json", "query-meals.json",
                     "query-meal-detail.json", "list-nutrition-foods.json", "calculate-price.json"):
            self.assertTrue((m.DEMO_DIR / name).exists(), name)

    def test_demo_replay_is_deterministic(self):
        fetcher = m.DemoFetcher()
        now = fetcher.now()
        self.assertEqual(now.strftime("%H:%M"), "12:20")

        store = m.pick_store(fetcher.stores("", ""))
        self.assertEqual(store.code, "1420762")
        walk, _ = store.walk_minutes(None)
        self.assertEqual(walk, 7)                     # 403m * 1.25 / 75 -> 6.7 -> 7

        plan = m.Plan(1, store, now, now.replace(hour=13, minute=0), 40, walk, "test", 5, "", None, None)
        cands, _ = m.build_candidates(fetcher, store.code, 1, None)
        m.evaluate(cands, plan, MODEL, None, None, 1)

        self.assertEqual(plan.verdict, "来得及")
        # 联调后的基准：官方的「500大卡套餐」进入候选池后，最快的一单从 34 分钟
        # 降到 30 分钟，最晚下单时刻随之从 12:26 提前到 12:30。
        self.assertEqual(plan.lot.strftime("%H:%M"), "12:30")
        self.assertEqual(plan.best.need_min, 30)
        self.assertEqual(plan.best.name, "“辣”么快乐套餐")


class TestCalibration(unittest.TestCase):
    """针对联调发现的真实返回结构：data.meals 字典、字符串价格、canWithOrder 陷阱。"""

    @classmethod
    def setUpClass(cls):
        import json
        cls.menu = json.loads((m.DEMO_DIR / "query-meals.json").read_text(encoding="utf-8"))
        cls.items = m.parse_menu(cls.menu)

    def test_parse_menu_reads_name_and_price_from_meals_dict(self):
        self.assertEqual(len(self.items), 110)
        sample = next(i for i in self.items if i.code == "9900005456")
        self.assertEqual(sample.name, "麦辣鸡腿汉堡三件套")
        self.assertEqual(sample.price_cents, 3350)

    def test_current_price_is_a_string_upstream(self):
        # 官方返回的是 "33.5" 而不是 33.5，直接拿去做算术会 TypeError
        self.assertEqual(self.menu["meals"]["9900005456"]["currentPrice"], "33.5")

    def test_can_with_order_false_must_not_filter_out_combos(self):
        # 实测 14 个套餐的 canWithOrder 全是 False；按它过滤会把套餐筛没
        combos = [i for i in self.items if "套餐" in i.joined_tags]
        self.assertGreater(len(combos), 0)
        for item in combos:
            self.assertFalse(self.menu["meals"][item.code].get("canWithOrder"))
            self.assertIn(item.code, [x.code for x in self.items])

    def test_menu_has_categories_without_name_lookup(self):
        # categories 里只有 code+tags，名称必须回 meals 字典查
        first_cat_meal = self.menu["categories"][0]["meals"][0]
        self.assertEqual(set(first_cat_meal.keys()), {"code", "tags"})


class TestCallEconomy(unittest.TestCase):
    """单位：只给套餐拉详情，单品直接查营养表。"""

    def test_only_combos_trigger_detail_calls(self):
        fetcher = m.DemoFetcher()
        store = m.pick_store(fetcher.stores("", ""))
        cands, notes = m.build_candidates(fetcher, store.code, 1, None)
        detail_calls = sum(1 for t in fetcher.trace if t.startswith("query-meal-detail"))
        combos = sum(1 for c in cands if c.kind == "combo")
        self.assertEqual(detail_calls, combos)
        self.assertLess(detail_calls, m.MAX_CANDIDATES)
        self.assertTrue(any("补拉详情" in n for n in notes), notes)

    def test_settled_price_overrides_menu_price(self):
        class Skewed(m.DemoFetcher):
            def price(self, store_code, codes):
                payload = super().price(store_code, codes)
                for row in payload.get("productList") or []:
                    row["subtotal"] = 1        # 故意给一个荒谬的结算价
                return payload

        fetcher = Skewed()
        store = m.pick_store(fetcher.stores("", ""))
        cands, notes = m.build_candidates(fetcher, store.code, 1, None)
        self.assertTrue(all(c.price_cents == 1 for c in cands))
        self.assertTrue(any("结算价" in n for n in notes), notes)


class TestTakeWay(unittest.TestCase):
    def test_pick_take_way_prefers_returned_codes(self):
        payload = {"takeWayList": [{"code": "eat-in"}, {"code": "locker-out"}]}
        self.assertEqual(m.pick_take_way(payload), "locker-out")

    def test_pick_take_way_falls_back(self):
        self.assertEqual(m.pick_take_way({}), "eat-in")
        self.assertEqual(m.pick_take_way({"takeWayList": [{"code": "eat-in"}]}), "eat-in")


class TestEligibility(unittest.TestCase):
    """资格类与周边商品不能推荐给所有人。"""

    def test_ineligible_markers(self):
        for name in ("学生专享汉堡小食随心选", "开心亲子双人餐", "麦当劳限定丝巾"):
            self.assertFalse(m.is_eligible(m.MenuItem("x", name, 100, "某分类", [])), name)
        self.assertTrue(m.is_eligible(m.MenuItem("x", "麦辣鸡腿汉堡三件套", 3350, "鸡肉汉堡/卷", ["套餐"])))

    def test_student_only_items_stay_out_of_the_pool(self):
        fetcher = m.DemoFetcher()
        store = m.pick_store(fetcher.stores("", ""))
        cands, notes = m.build_candidates(fetcher, store.code, 1, None)
        names = [c.name for c in cands]
        for banned in ("学生专享", "亲子", "丝巾"):
            self.assertFalse(any(banned in n for n in names), names)
        self.assertTrue(any("已排除" in n for n in notes), notes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
