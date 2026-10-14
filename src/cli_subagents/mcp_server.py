"""Minimal MCP server over stdio (JSON-RPC 2.0, one message per line).

Implements the parts of the protocol a tool server needs: initialize, ping,
tools/list and tools/call. No third-party dependencies.
"""

from __future__ import annotations

import json
import sys
import threading
import traceback
from typing import Any, Callable

from cli_subagents import __version__, tools

SUPPORTED_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
INSTRUCTIONS = (
    "Start subagents in other installed agent CLIs (Claude Code first). Each job runs in the "
    "background under one of your own accounts, with the same MCP servers, plugins, skills and "
    "settings as your main setup. Start with subagent_start, then use subagent_status (with "
    "wait_seconds) and subagent_result. Check subagent_accounts to see which accounts are ready."
)


class Server:
    def __init__(self, write: Callable[[dict[str, Any]], None]) -> None:
        self._write = write

    def handle(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        request_id = message.get("id")
        if method is None:
            return  # a response to something we never send
        if request_id is None:
            return  # notification (initialized, cancelled, ...)
        try:
            params = message.get("params") or {}
            if not isinstance(params, dict):
                raise _RpcError(-32602, "params must be an object")
            result = self._dispatch(method, params)
            self._write({"jsonrpc": "2.0", "id": request_id, "result": result})
        except _RpcError as exc:
            self._write({"jsonrpc": "2.0", "id": request_id, "error": {"code": exc.code, "message": str(exc)}})
        except Exception as exc:  # pragma: no cover - defensive
            traceback.print_exc(file=sys.stderr)
            self._write({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"}})

    def _dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "initialize":
            requested = params.get("protocolVersion")
            version = requested if requested in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[-1]
            return {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "cli-subagents", "version": __version__},
                "instructions": INSTRUCTIONS,
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": tools.definitions()}
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                raise _RpcError(-32602, "arguments must be an object")
            try:
                value = tools.call(str(name), arguments)
                is_error = False
            except tools.ToolError as exc:
                value = {"error": str(exc)}
                is_error = True
            return {
                "content": [{"type": "text", "text": json.dumps(value, indent=2, ensure_ascii=False, default=str)}],
                "isError": is_error,
            }
        raise _RpcError(-32601, f"method not found: {method}")


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def serve(stdin=None, stdout=None) -> None:
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    lock = threading.Lock()

    def write(message: dict[str, Any]) -> None:
        data = (json.dumps(message, ensure_ascii=False, default=str) + "\n").encode("utf-8")
        with lock:
            stdout.write(data)
            stdout.flush()

    server = Server(write)
    threads: list[threading.Thread] = []
    for raw in stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            write({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        batch = message if isinstance(message, list) else [message]
        for item in batch:
            if isinstance(item, dict):
                thread = threading.Thread(target=server.handle, args=(item,), daemon=True)
                thread.start()
                threads.append(thread)
        threads = [thread for thread in threads if thread.is_alive()]
    for thread in threads:
        thread.join(timeout=5)


def main() -> int:
    tools.startup()
    serve()
    return 0
