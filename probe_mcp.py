"""T+0 probe. Proves the Streamable-HTTP handshake works and dumps the *real*
tool schemas before a single line of business logic is written.

Usage (token never goes into a file):

    export MCD_MCP_TOKEN="..."           # bash
    $env:MCD_MCP_TOKEN="..."             # PowerShell
    python probe_mcp.py

Outputs:
    docs/mcp-tools.schema.json   raw `tools/list` snapshot (source of truth for
                                 parameter names used by the rest of the project)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from mcp_client import McpClient, McpError

ROOT = Path(__file__).resolve().parent

# Tools this project intends to use, in the order the pipeline calls them.
WANTED = [
    "now-time-info",
    "query-nearby-stores",
    "query-meals",
    "query-meal-detail",
    "list-nutrition-foods",
    "calculate-price",
    "create-order",
]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    client = McpClient(verbose=True)

    print("[1/4] initialize ...")
    info = client.initialize()
    server = info.get("serverInfo", {}) if isinstance(info, dict) else {}
    print(f"      protocol = {info.get('protocolVersion')}")
    print(f"      server   = {server.get('name')} {server.get('version')}")
    print(f"      session  = {client.session_id or '<none>'}")

    print("[2/4] tools/list ...")
    tools = client.list_tools()
    print(f"      {len(tools)} tools exposed")

    out = ROOT / "docs" / "mcp-tools.schema.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"endpoint": client.endpoint, "protocol": info, "tools": tools},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"      schema saved -> {out.relative_to(ROOT)}")

    print("[3/4] check the tools this project depends on ...")
    present = {t.get("name"): t for t in tools}
    missing = []
    for name in WANTED:
        tool = present.get(name)
        if tool:
            schema = tool.get("inputSchema", {}) or {}
            params = list((schema.get("properties") or {}).keys())
            required = schema.get("required") or []
            print(f"      OK   {name:24s} required={required}")
            print(f"           params: {', '.join(params) if params else '<none>'}")
        else:
            missing.append(name)
            print(f"      MISS {name}")
    if missing:
        print(f"      WARNING: {len(missing)} expected tool(s) not found: {missing}")

    print("[4/4] live call now-time-info ...")
    try:
        data = client.call_tool("now-time-info", {})
        print("      " + json.dumps(data, ensure_ascii=False)[:400])
    except McpError as exc:
        print(f"      FAILED: {exc}")
        return 1

    print(f"\nDone. {client.call_count} HTTP round-trips.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
