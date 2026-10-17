"""Offline Codex stand-in for descendant cleanup and live event tests."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time


def emit(event):
    print(json.dumps(event), flush=True)


def main():
    if "--child" in sys.argv:
        time.sleep(120)
        return
    sys.stdin.read()
    mode = os.environ.get("FAKE_ROBUST_MODE", "tree")
    if mode in ("tree", "pipe_tree", "tree_wait"):
        child = subprocess.Popen(
            [sys.executable, __file__, "--child"], stdin=subprocess.DEVNULL,
            stdout=None if mode == "pipe_tree" else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        Path(os.environ["FAKE_ROBUST_CHILD_PID"]).write_text(str(child.pid), encoding="ascii")
    emit({"type": "thread.started", "thread_id": "robust-thread"})
    emit({"type": "turn.started"})
    if mode in ("stream", "tree_wait"):
        gate = Path(os.environ["FAKE_ROBUST_RELEASE"])
        deadline = time.monotonic() + 15
        while not gate.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
    emit({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}})
    emit({"type": "turn.completed", "usage": {}})


if __name__ == "__main__":
    main()
