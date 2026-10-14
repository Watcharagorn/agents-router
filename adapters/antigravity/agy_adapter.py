#!/usr/bin/env python
"""Execution adapter for Antigravity CLI."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from agy_pty import clean_terminal_output, run_pty
from capabilities import detect_capabilities
from executor_workspace import gated_executor_workspace


def _with_execution_contract(
    prompt: str,
    *,
    project_root: Path,
    request_dir: Path,
    broker_path: Path,
    approval_id: str,
) -> str:
    request_path = request_dir / f"{approval_id}.json"
    request_example = json.dumps(
        {
            "cwd": str(project_root),
            "command": "<exact command>",
            "timeout_seconds": 300,
        },
        separators=(",", ":"),
    )
    broker_command = broker_path.as_posix()
    return f"""{prompt}

Mandatory terminal execution contract:
- Never call a terminal command directly; headless Antigravity will deny it.
- For each terminal command, first use the workspace file-write tool to write
  exactly one UTF-8 JSON object to `{request_path}` with exactly these fields:
  `{request_example}`.
- Then call the terminal tool with exactly this command line:
  `{broker_command} {approval_id}`
- Wait for that call to finish before creating the next request. The broker
  removes the request after consuming a separate reviewer's signed one-use
  approval. If the reviewer denies it, do not bypass or rephrase the command.
"""


def _build_print_args(
    *,
    agy_cmd: str,
    prompt: str,
    timeout: str,
    model: str | None,
    effort: str | None,
    supports_model_flag: bool,
    supports_effort_flag: bool,
    add_dirs: list[Path],
) -> list[str]:
    args = [agy_cmd, "--mode=accept-edits"]
    if model and supports_model_flag:
        args.extend(["--model", model])
    if effort and supports_effort_flag:
        args.extend(["--effort", effort])
    for directory in add_dirs:
        args.append(f"--add-dir={directory}")
    args.extend([f"--print={prompt}", f"--print-timeout={timeout}"])
    return args


def run_agy_print(
    *,
    prompt: str,
    cwd: Path,
    model: str | None,
    effort: str | None,
    timeout: str,
    wall_timeout_seconds: float,
    add_dirs: list[Path],
    audit_dir: Path,
    capabilities: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run `agy -p` through the selected backend and return captured output."""

    caps = capabilities or detect_capabilities(cwd)
    supports_model_flag = bool(caps.get("supports", {}).get("model_flag"))
    supports_effort_flag = bool(caps.get("supports", {}).get("effort_flag"))
    agy_cmd = str(caps.get("agy_cmd") or os.environ.get("AGY_CMD") or "agy")
    if model and not supports_model_flag:
        raise RuntimeError("Installed AGY does not support --model; refusing settings-file fallback.")
    if effort and not supports_effort_flag:
        raise RuntimeError("Installed AGY does not support --effort.")
    workspace_dirs: list[Path] = []
    for directory in [cwd, *add_dirs]:
        if directory not in workspace_dirs:
            workspace_dirs.append(directory)
    with gated_executor_workspace(
        project_root=cwd,
        objective=prompt,
        agy_cmd=agy_cmd,
        audit_dir=audit_dir,
    ) as harness:
        execution_prompt = _with_execution_contract(
            prompt,
            project_root=cwd,
            request_dir=harness.request_dir,
            broker_path=harness.broker_path,
            approval_id=harness.approval_id,
        )
        args = _build_print_args(
            agy_cmd=agy_cmd,
            prompt=execution_prompt,
            timeout=timeout,
            model=model,
            effort=effort,
            supports_model_flag=supports_model_flag,
            supports_effort_flag=supports_effort_flag,
            # Explicitly mount the short-lived harness so Antigravity treats
            # its `.agents/hooks.json` as a workspace customization. The real
            # project roots follow and remain the only user-data roots that
            # the gate authorizes.
            add_dirs=[harness.workspace, *workspace_dirs],
        )
        backend = "pty" if os.name == "nt" else "subprocess"
        used_cli_model_flag = bool(model and supports_model_flag)
        exitstatus, raw_output = run_pty(
            args, str(harness.workspace), wall_timeout_seconds
        )

    backend_warning = None
    output_source = raw_output
    if raw_output.startswith("[pty unavailable:"):
        first_line, _separator, remainder = raw_output.partition("\n")
        backend_warning = first_line.strip()
        output_source = remainder

    return {
        "exit_status": exitstatus,
        "raw_output": raw_output,
        "output": clean_terminal_output(output_source),
        "backend": backend,
        "backend_warning": backend_warning,
        "used_cli_model_flag": used_cli_model_flag,
        "used_settings_model_fallback": False,
        "args_shape": [
            arg if not arg.startswith("--print=") else "--print=<prompt>"
            for arg in args
        ],
    }
