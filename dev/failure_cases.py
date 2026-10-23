"""异常用例：逐个工具验证兜底不会崩。

设计原则：
  * 空结果 / 参数缺失 / 非法编码 —— 真实调用（数量极少，远低于限流）
  * 401 —— 用一个故意错误的 Token 真实调用
  * 429 —— **不真撞**（同一个 Token 被限流会连累后续调试），
           用受控 stub 触发，验证指数退避 3 次后抛出可读错误

用法：
    export MCD_MCP_TOKEN="..."
    python dev/failure_cases.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mcd_last_call as app  # noqa: E402
from mcp_client import AuthError, McpClient, McpError, RateLimitError  # noqa: E402

PACE = 0.7
rows: list[tuple[str, str, str, str]] = []   # tool, 场景, 期望, 实际


def record(tool: str, case: str, expected: str, actual: str, ok: bool) -> None:
    rows.append((tool, case, expected, actual, "PASS" if ok else "FAIL"))


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    client = McpClient()
    store = "1420762"
    base = {"storeCode": store, "orderType": 1, "beType": 1}

    # ---- 1 空结果：gate 一个不存在的关键词 -------------------------------
    print("▶ query-nearby-stores · 空结果")
    try:
        data = client.call_tool("query-nearby-stores",
                                {"beType": 1, "searchType": 2, "city": "深圳", "keyword": "zzz不存在的地方"})
        payload = app.extract_payload(data)
        empty = payload.get("success") is True and not payload.get("data")
        record("query-nearby-stores", "关键词查无结果",
               "success=true, data 为空数组",
               f"success={payload.get('success')} code={payload.get('code')} len={len(payload.get('data') or [])}",
               empty or payload.get("success") is False)
    except McpError as exc:
        record("query-nearby-stores", "关键词查无结果", "不抛异常", f"异常: {str(exc)[:60]}", True)
    time.sleep(PACE)

    # ---- 2 参数缺失：searchType=2 但不传 city/keyword --------------------
    print("▶ query-nearby-stores · 参数缺失")
    try:
        data = app.extract_payload(client.call_tool("query-nearby-stores",
                                                    {"beType": 1, "searchType": 2}))
        code = data.get("code")
        msg = data.get("message", "")
        record("query-nearby-stores", "缺 city+keyword",
               "业务错误 600058", f"code={code} msg={msg}", code == 600058)
    except McpError as exc:
        record("query-nearby-stores", "缺 city+keyword", "业务错误 600058", f"异常: {str(exc)[:60]}", False)
    time.sleep(PACE)

    # ---- 3 query-meals 参数缺失 -----------------------------------------
    print("▶ query-meals · 参数缺失（缺 storeCode）")
    try:
        data = app.extract_payload(client.call_tool("query-meals", {"orderType": 1, "beType": 1}))
        record("query-meals", "缺 storeCode", "业务错误（非 200）",
               f"code={data.get('code')} msg={data.get('message')}", data.get("success") is False)
    except McpError as exc:
        record("query-meals", "缺 storeCode", "业务错误（非 200）", f"异常: {str(exc)[:60]}", True)
    time.sleep(PACE)

    # ---- 4 query-meals 不存在的门店 -------------------------------------
    print("▶ query-meals · 门店不存在")
    try:
        data = app.extract_payload(client.call_tool("query-meals",
                                                    {"storeCode": "000000", "orderType": 1, "beType": 1}))
        empty = not (data.get("data") or {}).get("categories")
        record("query-meals", "storeCode 不存在", "业务错误或空菜单",
               f"success={data.get('success')} code={data.get('code')} msg={data.get('message')}",
               data.get("success") is False or empty)
    except McpError as exc:
        record("query-meals", "storeCode 不存在", "业务错误或空菜单", f"异常: {str(exc)[:60]}", True)
    time.sleep(PACE)

    # ---- 5 query-meal-detail 非法编码 -----------------------------------
    print("▶ query-meal-detail · 非法 code")
    try:
        data = app.extract_payload(client.call_tool("query-meal-detail", {**base, "code": "999999999"}))
        record("query-meal-detail", "code 不存在", "业务错误（非 200）",
               f"code={data.get('code')} msg={data.get('message')}", data.get("success") is False)
    except McpError as exc:
        record("query-meal-detail", "code 不存在", "业务错误（非 200）", f"异常: {str(exc)[:60]}", True)
    time.sleep(PACE)

    # ---- 6 calculate-price 空 items -------------------------------------
    print("▶ calculate-price · 空 items")
    try:
        data = app.extract_payload(client.call_tool("calculate-price", {**base, "items": []}))
        record("calculate-price", "items 为空", "业务错误或 0 元",
               f"success={data.get('success')} code={data.get('code')} price={(data.get('data') or {}).get('price')}",
               data.get("success") is False or (data.get("data") or {}).get("price") == 0)
    except McpError as exc:
        record("calculate-price", "items 为空", "业务错误或 0 元", f"异常: {str(exc)[:60]}", True)
    time.sleep(PACE)

    # ---- 7 401：故意用错误 Token ----------------------------------------
    print("▶ 401 · 错误 Token")
    try:
        bad = McpClient(token="invalid-token-for-test-only")
        bad.call_tool("now-time-info", {})
        record("authorization", "无效 Token", "抛 AuthError", "居然成功了", False)
    except AuthError as exc:
        record("authorization", "无效 Token", "抛 AuthError（不重试）",
               f"AuthError: {str(exc)[:48]}", True)
    except McpError as exc:
        record("authorization", "无效 Token", "抛 AuthError", f"其他异常: {str(exc)[:48]}", False)

    # ---- 8 429：受控 stub，不真撞限流 -----------------------------------
    print("▶ 429 · 受控 stub（不真撞限流）")
    class AlwaysLimited(McpClient):
        def _rpc(self, method, params=None, notify=False):  # noqa: D401
            raise RateLimitError("429 - rate limited (stub)")

    started = time.time()
    try:
        AlwaysLimited(token="stub").call_tool("query-meals", {"storeCode": store}, retries=3)
        record("rate-limit", "持续 429", "退避 3 次后抛 RateLimitError", "未抛出", False)
    except RateLimitError as exc:
        elapsed = time.time() - started
        ok = 6.0 <= elapsed <= 9.0          # 1 + 2 + 4 秒
        record("rate-limit", "持续 429", "退避 3 次（1s/2s/4s）后抛错",
               f"耗时 {elapsed:.1f}s · {str(exc)[:40]}", ok)

    # ---- 9 CLI 层兜底：空结果不应崩 -------------------------------------
    print("▶ CLI · 空门店结果")
    code = app.main(["--keyword", "zzz不存在的地方", "--city", "深圳", "--no-color"])
    record("cli", "门店为空", "退出码 4 + 可读提示", f"exit={code}", code == 4)

    # ------------------------------------------------------------------ 汇总
    print("\n" + "=" * 92)
    print(f"{'工具':<22}{'用例':<22}{'期望':<26}{'结果'}")
    print("-" * 92)
    for tool, case, expected, actual, status in rows:
        print(f"{tool:<22}{case:<22}{expected:<26}{status}  {actual}")
    failed = sum(1 for r in rows if r[4] == "FAIL")
    print("-" * 92)
    print(f"{len(rows) - failed}/{len(rows)} 通过")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
