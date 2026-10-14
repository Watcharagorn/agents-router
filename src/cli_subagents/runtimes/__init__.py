"""Runtime adapters. Each adapter launches one vendor CLI headless."""

from __future__ import annotations

from cli_subagents.runtimes.base import Runtime

# Runtimes with a working adapter. Others are accepted in the config but not yet runnable.
AVAILABLE = ("claude", "codex", "antigravity")


def get_runtime(name: str) -> Runtime:
    if name not in AVAILABLE:
        raise ValueError(f"runtime '{name}' is not available yet. Available: {', '.join(AVAILABLE)}")
    if name == "claude":
        from cli_subagents.runtimes.claude import ClaudeRuntime

        return ClaudeRuntime()
    if name == "codex":
        from cli_subagents.runtimes.codex import CodexRuntime

        return CodexRuntime()
    if name == "antigravity":
        from cli_subagents.runtimes.antigravity import AntigravityRuntime

        return AntigravityRuntime()
    raise ValueError(f"unknown runtime '{name}'")
