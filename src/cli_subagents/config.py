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

RUNTIMES = ("claude", "codex")
FAILOVER_MODES = ("off", "on-limit")
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
    raise ValueError(f"unknown runtime: {runtime}")


@dataclass
class Account:
    name: str
    home: Path
    enabled: bool = True
    is_primary: bool = False


@dataclass
class RuntimeConfig:
    runtime: str
    primary: Path
    binary: str | None = None
    include_primary: bool = False
    failover: str = "off"
    max_jobs_per_account: int = 1
    connectors: str = "account"
    edit_permission_mode: str = "auto"
    default_model: str | None = None
    cooldown_minutes: int = 60
    accounts: list[Account] = field(default_factory=list)

    def primary_account(self) -> Account:
        return Account(name="primary", home=self.primary, is_primary=True)

    def worker_accounts(self) -> list[Account]:
        """Accounts that may run workers, in configuration order."""

        accounts = [account for account in self.accounts if account.enabled]
        if self.include_primary or not accounts:
            accounts.insert(0, self.primary_account())
        return accounts

    def find(self, name: str) -> Account | None:
        if name == "primary":
            return self.primary_account()
        return next((account for account in self.accounts if account.name == name), None)


@dataclass
class Config:
    runtimes: dict[str, RuntimeConfig]
    job_retention_days: int = 7
    max_depth: int = 1

    def runtime(self, name: str) -> RuntimeConfig:
        if name not in self.runtimes:
            raise ValueError(f"unknown runtime '{name}'. Known: {', '.join(RUNTIMES)}")
        return self.runtimes[name]


def _path(value: Any, fallback: Path) -> Path:
    if isinstance(value, str) and value.strip():
        return Path(os.path.expandvars(value)).expanduser()
    return fallback


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
            )
        )
    failover = str(data.get("failover", "off"))
    if failover not in FAILOVER_MODES:
        raise ValueError(f"[{name}] failover must be one of {FAILOVER_MODES}")
    connectors = str(data.get("connectors", "account"))
    if connectors not in ("account", "off"):
        raise ValueError(f"[{name}] connectors must be 'account' or 'off'")
    return RuntimeConfig(
        runtime=name,
        primary=_path(data.get("primary"), default_primary(name)),
        binary=data.get("binary") or None,
        include_primary=bool(data.get("include_primary", False)),
        failover=failover,
        max_jobs_per_account=max(1, int(data.get("max_jobs_per_account", 1))),
        connectors=connectors,
        edit_permission_mode=str(data.get("edit_permission_mode", "auto")),
        default_model=data.get("default_model") or None,
        cooldown_minutes=max(1, int(data.get("cooldown_minutes", 60))),
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
        for account in runtime.accounts:
            lines += [
                "",
                f"[[{name}.accounts]]",
                f"name = {_toml_value(account.name)}",
                f"home = {_toml_value(str(account.home))}",
                f"enabled = {_toml_value(account.enabled)}",
            ]
    return "\n".join(lines) + "\n"


def save_config(config: Config, path: Path | None = None) -> None:
    write_text(path or config_path(), dump_config(config))
