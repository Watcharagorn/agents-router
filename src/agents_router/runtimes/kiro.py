"""Kiro CLI as a worker: `kiro-cli chat --no-interactive`, prompt on stdin, answer on stdout.

Built from Kiro's docs (kiro.dev/docs/cli/headless) and checked only against
`tests/fake_kiro.py`; what isn't verified live is listed in docs/kiro.md.

- **One account.** Kiro keeps its login outside `~/.kiro` (in its own data
  store), so a second login can't be kept apart by folder: jobs run on your one
  login, or on `KIRO_API_KEY` when you set it yourself (Kiro's docs say
  headless runs need it). agt never handles the key and refuses extra Kiro
  accounts.
- **Access.** Headless Kiro can't ask, so every tool is either trusted up front
  or refused. `read` jobs trust Kiro's file-reading tools; `edit` jobs also
  trust file writes, but not the shell (a shell gate through Kiro's hooks waits
  for a live check); `full` trusts every tool (`--trust-all-tools`).
- **Tool names** follow Kiro's agent engine (`chat.agentEngine` in
  `~/.kiro/settings/cli.json`): V2 names such as `fs_read`, V3 names such as
  `read`. They come from share/toolmap.py, which share uses too.
- **Output.** Kiro prints its answer as plain text (its `stream-json` events
  aren't documented yet), so the parser keeps the text lines and reads the exit
  code: 1 is a failure (an auth error, for one), 3 a required MCP server that
  didn't start, 4 an agent that doesn't exist. Usage limits are recognised by
  their wording.
- **What a worker sees:** your real `~/.kiro`, so share's Kiro copies of your
  agents and skills are what it loads. agt writes nothing into `~/.kiro`, so a
  Kiro worker doesn't get the agt-parent tools or a Claude parent's MCP servers.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

from agents_router.config import Account, RuntimeConfig
from agents_router.fsutil import no_window_kwargs
from agents_router.runtimes.base import (
    AUTH, ERROR, SUCCESS, USAGE_LIMIT, AttemptResult, JobSpec, Launch, clean_environment,
)
from agents_router.hosts import parent_note
from agents_router.share.toolmap import KIRO_V2, KIRO_V3_RENAMES

WORKER_NOTE = (
    "[You are running as a delegated subagent for another agent session, which will read your "
    "final message. Work only on this task, stay inside its scope, and end with a short summary "
    "of what you did, the result, and anything left open.]\n\n"
)
LIMIT_RE = re.compile(r"(?i)usage limit|limit (?:reached|exceeded)|quota|throttl|too many requests|\b429\b|rate.?limit")
AUTH_RE = re.compile(r"(?i)not logged in|kiro-cli login|log ?in again|unauthori[sz]ed|authenticat|KIRO_API_KEY|expired token")
EXIT_MESSAGES = {3: "a required MCP server didn't start", 4: "Kiro couldn't find the agent it was asked to use"}
READ_TOOLS = ("Read", "Glob", "Grep")
WRITE_TOOLS = ("Write",)


def default_home() -> Path:
    return Path.home() / ".kiro"


def engine(home: Path | None = None) -> str:
    """The Kiro agent engine your settings pick: "v3", or "v2" (Kiro CLI 2.x's default)."""

    try:
        data = json.loads(((home or default_home()) / "settings" / "cli.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "v2"
    value = str((data.get("chat.agentEngine") or (data.get("chat") or {}).get("agentEngine") or "v2")
                if isinstance(data, dict) else "v2").lower()
    return "v3" if value in ("v3", "3") else "v2"


def tool_ids(names: tuple[str, ...], kiro_engine: str) -> list[str]:
    """Kiro's own ids for Claude tool names, in that engine."""

    found: list[str] = []
    for name in names:
        for tool in KIRO_V2.get(name, []):
            if kiro_engine == "v3":
                tool = KIRO_V3_RENAMES.get(tool, {"fs_read": "read", "fs_write": "write"}.get(tool, tool))
            if tool not in found:
                found.append(tool)
    return found


def access_args(access: str, kiro_engine: str) -> list[str]:
    if access == "full":
        return ["--trust-all-tools"]
    names = READ_TOOLS + (WRITE_TOOLS if access == "edit" else ())
    return [f"--trust-tools={','.join(tool_ids(names, kiro_engine))}"]


class KiroParser:
    """Kiro's plain-text answer (the runner passes each line it can't read as JSON as a `text` event)."""

    plain_text = True

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.lines: list[str] = []
        self.last_text = ""

    def feed(self, event: dict[str, Any]) -> dict[str, Any] | None:
        if event.get("type") == "text":
            self.lines.append(str(event.get("text") or ""))
            self.last_text = "\n".join(self.lines).strip()
        return None

    def progress(self) -> dict[str, Any]:
        return {"tool_uses": 0, "last_tool": None, "last_text": self.last_text[-600:]}

    def finish(self, exit_code: int | None, stderr_tail: str) -> AttemptResult:
        text = self.last_text
        if exit_code == 0 and text:
            return AttemptResult(SUCCESS, text=text)
        details = "\n".join(part for part in (stderr_tail, text[-2000:]) if part)
        message = next((line.strip() for line in stderr_tail.splitlines() if line.strip()), "")[:300]
        if LIMIT_RE.search(details):
            return AttemptResult(USAGE_LIMIT, message=message or "Kiro usage limit reached", text=text, error_text=details[-4000:])
        if AUTH_RE.search(details):
            return AttemptResult(AUTH, message=message or "Kiro isn't logged in (`kiro-cli login`, or set KIRO_API_KEY)",
                                 text=text)
        if exit_code == 0:
            return AttemptResult(ERROR, message="Kiro finished without an answer", text=text)
        reason = EXIT_MESSAGES.get(exit_code or 0) or message
        return AttemptResult(ERROR, message=f"kiro-cli exited with status {exit_code}" + (f": {reason}" if reason else ""),
                             text=text, error_text=details[-4000:])


class KiroRuntime:
    name = "kiro"

    def find_binary(self, config: RuntimeConfig) -> list[str] | None:
        override = os.environ.get("AGT_KIRO_COMMAND")
        if override:
            return list(json.loads(override))
        if config.binary:
            found = shutil.which(config.binary) or (config.binary if Path(config.binary).is_file() else None)
        else:
            found = shutil.which("kiro-cli")
        return [found] if found else None

    def account_env(self, config: RuntimeConfig, account: Account) -> dict[str, str]:
        return clean_environment()  # KIRO_API_KEY, when you set it, passes through as it is

    def _run_quiet(self, config: RuntimeConfig, args: list[str]) -> tuple[int, str]:
        command = self.find_binary(config)
        if not command:
            return 127, "kiro-cli not found"
        try:
            completed = subprocess.run([*command, *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                                       timeout=60, env=clean_environment(), stdin=subprocess.DEVNULL, **no_window_kwargs())
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 1, f"{type(exc).__name__}: {exc}"
        return completed.returncode, (completed.stdout or "") + (completed.stderr or "")

    def version(self, config: RuntimeConfig) -> str | None:
        code, output = self._run_quiet(config, ["--version"])
        return output.strip().splitlines()[0] if code == 0 and output.strip() else None

    def auth_status(self, config: RuntimeConfig, account: Account) -> dict[str, Any]:
        """`kiro-cli whoami --format json`: read-only, makes no model request."""

        code, output = self._run_quiet(config, ["whoami", "--format", "json"])
        try:
            data = json.loads(output[output.index("{"):]) if "{" in output else {}
        except ValueError:
            data = {}
        logged_in = code == 0 and not AUTH_RE.search(output) if not data else code == 0
        method = data.get("authMethod") or data.get("accountType") or data.get("method") if isinstance(data, dict) else None
        if os.environ.get("KIRO_API_KEY"):
            method = method or "api_key"
        return {"loggedIn": bool(logged_in), "authMethod": method, "detail": output.strip().splitlines()[-1][:200] if output.strip() else None}

    def login_command(self, config: RuntimeConfig, account: Account) -> tuple[list[str], dict[str, str]]:
        command = self.find_binary(config)
        if not command:
            raise RuntimeError("kiro-cli not found; install the Kiro CLI or set [kiro] binary")
        return [*command, "login"], self.account_env(config, account)

    def prepare(self, config: RuntimeConfig, account: Account, spec: JobSpec) -> dict[str, Any]:
        return {"copied": [], "linked": [], "conflicts": [], "warnings": []}  # one account, your own ~/.kiro

    def launch(self, config: RuntimeConfig, account: Account, spec: JobSpec, prompt: str, setup: dict[str, Any]) -> Launch:
        command = self.find_binary(config)
        if not command:
            raise RuntimeError("kiro-cli not found; install the Kiro CLI or set [kiro] binary in agt's settings.toml")
        kiro_engine = engine(account.home)
        argv = [*command, "chat", "--no-interactive", "--wrap", "never", *access_args(spec.access, kiro_engine)]
        model = spec.model or config.default_model
        if model:
            argv += ["--model", model]
        if spec.effort:
            argv += ["--effort", spec.effort]
        if spec.resume_session:
            argv += ["--resume-id", str(spec.resume_session)]
        env = self.account_env(config, account)
        env["AGT_DEPTH"] = str(spec.depth)
        env["AGT_JOB_ID"] = spec.job_id
        warnings = []
        if spec.access == "edit":
            warnings.append("Kiro edit jobs can read and write files but not run shell commands; use access=full for that")
        stdin = WORKER_NOTE + ("" if spec.resume_session else parent_note(spec.parent, "kiro")) + prompt
        return Launch(argv=argv, env=env, stdin=stdin, session_id=spec.resume_session, warnings=warnings)

    def interactive(self, config: RuntimeConfig, account: Account, setup: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
        command = self.find_binary(config)
        if not command:
            raise RuntimeError("kiro-cli not found")
        return [*command, "chat"], self.account_env(config, account)

    def new_parser(self) -> KiroParser:
        return KiroParser()
