"""Create a temporary Antigravity workspace containing the enforced review hook."""

from __future__ import annotations

import json
import base64
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from broker_security import (
    BrokerTokenLease,
    default_approval_dir,
    default_secret_path,
)
from hook_command import hook_command, python_command_prefix


@dataclass(frozen=True)
class ExecutorHarness:
    workspace: Path
    request_dir: Path
    broker_path: Path
    approval_id: str


@contextmanager
def gated_executor_workspace(
    *,
    project_root: Path,
    objective: str,
    agy_cmd: str,
    audit_dir: Path,
) -> Iterator[ExecutorHarness]:
    gate_script = Path(__file__).with_name("review_gate.py").resolve()
    broker_path = Path(__file__).with_name("reviewed_exec.cmd").resolve()
    if not broker_path.is_file():
        raise FileNotFoundError(f"reviewed command broker is missing: {broker_path}")
    # Antigravity only loads workspace hooks from trusted workspace trees. The
    # caller has explicitly selected project_root as the active workspace, so
    # create the short-lived harness beneath it and remove it on exit.
    with BrokerTokenLease() as approval_id, tempfile.TemporaryDirectory(
        prefix=".codex-agy-executor-", dir=str(project_root.resolve())
    ) as temp:
        workspace = Path(temp).resolve()
        request_dir = workspace / "command-requests"
        request_dir.mkdir()
        hooks_dir = workspace / ".agents"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        objective_path = workspace / "objective.txt"
        objective_path.write_text(objective[:16_000], encoding="utf-8")

        gate_config = {
            "project_root": str(project_root.resolve()),
            "objective_file": str(objective_path),
            "agy": agy_cmd,
            "audit_dir": str(audit_dir.resolve()),
            "approval_dir": str(default_approval_dir()),
            "approval_id": approval_id,
            "broker_path": str(broker_path),
            "request_dir": str(request_dir),
            "secret_path": str(default_secret_path()),
        }
        config_b64 = base64.urlsafe_b64encode(
            json.dumps(gate_config, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        command = hook_command(
            [
                *python_command_prefix(),
                str(gate_script),
                "gate",
                "--config-b64",
                config_b64,
            ]
        )
        hooks = {
            "codex-gemini-review-gate": {
                "PreToolUse": [
                    {
                        "matcher": "*",
                        "hooks": [
                            {
                                "type": "command",
                                "command": command,
                                "timeout": 120,
                            }
                        ],
                    }
                ]
            }
        }
        (hooks_dir / "hooks.json").write_text(json.dumps(hooks), encoding="utf-8")
        yield ExecutorHarness(
            workspace=workspace,
            request_dir=request_dir,
            broker_path=broker_path,
            approval_id=approval_id,
        )
