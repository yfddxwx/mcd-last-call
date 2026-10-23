#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""纯计算器：JSON 进，结论出。不联网、不需要 Token。

为什么单独做这个：Agent 自己就能调麦当劳 MCP 拿数据（连接器已经提供了工具），
但"到底来不来得及、最晚几点下单"这件事需要确定性计算，不能让模型口算。
把求解逻辑抽成纯函数后，无论数据是 Agent 拉的、脚本拉的还是离线快照，
算出来的结论都一样。

用法：
    python scripts/solve.py payload.json
    cat payload.json | python scripts/solve.py

输入（字段都可选，缺省按下述默认）：
{
  "now": "2026-10-23T12:20:00",      // 必填，来自 now-time-info 的 data.datetime
  "deadline": "13:00",               // 与 budget_min 二选一
  "budget_min": 40,                  // 从 now 起的可用分钟数
  "walk_min": 7,                     // 单程步行分钟；缺省按 distance_m 估
  "distance_m": 403,                 // 没有 walk_min 时用它算
  "buffer_min": 5,                   // 找店/取餐/电梯的缓冲，默认 5
  "scenario": 1,                     // 1 来得及吗 / 2 只剩几分钟 / 3 三重约束
  "kcal_cap": 600,                   // 场景 3 用
  "money_cap": 30,                   // 场景 3 用，单位元
  "store": {"code": "...", "name": "...", "address": "..."},
  "candidates": [                    // 必填，至少 1 个
    {"code": "9900011123", "name": "“辣”么快乐套餐", "kind": "combo",
     "items": 3, "kcal": 394, "price_cents": 2400}
  ]
}

输出：与 `mcd_last_call.py --json` 同构，可直接交给用户看。
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mcd_last_call as core  # noqa: E402

VALID_KINDS = {"combo", "burger", "snack", "drink", "dessert", "other"}


def _parse_now(value) -> datetime:
    text = str(value).replace("Z", "+00:00")
    moment = datetime.fromisoformat(text)
    return moment.replace(tzinfo=None)


def _resolve_deadline(now: datetime, payload: dict) -> datetime:
    if payload.get("deadline"):
        raw = str(payload["deadline"])
        if "T" in raw or "-" in raw:
            target = _parse_now(raw)
        else:
            hour, _, minute = raw.partition(":")
            target = now.replace(hour=int(hour), minute=int(minute or 0), second=0, microsecond=0)
        if target < now:                      # 只给了 HH:MM 且已过 → 视为次日
            target += timedelta(days=1)
        return target
    minutes = int(payload.get("budget_min") or 40)
    return now + timedelta(minutes=minutes)


def _walk(payload: dict) -> tuple[int, str]:
    if payload.get("walk_min") is not None:
        return max(1, int(round(float(payload["walk_min"])))), "调用方指定"
    distance = payload.get("distance_m")
    if not distance:
        return 6, "距离缺失，按默认 400m 估算（置信度低）"
    minutes = float(distance) * core.DETOUR / core.WALK_M_PER_MIN
    return max(1, math.ceil(minutes)), f"{int(distance)}m × 绕行 {core.DETOUR} ÷ {core.WALK_M_PER_MIN:.0f}m/min"


def _candidate(row: dict) -> core.Candidate:
    kcal_raw = row.get("kcal")
    kcal_lo = int(row.get("kcal_lo") if row.get("kcal_lo") is not None else (kcal_raw or 0))
    kcal_hi = int(row.get("kcal_hi") if row.get("kcal_hi") is not None else (kcal_raw or 0))
    kcal_mid = int(kcal_raw) if kcal_raw is not None else (kcal_lo + kcal_hi) // 2
    kind = str(row.get("kind") or "other")
    return core.Candidate(
        code=str(row.get("code") or row.get("name") or "?"),
        name=str(row.get("name") or row.get("code") or "未命名"),
        kind=kind if kind in VALID_KINDS else "other",
        item_count=max(1, int(row.get("items") or 1)),
        kcal_mid=max(0, kcal_mid),
        kcal_lo=max(0, kcal_lo),
        kcal_hi=max(0, kcal_hi),
        kcal_coverage=str(row.get("kcal_coverage") or "1/1"),
        price_cents=int(row.get("price_cents") or 0),
    )


def solve(payload: dict) -> dict:
    if not payload.get("now"):
        raise ValueError("缺少 now：请填 now-time-info 返回的 data.datetime")
    rows = payload.get("candidates") or []
    if not rows:
        raise ValueError("缺少 candidates：至少给 1 个候选餐品")

    now = _parse_now(payload["now"])
    deadline = _resolve_deadline(now, payload)
    walk_min, walk_basis = _walk(payload)
    scenario = int(payload.get("scenario") or 1)
    buffer_min = int(payload.get("buffer_min") if payload.get("buffer_min") is not None else 5)

    store_raw = payload.get("store") or {}
    store = core.Store(
        code=str(store_raw.get("code") or ""),
        name=str(store_raw.get("name") or "未指定门店"),
        address=str(store_raw.get("address") or ""),
        distance_m=store_raw.get("distance_m") or payload.get("distance_m"),
        open_at=store_raw.get("open_at"),
        close_at=store_raw.get("close_at"),
        business_status=store_raw.get("business_status"),
    )

    plan = core.Plan(
        scenario=scenario, store=store, now=now, deadline=deadline,
        budget_min=max(0, int((deadline - now).total_seconds() // 60)),
        walk_min=walk_min, walk_basis=walk_basis, buffer_min=buffer_min,
        verdict="", lot=None, best=None,
    )

    candidates = [_candidate(row) for row in rows]
    kcal_cap = payload.get("kcal_cap")
    money_cap = payload.get("money_cap")
    core.evaluate(candidates, plan, core.load_model(),
                  int(kcal_cap) if kcal_cap else None,
                  float(money_cap) if money_cap else None,
                  scenario)

    if plan.budget_min <= 0:
        plan.verdict = "来不及"
        plan.lot = None
        plan.notes.append("截止时刻已经过去，时间预算为 0")

    return {
        "scenario": plan.scenario,
        "now": plan.now.strftime("%H:%M"),
        "deadline": plan.deadline.strftime("%H:%M"),
        "budget_min": plan.budget_min,
        "walk_min": plan.walk_min,
        "walk_basis": plan.walk_basis,
        "buffer_min": plan.buffer_min,
        "store": {"code": store.code, "name": store.name, "address": store.address},
        "verdict": plan.verdict,
        "last_order_time": plan.lot.strftime("%H:%M") if plan.lot else None,
        "best": plan.best.to_dict() if plan.best else None,
        "ranked": [c.to_dict() for c in plan.ranked],
        "notes": plan.notes,
        "assumptions": {
            "wait": "max(步行, 出餐)，两者并行",
            "eat": "clamp(kcal/60, 6, 20) 分钟",
            "prep": "品类基值 × 时段系数 × (1+0.15×(单品数-1))，估算非实时",
        },
    }


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    argv = argv if argv is not None else sys.argv[1:]
    try:
        if argv and argv[0] != "-":
            raw = Path(argv[0]).read_text(encoding="utf-8")
        else:
            raw = sys.stdin.read()
        payload = json.loads(raw)
        print(json.dumps(solve(payload), ensure_ascii=False, indent=2))
        return 0
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
