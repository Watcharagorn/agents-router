"""Antigravity adapter: `agy -p --output-format stream-json` workers (Gemini and other models).

Antigravity CLI has no setting to move its config folder, and Google's FAQ says
consumer plans don't support automating account rotation. So this runtime
uses the single signed-in account and never fails over.

AGY 1.3.5 streams JSON through a plain pipe on Windows, so no PTY is needed.
The standalone adapter in `adapters/antigravity/` (ConPTY plus a command
reviewer) remains for older AGY versions and for reviewed shell commands.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

from cli_subagents.config import Account, RuntimeConfig
from cli_subagents.hosts import parent_note
from cli_subagents.runtimes.base import (
    AUTH,
    ERROR,
    SUCCESS,
    USAGE_LIMIT,
    AttemptResult,
    JobSpec,
    Launch,
    clean_environment,
)

MAX_PROMPT_CHARS = 24_000  # the prompt travels as one command-line argument
MODES = {"read": "plan", "full": "accept-edits"}
VALID_MODES = ("plan", "accept-edits", "auto")

WORKER_NOTE = (
    "[You are running as a delegated subagent for another agent session, which will read your "
    "final message. Work only on this task, stay inside its scope, and end with a short summary "
    "of what you did, the result, and anything left open.]\n\n"
)

LIMIT_RE = re.compile(r"quota|resource exhausted|rate limit|usage limit|\b429\b", re.IGNORECASE)
# Only wording that says the CLI itself lacks a login; "sign in" alone shows up in ordinary output.
AUTH_RE = re.compile(r"authentication (is )?required|not (currently )?signed in|unauthenticated", re.IGNORECASE)


class AntigravityParser:
    def __init__(self) -> None:
        self.session_id: str | None = None
        self.init: dict[str, Any] = {}
        self.last_text = ""
        self.tool_uses = 0
        self.last_tool: str | None = None
        self.result: dict[str, Any] | None = None
        self._texts: dict[int, str] = {}
        self._steps_seen: set[int] = set()

    def feed(self, event: dict[str, Any]) -> dict[str, Any] | None:
        kind = event.get("event")
        if event.get("conversation_id"):
            self.session_id = event["conversation_id"]
        if kind == "init":
            init = event.get("init") or {}
            self.init = {
                "model": init.get("model"),
                "permission_mode": init.get("permission_mode"),
                "tools": len(init.get("tools") or []),
            }
        elif kind == "step_update":
            step = event.get("step_update") or {}
            index = int(step.get("step_index") or 0)
            step_type = step.get("step_type")
            if step_type == "agent_response":
                self._texts[index] = self._texts.get(index, "") + str(step.get("text_delta") or "")
                if self._texts[index].strip():
                    self.last_text = self._texts[index]
            elif step_type not in (None, "user_input") and index not in self._steps_seen:
                self._steps_seen.add(index)
                self.tool_uses += 1
                self.last_tool = str(step_type)
        elif kind == "result":
            self.result = event.get("result") or {}
            self.session_id = self.result.get("conversation_id") or self.session_id
        return None

    def progress(self) -> dict[str, Any]:
        return {"tool_uses": self.tool_uses, "last_tool": self.last_tool, "last_text": self.last_text[-600:]}

    def finish(self, exit_code: int | None, stderr_tail: str) -> AttemptResult:
        result = self.result or {}
        common = {"session_id": self.session_id, "usage": result.get("usage"), "num_turns": result.get("num_turns")}
        if result.get("status") == "SUCCESS":
            return AttemptResult(SUCCESS, text=(str(result.get("response") or "") or self.last_text).strip(), **common)
        text = self.last_text  # the worker's last real message, not an error text
        error = result.get("error")
        error_text = error if isinstance(error, str) else str((error or {}).get("message") or "")
        haystack = "\n".join(part for part in (error_text, stderr_tail) if part)
        message = next((line.strip() for line in haystack.splitlines() if line.strip()), "")[:300]
        if LIMIT_RE.search(haystack):
            return AttemptResult(USAGE_LIMIT, message=message or "quota reached", text=text, **common)
        if AUTH_RE.search(haystack):
            return AttemptResult(AUTH, message=message or "Antigravity needs a login", text=text, **common)
        status = result.get("status") or f"exit {exit_code}"
        return AttemptResult(ERROR, message=message or f"agy finished with {status}", text=text, **common)


class AntigravityRuntime:
    name = "antigravity"

    def find_binary(self, config: RuntimeConfig) -> list[str] | None:
        override = os.environ.get("CLI_SUBAGENTS_AGY_COMMAND")
        if override:
            import json

            return list(json.loads(override))
        candidates = [config.binary, os.environ.get("AGY_CMD"), "agy"]
        for candidate in candidates:
            if candidate:
                found = shutil.which(candidate) or (candidate if Path(candidate).is_file() else None)
                if found:
                    return [found]
        local = os.environ.get("LOCALAPPDATA")
        if local and (Path(local) / "agy" / "bin" / "agy.exe").is_file():
            return [str(Path(local) / "agy" / "bin" / "agy.exe")]
        return None

    def _run_quiet(self, config: RuntimeConfig, args: list[str], timeout: int = 60) -> tuple[int, str]:
        command = self.find_binary(config)
        if not command:
            return 127, "agy binary not found"
        try:
            completed = subprocess.run(
                [*command, *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout, env=clean_environment(), stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 1, f"{type(exc).__name__}: {exc}"
        return completed.returncode, (completed.stdout or "") + (completed.stderr or "")

    def version(self, config: RuntimeConfig) -> str | None:
        code, output = self._run_quiet(config, ["--version"])
        return output.strip().splitlines()[0] if code == 0 and output.strip() else None

    def auth_status(self, config: RuntimeConfig, account: Account) -> dict[str, Any]:
        """Lists models, which needs a login but no model request."""

        code, output = self._run_quiet(config, ["models"], timeout=90)
        models = [line.split()[0] for line in output.splitlines() if re.match(r"^[a-z0-9][a-z0-9._-]*\s", line)]
        if code == 0 and models:
            return {"loggedIn": True, "models": models}
        return {"loggedIn": False, "error": output.strip().splitlines()[-1][:200] if output.strip() else f"exit {code}"}

    def login_command(self, config: RuntimeConfig, account: Account) -> tuple[list[str], dict[str, str]]:
        command = self.find_binary(config)
        if not command:
            raise RuntimeError("agy binary not found; install Antigravity CLI or set [antigravity] binary")
        return [*command], clean_environment()

    def prepare(self, config: RuntimeConfig, account: Account, spec: JobSpec) -> dict[str, Any]:
        if not account.is_primary:
            raise RuntimeError("Antigravity runs only on its signed-in account; extra accounts aren't supported")
        return {"linked": [], "copied": [], "removed": [], "conflicts": [], "warnings": []}

    def launch(
        self, config: RuntimeConfig, account: Account, spec: JobSpec, prompt: str, setup: dict[str, Any]
    ) -> Launch:
        command = self.find_binary(config)
        if not command:
            raise RuntimeError("agy binary not found; install Antigravity CLI or set [antigravity] binary")
        text = WORKER_NOTE + ("" if spec.resume_session else parent_note(spec.parent, "antigravity")) + prompt
        if len(text) > MAX_PROMPT_CHARS:
            raise RuntimeError(
                f"prompt is {len(text)} characters; Antigravity takes at most {MAX_PROMPT_CHARS}. "
                "Point to files instead of pasting them."
            )
        mode = MODES.get(spec.access) or (config.edit_permission_mode if config.edit_permission_mode in VALID_MODES else "accept-edits")
        argv = [*command, "--mode", mode, "--output-format", "stream-json"]
        if spec.access == "full":
            argv.append("--dangerously-skip-permissions")
        model = spec.model or config.default_model
        if model:
            argv += ["--model", model]
        if spec.effort:
            argv += ["--effort", spec.effort]
        for directory in spec.add_dirs:
            argv.append(f"--add-dir={directory}")
        if spec.resume_session:
            argv += ["--conversation", str(spec.resume_session)]
        argv.append(f"--print-timeout={spec.timeout_seconds}s")
        argv.append(f"--print={text}")
        env = clean_environment()
        env["CLI_SUBAGENTS_DEPTH"] = str(spec.depth)
        env["CLI_SUBAGENTS_JOB_ID"] = spec.job_id
        return Launch(argv=argv, env=env, stdin="", session_id=spec.resume_session)

    def interactive(self, config: RuntimeConfig, account: Account, setup: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
        return self.login_command(config, account)

    def new_parser(self) -> AntigravityParser:
        return AntigravityParser()
