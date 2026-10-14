#!/usr/bin/env python
"""Start the MCP server after loading its runtime secret from local state."""

from __future__ import annotations

import os

from broker_security import RUNTIME_SECRET_RE, default_runtime_secret_path


def main() -> None:
    secret = default_runtime_secret_path().read_text(encoding="ascii").strip()
    if not RUNTIME_SECRET_RE.fullmatch(secret):
        raise RuntimeError("bridge runtime secret file is missing or invalid")
    os.environ["ANTIGRAVITY_BRIDGE_RUNTIME_SECRET"] = secret
    from mcp_server import mcp

    mcp.run("stdio")


if __name__ == "__main__":
    main()
