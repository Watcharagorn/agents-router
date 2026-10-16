"""User configuration (`~/.cli-subagents/config.toml`) and state locations."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import tomllib
from typing import Any

from cli_subagents.fsutil import write_text

RUNTIMES = ("claude", "codex", "antigravity")
# Runtimes whose extra accounts and failover are opt-in: failover is "off" by default,
# and then unpinned jobs run on the primary account only (Antigravity: Google's FAQ
# says consumer plans don't support automated account rotation).
OPT_IN_FAILOVER = ("antigravity",)
FAILOVER_MODES = ("off", "on-limit")
WHEN_FULL_MODES = ("refuse", "queue")
REVIEWERS = ("agy", "claude", "off")
WINDOWS_SANDBOX_MODES = ("unelevated", "elevated", "off")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def home() -> Path:
    """Root folder for config, job records and per-account homes."""

    override = os.environ.get("CLI_SUBAGENTS_HOME")
    return Path(override).expanduser() if override else Path.home() / ".cli-subagents"


def config_path() -> Path:
    return home() / "config.toml"


def jobs_dir() -> Path:
    return home() / "jobs"


def state_dir() -> Path:
    return home() / "state"


def cache_dir() -> Path:
    return home() / "cache"


def homes_dir() -> Path:
    return home() / "homes"


def default_primary(runtime: str) -> Path:
    if runtime == "claude":
        configured = os.environ.get("CLAUDE_CONFIG_DIR")
        return Path(configured).expanduser() if configured else Path.home() / ".claude"
    if runtime == "codex":
        configured = os.environ.get("CODEX_HOME")
        return Path(configured).expanduser() if configured else Path.home() / ".codex"
    if runtime == "antigravity":
        return Path.home() / ".gemini" / "antigravity-cli"
    raise ValueError(f"unknown runtime: {runtime}")


@dataclass
class Account:
    name: str
    home: Path
    enabled: bool = True
    is_primary: bool = False
    fallback: bool = False  # used only when every other account is busy, cooling down or excluded
    # Limits (see plans.py). None means "not set": the plan preset, then the runtime default, decide.
    plan: str | None = None
    max_jobs: int | None = None
    max_effort: str | None = None
    models: list[str] | None = None


@dataclass
class RuntimeConfig:
    runtime: str
    primary: Path
    binary: str | None = None
    include_primary: bool = False
    failover: str = "on-limit"
    max_jobs_per_account: int = 1
    connectors: str = "account"
    edit_permission_mode: str = "auto"
    default_model: str | None = None
    cooldown_minutes: int = 60
    windows_sandbox: str = "unelevated"  # [codex] only
    share_parent_mcp: bool = True  # [codex] and [antigravity]: give workers started from Claude Code its MCP servers
    # [antigravity] only: the review gate for read and edit jobs (see docs/gemini.md#safety)
    reviewer: str = "agy"  # "agy", "claude" or "off" (fixed rules only; the rest is denied)
    reviewer_model: str | None = None
    reviewer_account: str | None = None  # Claude account for reviewer = "claude"
    sandbox: bool = True  # run edit jobs with agy --sandbox
    # Limits. `plan` and the primary_* keys describe the primary account.
    plan: str | None = None
    primary_max_jobs: int | None = None
    primary_max_effort: str | None = None
    primary_models: list[str] | None = None
    max_jobs: int | None = None  # runtime-wide cap on running jobs
    usage_ceiling: float = 0.95  # accounts at or above this share of a usage window get no new jobs
    when_full: str = "refuse"
    accounts: list[Account] = field(default_factory=list)

    def primary_account(self) -> Account:
        return Account(
            name="primary", home=self.primary, is_primary=True, plan=self.plan, max_jobs=self.primary_max_jobs,
            max_effort=self.primary_max_effort, models=self.primary_models,
        )

    def worker_accounts(self) -> list[Account]:
        """Accounts that unpinned jobs may use, in configuration order."""

        if self.runtime in OPT_IN_FAILOVER and self.failover == "off":
            return [self.primary_account()]
        accounts = [account for account in self.accounts if account.enabled]
        if self.include_primary or not accounts:
            accounts.insert(0, self.primary_account())
        return accounts

    def pool_accounts(self) -> list[Account]:
        """Accounts that may run workers, pinned or not (doctor --live checks these)."""

        if self.runtime in OPT_IN_FAILOVER:
            return [self.primary_account(), *(account for account in self.accounts if account.enabled)]
        return self.worker_accounts()

    def listed_accounts(self) -> list[Account]:
        """Accounts shown in reports: for opt-in runtimes the primary and every extra account, disabled ones too."""

        if self.runtime in OPT_IN_FAILOVER:
            return [self.primary_account(), *self.accounts]
        return self.worker_accounts()

    def find(self, name: str) -> Account | None:
        if name == "primary":
            return self.primary_account()
        return next((account for account in self.accounts if account.name == name), None)


@dataclass
class Config:
    runtimes: dict[str, RuntimeConfig]
    job_retention_days: int = 7
    max_depth: int = 1
    max_jobs: int | None = None  # cap on running jobs across all runtimes

    def runtime(self, name: str) -> RuntimeConfig:
        if name not in self.runtimes:
            raise ValueError(f"unknown runtime '{name}'. Known: {', '.join(RUNTIMES)}")
        return self.runtimes[name]


def _path(value: Any, fallback: Path) -> Path:
    if isinstance(value, str) and value.strip():
        return Path(os.path.expandvars(value)).expanduser()
    return fallback


def _opt_int(value: Any, minimum: int = 1) -> int | None:
    return max(minimum, int(value)) if value is not None else None


def _opt_text(value: Any) -> str | None:
    return str(value).strip().lower() or None if value is not None else None


def _opt_models(value: Any) -> list[str] | None:
    if value is None:
        return None
    items = [value] if isinstance(value, str) else list(value)
    return [str(item).strip() for item in items if str(item).strip()] or None


def _runtime_from(name: str, data: dict[str, Any]) -> RuntimeConfig:
    accounts: list[Account] = []
    for item in data.get("accounts", []) or []:
        if not isinstance(item, dict) or not NAME_RE.match(str(item.get("name", ""))):
            raise ValueError(f"[{name}] account entries need a simple name")
        account_name = str(item["name"])
        if account_name == "primary":
            raise ValueError("'primary' is reserved for the main setup")
        accounts.append(
            Account(
                name=account_name,
                home=_path(item.get("home"), homes_dir() / name / account_name),
                enabled=bool(item.get("enabled", True)),
                fallback=bool(item.get("fallback", False)),
                plan=_opt_text(item.get("plan")),
                max_jobs=_opt_int(item.get("max_jobs")),
                max_effort=_opt_text(item.get("max_effort")),
                models=_opt_models(item.get("models")),
            )
        )
    opt_in = name in OPT_IN_FAILOVER
    failover = str(data.get("failover", "off" if opt_in else "on-limit"))
    if failover not in FAILOVER_MODES:
        raise ValueError(f"[{name}] failover must be one of {FAILOVER_MODES}")
    connectors = str(data.get("connectors", "account"))
    if connectors not in ("account", "off"):
        raise ValueError(f"[{name}] connectors must be 'account' or 'off'")
    windows_sandbox = str(data.get("windows_sandbox", "unelevated"))
    if windows_sandbox not in WINDOWS_SANDBOX_MODES:
        raise ValueError(f"[{name}] windows_sandbox must be one of {WINDOWS_SANDBOX_MODES}")
    when_full = str(data.get("when_full", "refuse"))
    if when_full not in WHEN_FULL_MODES:
        raise ValueError(f"[{name}] when_full must be one of {WHEN_FULL_MODES}")
    reviewer = str(data.get("reviewer", "agy"))
    if reviewer not in REVIEWERS:
        raise ValueError(f"[{name}] reviewer must be one of {REVIEWERS}")
    ceiling = float(data.get("usage_ceiling", 0.95))
    if not 0 < ceiling <= 1:
        raise ValueError(f"[{name}] usage_ceiling must be above 0 and at most 1")
    return RuntimeConfig(
        runtime=name,
        primary=_path(data.get("primary"), default_primary(name)),
        binary=data.get("binary") or None,
        # Opt-in runtimes rotate through the primary account too, unless told otherwise.
        include_primary=bool(data.get("include_primary", opt_in)),
        failover=failover,
        max_jobs_per_account=max(1, int(data.get("max_jobs_per_account", 1))),
        connectors=connectors,
        edit_permission_mode=str(data.get("edit_permission_mode", "accept-edits" if name == "antigravity" else "auto")),
        default_model=data.get("default_model") or None,
        cooldown_minutes=max(1, int(data.get("cooldown_minutes", 60))),
        windows_sandbox=windows_sandbox,
        share_parent_mcp=bool(data.get("share_parent_mcp", True)),
        reviewer=reviewer,
        reviewer_model=str(data.get("reviewer_model") or "").strip() or None,
        reviewer_account=str(data.get("reviewer_account") or "").strip() or None,
        sandbox=bool(data.get("sandbox", True)),
        plan=_opt_text(data.get("plan")),
        primary_max_jobs=_opt_int(data.get("primary_max_jobs")),
        primary_max_effort=_opt_text(data.get("primary_max_effort")),
        primary_models=_opt_models(data.get("primary_models")),
        max_jobs=_opt_int(data.get("max_jobs")),
        usage_ceiling=ceiling,
        when_full=when_full,
        accounts=accounts,
    )


def load_config(path: Path | None = None) -> Config:
    path = path or config_path()
    data: dict[str, Any] = {}
    if path.exists():
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    runtimes = {name: _runtime_from(name, data.get(name, {}) or {}) for name in RUNTIMES}
    return Config(
        runtimes=runtimes,
        job_retention_days=max(1, int(data.get("job_retention_days", 7))),
        max_depth=max(0, int(data.get("max_depth", 1))),
        max_jobs=_opt_int(data.get("max_jobs")),
    )


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(str(value))


def dump_config(config: Config) -> str:
    """Serialize the config back to TOML (only the keys this tool understands)."""

    lines = [
        "# CLI Subagents configuration. See docs/configuration.md.",
        f"job_retention_days = {config.job_retention_days}",
        f"max_depth = {config.max_depth}",
    ]
    if config.max_jobs:
        lines.append(f"max_jobs = {config.max_jobs}")
    for name in RUNTIMES:
        runtime = config.runtimes[name]
        lines += [
            "",
            f"[{name}]",
            f"primary = {_toml_value(str(runtime.primary))}",
            f"include_primary = {_toml_value(runtime.include_primary)}",
            f"failover = {_toml_value(runtime.failover)}",
            f"max_jobs_per_account = {runtime.max_jobs_per_account}",
            f"connectors = {_toml_value(runtime.connectors)}",
            f"edit_permission_mode = {_toml_value(runtime.edit_permission_mode)}",
            f"cooldown_minutes = {runtime.cooldown_minutes}",
        ]
        if runtime.binary:
            lines.append(f"binary = {_toml_value(runtime.binary)}")
        if runtime.default_model:
            lines.append(f"default_model = {_toml_value(runtime.default_model)}")
        if name == "codex" and runtime.windows_sandbox != "unelevated":
            lines.append(f"windows_sandbox = {_toml_value(runtime.windows_sandbox)}")
        if name in ("codex", "antigravity") and not runtime.share_parent_mcp:
            lines.append("share_parent_mcp = false")
        if name == "antigravity":
            if runtime.reviewer != "agy":
                lines.append(f"reviewer = {_toml_value(runtime.reviewer)}")
            for key, value in (("reviewer_model", runtime.reviewer_model), ("reviewer_account", runtime.reviewer_account)):
                if value:
                    lines.append(f"{key} = {_toml_value(value)}")
            if not runtime.sandbox:
                lines.append("sandbox = false")
        for key, value in (
            ("plan", runtime.plan), ("primary_max_jobs", runtime.primary_max_jobs),
            ("primary_max_effort", runtime.primary_max_effort), ("max_jobs", runtime.max_jobs),
        ):
            if value is not None:
                lines.append(f"{key} = {_toml_value(value)}")
        if runtime.primary_models:
            lines.append(f"primary_models = {json.dumps(runtime.primary_models)}")
        if runtime.usage_ceiling != 0.95:
            lines.append(f"usage_ceiling = {runtime.usage_ceiling}")
        if runtime.when_full != "refuse":
            lines.append(f"when_full = {_toml_value(runtime.when_full)}")
        for account in runtime.accounts:
            lines += [
                "",
                f"[[{name}.accounts]]",
                f"name = {_toml_value(account.name)}",
                f"home = {_toml_value(str(account.home))}",
                f"enabled = {_toml_value(account.enabled)}",
            ]
            if account.fallback:
                lines.append("fallback = true")
            for key, value in (("plan", account.plan), ("max_jobs", account.max_jobs), ("max_effort", account.max_effort)):
                if value is not None:
                    lines.append(f"{key} = {_toml_value(value)}")
            if account.models:
                lines.append(f"models = {json.dumps(account.models)}")
    return "\n".join(lines) + "\n"


def save_config(config: Config, path: Path | None = None) -> None:
    write_text(path or config_path(), dump_config(config))
