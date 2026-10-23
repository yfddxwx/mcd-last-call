#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mcd-last-call · 麦门末班车

Answer one question: given the minutes you actually have, can you still eat
this McDonald's — and what is the latest moment you may place the order?

Pure standard library. Two data sources behind one interface:

    online  -> McDonald's China MCP (https://mcp.mcd.cn, Streamable HTTP)
    --demo  -> recorded fixtures in data/demo/, identical code path downstream

Examples
--------
    MCD_MCP_TOKEN=... python mcd_last_call.py
    python mcd_last_call.py --demo --scenario 1 --deadline 13:00 --walk 8
    python mcd_last_call.py --demo --scenario 2 --minutes 20
    python mcd_last_call.py --demo --scenario 3 --minutes 40 --kcal 600 --budget 30
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from mcp_client import AuthError, McpClient, McpError

ROOT = Path(__file__).resolve().parent
DEMO_DIR = ROOT / "data" / "demo"
MODEL_DIR = ROOT / "data" / "model"

MAX_CANDIDATES = 10         # detail lookups are the expensive part of the call chain
WALK_M_PER_MIN = 75.0       # average walking pace, metres per minute
DETOUR = 1.25               # straight-line distance -> real walking distance

_J = json.JSONDecoder()

# --------------------------------------------------------------------------- #
# response parsing
# --------------------------------------------------------------------------- #

_MARKER = "## Original Response"


def _loads_at(text: str) -> dict | None:
    idx = text.find("{")
    if idx < 0:
        return None
    try:
        obj, _ = _J.raw_decode(text[idx:])
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def extract_payload(raw) -> dict:
    """mcd.cn wraps JSON in a markdown explainer; dig the payload back out."""
    if isinstance(raw, dict):
        return raw
    text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
    idx = text.find(_MARKER)
    if idx >= 0:
        found = _loads_at(text[idx + len(_MARKER):])
        if found is not None:
            return found
    found = _loads_at(text)
    if found is not None:
        return found
    return {"success": False, "message": text[:300]}


def parse_nutrition_table(blob: str) -> dict[str, dict]:
    """Turn the 160-row pipe-free CSV-ish blob into {productName: {...}}."""
    table: dict[str, dict] = {}
    for line in blob.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 9:
            continue
        name = parts[0]
        if not name or name.lower().startswith("productname") or name.startswith("["):
            continue
        try:
            kcal = int(float(parts[3]))
        except (ValueError, IndexError):
            continue
        table[name] = {
            "kcal": kcal,
            "protein": parts[4],
            "fat": parts[5],
            "carb": parts[6],
            "sodium": parts[7],
        }
    return table


# --------------------------------------------------------------------------- #
# fetchers
# --------------------------------------------------------------------------- #


def pick_take_way(price_payload: dict) -> str:
    """从 calculate-price 的 takeWayList 里挑一个取餐方式码。"""
    options = [row.get("code") for row in (price_payload or {}).get("takeWayList") or []
               if row.get("code")]
    for preferred in ("locker-out", "locker-in", "eat-in"):
        if preferred in options:
            return preferred
    return options[0] if options else "eat-in"


class OnlineFetcher:
    """Talks to the real MCP server. Optionally records a replay fixture."""

    kind = "online"

    def __init__(self, client: McpClient, record: bool = False, verbose: bool = False) -> None:
        self.client = client
        self.record = record
        self.verbose = verbose
        self.trace: list[str] = []
        self.recorded: dict[str, list] = {}
        self.last_price: dict = {}

    def _call(self, tool: str, args: dict):
        self.trace.append(f"{tool}({json.dumps(args, ensure_ascii=False)})")
        if self.verbose:
            print(f"  -> {tool}", file=sys.stderr)
        payload = extract_payload(self.client.call_tool(tool, args))
        if not payload.get("success", True):
            raise McpError(
                f"{tool} 业务失败: code={payload.get('code')} msg={payload.get('message')}"
            )
        data = payload.get("data", payload)
        if self.record:
            self.recorded.setdefault(tool, []).append({"args": args, "data": data})
        return data

    def now(self) -> datetime:
        data = self._call("now-time-info", {})
        stamp = data.get("datetime") or data.get("formatted")
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).replace(tzinfo=None)

    def stores(self, city: str, keyword: str) -> list[dict]:
        # NOTE: searchType=2 requires BOTH city and keyword, although the tool
        # schema only marks city as required. Omitting keyword returns code 600058.
        return self._call("query-nearby-stores",
                          {"beType": 1, "searchType": 2, "city": city, "keyword": keyword}) or []

    def menu(self, store_code: str) -> dict:
        return self._call("query-meals",
                          {"storeCode": store_code, "orderType": 1, "beType": 1})

    def detail(self, store_code: str, code: str) -> dict:
        return self._call("query-meal-detail",
                          {"storeCode": store_code, "orderType": 1, "beType": 1, "code": code})

    def nutrition(self) -> dict[str, dict]:
        data = self._call("list-nutrition-foods", {})
        return parse_nutrition_table(data if isinstance(data, str) else json.dumps(data))

    def price(self, store_code: str, codes: list[str]) -> dict:
        items = [{"productCode": c, "quantity": 1} for c in codes]
        data = self._call("calculate-price",
                          {"storeCode": store_code, "orderType": 1, "beType": 1, "items": items})
        self.last_price = data          # create-order 的 takeWayCode 要从这里取
        return data

    def submit_order(self, store_code: str, codes: list[str], take_way: str | None = None) -> dict:
        # takeWayCode 不是固定值：实测同一门店会返回 eat-in / locker-out，
        # 早前的 locker-in 并非有效取值，硬编码会建单失败。
        chosen = take_way or pick_take_way(self.last_price)
        items = [{"productCode": c, "quantity": 1} for c in codes]
        return self._call("create-order",
                          {"storeCode": store_code, "orderType": 1, "beType": 1,
                           "items": items, "takeWayCode": chosen, "needTableware": 0})


class DemoFetcher:
    """Replays the fixture recorded from a real session. Same downstream code."""

    kind = "demo"

    def __init__(self, demo_dir: Path = DEMO_DIR) -> None:
        self.dir = demo_dir
        self.trace: list[str] = []
        self.meta = self._json("meta.json", {})
        self._stores = self._json("query-nearby-stores.json", [])
        self._menu = self._json("query-meals.json", {})
        self._details = self._json("query-meal-detail.json", {})
        self._nutrition = self._json("list-nutrition-foods.json", "")
        self._price = self._json("calculate-price.json", {})

    def _json(self, name: str, default):
        path = self.dir / name
        if not path.exists():
            raise McpError(
                f"缺少离线快照 {path}。请先联网录制一次：\n"
                f"  python mcd_last_call.py --record"
            )
        return json.loads(path.read_text(encoding="utf-8"))

    def _note(self, msg: str) -> None:
        self.trace.append(msg)

    def now(self) -> datetime:
        self._note("now-time-info")
        stamp = self.meta.get("story_now")
        if not stamp:
            raise McpError("meta.json 缺少 story_now")
        return datetime.fromisoformat(stamp)

    def stores(self, city: str, keyword: str) -> list[dict]:
        self._note("query-nearby-stores")
        return self._stores

    def menu(self, store_code: str) -> dict:
        self._note("query-meals")
        return self._menu

    def detail(self, store_code: str, code: str) -> dict:
        self._note("query-meal-detail")
        data = self._details.get(code)
        if data is None:
            raise McpError(f"快照里没有 {code} 的详情")
        return data

    def nutrition(self) -> dict[str, dict]:
        self._note("list-nutrition-foods")
        blob = self._nutrition if isinstance(self._nutrition, str) else json.dumps(self._nutrition)
        return parse_nutrition_table(blob)

    def price(self, store_code: str, codes: list[str]) -> dict:
        self._note("calculate-price")
        return self._price

    def submit_order(self, store_code: str, codes: list[str], take_way: str = "locker-in") -> dict:
        raise McpError("离线模式不建单。去掉 --demo 才会真正调用 create-order。")


# --------------------------------------------------------------------------- #
# domain model
# --------------------------------------------------------------------------- #


@dataclass
class Store:
    code: str
    name: str
    address: str
    distance_m: float | None
    open_at: str | None
    close_at: str | None
    business_status: bool | None

    def walk_minutes(self, override: float | None = None) -> tuple[int, str]:
        if override is not None:
            return max(1, int(round(override))), "手动指定"
        if not self.distance_m or self.distance_m <= 0:
            return 6, "距离缺失，按默认 400m 估算（置信度低）"
        minutes = self.distance_m * DETOUR / WALK_M_PER_MIN
        return max(1, math.ceil(minutes)), f"{int(self.distance_m)}m × 绕行 1.25 ÷ 75m/min"


@dataclass
class Candidate:
    code: str
    name: str
    kind: str
    item_count: int
    kcal_mid: int
    kcal_lo: int
    kcal_hi: int
    kcal_coverage: str
    price_cents: int
    prep_min: int = 0
    eat_min: int = 0
    need_min: int = 0
    slack_min: int = 0
    ready_at: str = ""

    @property
    def price_yuan(self) -> str:
        return f"¥{self.price_cents / 100:.2f}"

    def to_dict(self) -> dict:
        return {
            "code": self.code, "name": self.name, "kind": self.kind,
            "items": self.item_count, "kcal": [self.kcal_lo, self.kcal_mid, self.kcal_hi],
            "kcal_coverage": self.kcal_coverage, "price_cents": self.price_cents,
            "prep_min": self.prep_min, "eat_min": self.eat_min,
            "need_min": self.need_min, "slack_min": self.slack_min,
            "ready_at": self.ready_at,
        }


@dataclass
class Plan:
    scenario: int
    store: Store
    now: datetime
    deadline: datetime
    budget_min: int
    walk_min: int
    walk_basis: str
    buffer_min: int
    verdict: str
    lot: datetime | None
    best: Candidate | None
    ranked: list[Candidate] = field(default_factory=list)
    rejected: list[Candidate] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    trace: list[str] = field(default_factory=list)
    demo: bool = False


# --------------------------------------------------------------------------- #
# timing model
# --------------------------------------------------------------------------- #

BASE_PREP = {"combo": 5.5, "burger": 4.0, "snack": 3.0, "drink": 2.0, "dessert": 2.0, "other": 4.0}


def _minutes_of_day(text: str) -> int:
    hour, _, minute = str(text).partition(":")
    return int(hour) * 60 + int(minute or 0)


def busy_factor(moment: datetime, model: dict | None = None) -> float:
    windows = (model or {}).get("busy") or [
        {"start": "11:30", "end": "13:00", "factor": 1.4},
        {"start": "17:30", "end": "19:00", "factor": 1.3},
    ]
    current = moment.hour * 60 + moment.minute
    factor = 1.0
    for window in windows:
        if _minutes_of_day(window["start"]) <= current < _minutes_of_day(window["end"]):
            factor = max(factor, float(window.get("factor", 1.0)))
    return factor


def prep_minutes(kind: str, item_count: int, moment: datetime, model: dict) -> int:
    base = model.get("base", BASE_PREP).get(kind, BASE_PREP["other"])
    raw = base * busy_factor(moment, model) * (1 + 0.15 * max(0, item_count - 1))
    return max(2, math.ceil(raw))


def eat_minutes(kcal_mid: int, model: dict | None = None) -> int:
    rate = float((model or {}).get("eat_kcal_per_min", 60)) or 60.0
    floor = int((model or {}).get("eat_min_floor", 6))
    ceiling = int((model or {}).get("eat_min_ceiling", 20))
    return max(floor, min(ceiling, int(round(kcal_mid / rate))))


def classify(category: str, detail: dict) -> str:
    if detail.get("rounds"):
        return "combo"
    name = detail.get("name", "") + " " + category
    if any(k in name for k in ("可乐", "雪碧", "咖啡", "茶", "牛奶", "朱古力")):
        return "drink"
    if any(k in name for k in ("冰淇淋", "新地", "麦旋风", "派")):
        return "dessert"
    if any(k in name for k in ("薯条", "鸡块", "鸡翅", "玉米", "小食")):
        return "snack"
    if "堡" in name:
        return "burger"
    return "other"


_NOISE = re.compile(r"[\s\-—_·•()（）\[\]【】\"“”'’]")


def _normalize_name(text: str) -> str:
    """去掉空格、连字符、括号等噪声，让「麦辣鸡翅2块」能对上「麦辣鸡翅-2块」。"""
    return _NOISE.sub("", text or "")


def match_kcal(name: str, table: dict[str, dict]) -> int | None:
    if name in table:
        return table[name]["kcal"]
    stripped = re.sub(r"[（(].*?[)）]", "", name).strip()
    if stripped in table:
        return table[stripped]["kcal"]

    target = _normalize_name(name)
    if not target:
        return None
    normalized = {_normalize_name(k): v for k, v in table.items() if k}
    if target in normalized:
        return normalized[target]["kcal"]
    hits = [k for k in normalized if k and (k in target or target in k)]
    if hits:
        return normalized[max(hits, key=len)]["kcal"]
    return None


def join_items(detail: dict) -> list[tuple[str, int]]:
    """Flatten a combo detail into [(item name, qty)] using the default choices."""
    items: list[tuple[str, int]] = []
    for rnd in detail.get("rounds") or []:
        chosen = [c for c in rnd.get("choices", []) if (c.get("quantity") or 0) > 0]
        for choice in chosen:
            items.append((choice.get("name", ""), int(choice.get("quantity") or 1)))
    if not items and detail.get("name"):
        items.append((detail["name"], 1))
    return items


# --------------------------------------------------------------------------- #
# solver
# --------------------------------------------------------------------------- #


@dataclass
class MenuItem:
    code: str
    name: str
    price_cents: int
    category: str
    tags: list[str]

    @property
    def joined_tags(self) -> str:
        return " ".join(self.tags)


def parse_menu(menu: dict) -> list[MenuItem]:
    """query-meals 的 data 由两部分拼成，这是联调实测出来的结构：

        data.categories[] = {name, meals: [{code, tags}]}    # 只有顺序和标签
        data.meals{code}  = {name, currentPrice, originalPrice, canWithOrder, image}

    注意 ``canWithOrder`` 不能当"可售"用：实测 14 个套餐全部是 false，
    按它过滤会把所有套餐筛没，所以这里刻意忽略该字段。
    """
    catalogue = menu.get("meals") or {}
    items: list[MenuItem] = []
    seen: set[str] = set()
    for category in menu.get("categories") or []:
        cat_name = (category.get("name") or "").replace("\n", "")
        for entry in category.get("meals") or []:
            code = entry.get("code")
            if not code or code in seen:
                continue
            seen.add(code)
            meta = catalogue.get(code) or {}
            try:
                price_cents = int(round(float(meta.get("currentPrice") or 0) * 100))
            except (TypeError, ValueError):
                price_cents = 0
            items.append(
                MenuItem(code=code, name=meta.get("name") or code,
                         price_cents=price_cents, category=cat_name,
                         tags=list(entry.get("tags") or []))
            )
    return items


# 名字里带这些词的 SKU 不能直接推荐给所有人：需要学生/亲子身份，或者根本不是餐品。
INELIGIBLE_MARKERS = ("学生专享", "亲子", "儿童", "玩具", "丝巾", "周边", "礼品卡", "兑换券")


def is_eligible(item: "MenuItem") -> bool:
    return not any(marker in item.name for marker in INELIGIBLE_MARKERS)


def _relevance(item: MenuItem, scenario: int, kcal_cap: int | None) -> float:
    tags = item.joined_tags
    score = 0.0
    if "套餐" in tags:
        score += 5          # a real meal beats a side dish for a lunch decision
    if "单品" in tags:
        score += 1
    if any(k in tags for k in ("爆款", "人气", "经典")):
        score += 1.5
    if "精选" in tags:
        score += 0.5
    if "随心配" in tags:
        score += 1.0
    if "单人餐" in item.category:
        score += 1
    if any(k in item.category for k in ("多人", "拼盘")):
        score -= 3          # too much food for a 20 minute window
    if "大卡" in item.category:
        # 官方「500大卡套餐」分类，实测 394/412/476/495 kcal：
        # 天生就是"又想快、又不想吃太撑"的那一档，任何场景都该进候选池。
        score += 3.5
        if kcal_cap:
            score += 4.5    # 热量受限时进一步前置，否则会被普通套餐挤出去
    if scenario == 2 and 0 < item.price_cents <= 2000:
        score += 0.5        # 赶时间的人通常也不想点贵的
    return score


def build_candidates(fetcher, store_code: str, scenario: int = 1,
                     kcal_cap: int | None = None) -> tuple[list[Candidate], list[str]]:
    notes: list[str] = []
    menu = fetcher.menu(store_code)
    items = parse_menu(menu)
    if not items:
        raise McpError("门店菜单为空（query-meals 返回 0 个 SKU），可能已打烊或该门店不支持自取")

    # 同分时先选便宜的那份：既符合"午休只想快速解决"的直觉，
    # 也避免分类名额被贵的套餐占满（曾因此漏掉最快的那一单）。
    ineligible = [it for it in items if not is_eligible(it)]
    if ineligible:
        notes.append(
            f"{len(ineligible)} 个 SKU 需要资格验证或不是餐品，已排除："
            + "、".join(it.name for it in ineligible[:3])
        )
    items = [it for it in items if is_eligible(it)]

    ranked = sorted(enumerate(items),
                    key=lambda pair: (-_relevance(pair[1], scenario, kcal_cap),
                                      pair[1].price_cents or 10 ** 9, pair[0]))
    picked: list[MenuItem] = []
    per_category: dict[str, int] = {}
    for _, item in ranked:
        if per_category.get(item.category, 0) >= 2:
            continue        # keep the shortlist diverse instead of one category
        per_category[item.category] = per_category.get(item.category, 0) + 1
        picked.append(item)
        if len(picked) >= MAX_CANDIDATES:
            break
    notes.append(f"菜单 {len(items)} 个 SKU（含名称与现价），按标签/分类预筛 {len(picked)} 个")

    nutrition = fetcher.nutrition()
    candidates: list[Candidate] = []
    detail_calls = 0
    for item in picked:
        # 只有套餐需要拉详情还原构成；单品直接用菜单名查营养表，省调用
        # 不能只看 tags：官方「500大卡套餐」分类下的 SKU 标签是"粉丝之选"或空，
        # 名字里带"套餐"的也必须拉详情，否则热量算不出来会被误排除。
        needs_detail = "套餐" in item.joined_tags or "套餐" in item.name
        detail: dict = {}
        if needs_detail:
            try:
                detail = fetcher.detail(store_code, item.code)
                detail_calls += 1
            except McpError as exc:
                notes.append(f"{item.name} 详情拉取失败，已跳过（{exc}）")
                continue

        composition = join_items(detail) if detail else [(item.name, 1)]
        kind = classify(item.category, detail or {"name": item.name})
        total_lo = 0
        missing = 0
        for item_name, qty in composition:
            kcal = match_kcal(item_name, nutrition)
            if kcal is None:
                missing += 1
            else:
                total_lo += kcal * qty
        total_items = sum(q for _, q in composition)
        coverage = f"{total_items - missing}/{total_items}"
        hi = total_lo + 90 * missing
        mid = total_lo if missing == 0 else (total_lo + hi) // 2
        candidates.append(
            Candidate(
                code=item.code,
                name=detail.get("name") or item.name,
                kind=kind,
                item_count=max(1, total_items),
                kcal_mid=max(0, mid),
                kcal_lo=total_lo,
                kcal_hi=hi,
                kcal_coverage=coverage,
                price_cents=item.price_cents,
            )
        )

    if detail_calls:
        notes.append(f"其中 {detail_calls} 个套餐补拉详情，其余单品直接查营养表")

    if candidates:
        priced = fetcher.price(store_code, [c.code for c in candidates])
        subtotals = {str(row.get("productCode")): row.get("subtotal") or 0
                     for row in priced.get("productList") or []}
        mismatched = 0
        for cand in candidates:
            settled = int(subtotals.get(cand.code) or 0)
            if settled:
                if cand.price_cents and abs(settled - cand.price_cents) > 1:
                    mismatched += 1
                cand.price_cents = settled
        if not any(c.price_cents for c in candidates):
            share = int(priced.get("price") or 0) // max(1, len(candidates))
            notes.append("calculate-price 未返回单项小计，价格按总价均摊（仅供参考）")
            for cand in candidates:
                cand.price_cents = share
        elif mismatched:
            notes.append(f"{mismatched} 个餐品菜单价与结算价不一致，已以 calculate-price 为准")
    return candidates, notes


def evaluate(candidates: list[Candidate], plan: Plan, model: dict,
             kcal_limit: int | None, money_limit: int | None, scenario: int) -> None:
    for cand in candidates:
        cand.prep_min = prep_minutes(cand.kind, cand.item_count, plan.now, model)
        cand.eat_min = eat_minutes(cand.kcal_mid, model)
        wait = max(plan.walk_min, cand.prep_min)
        cand.need_min = wait + cand.eat_min + plan.walk_min + plan.buffer_min
        cand.slack_min = plan.budget_min - cand.need_min
        cand.ready_at = (plan.now + timedelta(minutes=wait)).strftime("%H:%M")

    pool = list(candidates)

    unknown = [c for c in pool if c.kcal_lo <= 0]
    if unknown:
        pool = [c for c in pool if c.kcal_lo > 0]
        plan.notes.append(
            f"{len(unknown)} 个候选热量在官方营养表里查不到，已排除（不给假数字）："
            + "、".join(c.name for c in unknown[:3])
        )

    if scenario in (1, 3):
        # a lunch is a burger/combo, not a 289 kcal fries that happens to clear the bar
        main = [c for c in pool if c.kind in ("combo", "burger") or c.kcal_mid >= 350]
        if main and len(main) < len(pool):
            plan.notes.append(f"{len(pool) - len(main)} 个候选是零嘴/饮料，非正餐，不进推荐")
            pool = main

    if kcal_limit:
        over = [c for c in pool if c.kcal_mid > kcal_limit]
        pool = [c for c in pool if c.kcal_mid <= kcal_limit]
        if over:
            plan.notes.append(f"{len(over)} 个选项超热量线（>{kcal_limit} kcal）已排除")
    if scenario == 3 and money_limit:
        over = [c for c in pool if c.price_cents > money_limit * 100]
        pool = [c for c in pool if c.price_cents <= money_limit * 100]
        if over:
            plan.notes.append(f"{len(over)} 个选项超预算（>¥{money_limit}）已排除")

    kept = {id(c) for c in pool}
    plan.rejected = [c for c in candidates if id(c) not in kept]
    if not pool:
        plan.verdict = "无可行选项"
        plan.best = None
        plan.lot = None
        plan.ranked = []
        _suggest_relaxation(plan, candidates, kcal_limit, money_limit, scenario)
        return

    if scenario == 2:
        pool.sort(key=lambda c: (c.prep_min, c.eat_min, -c.slack_min))
    elif scenario == 3:
        pool.sort(key=lambda c: (c.price_cents, c.kcal_mid, -c.slack_min))
    else:
        pool.sort(key=lambda c: (-c.slack_min, c.need_min))
    plan.ranked = pool

    feasible = [c for c in pool if c.slack_min >= 0]
    plan.best = feasible[0] if feasible else pool[0]

    if feasible:
        best_slack = max(c.slack_min for c in feasible)
        plan.verdict = "来得及" if best_slack >= 3 else "卡线"
        best = max(feasible, key=lambda c: c.slack_min)
        lot_back = plan.buffer_min + max(plan.walk_min, best.prep_min) + best.eat_min + plan.walk_min
        plan.lot = plan.deadline - timedelta(minutes=lot_back)
    else:
        plan.verdict = "来不及"


def _suggest_relaxation(plan: Plan, candidates: list[Candidate],
                        kcal_limit: int | None, money_limit: float | None,
                        scenario: int) -> None:
    """When nothing fits, say precisely which constraint to loosen and by how much."""
    pool = [c for c in candidates if c.kcal_lo > 0]
    if not pool:
        return

    def violation(cand: Candidate) -> float:
        total = 0.0
        if kcal_limit and cand.kcal_mid > kcal_limit:
            total += (cand.kcal_mid - kcal_limit) / max(1, kcal_limit)
        if scenario == 3 and money_limit and cand.price_cents > money_limit * 100:
            total += (cand.price_cents - money_limit * 100) / max(1, money_limit * 100)
        if cand.slack_min < 0:
            total += (-cand.slack_min) / max(1, plan.budget_min)
        return total

    failed = [c for c in pool if violation(c) > 0]
    if not failed:
        # everything that fits the constraints is a snack; the blocker is "no real meal"
        snack = min(pool, key=lambda c: c.price_cents)
        plan.notes.insert(
            0,
            f"能同时满足时间/热量/预算的只有零嘴（「{snack.name}」{snack.kcal_mid} kcal、"
            f"{snack.price_yuan}），没有正餐选项 —— 要么放宽热量线，要么这顿改成垫一口",
        )
        return

    nearest = min(failed, key=violation)
    plan.best = nearest
    hints: list[str] = []
    if kcal_limit and nearest.kcal_mid > kcal_limit:
        hints.append(f"热量放到 {nearest.kcal_mid} kcal")
    if scenario == 3 and money_limit and nearest.price_cents > money_limit * 100:
        hints.append(f"预算放到 ¥{nearest.price_cents / 100:.2f}")
    if nearest.slack_min < 0:
        hints.append(f"时间多给 {-nearest.slack_min} 分钟")
    if hints:
        plan.notes.insert(0, f"最接近的一单是「{nearest.name}」：" + "，".join(hints) + " 就能吃上")


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #


def dw(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - dw(text))


_COLORS = {"ok": "\033[32m", "warn": "\033[33m", "bad": "\033[31m", "dim": "\033[2m", "b": "\033[1m"}


def paint(text: str, style: str, enabled: bool) -> str:
    if not enabled:
        return text
    return f"{_COLORS.get(style, '')}{text}\033[0m"


def render(plan: Plan, model_note: str, color: bool) -> str:
    width = 74
    line = "─" * width
    out: list[str] = []

    title = f" 麦门末班车 · 场景 {plan.scenario} · " + {
        1: "还来不来得及", 2: "只剩几分钟", 3: "时间/热量/预算 三重约束"
    }.get(plan.scenario, "")

    out.append(line)
    out.append(paint(title, "b", color))
    out.append(line)
    if plan.demo:
        out.append(paint(" 离线回放（快照数据）：去掉 --demo 并配置 MCD_MCP_TOKEN 即为真实门店数据", "dim", color))

    walk_note = f"{plan.walk_min} 分钟（{plan.walk_basis}）"
    out.append(f" 门店    {plan.store.name}")
    out.append(f"         {plan.store.address}")
    out.append(f" 距离    {walk_note}")
    out.append(f" 现在    {plan.now.strftime('%H:%M')}      截止 {plan.deadline.strftime('%H:%M')}"
               f"      可用 {plan.budget_min} 分钟")
    out.append(line)

    if plan.verdict == "来得及":
        badge = paint("✓ 来得及", "ok", color)
    elif plan.verdict == "卡线":
        badge = paint("△ 卡线", "warn", color)
    elif plan.verdict == "来不及":
        badge = paint("✗ 来不及", "bad", color)
    else:
        badge = paint("· 无可行选项", "warn", color)

    if plan.verdict == "无可行选项":
        out.append(f" 判定    {badge}")
    elif plan.best and plan.verdict == "来不及":
        out.append(f" 判定    {badge}      最快的一单也要 {plan.best.need_min} 分钟，"
                   f"还差 {plan.best.need_min - plan.budget_min} 分钟")
    elif plan.best:
        out.append(f" 判定    {badge}      最快的一单 {plan.best.need_min} 分钟，"
                   f"余 {plan.best.slack_min} 分钟")
    else:
        out.append(f" 判定    {badge}")

    if plan.lot:
        out.append(f" 最晚下单时刻  {paint(plan.lot.strftime('%H:%M'), 'b', color)}"
                   f"    ← 晚于这个点，最早的一单也赶不上")
    elif plan.verdict == "来不及" and plan.budget_min > 0 and plan.best:
        out.append(f" 最晚下单时刻  {paint('已过', 'bad', color)}"
                   f"    想赶上，需要再多 {plan.best.need_min - plan.budget_min} 分钟，或换更近的门店")
    elif plan.budget_min <= 0:
        out.append(f" 最晚下单时刻  {paint('已过', 'bad', color)}    截止时刻已经过去，本轮没有可用时间")

    out.append(line)
    if plan.ranked:
        header = {1: "推荐（按稳妥程度）", 2: "推荐（按到手速度）", 3: "推荐（按价格）"}.get(plan.scenario, "推荐")
        out.append(f" {header}")
        for idx, cand in enumerate(plan.ranked[:3], 1):
            flag = "" if cand.slack_min >= 0 else "  " + paint("赶不上", "bad", color)
            kcal = f"{cand.kcal_mid}kcal"
            if cand.kcal_lo != cand.kcal_hi:
                kcal = f"{cand.kcal_lo}-{cand.kcal_hi}kcal"
            out.append(f" {idx}. {pad(cand.name, 24)} {pad(cand.price_yuan, 9)}"
                       f" {pad(kcal, 13)} 到手约 {cand.ready_at}{flag}")
            breakdown = (
                f"max(步行{plan.walk_min}, 出餐{cand.prep_min}) + 吃{cand.eat_min}"
                f" + 返程{plan.walk_min} + 缓冲{plan.buffer_min} = {cand.need_min} 分钟"
            )
            out.append("    " + paint(breakdown, "dim", color))
        out.append(line)

    out.append(f" 口径    {model_note}")
    if plan.notes:
        for note in plan.notes[:5]:
            out.append(f"         · {note}")
    if plan.trace:
        counts: dict[str, int] = {}
        for item in plan.trace:
            name = item.split("(")[0].strip()
            counts[name] = counts.get(name, 0) + 1
        seq = " → ".join(f"{k}×{v}" if v > 1 else k for k, v in counts.items())
        out.append(f" 调用    {len(plan.trace)} 次 → {seq}")
    out.append(line)
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #


def load_model() -> dict:
    path = MODEL_DIR / "prep_time.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"base": BASE_PREP}


def pick_store(stores: list[dict]) -> Store:
    if not stores:
        raise McpError("附近没有查到门店，换个关键词试试")
    raw = min(stores, key=lambda s: s.get("distance") or 10**9)
    return Store(
        code=str(raw.get("storeCode")),
        name=raw.get("storeName") or "未命名门店",
        address=raw.get("address") or "",
        distance_m=raw.get("distance"),
        open_at=raw.get("businessStartTime"),
        close_at=raw.get("businessEndTime"),
        business_status=raw.get("businessStatus"),
    )


def resolve_deadline(now: datetime, args) -> datetime:
    if args.minutes:
        return now + timedelta(minutes=args.minutes)
    if args.deadline:
        hour, _, minute = args.deadline.partition(":")
        target = now.replace(hour=int(hour), minute=int(minute or 0), second=0, microsecond=0)
        return target
    return now + timedelta(minutes=40)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        prog="mcd-last-call",
        description="算清楚这顿麦当劳还来不来得及，以及最晚几点必须下单。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--scenario", type=int, default=1, choices=[1, 2, 3])
    parser.add_argument("--demo", action="store_true", help="离线回放 data/demo 快照，无需 Token")
    parser.add_argument("--record", action="store_true", help="联网跑一次并把响应录成离线快照")
    parser.add_argument("--deadline", help="截止时刻，如 13:00")
    parser.add_argument("--minutes", type=int, help="从现在起可用分钟数")
    parser.add_argument("--walk", type=float, help="手动指定单程步行分钟数")
    parser.add_argument("--kcal", type=int, help="热量上限（kcal）")
    parser.add_argument("--budget", type=float, help="预算上限（元）")
    parser.add_argument("--buffer", type=int, default=5, help="取餐/找店缓冲分钟，默认 5")
    parser.add_argument("--city", default="深圳")
    parser.add_argument("--keyword", default="科技园")
    parser.add_argument("--store", help="直接指定 storeCode，跳过门店搜索")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument("--order", action="store_true", help="真的建单（返回支付链接，不代付）")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    color = (not args.no_color) and sys.stdout.isatty()
    model = load_model()

    try:
        if args.demo:
            fetcher = DemoFetcher()
        else:
            fetcher = OnlineFetcher(McpClient(verbose=args.verbose), record=args.record,
                                    verbose=args.verbose)

        now = fetcher.now()
        stores = fetcher.stores(args.city, args.keyword)
        if args.store:
            chosen = next((s for s in stores if str(s.get("storeCode")) == args.store), None)
            store = pick_store([chosen] if chosen else stores)
        else:
            store = pick_store(stores)

        walk_min, walk_basis = store.walk_minutes(args.walk)

        plan = Plan(
            scenario=args.scenario,
            store=store,
            now=now,
            deadline=resolve_deadline(now, args),
            budget_min=0,
            walk_min=walk_min,
            walk_basis=walk_basis,
            buffer_min=args.buffer,
            verdict="",
            lot=None,
            best=None,
            demo=args.demo,
        )
        plan.budget_min = int((plan.deadline - plan.now).total_seconds() // 60)

        candidates, build_notes = build_candidates(fetcher, store.code, args.scenario, args.kcal)
        plan.notes.extend(build_notes)
        evaluate(candidates, plan, model, args.kcal, args.budget, args.scenario)

        if plan.budget_min <= 0:
            plan.verdict = "来不及"
            plan.lot = None
            plan.notes.append("截止时刻已经过去，时间预算为 0")

        plan.trace = getattr(fetcher, "trace", [])

        if args.order and plan.best:
            if args.demo:
                raise McpError("离线模式不建单")
            order = fetcher.submit_order(store.code, [plan.best.code])
            plan.notes.append(f"已建单：{order.get('orderNo') or order.get('orderId') or '见返回'}")

        if args.record and isinstance(fetcher, OnlineFetcher):
            save_fixtures(fetcher, store, now)
            plan.notes.append("已录制离线快照 → data/demo/")

        if args.json:
            print(json.dumps({
                "scenario": plan.scenario,
                "demo": plan.demo,
                "now": plan.now.isoformat(timespec="minutes"),
                "deadline": plan.deadline.isoformat(timespec="minutes"),
                "budget_min": plan.budget_min,
                "store": {"code": store.code, "name": store.name,
                          "address": store.address, "distance_m": store.distance_m},
                "walk_min": plan.walk_min,
                "buffer_min": plan.buffer_min,
                "verdict": plan.verdict,
                "last_order_time": plan.lot.strftime("%H:%M") if plan.lot else None,
                "best": plan.best.to_dict() if plan.best else None,
                "ranked": [c.to_dict() for c in plan.ranked],
                "notes": plan.notes,
                "mcp_trace": plan.trace,
            }, ensure_ascii=False, indent=2))
        else:
            print(render(plan, model.get("note", "出餐时长为结构估算，非门店实时排队数据"), color))

        return 0 if plan.verdict in ("来得及", "卡线") else 2

    except AuthError as exc:
        print(f"[Token 错误] {exc}", file=sys.stderr)
        return 3
    except McpError as exc:
        print(f"[MCP 错误] {exc}", file=sys.stderr)
        print("提示：没有 Token 或接口不可用时，用 --demo 跑离线快照。", file=sys.stderr)
        return 4


def _scrub(value):
    """脱敏：递归去掉可追溯的请求标识与凭证类字段。"""
    drop = {"traceId", "trace_id", "token", "accessToken", "phone", "mobile", "openId"}
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items() if k not in drop}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def save_fixtures(fetcher: OnlineFetcher, store: Store, now: datetime) -> None:
    """Persist the recorded responses so --demo replays the exact same data.

    详情是**累积合并**的：不同场景会选中不同套餐（场景 3 会优先官方的
    「500大卡套餐」分类），必须把多次 --record 的结果合并，否则离线回放会在
    "快照里没有这个 code 的详情"处断掉。
    """
    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    recorded = fetcher.recorded

    def first(tool: str, default):
        rows = recorded.get(tool) or []
        return _scrub(rows[0]["data"]) if rows else default

    (DEMO_DIR / "meta.json").write_text(json.dumps({
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
        "story_now": now.replace(hour=12, minute=20, second=0, microsecond=0).isoformat(),
        "server_now": now.isoformat(timespec="seconds"),
        "city": None,
        "store": {"code": store.code, "name": store.name, "address": store.address,
                  "distance": store.distance_m},
        "note": "story_now 是回放时使用的叙事时间；server_now 是真实采集时间",
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    (DEMO_DIR / "query-nearby-stores.json").write_text(
        json.dumps(first("query-nearby-stores", []), ensure_ascii=False, indent=2), encoding="utf-8")
    (DEMO_DIR / "query-meals.json").write_text(
        json.dumps(first("query-meals", {}), ensure_ascii=False, indent=2), encoding="utf-8")
    (DEMO_DIR / "list-nutrition-foods.json").write_text(
        json.dumps(first("list-nutrition-foods", ""), ensure_ascii=False, indent=2), encoding="utf-8")
    (DEMO_DIR / "calculate-price.json").write_text(
        json.dumps(first("calculate-price", {}), ensure_ascii=False, indent=2), encoding="utf-8")

    detail_path = DEMO_DIR / "query-meal-detail.json"
    merged: dict = {}
    if detail_path.exists():
        try:
            merged = json.loads(detail_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            merged = {}
    for row in recorded.get("query-meal-detail", []):
        code = row["args"].get("code")
        if code:
            merged[code] = _scrub(row["data"])
    detail_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[record] 详情快照累计 {len(merged)} 个 SKU", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
