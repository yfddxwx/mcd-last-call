"""联调校准脚本：把每个核心工具的真实响应落盘并核对字段假设。

用法（Token 只从环境变量读，不落盘）：
    export MCD_MCP_TOKEN="..."
    python dev/calibrate.py

产物：
    dev/raw/<tool>.json     脱敏后的真实响应（traceId 置空），不提交到仓库
输出：
    字段树 + 断言结果（PASS/FAIL），FAIL 说明代码里的假设与真实返回不一致
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mcp_client import McpClient  # noqa: E402

RAW_DIR = ROOT / "dev" / "raw"
PACE = 0.6  # 秒/次，远低于 600 次/分钟的限流

STORE = "1420762"       # 同方信息港，前一轮探测用过的门店
ORDER_TYPE = 1
BE_TYPE = 1


def outline(node, prefix="data", depth=0, max_depth=4, lines=None):
    """打印字段树：路径 + 类型 + 数组长度。"""
    if lines is None:
        lines = []
    pad = "  " * depth
    if isinstance(node, dict):
        for key, value in node.items():
            kind = type(value).__name__
            if isinstance(value, (dict, list)):
                extra = f"[{len(value)}]" if isinstance(value, list) else ""
                lines.append(f"{pad}{prefix}.{key}: {kind}{extra}")
                if depth + 1 < max_depth:
                    outline(value, f"{prefix}.{key}", depth + 1, max_depth, lines)
            else:
                sample = str(value)
                if len(sample) > 40:
                    sample = sample[:40] + "…"
                lines.append(f"{pad}{prefix}.{key}: {kind} = {sample!r}")
    elif isinstance(node, list):
        if node:
            lines.append(f"{pad}{prefix}[] ({len(node)} items), first:")
            outline(node[0], f"{prefix}[0]", depth + 1, max_depth, lines)
    return lines


def sanitize(payload: dict) -> dict:
    """去掉 traceId 与调用时间，避免把可追溯的请求标识带进仓库。"""
    clean = dict(payload)
    clean.pop("traceId", None)
    clean.pop("datetime", None)
    return clean


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    client = McpClient()

    calls: list[tuple[str, dict]] = [
        ("now-time-info", {}),
        ("query-nearby-stores", {"beType": BE_TYPE, "searchType": 2, "city": "深圳", "keyword": "科技园"}),
        ("query-meals", {"storeCode": STORE, "orderType": ORDER_TYPE, "beType": BE_TYPE}),
        ("list-nutrition-foods", {}),
        ("query-meal-detail", {"storeCode": STORE, "orderType": ORDER_TYPE, "beType": BE_TYPE, "code": "9900004835"}),
        ("calculate-price", {"storeCode": STORE, "orderType": ORDER_TYPE, "beType": BE_TYPE,
                             "items": [{"productCode": "9900004835", "quantity": 1},
                                       {"productCode": "9900005462", "quantity": 1}]}),
    ]

    results: dict[str, dict] = {}
    for tool, args in calls:
        print(f"\n{'=' * 72}\n▶ {tool}  args={json.dumps(args, ensure_ascii=False)}")
        raw = client.call_tool(tool, args)
        payload = raw if isinstance(raw, dict) else {"_text": raw}
        if "_text" in payload:
            from mcd_last_call import extract_payload  # 延迟导入，避免循环
            payload = extract_payload(payload["_text"])
        clean = sanitize(payload)
        results[tool] = clean
        (RAW_DIR / f"{tool}.json").write_text(
            json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  success={clean.get('success')} code={clean.get('code')}")
        for line in outline(clean.get("data"))[:26]:
            print("  " + line)
        time.sleep(PACE)

    # ---------------------------------------------------------------- 断言
    print(f"\n{'=' * 72}\n字段假设核对")

    checks: list[tuple[str, bool, str]] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        checks.append((label, bool(condition), detail))

    nt = results.get("now-time-info", {}).get("data") or {}
    check("now-time-info.data.datetime 存在", bool(nt.get("datetime")), str(nt.get("datetime")))

    stores = results.get("query-nearby-stores", {}).get("data") or []
    first_store = stores[0] if isinstance(stores, list) and stores else {}
    for field in ("storeCode", "storeName", "address", "distance",
                  "businessStartTime", "businessEndTime", "businessStatus"):
        check(f"query-nearby-stores.data[0].{field}", field in first_store, str(first_store.get(field))[:40])
    check("distance 是数字（米）", isinstance(first_store.get("distance"), (int, float)),
          f"{first_store.get('distance')!r}")

    menu = results.get("query-meals", {}).get("data") or {}
    cats = menu.get("categories") or []
    first_meal = (cats[0].get("meals") or [{}])[0] if cats else {}
    check("query-meals.data.categories[] 存在", bool(cats), f"{len(cats)} 个分类")
    check("meals[].code 存在", "code" in first_meal, str(first_meal.get("code")))
    check("meals[] 不含 name（需靠 detail 补全）", "name" not in first_meal, str(sorted(first_meal.keys())))
    check("meals[] 不含 price（需靠 calculate-price）", "price" not in first_meal, str(sorted(first_meal.keys())))

    nutrition = results.get("list-nutrition-foods", {}).get("data")
    check("list-nutrition-foods.data 是字符串表格", isinstance(nutrition, str),
          type(nutrition).__name__)
    if isinstance(nutrition, str):
        check("营养表首行是列名声明", "productName" in nutrition.splitlines()[0], nutrition.splitlines()[0][:60])

    detail = results.get("query-meal-detail", {}).get("data") or {}
    rounds = detail.get("rounds") or []
    choice = ((rounds[0].get("choices") or [{}])[0]) if rounds else {}
    check("query-meal-detail.data.name 存在", bool(detail.get("name")), str(detail.get("name")))
    check("query-meal-detail.data.rounds[] 存在", bool(rounds), f"{len(rounds)} 轮")
    check("choices[].name 存在", "name" in choice, str(choice.get("name")))
    check("choices[].quantity 存在", "quantity" in choice, str(choice.get("quantity")))

    price = results.get("calculate-price", {}).get("data") or {}
    check("calculate-price.data.price 存在", "price" in price, str(price.get("price")))
    check("price 是整数（单位：分）", isinstance(price.get("price"), int), str(price.get("price")))
    plist = price.get("productList") or []
    check("productList[].subtotal 存在", bool(plist) and "subtotal" in plist[0],
          str(plist[0].get("subtotal") if plist else None))
    check("productList 覆盖全部入参商品", len(plist) == 2, f"{len(plist)} 行")
    check("takeWayList 提供取餐方式码", bool(price.get("takeWayList")),
          str([row.get("code") for row in price.get("takeWayList") or []]))

    failed = 0
    for label, ok, detail_text in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   → {detail_text}" if detail_text else ""))
        if not ok:
            failed += 1
    print(f"\n{len(checks) - failed}/{len(checks)} 通过，{failed} 项需要改代码")
    print(f"真实响应已脱敏落盘 → {RAW_DIR.relative_to(ROOT)}")
    print(f"本轮共 {client.call_count} 次请求，限流上限 600 次/分钟。")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
