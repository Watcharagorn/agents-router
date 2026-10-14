"""Types shared by runtime adapters."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import os
from typing import Any, Protocol

from cli_subagents.config import Account, RuntimeConfig

ACCESS_LEVELS = ("read", "edit", "full")

# Outcomes a finished attempt can have.
SUCCESS = "success"
USAGE_LIMIT = "usage_limit"
AUTH = "auth"
ERROR = "error"
TIMEOUT = "timeout"
CANCELLED = "cancelled"
MAX_TURNS = "max_turns"
NO_ACCOUNT = "no_account"
LOST = "lost"

# Environment variables that identify or authenticate the *host* session.
# A worker must never inherit them, or it may run as the host's account.
HOST_ENV_PREFIXES = ("CLAUDE", "ANTHROPIC_", "CODEX_COMPANION_", "CODEX_THREAD", "CLI_SUBAGENTS_")
HOST_ENV_EXACT = {
    "CODEX_HOME",
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "MCP_CONNECTION_NONBLOCKING",
    "MCP_SERVER_CONNECTION_BATCH_SIZE",
}
KEEP_ENV = {"CLAUDE_CODE_GIT_BASH_PATH", "CLI_SUBAGENTS_HOME"}


def clean_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    """Copy of the environment without host-session identity or credentials."""

    source = dict(os.environ if source is None else source)
    result: dict[str, str] = {}
    for key, value in source.items():
        upper = key.upper()
        if upper in KEEP_ENV:
            result[key] = value
            continue
        if upper in HOST_ENV_EXACT or upper.startswith(HOST_ENV_PREFIXES):
            continue
        result[key] = value
    return result


@dataclass
class JobSpec:
    job_id: str
    runtime: str
    prompt: str
    cwd: str
    account: str | None = None
    model: str | None = None
    effort: str | None = None
    access: str = "edit"
    max_turns: int | None = None
    timeout_seconds: int = 1800
    add_dirs: list[str] = field(default_factory=list)
    resume_session: str | None = None
    depth: int = 1
    created: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JobSpec":
        known = {name for name in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in data.items() if key in known})


@dataclass
class Launch:
    argv: list[str]
    env: dict[str, str]
    stdin: str
    session_id: str | None
    warnings: list[str] = field(default_factory=list)


@dataclass
class AttemptResult:
    outcome: str
    message: str = ""
    text: str = ""
    session_id: str | None = None
    resets_at: float | None = None
    cost_usd: float | None = None
    usage: dict[str, Any] | None = None
    num_turns: int | None = None


class Parser(Protocol):
    session_id: str | None
    last_text: str

    def feed(self, event: dict[str, Any]) -> dict[str, Any] | None:
        """Consume one event. May return a usage update for the account pool."""

    def progress(self) -> dict[str, Any]:
        ...

    def finish(self, exit_code: int | None, stderr_tail: str) -> AttemptResult:
        ...


class Runtime(Protocol):
    name: str

    def find_binary(self, config: RuntimeConfig) -> list[str] | None:
        ...

    def version(self, config: RuntimeConfig) -> str | None:
        ...

    def auth_status(self, config: RuntimeConfig, account: Account) -> dict[str, Any]:
        ...

    def prepare(self, config: RuntimeConfig, account: Account, spec: JobSpec) -> dict[str, Any]:
        ...

    def launch(
        self, config: RuntimeConfig, account: Account, spec: JobSpec, prompt: str, setup: dict[str, Any]
    ) -> Launch:
        ...

    def new_parser(self) -> Parser:
        ...

    def login_command(self, config: RuntimeConfig, account: Account) -> tuple[list[str], dict[str, str]]:
        ...
