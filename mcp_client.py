"""Minimal MCP Streamable-HTTP client built on the Python standard library.

No third-party dependencies on purpose: the whole project must run on a clean
CPython install so that anyone who clones it can try it without a venv.

Protocol notes (MCP 2025-06-18 / Streamable HTTP):
  * every request is a POST of a JSON-RPC 2.0 envelope
  * the server may answer with ``application/json`` or with an SSE stream
    (``text/event-stream``), so both have to be handled
  * after ``initialize`` the server hands out a session id which must be echoed
    back in the ``Mcp-Session-Id`` header on subsequent calls
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

DEFAULT_ENDPOINT = "https://mcp.mcd.cn"
PROTOCOL_VERSION = "2025-06-18"
TOKEN_ENV = "MCD_MCP_TOKEN"


class McpError(RuntimeError):
    """Any transport or protocol level failure."""


class AuthError(McpError):
    """401 - token missing, invalid or expired."""


class RateLimitError(McpError):
    """429 - more than 600 requests per minute for this token."""


class ToolError(McpError):
    """The tool itself ran and reported a business level error."""


def _load_token(explicit: str | None = None) -> str:
    token = explicit or os.environ.get(TOKEN_ENV, "")
    token = token.strip()
    if not token:
        raise AuthError(
            f"MCP token not found. Set the {TOKEN_ENV} environment variable, e.g.\n"
            f'  export {TOKEN_ENV}="your-token"     (bash)\n'
            f'  $env:{TOKEN_ENV}="your-token"       (PowerShell)\n'
            "Or run with --demo to use the bundled offline fixtures instead."
        )
    return token


def _parse_sse(raw: str) -> list[dict]:
    """Split an SSE body into the JSON payloads carried by ``data:`` lines."""
    messages: list[dict] = []
    buffer: list[str] = []
    for line in raw.splitlines():
        if line.startswith("data:"):
            buffer.append(line[5:].lstrip())
        elif line == "" and buffer:
            chunk = "\n".join(buffer).strip()
            buffer = []
            if chunk:
                try:
                    messages.append(json.loads(chunk))
                except json.JSONDecodeError:
                    continue
    if buffer:
        chunk = "\n".join(buffer).strip()
        if chunk:
            try:
                messages.append(json.loads(chunk))
            except json.JSONDecodeError:
                pass
    return messages


class McpClient:
    """Tiny synchronous client. One instance == one MCP session."""

    def __init__(
        self,
        token: str | None = None,
        endpoint: str | None = None,
        timeout: float = 30.0,
        verbose: bool = False,
    ) -> None:
        self.endpoint = (endpoint or os.environ.get("MCD_MCP_ENDPOINT") or DEFAULT_ENDPOINT).rstrip("/")
        self.timeout = timeout
        self.verbose = verbose
        self._token = token  # resolved lazily so --demo never touches the env
        self.session_id: str | None = None
        self._next_id = 0
        self._initialized = False
        self.call_count = 0

    # ---------------------------------------------------------------- plumbing
    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._token is None:
            self._token = _load_token()
        headers["Authorization"] = f"Bearer {self._token}"
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    def _post(self, payload: dict) -> tuple[int, dict[str, str], str]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint, data=body, headers=self._headers(), method="POST"
        )
        if self.verbose:
            method = payload.get("method", "<response>")
            print(f"  -> {method} (id={payload.get('id')})")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
                return response.status, dict(response.headers), raw
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            if exc.code == 401:
                raise AuthError(
                    "401 - MCP token invalid, expired or missing. "
                    f"Check the {TOKEN_ENV} environment variable. Server said: {detail}"
                ) from exc
            if exc.code == 429:
                raise RateLimitError(
                    "429 - rate limited (600 requests/minute per token). Slow down."
                ) from exc
            raise McpError(f"HTTP {exc.code} from {self.endpoint}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise McpError(
                f"Cannot reach {self.endpoint}: {exc.reason}. "
                "Offline? Use --demo to run against the bundled fixtures."
            ) from exc

    def _rpc(self, method: str, params: dict | None = None, *, notify: bool = False) -> dict:
        self._next_id += 1
        payload: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not notify:
            payload["id"] = self._next_id

        status, headers, raw = self._post(payload)
        self.call_count += 1

        session = headers.get("Mcp-Session-Id") or headers.get("mcp-session-id")
        if session:
            self.session_id = session

        if notify:
            return {}

        if not raw.strip():
            raise McpError(f"Empty response body for {method} (HTTP {status})")

        content_type = (headers.get("Content-Type") or headers.get("content-type") or "").lower()
        if "text/event-stream" in content_type:
            messages = _parse_sse(raw)
        else:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise McpError(f"Malformed JSON for {method}: {raw[:300]}") from exc
            messages = parsed if isinstance(parsed, list) else [parsed]

        target_id = payload["id"]
        for message in messages:
            if message.get("id") == target_id:
                if "error" in message:
                    err = message["error"]
                    raise McpError(f"{method} failed: {err.get('message', err)}")
                return message.get("result", {})

        raise McpError(
            f"No JSON-RPC response matched id={target_id} for {method}. Raw: {raw[:300]}"
        )

    # ------------------------------------------------------------------- public
    def initialize(self) -> dict:
        if self._initialized:
            return {}
        result = self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "mcd-last-call", "version": "1.0.0"},
            },
        )
        self._rpc("notifications/initialized", notify=True)
        self._initialized = True
        return result

    def list_tools(self, retries: int = 3) -> list[dict]:
        tools: list[dict] = []
        cursor = None
        delay = 1.0
        for attempt in range(retries + 1):
            try:
                self.initialize()
                for _ in range(10):  # defensive: never loop forever on a bad cursor
                    params = {"cursor": cursor} if cursor else {}
                    result = self._rpc("tools/list", params)
                    tools.extend(result.get("tools", []))
                    cursor = result.get("nextCursor")
                    if not cursor:
                        break
                return tools
            except RateLimitError:
                if attempt == retries:
                    raise
                time.sleep(delay)
                delay *= 2
                tools = []
                cursor = None
        return tools

    def call_tool(self, name: str, arguments: dict | None = None, *, retries: int = 3) -> dict:
        """Call a tool and return the parsed payload.

        MCP wraps tool output in ``content: [{type: "text", text: "<json>"}]``,
        so the text blocks are unwrapped here and returned as plain data.
        """
        arguments = arguments or {}
        delay = 1.0
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                # initialize 也放在重试循环里：握手阶段同样可能被 429 打回，
                # 放在循环外会导致"限流时一次都不退避就失败"（联调实测踩到）。
                self.initialize()
                result = self._rpc("tools/call", {"name": name, "arguments": arguments})
                return self._unwrap(name, result)
            except RateLimitError as exc:
                last_error = exc
                if attempt == retries:
                    break
                time.sleep(delay)
                delay *= 2
        raise RateLimitError(
            f"{name}: still rate limited after {retries} retries. {last_error}"
        )

    @staticmethod
    def _unwrap(name: str, result: dict):
        if result.get("isError"):
            texts = " ".join(
                block.get("text", "") for block in result.get("content", []) if isinstance(block, dict)
            )
            raise ToolError(f"{name} returned an error: {texts.strip()[:400]}")

        blocks = [b for b in result.get("content", []) if isinstance(b, dict)]
        texts = [b.get("text", "") for b in blocks if b.get("type") == "text"]

        if texts:
            # mcd.cn returns a markdown blob ("Response Structure" + "Original
            # Response") and sometimes appends an extra instruction block, so
            # the blocks are joined and handed to the caller's extractor.
            joined = "\n".join(texts)
            try:
                return json.loads(joined)
            except (json.JSONDecodeError, TypeError):
                return joined

        if "structuredContent" in result:
            return result["structuredContent"]
        return result
