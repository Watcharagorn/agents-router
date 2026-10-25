import atexit
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

FAKE_CLAUDE = Path(__file__).with_name("fake_claude.py")

# Variables of the session that runs the tests (an agent CLI, an agt worker, a git hook).
DROPPED_PREFIXES = ("CLAUDE", "CODEX_", "AGY_", "AGT_", "KIRO_", "ANTHROPIC_", "OPENAI_")
DROPPED = {"AGY_CMD", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"}
OWN_COMMANDS = ("agt", "agents-router-mcp", "csa", "cli-subagents-mcp")


def isolate_process() -> Path:
    """Point the whole test process at throwaway folders, whichever test module loads first.

    The tests must never see the real ~/.claude, ~/.codex, ~/.gemini, ~/.kiro or
    ~/.agents-router, an installed agt on PATH, or the markers of the agent session
    running them (CLAUDECODE and the like). Test classes still set their own folders.
    """

    root = Path(tempfile.mkdtemp(prefix="agt-tests-")).resolve()
    atexit.register(shutil.rmtree, root, ignore_errors=True)
    for key in list(os.environ):
        if key.startswith(DROPPED_PREFIXES) or key in DROPPED:
            del os.environ[key]
    home = root / "home"
    home.mkdir()
    folders = os.environ.get("PATH", "").split(os.pathsep)
    kept = [folder for folder in folders if not any(
        (Path(folder) / name).exists() or (Path(folder) / f"{name}.exe").exists() for name in OWN_COMMANDS
    )]
    os.environ.update(
        HOME=str(home), USERPROFILE=str(home), PATH=os.pathsep.join(kept),
        CLAUDE_CONFIG_DIR=str(home / ".claude"), CODEX_HOME=str(home / ".codex"),
        AGT_HOME=str(home / ".agents-router"), AGT_CONFIG_DIR=str(home / ".agt"),
        GIT_CONFIG_GLOBAL=str(root / "gitconfig"), GIT_CONFIG_NOSYSTEM="1",
    )
    return root


PROCESS_ROOT = isolate_process()


def stop_job_runners(home: Path, wait: float = 2.0) -> None:
    """Ask the jobs a test left running to stop, so its folder can be removed.

    Uses the runner's own cancel marker and kills only a worker whose PID still
    belongs to the same process (checked by start time), never a bare PID.
    """

    from agents_router.fsutil import kill_tree, read_json, same_process

    active = []
    for folder in (home / "jobs").glob("*"):
        status = read_json(folder / "status.json", {}) or {}
        if status.get("state") not in ("queued", "starting", "running"):
            continue
        active.append(folder)
        try:
            (folder / "cancel").write_text("test teardown", encoding="utf-8")
        except OSError:
            pass
        pid, started = status.get("child_pid"), status.get("child_started")
        try:
            if pid and started is not None and same_process(int(pid), int(started)):
                kill_tree(int(pid))
        except (OSError, TypeError, ValueError):
            pass
    deadline = time.monotonic() + wait
    while active and time.monotonic() < deadline:
        active = [f for f in active if (read_json(f / "status.json", {}) or {}).get("state") in ("queued", "starting", "running")]
        time.sleep(0.1)


class TempHome(unittest.TestCase):
    """Isolated AGT_HOME, primary setup and fake claude binary."""

    def setUp(self):
        # A job runner that is still finishing can hold its log open for a moment on Windows.
        self._temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._temp.name).resolve()
        self.home = self.root / "agt"
        self.primary = self.root / "primary"
        self.project = self.root / "project"
        for folder in (self.home, self.primary, self.project):
            folder.mkdir()
        self.record = self.root / "record.jsonl"
        self._env = patch.dict(
            os.environ,
            {
                "AGT_HOME": str(self.home),
                "CLAUDE_CONFIG_DIR": str(self.primary),
                "CODEX_HOME": str(self.root / "codex-home"),
                "AGT_CLAUDE_COMMAND": json.dumps([sys.executable, str(FAKE_CLAUDE)]),
                "FAKE_CLAUDE_RECORD": str(self.record),
                "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            },
        )
        self._env.start()
        for key in ("AGT_DEPTH", "FAKE_CLAUDE_LIMITED", "FAKE_CLAUDE_SCENARIO"):
            os.environ.pop(key, None)

    def tearDown(self):
        stop_job_runners(self.home)
        self._env.stop()
        self._temp.cleanup()

    def write_config(self, accounts=("a1", "a2"), failover="on-limit", extra=""):
        lines = [
            "[claude]",
            f"primary = {json.dumps(str(self.primary))}",
            f'failover = "{failover}"',
            extra,
        ]
        for name in accounts:
            lines += ["[[claude.accounts]]", f'name = "{name}"', f"home = {json.dumps(str(self.root / 'homes' / name))}"]
        (self.home / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def records(self):
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text(encoding="utf-8").splitlines() if line.strip()]
