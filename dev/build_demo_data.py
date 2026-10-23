"""从离线快照生成试玩页的数据文件 docs/data.js。

为什么要并集：场景 1 的候选是"稳妥的正餐"，场景 3 在热量受限时会把官方的
「500大卡套餐」拉进候选（实测 394 / 412 kcal）。试玩页上有热量滑杆，
如果只放场景 1 的候选，把滑杆拖到 400 就什么都剩不下，体验是假的。

用法：python dev/build_demo_data.py
"""

from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mcd_last_call as m  # noqa: E402


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    fetcher = m.DemoFetcher()
    now = fetcher.now()
    store = m.pick_store(fetcher.stores("", ""))
    walk, _ = store.walk_minutes(None)
    model = m.load_model()

    merged: dict[str, dict] = {}
    for scenario, kcal_cap in ((1, None), (3, 600)):
        cands, _ = m.build_candidates(fetcher, store.code, scenario, kcal_cap)
        plan = m.Plan(scenario, store, now, now + timedelta(minutes=40), 40, walk, "demo", 5, "", None, None)
        m.evaluate(cands, plan, model, None, None, scenario)
        for cand in plan.ranked:
            merged.setdefault(cand.code, cand.to_dict())

    payload = {
        "story_now": now.strftime("%H:%M"),
        "store": {"name": store.name, "address": store.address,
                  "distance_m": store.distance_m, "walk_min": walk},
        "model": {"base": model["base"], "busy": model["busy"],
                  "eat_kcal_per_min": model["eat_kcal_per_min"]},
        "candidates": list(merged.values()),
    }

    target = ROOT / "docs" / "data.js"
    target.write_text("window.MCD_DATA = " + json.dumps(payload, ensure_ascii=False, indent=1) + ";\n",
                      encoding="utf-8")

    kcals = sorted(c["kcal"][1] for c in payload["candidates"])
    print(f"写入 {target.relative_to(ROOT)}：{len(payload['candidates'])} 个候选，"
          f"热量区间 {kcals[0]}–{kcals[-1]} kcal")
    for cand in payload["candidates"]:
        print(f"  {cand['name']:<18} ¥{cand['price_cents'] / 100:>6.2f}  {cand['kcal'][1]:>4} kcal  {cand['kind']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
