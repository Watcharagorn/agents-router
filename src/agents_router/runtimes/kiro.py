"""Kiro CLI as a worker: `kiro-cli chat --no-interactive`, prompt on stdin, answer on stdout.

Built from Kiro's docs (kiro.dev/docs/cli/headless), tested against
`tests/fake_kiro.py` and run live once (kiro-cli 2.37.0); docs/kiro.md lists
what that run confirmed and what it didn't.

- **One account.** Kiro keeps its login outside `~/.kiro` (in its own data
  store), so a second login can't be kept apart by folder: jobs run on your one
  login, or on `KIRO_API_KEY` when you set it yourself (Kiro's docs say
  headless runs need it). agt never handles the key and refuses extra Kiro
  accounts.
- **Access.** Headless Kiro can't ask, so every tool is either trusted up front
  or refused. `read` jobs trust Kiro's file-reading and web tools (search and
  fetch change nothing on your machine); `edit` jobs also
  trust file writes, but not the shell (a shell gate through Kiro's hooks waits
  for a live check); `full` trusts every tool (`--trust-all-tools`).
- **Engine.** Every job names Kiro's agent engine (`--v2` or `--v3`) when the
  CLI has those flags: `[kiro] engine` picks it, and `auto` follows
  `chat.agentEngine` in `~/.kiro/settings/cli.json`, else V3 on kiro-cli 3.0 or
  later, else V2. agt learns the flags from `kiro-cli chat --help` and the
  version from `kiro-cli --version` (`probe`), neither of which asks a model.
- **Tool names** are Kiro's V2 ids (`fs_read`, `fs_write` ...) from
  share/toolmap.py on both engines; V3 resolves them through its alias table.
- **Output.** When `kiro-cli chat --help` lists `--output-format`, jobs ask for
  `stream-json`: JSON Lines (`runStarted`, `sessionUpdate`, `metadata`,
  `runFinished`) that give the answer (`runFinished.finalText`), the session id
  and tool progress. An older kiro-cli prints plain text, which the parser keeps
  as it always did. Failures are read from `runFinished.status`, the exit code
  (1 a failure such as an auth error, 3 a required MCP server that didn't start,
  4 an agent that doesn't exist) and the wording of usage limits and auth
  errors; V3's `[INFO]`/`[DEBUG]`/`[WARN]` stderr lines are left out.
- **What a worker sees:** your real `~/.kiro`, so share's Kiro copies of your
  agents and skills are what it loads. agt writes nothing into `~/.kiro`, so a
  Kiro worker doesn't get the agt-parent tools or a Claude parent's MCP servers.
- **Follow-ups.** A stream-json job has a session id, so a finished job can be
  continued (`--resume-id`) and a message sent while it runs becomes a follow-up
  turn once the current one ends. A resumed turn keeps its session's engine:
  `sess_<id>` is V3, a plain uuid V2. Plain-text jobs have no session id and
  can't be continued.
"""

from __future__ import annotations

from dataclasses import dataclass
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
from agents_router.share.toolmap import KIRO_V2

WORKER_NOTE = (
    "[You are running as a delegated subagent for another agent session, which will read your "
    "final message. Work only on this task, stay inside its scope, and end with a short summary "
    "of what you did, the result, and anything left open.]\n\n"
)
LIMIT_RE = re.compile(r"(?i)usage limit|limit (?:reached|exceeded)|quota|throttl|too many requests|\b429\b|rate.?limit")
AUTH_RE = re.compile(r"(?i)not logged in|kiro-cli login|log ?in again|unauthori[sz]ed|authenticat|KIRO_API_KEY|expired token")
EXIT_MESSAGES = {3: "a required MCP server didn't start", 4: "Kiro couldn't find the agent it was asked to use"}
READ_TOOLS = ("Read", "Glob", "Grep", "WebSearch", "WebFetch")
WRITE_TOOLS = ("Write",)
PROBE_FLAGS = ("--v2", "--v3", "--output-format")
PROBE_TIMEOUT = 10  # seconds for each of `--version` and `chat --help`
VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")
STREAM_EVENTS = ("runStarted", "sessionUpdate", "metadata", "runFinished")
NOISE_RE = re.compile(r"\[(?:INFO|DEBUG|WARN|TRACE)\]")  # V3's log lines on stderr, printed even on success
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
V3_SESSION_RE = re.compile(r"sess_[0-9A-Za-z-]+")
V2_SESSION_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def default_home() -> Path:
    return Path.home() / ".kiro"


def engine(home: Path | None = None) -> str | None:
    """`chat.agentEngine` in Kiro's settings: "v2", "v3", None when unset or empty, else the value as set (say "v1")."""

    try:
        data = json.loads(((home or default_home()) / "settings" / "cli.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    chat = data.get("chat")
    value = str(data.get("chat.agentEngine") or (chat.get("agentEngine") if isinstance(chat, dict) else None) or "")
    value = value.strip().lower()
    return {"2": "v2", "3": "v3"}.get(value, value) or None


@dataclass(frozen=True)
class KiroCli:
    """What `probe` learned: the version (text and numbers) and which of PROBE_FLAGS `kiro-cli chat` takes."""

    version: str | None = None
    version_tuple: tuple[int, ...] | None = None
    flags: frozenset[str] = frozenset()
    error: str | None = None  # why `chat --help` couldn't be read, when it couldn't


def _parse_version(output: str) -> tuple[str | None, tuple[int, ...] | None]:
    found = VERSION_RE.search(output)
    if not found:
        return None, None
    return found.group(0), tuple(int(part) for part in found.groups() if part is not None)


def _parse_flags(help_text: str) -> frozenset[str]:
    return frozenset(flag for flag in PROBE_FLAGS if re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", help_text))


def _run_quiet(command: list[str], args: list[str], *, timeout: float = 60,
               env: dict[str, str] | None = None) -> tuple[int, str]:
    completed = subprocess.run([*command, *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=timeout, env=env if env is not None else clean_environment(),
                               stdin=subprocess.DEVNULL, **no_window_kwargs())
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def probe(command: list[str], env: dict[str, str] | None = None, timeout: float | None = None,
          *, version: str | None = None) -> KiroCli:
    """`kiro-cli --version` and `kiro-cli chat --help`; neither makes a model request.

    A supplied version (including an empty string for an unknown version) skips `--version`.
    Anything that goes wrong (a missing binary, a timeout, a non-zero exit) leaves flags empty and
    records the reason in `error`, so callers can distinguish it from a CLI confirmed to have no flags.
    """

    timeout = PROBE_TIMEOUT if timeout is None else timeout

    def run(args: list[str]) -> tuple[str | None, str | None]:
        """(output, None), or (None, the short reason it failed)."""

        try:
            code, output = _run_quiet(command, args, timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            return None, f"{' '.join(args)} timed out after {timeout:g} s"
        except OSError as exc:
            return None, f"couldn't run it ({exc.strerror or type(exc).__name__})"
        except (subprocess.SubprocessError, ValueError) as exc:
            return None, f"{' '.join(args)} failed ({type(exc).__name__})"
        if code != 0:
            return None, f"{' '.join(args)} exited {code}"
        return output, None

    version, numbers = _parse_version(version if version is not None else run(["--version"])[0] or "")
    help_text, error = run(["chat", "--help"])
    return KiroCli(version=version, version_tuple=numbers, flags=_parse_flags(help_text) if help_text else frozenset(),
                   error=error)


def resolve_engine(setting: str, configured: str | None, cli: KiroCli) -> tuple[str | None, str]:
    """The engine flag a job passes ("v2", "v3") or None, and why, per `[kiro] engine` (docs/kiro.md).

    Raises RuntimeError when `[kiro] engine = "v3"` meets a kiro-cli whose flags couldn't be read, or
    that has no `--v3`.
    """

    version = f"kiro-cli {cli.version}" if cli.version else "kiro-cli (unknown version)"
    kiro_default = "Kiro's own default"
    unread = f"none (couldn't read kiro-cli's flags: {cli.error}; {kiro_default})" if cli.error else None
    if setting in ("v2", "v3"):
        if f"--{setting}" in cli.flags:
            return setting, f"{setting} (set in [kiro] engine)"
        if setting == "v3" and cli.error:
            raise RuntimeError(f"couldn't read kiro-cli's flags: {cli.error}")
        if setting == "v3":
            raise RuntimeError(f"kiro-cli {cli.version or '(unknown version)'} has no V3 engine; "
                               'update Kiro or set [kiro] engine = "auto"')
        return None, unread or f"none ({version} has no --{setting}; {kiro_default})"
    if not {"--v2", "--v3"} <= cli.flags:
        return None, unread or f"none ({version} has no engine flags; {kiro_default})"
    if configured in ("v2", "v3"):
        return configured, f"{configured} (chat.agentEngine)"
    if configured:
        return None, unread or f"none (chat.agentEngine is {configured}; Kiro's own setting)"
    flag = "v3" if cli.version_tuple and cli.version_tuple[0] >= 3 else "v2"
    return flag, f"{flag} (auto: {version})"


def quiet_stderr(text: str) -> str:
    """Kiro's stderr without its `[INFO]`, `[DEBUG]`, `[WARN]` log lines, so they never reach a message."""

    return "\n".join(line for line in ANSI_RE.sub("", text).splitlines() if line.strip() and not NOISE_RE.search(line))


def session_engine(session_id: str) -> str | None:
    """The engine a session was made on, from its id: `sess_<id>` is V3, a plain uuid V2, else None.

    A probed fact (kiro-cli 2.37.1), not a documented contract. V2 can't open a V3 session.
    """

    if V3_SESSION_RE.fullmatch(session_id):
        return "v3"
    if V2_SESSION_RE.fullmatch(session_id):
        return "v2"
    return None


def access_args(access: str) -> list[str]:
    """The trust flags for an access level: the same V2 tool ids on both engines (V3 aliases them)."""

    if access == "full":
        return ["--trust-all-tools"]
    tools: list[str] = []
    for name in READ_TOOLS + (WRITE_TOOLS if access == "edit" else ()):
        tools += [tool for tool in KIRO_V2.get(name, []) if tool not in tools]
    return [f"--trust-tools={','.join(tools)}"]


class KiroParser:
    """Kiro's `--output-format stream-json` events, or its plain-text answer from a kiro-cli without them.

    `plain_text = "auto"`: the runner parses each line as JSON and passes any line that isn't a JSON object
    with a string `type` and object `data` as a `text` event. Unrecognised JSON objects are also answer
    text until the first Kiro event (`runStarted`, `sessionUpdate`, `metadata`, `runFinished`) puts
    the parser in stream mode, where stray text lines and unknown events are no part of the answer.
    """

    plain_text = "auto"

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.stream = False
        self.lines: list[str] = []  # plain-text answer lines
        self.since_tool: list[str] = []  # the chunks after the last tool call
        self.tool_uses = 0
        self.last_tool: str | None = None
        self.finished: dict[str, Any] | None = None  # `runFinished.data`
        self.event_errors: list[str] = []  # error wording carried by events
        self.last_text = ""

    def feed(self, event: dict[str, Any]) -> dict[str, Any] | None:
        kind = event.get("type")
        if not self.stream and kind not in STREAM_EVENTS and isinstance(event.get("data"), dict):
            # A plain-text answer may itself be JSON; only Kiro's known events establish a stream.
            text = event.get("_raw")
            event = {"type": "text", "text": text if isinstance(text, str) else json.dumps(event, ensure_ascii=False)}
            kind = "text"
        if kind == "text":
            if not self.stream:
                self.lines.append(str(event.get("text") or ""))
                self.last_text = "\n".join(self.lines).strip()
            return None
        if kind not in STREAM_EVENTS:
            return None
        self.stream = True
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if isinstance(data.get("sessionId"), str) and data["sessionId"].strip():
            self.session_id = data["sessionId"].strip()
        if kind == "runFinished":
            self.finished = data
        elif kind == "sessionUpdate":
            update = data.get("update") if isinstance(data.get("update"), dict) else {}
            what = update.get("sessionUpdate")
            if what == "tool_call":
                self.tool_uses += 1
                self.last_tool = str(update.get("title") or update.get("kind") or "tool")
                self.since_tool = []
            elif what == "agent_message_chunk":
                content = update.get("content") if isinstance(update.get("content"), dict) else {}
                if content.get("type", "text") == "text" and isinstance(content.get("text"), str):
                    if self.lines:  # plain text before the stream isn't part of its message chunks
                        self.lines.clear()
                        self.last_text = ""
                    self.since_tool.append(content["text"])
                    self.last_text = (self.last_text + content["text"])[-4000:]
        for key in ("error", "message", "errorMessage"):  # wording a failure may carry (never seen live)
            value = data.get(key)
            if isinstance(value, dict):
                value = value.get("message")
            if isinstance(value, str) and value.strip():
                self.event_errors.append(value.strip()[:500])
        return None

    def progress(self) -> dict[str, Any]:
        return {"tool_uses": self.tool_uses, "last_tool": self.last_tool, "last_text": self.last_text[-600:]}

    def answer(self) -> str:
        if not self.stream:
            return self.last_text
        final = self.finished.get("finalText") if self.finished else None
        if isinstance(final, str) and final.strip() and not self.finished.get("finalTextTruncated"):
            return final.strip()
        return "".join(self.since_tool).strip() or (final.strip() if isinstance(final, str) else "")

    def finish(self, exit_code: int | None, stderr_tail: str) -> AttemptResult:
        text = self.answer()
        common = {"text": text, "session_id": self.session_id}
        status = str(self.finished.get("status") or "") if self.finished else ""
        stream_ok = not self.stream or status == "success"
        if exit_code == 0 and text and stream_ok:
            return AttemptResult(SUCCESS, **common)
        if len(stderr_tail) >= 4000:  # the runner's tail: its first line may be cut off
            stderr_tail = stderr_tail.split("\n", 1)[-1]
        stderr = quiet_stderr(stderr_tail)
        reasons = [*self.event_errors]
        if self.finished and status != "success":
            reasons.append(f"Kiro's run ended with status {status or 'unknown'}"
                           + (f" ({self.finished['stopReason']})" if self.finished.get("stopReason") else ""))
        details = "\n".join(part for part in (*reasons, stderr, text[-2000:]) if part)
        message = next((line.strip() for line in "\n".join([*reasons, stderr]).splitlines() if line.strip()), "")[:300]
        if LIMIT_RE.search(details):
            return AttemptResult(USAGE_LIMIT, message=message or "Kiro usage limit reached", error_text=details[-4000:], **common)
        if AUTH_RE.search(details):
            return AttemptResult(AUTH, message=message or "Kiro isn't logged in (`kiro-cli login`, or set KIRO_API_KEY)",
                                 **common)
        if not message and self.stream and not self.finished:
            message = "the run ended before Kiro reported a result"
        if exit_code == 0:
            return AttemptResult(ERROR, message=message or "Kiro finished without an answer", error_text=details[-4000:],
                                 **common)
        reason = EXIT_MESSAGES.get(exit_code or 0) or message
        return AttemptResult(ERROR, message=f"kiro-cli exited with status {exit_code}" + (f": {reason}" if reason else ""),
                             error_text=details[-4000:], **common)


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
            return _run_quiet(command, args)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 1, f"{type(exc).__name__}: {exc}"

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
        env = self.account_env(config, account)
        cli = probe(command, env)
        warnings = []
        resumed = session_engine(str(spec.resume_session)) if spec.resume_session else None
        if resumed:
            # A session stays on the engine it was made on (V2 can't open a V3 session), whatever [kiro] engine says.
            if cli.error:
                raise RuntimeError(f"couldn't read kiro-cli's flags: {cli.error}; "
                                   f"session {spec.resume_session} needs --{resumed}")
            engine_flag = resumed if f"--{resumed}" in cli.flags else None
        else:
            if spec.resume_session:
                warnings.append(f"couldn't tell Kiro session {spec.resume_session}'s engine from its id; "
                                "using the engine a new job would")
            engine_flag, _ = resolve_engine(config.engine, engine(account.home), cli)
        argv = [*command, "chat", "--no-interactive", "--wrap", "never", *([f"--{engine_flag}"] if engine_flag else []),
                *(["--output-format", "stream-json"] if "--output-format" in cli.flags else []),
                *access_args(spec.access)]
        model = spec.model or config.default_model
        if model:
            argv += ["--model", model]
        if spec.effort:
            argv += ["--effort", spec.effort]
        if spec.resume_session:
            argv += ["--resume-id", str(spec.resume_session)]
        env["AGT_DEPTH"] = str(spec.depth)
        env["AGT_JOB_ID"] = spec.job_id
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
