"""Codex adapter: `codex exec --json` workers with a shared setup across accounts.

Each account has its own `CODEX_HOME` holding its login (`auth.json`, forced to
file storage so logins never share a keyring entry), sessions and state
databases. From the primary home, every run gets:

* `config.toml` (MCP servers, model defaults, features), minus this tool's own
  MCP server so workers can't start subagents of their own;
* `skills/` and `plugins/` entries, linked; `AGENTS.md`, copied.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tomllib
from typing import Any

from cli_subagents import sync
from cli_subagents.config import Account, RuntimeConfig, state_dir
from cli_subagents.fsutil import file_lock, is_link, same_path, write_text
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

SYNC_PLAN = sync.SyncPlan(folders=("skills", "plugins"), files=("AGENTS.md",))
FILE_AUTH = "cli_auth_credentials_store=file"
SELF_COMMANDS = {"csa", "csa.exe", "cli-subagents-mcp", "cli-subagents-mcp.exe"}

WORKER_NOTE = (
    "[You are running as a delegated subagent for another agent session, which will read your "
    "final message. Work only on this task, stay inside its scope, and end with a short summary "
    "of what you did, the result, and anything left open.]\n\n"
)

LIMIT_RE = re.compile(r"usage limit|rate limit|too many requests|\b429\b|quota|try again at", re.IGNORECASE)
AUTH_RE = re.compile(r"not logged in|\b401\b|unauthorized|log ?in again|token (has )?expired|refresh token", re.IGNORECASE)
TOOL_ITEMS = {"command_execution", "file_change", "mcp_tool_call", "web_search"}


def _default_home() -> Path:
    return Path.home() / ".codex"


def strip_own_server(text: str) -> str:
    """Remove this tool's MCP server tables from a Codex config.toml."""

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return text
    servers = data.get("mcp_servers") or {}
    drop = set()
    for name, server in servers.items():
        if not isinstance(server, dict):
            continue
        command = Path(str(server.get("command") or "")).name.lower()
        args = [str(item) for item in server.get("args") or []]
        if command in SELF_COMMANDS or "cli_subagents" in args:
            drop.add(name)
    if not drop:
        return text
    out: list[str] = []
    skipping = False
    for line in text.splitlines(keepends=True):
        header = re.match(r"\s*\[\s*mcp_servers\.(\"[^\"]+\"|'[^']+'|[A-Za-z0-9_-]+)(\.[^\]]*)?\s*\]\s*$", line)
        if header:
            name = header.group(1).strip("\"'")
            skipping = name in drop
        elif re.match(r"\s*\[", line):
            skipping = False
        if not skipping:
            out.append(line)
    return "".join(out)


class CodexParser:
    """Turns `codex exec --json` events into progress and a result."""

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.init: dict[str, Any] = {}
        self.last_text = ""
        self.tool_uses = 0
        self.last_tool: str | None = None
        self.usage: dict[str, Any] | None = None
        self.completed = False
        self.errors: list[str] = []

    def feed(self, event: dict[str, Any]) -> dict[str, Any] | None:
        kind = event.get("type")
        if kind == "thread.started":
            self.session_id = event.get("thread_id")
        elif kind in ("item.completed", "item.started"):
            item = event.get("item") or {}
            item_type = item.get("type")
            if kind == "item.completed" and item_type == "agent_message" and str(item.get("text") or "").strip():
                self.last_text = str(item["text"])
            elif kind == "item.started" and item_type in TOOL_ITEMS:
                self.tool_uses += 1
                self.last_tool = item_type
            elif item_type == "error" and item.get("message"):
                self.errors.append(str(item["message"]))
        elif kind == "turn.completed":
            self.completed = True
            self.usage = event.get("usage")
        elif kind == "turn.failed":
            message = (event.get("error") or {}).get("message") or "turn failed"
            self.errors.append(str(message))
        elif kind == "error" and event.get("message"):
            self.errors.append(str(event["message"]))
        return None

    def progress(self) -> dict[str, Any]:
        return {"tool_uses": self.tool_uses, "last_tool": self.last_tool, "last_text": self.last_text[-600:]}

    def finish(self, exit_code: int | None, stderr_tail: str) -> AttemptResult:
        common = {"session_id": self.session_id, "usage": self.usage}
        if self.completed and not self.errors and exit_code in (0, None):
            return AttemptResult(SUCCESS, text=self.last_text, **common)
        haystack = "\n".join([*self.errors, stderr_tail])
        message = next((line.strip() for line in haystack.splitlines() if line.strip()), "")[:300]
        if LIMIT_RE.search(haystack):
            return AttemptResult(USAGE_LIMIT, message=message or "usage limit reached", text=self.last_text, **common)
        if AUTH_RE.search(haystack):
            return AttemptResult(AUTH, message=message or "authentication failed", text=self.last_text, **common)
        if self.completed and exit_code in (0, None):
            return AttemptResult(SUCCESS, text=self.last_text, **common)
        return AttemptResult(ERROR, message=message or f"codex exited with status {exit_code}", text=self.last_text, **common)


class CodexRuntime:
    name = "codex"

    def find_binary(self, config: RuntimeConfig) -> list[str] | None:
        override = os.environ.get("CLI_SUBAGENTS_CODEX_COMMAND")
        if override:
            return list(json.loads(override))
        found = None
        if config.binary:
            found = shutil.which(config.binary) or (config.binary if Path(config.binary).is_file() else None)
        else:
            found = shutil.which("codex")
        if not found:
            return None
        path = Path(found)
        if path.suffix.lower() in (".cmd", ".ps1", ".bat"):
            # npm shim: call its launcher with node directly instead of going through cmd.exe.
            launcher = path.parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
            node = path.parent / "node.exe"
            node_path = str(node) if node.is_file() else shutil.which("node")
            if launcher.is_file() and node_path:
                return [node_path, str(launcher)]
        return [found]

    def _uses_own_home(self, account: Account) -> bool:
        return not (account.is_primary and same_path(account.home, _default_home()))

    def account_env(self, config: RuntimeConfig, account: Account) -> dict[str, str]:
        env = clean_environment()
        if self._uses_own_home(account):
            env["CODEX_HOME"] = str(account.home)
        return env

    def _auth_args(self, account: Account) -> list[str]:
        return [] if account.is_primary else ["-c", FILE_AUTH]

    def _run_quiet(self, config: RuntimeConfig, args: list[str], env: dict[str, str] | None = None) -> tuple[int, str]:
        command = self.find_binary(config)
        if not command:
            return 127, "codex binary not found"
        try:
            completed = subprocess.run(
                [*command, *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=60, env=env, stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 1, f"{type(exc).__name__}: {exc}"
        return completed.returncode, (completed.stdout or "") + (completed.stderr or "")

    def version(self, config: RuntimeConfig) -> str | None:
        code, output = self._run_quiet(config, ["--version"], env=clean_environment())
        return output.strip().splitlines()[0] if code == 0 and output.strip() else None

    def auth_status(self, config: RuntimeConfig, account: Account) -> dict[str, Any]:
        """`codex login status` for one account. Makes no model request."""

        code, output = self._run_quiet(config, ["login", "status", *self._auth_args(account)], env=self.account_env(config, account))
        text = output.strip()
        logged_in = code == 0 and text.lower().startswith("logged in")
        method = None
        if "chatgpt" in text.lower():
            method = "chatgpt"
        elif "api key" in text.lower():
            method = "api_key"
        return {"loggedIn": logged_in, "authMethod": method, "detail": text.splitlines()[-1][:200] if text else None}

    def login_command(self, config: RuntimeConfig, account: Account) -> tuple[list[str], dict[str, str]]:
        command = self.find_binary(config)
        if not command:
            raise RuntimeError("codex binary not found; install Codex or set [codex] binary")
        return [*command, "login", *self._auth_args(account)], self.account_env(config, account)

    # Shared setup ---------------------------------------------------------

    def prepare(self, config: RuntimeConfig, account: Account, spec: JobSpec) -> dict[str, Any]:
        report = sync.new_report()
        if account.is_primary or same_path(account.home, config.primary):
            return report
        with file_lock(state_dir() / "sync" / f"codex-{account.name}.lock", timeout=30):
            sync.sync(config.primary, account.home, SYNC_PLAN, report)
            self._write_config(config.primary, account.home, report)
        return report

    def _write_config(self, primary: Path, home: Path, report: dict[str, Any]) -> None:
        source = primary / "config.toml"
        destination = home / "config.toml"
        if is_link(destination) or is_link(home):
            report["conflicts"].append("config.toml: is a link in the account folder, left in place")
            return
        content = strip_own_server(source.read_text(encoding="utf-8")) if source.is_file() else ""
        current = destination.read_text(encoding="utf-8") if destination.is_file() else None
        if current == content:
            return
        marker = home / ".cli-subagents-config"
        if current is not None and not marker.exists():
            # First run on a folder that already had its own config: keep a copy.
            backup = home / "config.toml.before-cli-subagents"
            if not backup.exists():
                shutil.copyfile(destination, backup)
                report["warnings"].append(f"kept the account's previous config.toml as {backup.name}")
        write_text(destination, content)
        write_text(marker, "config.toml is generated from the primary setup on every run\n")
        report["copied"].append("config.toml")

    # Launch ---------------------------------------------------------------

    def _access_args(self, access: str, resume: bool) -> list[str]:
        if access == "full":
            return ["--dangerously-bypass-approvals-and-sandbox"]
        if access == "read":
            return ["-c", "sandbox_mode=read-only"] if resume else ["-s", "read-only"]
        if resume:
            return ["-c", "sandbox_mode=workspace-write", "-c", "approval_policy=on-request", "-c", "approvals_reviewer=auto_review"]
        return ["--approve-for-me"]

    def launch(
        self, config: RuntimeConfig, account: Account, spec: JobSpec, prompt: str, setup: dict[str, Any]
    ) -> Launch:
        command = self.find_binary(config)
        if not command:
            raise RuntimeError("codex binary not found; install Codex or set [codex] binary in config.toml")
        resume = bool(spec.resume_session)
        argv = [*command, "exec"]
        if resume:
            argv += ["resume", str(spec.resume_session)]
        argv += ["--json", "--skip-git-repo-check", *self._auth_args(account), *self._access_args(spec.access, resume)]
        if not resume:
            argv += ["-C", spec.cwd]
            for directory in spec.add_dirs:
                argv += ["--add-dir", directory]
        model = spec.model or config.default_model
        if model:
            argv += ["-m", model]
        if spec.effort:
            argv += ["-c", f"model_reasoning_effort={spec.effort}"]
        argv.append("-")

        env = self.account_env(config, account)
        env["CLI_SUBAGENTS_DEPTH"] = str(spec.depth)
        env["CLI_SUBAGENTS_JOB_ID"] = spec.job_id
        return Launch(argv=argv, env=env, stdin=WORKER_NOTE + prompt, session_id=spec.resume_session)

    def interactive(self, config: RuntimeConfig, account: Account, setup: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
        command = self.find_binary(config)
        if not command:
            raise RuntimeError("codex binary not found")
        return [*command, *self._auth_args(account)], self.account_env(config, account)

    def new_parser(self) -> CodexParser:
        return CodexParser()
