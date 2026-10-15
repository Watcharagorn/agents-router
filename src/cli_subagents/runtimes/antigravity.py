"""Antigravity adapter: `agy -p --output-format stream-json` workers (Gemini and other models).

The primary account is the user's normal `agy` login (kept in the OS keyring)
and runs exactly as `agy` does in a terminal.

Extra accounts are opt-in and "use at your own risk": Google's FAQ says consumer
plans don't support automating account rotation. AGY has no setting to move its
config folder, so an extra account gets its own HOME (USERPROFILE on Windows,
the XDG folders on macOS and Linux) plus SSH_CONNECTION, SSH_CLIENT and SSH_TTY.
In an SSH-style session agy stores its login in a file under that HOME instead
of the keyring (undocumented agy behaviour), so each account folder holds its
own login, written by agy itself. The shared setup (rules, skills, plugins,
instructions, hooks, settings) is linked from the primary `~/.gemini`, and the
account's `config/mcp_config.json` is written by this adapter: the primary's
MCP servers plus `csa-parent`, the worker's channel to the main session.

AGY 1.3.5 streams JSON through a plain pipe on Windows, so no PTY is needed.
The standalone adapter in `adapters/antigravity/` (ConPTY plus a command
reviewer) remains for older AGY versions and for reviewed shell commands.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

from cli_subagents import sync
from cli_subagents.config import Account, RuntimeConfig, state_dir
from cli_subagents.fsutil import copy_exclusive, file_lock, is_link, make_private_dir, read_json, same_path, write_text
from cli_subagents.hosts import parent_note
from cli_subagents.runtimes.base import (
    AUTH,
    ERROR,
    PARENT_SERVER,
    SUCCESS,
    USAGE_LIMIT,
    WORKER_TOOLS_NOTE,
    AttemptResult,
    JobSpec,
    Launch,
    clean_environment,
    parent_server,
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
AUTH_RE = re.compile(
    r"authentication (is )?required|authentication expired|not (currently )?signed in|unauthenticated"
    r"|not logged into antigravity|log back in|visit the url to log in",
    re.IGNORECASE,
)
# What agy prints on stderr when it has no usable login. A headless run then waits for a
# login code until --print-timeout, so the runner stops it as soon as this shows up.
LOGIN_PROMPT_RE = re.compile(
    r"You are not logged into Antigravity|Authentication expired|Please log back in"
    r"|Authentication required\.? Please visit the URL",
    re.IGNORECASE,
)

FAQ_URL = (
    "https://antigravity.google/docs/faq"
    "#can-i-use-antigravity-cli-headless-mode-in-third-party-coding-agents-or-automate-account-switching"
)
ADD_NOTE = (
    "Use at your own risk: Google's Antigravity FAQ says consumer plans don't support automating "
    f"account switching ({FAQ_URL}). This account is only used when a job pins it with account=NAME, "
    "or in rotation after you set failover = \"on-limit\" under [antigravity]."
)

# agy stores its login in a file instead of the OS keyring when it detects an SSH session.
# Dummy values; the same three variables a community skill exports for this.
SSH_ENV = {
    "SSH_CONNECTION": "127.0.0.1 50000 127.0.0.1 22",
    "SSH_CLIENT": "127.0.0.1 50000 22",
    "SSH_TTY": "/dev/pts/0",
}
# On macOS and Linux the XDG folders move into the account folder too (as agys does).
XDG_ENV = {
    "XDG_CONFIG_HOME": ".config",
    "XDG_DATA_HOME": ".local/share",
    "XDG_STATE_HOME": ".local/state",
    "XDG_CACHE_HOME": ".cache",
}
# Variables that carry a Google login or key of their own: an extra account must use only its file login.
GOOGLE_LOGIN_ENV = (
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_GENAI_USE_VERTEXAI",
    "GOOGLE_GENAI_USE_GCA",
    "GOOGLE_CLOUD_ACCESS_TOKEN",
    "CLOUDSDK_CONFIG",
)
# Login files agy writes in an SSH-style session, relative to the account folder. The first
# name is the one caam and a community skill report; the others are fallbacks. Any file whose
# name contains "oauth-token" in .gemini/antigravity-cli (or one folder below) counts too.
LOGIN_FILES = (
    ".gemini/antigravity-cli/antigravity-oauth-token",
    ".gemini/oauth_creds.json",
    ".gemini/google_accounts.json",
)
TOKEN_MARK = "oauth-token"

# The shared setup, relative to the primary ~/.gemini, synced as three folders so each
# gets its own manifest. Settings files that can carry an identity are shared only when
# they don't (see IDENTITY_KEYS).
ROOT_PLAN = sync.SyncPlan(files=("GEMINI.md", "AGENTS.md"))
CONFIG_FILES = ("GEMINI.md", "AGENTS.md", "hooks.json")
CLI_FILES: tuple[str, ...] = ()
IDENTITY_KEYS = {("config", "config.json"): "enableAdc", ("antigravity-cli", "settings.json"): "modelProvider"}
SYNCED_FOLDERS = ("", "config", "antigravity-cli")
SELF_COMMANDS = {"csa", "csa.exe", "cli-subagents-mcp", "cli-subagents-mcp.exe"}
SELF_NAMES = {"cli-subagents", "cli_subagents"}
PROBE_JOB = "probe"  # job id used by csa accounts add/sync and csa launch: no worker, no channel
MCP_MARKER = ".cli-subagents-mcp-config"


def gemini_root(config: RuntimeConfig) -> Path:
    """The primary ~/.gemini folder (config.primary is its antigravity-cli folder by default)."""

    primary = Path(config.primary).expanduser()
    return primary.parent if primary.name == "antigravity-cli" else primary


def _is_self_server(name: str, server: Any) -> bool:
    """True for this tool's own MCP server: a worker must not start subagents of its own."""

    if str(name).lower() in SELF_NAMES:
        return True
    if not isinstance(server, dict):
        return False
    command = Path(str(server.get("command") or "")).name.lower()
    args = server.get("args")
    args = [str(item) for item in args] if isinstance(args, list) else []
    return command in SELF_COMMANDS or any("cli_subagents" in item for item in args)


def _identity_reason(path: Path, key: str) -> str | None:
    """Why a settings file can't be shared (it sets the key, or can't be read), or None if it can."""

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return "could not be read as JSON"
    if isinstance(data, dict) and key in data:
        return f"sets {key}, which can select a login of its own"
    return None


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
            return AttemptResult(
                USAGE_LIMIT, message=message or "quota reached", text=text, error_text=haystack[-4000:], **common
            )
        if AUTH_RE.search(haystack):
            return AttemptResult(AUTH, message=message or "Antigravity needs a login", text=text, **common)
        status = result.get("status") or f"exit {exit_code}"
        return AttemptResult(ERROR, message=message or f"agy finished with {status}", text=text, **common)




class AntigravityRuntime:
    name = "antigravity"
    add_note = ADD_NOTE

    def find_binary(self, config: RuntimeConfig) -> list[str] | None:
        override = os.environ.get("CLI_SUBAGENTS_AGY_COMMAND")
        if override:
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

    # Accounts -------------------------------------------------------------

    def account_env(self, config: RuntimeConfig, account: Account) -> dict[str, str]:
        """The primary keeps the normal environment; an extra account gets its own HOME and a file login."""

        env = clean_environment()
        if account.is_primary:
            return env
        for key in list(env):
            if key.upper() in GOOGLE_LOGIN_ENV:
                del env[key]
        home = Path(account.home).expanduser()
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)
        env.update(SSH_ENV)
        if os.name != "nt":
            for key, relative in XDG_ENV.items():
                env[key] = str(home / relative)
        return env

    def logged_in(self, account: Account) -> bool:
        """An extra account counts as logged in when agy's login file exists (checked by name only)."""

        home = Path(account.home).expanduser()
        if any((home / name).is_file() for name in LOGIN_FILES):
            return True
        cli = home / ".gemini" / "antigravity-cli"
        try:
            folders = []
            for entry in cli.iterdir():
                if TOKEN_MARK in entry.name.lower() and entry.is_file():
                    return True
                if entry.is_dir() and not is_link(entry):
                    folders.append(entry)
            for folder in folders:
                if any(TOKEN_MARK in entry.name.lower() and entry.is_file() for entry in folder.iterdir()):
                    return True
        except OSError:
            return False
        return False

    def login_hint(self, account: Account) -> str:
        if account.is_primary:
            return "Run agy in a terminal and sign in."
        return f"Run: csa accounts login {account.name} --runtime antigravity"

    def preflight(self, config: RuntimeConfig, account: Account) -> AttemptResult | None:
        """Ends an extra-account job at once when the account has no login yet."""

        if account.is_primary or self.logged_in(account):
            return None
        return AttemptResult(
            AUTH, message=f"Antigravity account '{account.name}' has no login yet. {self.login_hint(account)}"
        )

    def check_stderr(self, text: str, account: Account) -> AttemptResult | None:
        """Called by the runner on new stderr: stop a run that is waiting for a login."""

        match = LOGIN_PROMPT_RE.search(text or "")
        if not match:
            return None
        line = next((part.strip() for part in text[match.start():].splitlines() if part.strip()), match.group(0))
        return AttemptResult(AUTH, message=f"{line[:200]} {self.login_hint(account)}", error_text=text[-4000:])

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
        """Primary: lists models, which needs a login but no model request. Extra accounts: the login file.

        agy models isn't run for an extra account: without a login it would wait for a login code.
        """

        if not account.is_primary:
            if self.logged_in(account):
                return {"loggedIn": True, "method": "file"}
            return {"loggedIn": False, "method": "file", "error": f"no login file yet. {self.login_hint(account)}"}
        code, output = self._run_quiet(config, ["models"], timeout=90)
        models = [line.split()[0] for line in output.splitlines() if re.match(r"^[a-z0-9][a-z0-9._-]*\s", line)]
        if code == 0 and models:
            return {"loggedIn": True, "models": models}
        return {"loggedIn": False, "error": output.strip().splitlines()[-1][:200] if output.strip() else f"exit {code}"}

    def login_command(self, config: RuntimeConfig, account: Account) -> tuple[list[str], dict[str, str]]:
        """Plain interactive agy. In an extra account's environment it shows a sign-in link and asks for the code."""

        command = self.find_binary(config)
        if not command:
            raise RuntimeError("agy binary not found; install Antigravity CLI or set [antigravity] binary")
        return [*command], self.account_env(config, account)

    def login_cwd(self, account: Account) -> str | None:
        return None if account.is_primary else str(Path(account.home).expanduser())

    def login_instructions(self, account: Account) -> str:
        if account.is_primary:
            return "Sign in in agy, then exit it."
        return (
            "agy prints a Google sign-in link. Open it, sign in with the account this profile should use, "
            "paste the code back into agy, then exit agy (/exit or Ctrl+C). Ignore odd characters in the "
            "prompt: agy thinks it runs over SSH and queries the terminal."
        )

    # Shared setup ---------------------------------------------------------

    def _plans(self, root: Path, report: dict[str, Any]) -> list[tuple[str, sync.SyncPlan]]:
        files = {"config": list(CONFIG_FILES), "antigravity-cli": list(CLI_FILES)}
        for (folder, name), key in IDENTITY_KEYS.items():
            source = root / folder / name
            if not source.is_file():
                continue
            reason = _identity_reason(source, key)
            if reason:
                report["warnings"].append(f"{folder}/{name} {reason}; not shared with extra Antigravity accounts")
            else:
                files[folder].append(name)
        try:
            # This tool's own plugin stays out: workers must not start subagents of their own.
            own = frozenset(
                ("plugins", entry.name)
                for entry in (root / "antigravity-cli" / "plugins").iterdir()
                if "cli-subagents" in entry.name.lower()
            )
        except OSError:
            own = frozenset()
        return [
            ("", ROOT_PLAN),
            ("config", sync.SyncPlan(whole=("rules",), folders=("skills",), files=tuple(files["config"]))),
            (
                "antigravity-cli",
                sync.SyncPlan(
                    whole=("rules",), folders=("skills", "plugins"), files=tuple(files["antigravity-cli"]), skip=own
                ),
            ),
        ]

    def prepare(self, config: RuntimeConfig, account: Account, spec: JobSpec) -> dict[str, Any]:
        report = sync.new_report()
        if account.is_primary:
            return report  # the user's own setup, used as is; no channel to the main session
        root = gemini_root(config)
        home = Path(account.home).expanduser()
        if same_path(home, root.parent) or same_path(home, Path.home()):
            raise RuntimeError(f"{home} is your own home folder; an extra Antigravity account needs its own folder")
        with file_lock(state_dir() / "sync" / f"antigravity-{account.name}.lock", timeout=30):
            make_private_dir(home)  # holds the account's login file
            gemini = home / ".gemini"
            for folder, plan in self._plans(root, report):
                part = sync.new_report()
                sync.sync(root / folder if folder else root, gemini / folder if folder else gemini, plan, part)
                prefix = f"{folder}/" if folder else ""
                for key, values in part.items():
                    report[key] += [value if key in ("conflicts", "warnings") else prefix + value for value in values]
            report["parent_channel"] = self._write_mcp_config(config, root, gemini, spec, report)
        return report

    def _write_mcp_config(
        self, config: RuntimeConfig, root: Path, gemini: Path, spec: JobSpec, report: dict[str, Any]
    ) -> bool:
        """Write the account's config/mcp_config.json: the primary's servers plus csa-parent for this job.

        agy has no flag for per-run MCP servers, and whether it passes its environment on to
        MCP servers is unverified, so the job id is written into the file. The file is
        rewritten for every job while that job holds the account's slot; with one slot per
        account (the default) no other job starts from it in between. With more slots the
        channel is left out. Returns True when the worker gets the channel.
        """

        source = root / "config" / "mcp_config.json"
        data: Any = read_json(source, {}) if source.is_file() else {}
        if not isinstance(data, dict):
            report["warnings"].append(
                "config/mcp_config.json in your setup is not a JSON object; extra accounts get no MCP servers from it"
            )
            data = {}
        servers = data.get("mcpServers")
        servers = servers if isinstance(servers, dict) else {}
        shared = {name: server for name, server in servers.items() if not _is_self_server(name, server)}
        channel = spec.job_id != PROBE_JOB and config.max_jobs_per_account == 1
        if channel:
            shared[PARENT_SERVER] = parent_server(spec.job_id)
        content = json.dumps({**data, "mcpServers": shared}, indent=2) + "\n"

        destination = gemini / "config" / "mcp_config.json"
        if is_link(destination) or is_link(destination.parent):
            report["conflicts"].append("config/mcp_config.json: is a link in the account folder, left in place")
            return False
        current = destination.read_text(encoding="utf-8") if destination.is_file() else None
        if current == content:
            return channel
        marker = gemini / MCP_MARKER
        if current is not None and not marker.exists():
            # First run on a folder that already had its own MCP config: keep a copy.
            backup = destination.with_name("mcp_config.json.before-cli-subagents")
            if not os.path.lexists(backup):
                copy_exclusive(destination, backup)
                report["warnings"].append(f"kept the account's previous config/mcp_config.json as {backup.name}")
        write_text(destination, content)
        write_text(marker, "config/mcp_config.json is generated from the primary setup for every job\n")
        return channel

    def fallback_copies(self, account: Account) -> list[str]:
        """Shared entries that fell back to copies, across the account's three synced folders."""

        if account.is_primary:
            return []
        gemini = Path(account.home).expanduser() / ".gemini"
        found: list[str] = []
        for folder in SYNCED_FOLDERS:
            prefix = f"{folder}/" if folder else ""
            found += [prefix + item for item in sync.fallback_copies(gemini / folder if folder else gemini)]
        return found

    # Launch ---------------------------------------------------------------

    def launch(
        self, config: RuntimeConfig, account: Account, spec: JobSpec, prompt: str, setup: dict[str, Any]
    ) -> Launch:
        command = self.find_binary(config)
        if not command:
            raise RuntimeError("agy binary not found; install Antigravity CLI or set [antigravity] binary")
        note = WORKER_NOTE
        if setup.get("parent_channel"):
            note = WORKER_NOTE.rstrip("]\n") + WORKER_TOOLS_NOTE + "]\n\n"
        text = note + ("" if spec.resume_session else parent_note(spec.parent, "antigravity")) + prompt
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
        env = self.account_env(config, account)
        env["CLI_SUBAGENTS_DEPTH"] = str(spec.depth)
        env["CLI_SUBAGENTS_JOB_ID"] = spec.job_id
        return Launch(argv=argv, env=env, stdin="", session_id=spec.resume_session)

    def interactive(self, config: RuntimeConfig, account: Account, setup: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
        return self.login_command(config, account)

    def new_parser(self) -> AntigravityParser:
        return AntigravityParser()
