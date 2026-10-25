"""Runtime adapters. Each adapter launches one vendor CLI headless."""

from __future__ import annotations

from agents_router.runtimes.base import Runtime

# Runtimes with a working adapter. Others are accepted in the config but not yet runnable.
AVAILABLE = ("claude", "codex", "antigravity", "kiro")


def get_runtime(name: str) -> Runtime:
    if name not in AVAILABLE:
        raise ValueError(f"runtime '{name}' is not available yet. Available: {', '.join(AVAILABLE)}")
    if name == "claude":
        from agents_router.runtimes.claude import ClaudeRuntime

        return ClaudeRuntime()
    if name == "codex":
        from agents_router.runtimes.codex import CodexRuntime

        return CodexRuntime()
    if name == "antigravity":
        from agents_router.runtimes.antigravity import AntigravityRuntime

        return AntigravityRuntime()
    if name == "kiro":
        from agents_router.runtimes.kiro import KiroRuntime

        return KiroRuntime()
    raise ValueError(f"unknown runtime '{name}'")
