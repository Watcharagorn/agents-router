"""agt's settings (one `settings.toml` per level, see levels.py) and state locations."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

from agents_router import levels, roles

RUNTIMES = ("claude", "codex", "antigravity", "kiro")
# Runtimes whose extra accounts are "use at your own risk" (Antigravity: Google's FAQ says
# consumer plans don't support automated account rotation). Failover is on by default, as for
# the others; with failover = "off", unpinned jobs run on the primary account only.
OPT_IN_FAILOVER = ("antigravity",)
FAILOVER_MODES = ("off", "on-limit")
WHEN_FULL_MODES = ("refuse", "queue")
REVIEWERS = ("agy", "claude", "off")
WEB_SEARCH_MODES = ("disabled", "cached", "indexed", "live")
WINDOWS_SANDBOX_MODES = ("unelevated", "elevated", "off")
REASONING_SUMMARY_MODES = ("detailed", "concise", "auto", "none", "off")
KIRO_ENGINES = ("auto", "v2", "v3")  # [kiro] engine: auto = chat.agentEngine, else V3 on kiro-cli 3.0+, else V2
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def home() -> Path:
    """Root folder for config, job records and per-account homes."""

    override = os.environ.get("AGT_HOME")
    return Path(override).expanduser() if override else Path.home() / ".agents-router"


def user_settings_path(env: dict[str, str] | None = None) -> Path:
    """`$AGT_CONFIG_DIR/settings.toml`; with only `AGT_HOME` set, `<AGT_HOME>/settings.toml`; else `~/.agt/settings.toml`."""

    env = os.environ if env is None else env
    configured = env.get("AGT_CONFIG_DIR")
    if configured:
        return Path(configured).expanduser() / "settings.toml"
    if env.get("AGT_HOME"):
        return Path(env["AGT_HOME"]).expanduser() / "settings.toml"
    return Path.home() / ".agt" / "settings.toml"


def legacy_config_path() -> Path:
    """Where agt 0.3.0 kept its settings. agt no longer reads it."""

    return home() / "config.toml"


def jobs_dir() -> Path:
    return home() / "jobs"


def state_dir() -> Path:
    return home() / "state"


def cache_dir() -> Path:
    return home() / "cache"


def shared_claude_folders() -> list[Path]:
    """Claude folders outside the home folder that `agt share` was told about (`[[share.folders]]`)."""

    try:
        from agents_router.share import settings as share_settings

        return [Path(folder.path).expanduser() for folder in share_settings.load(share_settings.path()).folders
                if folder.cli == "claude"]
    except Exception:
        return []


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
    if runtime == "kiro":
        return Path.home() / ".kiro"
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
    # "linked": agt links the primary setup into the folder (accounts under ~/.agents-router/homes).
    # "own": a Claude Code profile you use yourself; agt runs it with its own setup and links nothing into it
    # (`agt setup` installs the plugin there, as in any profile of yours).
    setup: str = "linked"
    found: bool = False  # a profile agt found on this machine (see profiles.py), not one in your settings
    login: str | None = None  # short hash of the login: folders with one login share their usage
    org_type: str | None = None
    logged_in: bool | None = None  # found profiles: None means unknown


@dataclass
class QuotaPool:
    name: str
    models: list[str]
    per_account: int | None = None
    total: int | None = None
    weekly_jobs: int | None = None
    cooldown_minutes: int | None = None


@dataclass
class RuntimeConfig:
    runtime: str
    primary: Path
    binary: str | None = None
    include_primary: bool = False
    failover: str = "on-limit"
    max_jobs_per_account: int | None = None  # unset: no cap on jobs per account
    connectors: str = "account"
    edit_permission_mode: str = "auto"
    default_model: str | None = None
    cooldown_minutes: int = 60
    capacity_retries: int = 3
    capacity_retry_seconds: int = 30  # initial delay; doubles for each retry
    windows_sandbox: str = "unelevated"  # [codex] only
    reasoning_summary: str = "detailed"  # [codex] only; off leaves the CLI default alone
    engine: str = "auto"  # [kiro] only: the agent engine jobs name (KIRO_ENGINES)
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
    max_jobs_by_room: list[list[float | int]] = field(default_factory=list)
    pools: list[QuotaPool] = field(default_factory=list)
    web_search: str | None = None  # [codex] only; unset preserves Codex's default
    network_access: bool = True  # [codex] only: network inside the sandbox for edit jobs (local test sockets too)
    accounts: list[Account] = field(default_factory=list)
    picked: list[str] | None = None  # a workspace's `accounts = [...]`: the only accounts its unpinned jobs use
    # [claude] only. `order`: the profiles unpinned jobs try, first to last. `ignore`: profiles (names or
    # folders) agt doesn't find. `session_home`: the folder of the Claude Code session calling agt, the
    # orchestrator; it never takes that session's delegated jobs. None when no Claude Code session calls.
    order: list[str] | None = None
    ignore: list[str] | None = None
    session_home: Path | None = None
    primary_from_settings: bool = False  # `primary` is written in the settings, not taken from the environment
    _found: list[Account] | None = field(default=None, repr=False, compare=False)

    # Profiles ---------------------------------------------------------------

    def found_accounts(self) -> list[Account]:
        """Claude Code profiles found on this machine (see profiles.py), registered accounts left out."""

        if self.runtime != "claude":
            return []
        if self._found is None:
            self._found = self._discover()
        return self._found

    def _discover(self) -> list[Account]:
        from agents_router import profiles
        from agents_router.profiles import same_folder

        extra = shared_claude_folders()
        ignored = [item for item in self.ignore or [] if "/" in item or "\\" in item or item.startswith("~")]
        skip = [account.home for account in self.accounts] + [Path(item) for item in ignored]
        if homes_dir().is_dir():
            skip.append(homes_dir())

        def check(folder: Path) -> bool | None:
            from agents_router.runtimes import get_runtime

            status = get_runtime("claude").auth_status(self, Account(name="profile", home=folder))
            return None if status.get("error") else bool(status.get("loggedIn"))

        found = []
        for profile in profiles.discover(extra=extra, skip=skip, check=check):
            if profile.name in (self.ignore or []) or any(profile.name == account.name for account in self.accounts):
                continue
            account = Account(name=profile.name, home=profile.home, setup="own", found=True, login=profile.login,
                              org_type=profile.org_type, logged_in=profile.logged_in)
            if same_folder(account.home, self.primary):
                # The primary folder under its profile name: the same limits as `primary`, whichever name a job uses.
                account = replace(account, is_primary=True, **self._primary_limits(set_only=True))
            found.append(account)
        found.sort(key=lambda account: (not profiles.is_default(account.home), account.name))
        return found

    def all_accounts(self) -> list[Account]:
        """Registered accounts, then found profiles."""

        return [*self.accounts, *self.found_accounts()]

    def is_session(self, account: Account) -> bool:
        """Whether the account is the folder of the Claude Code session calling agt."""

        from agents_router.profiles import same_folder

        return self.session_home is not None and same_folder(account.home, self.session_home)

    def session_login(self) -> str | None:
        if self.session_home is None:
            return None
        from agents_router.profiles import account_info

        return account_info(self.session_home)["login"]

    def login_group(self, account: Account) -> list[str]:
        """Names of every account with the same login as this one (itself included)."""

        if not account.login:
            return [account.name]
        names = [other.name for other in self.all_accounts() if other.login == account.login]
        return names if account.name in names else [account.name, *names]

    def _primary_limits(self, set_only: bool = False) -> dict[str, Any]:
        limits = dict(plan=self.plan, max_jobs=self.primary_max_jobs, max_effort=self.primary_max_effort,
                      models=self.primary_models)
        return {key: value for key, value in limits.items() if value is not None} if set_only else limits

    def primary_account(self) -> Account:
        from agents_router.profiles import same_folder

        for account in self.all_accounts():
            if same_folder(account.home, self.primary):
                # The primary folder is a known profile: one name for it, whoever calls.
                return replace(account, is_primary=True, **self._primary_limits(set_only=True))
        return Account(name="primary", home=self.primary, is_primary=True, **self._primary_limits())

    def named(self, account: Account, name: str) -> bool:
        """Whether `name` (from `accounts`, `order`, `ignore` or a job) means this account.

        Its own name; `primary` for the primary folder, whatever name it goes by; its folder label
        (`claude-ntb`) or the short form (`ntb`) for a profile; or its folder.
        """

        from agents_router import profiles

        if "/" in name or "\\" in name or name.startswith("~"):
            return profiles.same_folder(account.home, Path(name).expanduser())
        if name == account.name:
            return True
        if name == "primary":
            return profiles.same_folder(account.home, self.primary)
        if self.runtime != "claude":
            return False
        label = profiles.label(account.home)
        return name == label or (account.found and label in (f"claude-{name}", f"claude{name}"))

    def ignores(self, account: Account) -> bool:
        """Whether `[claude] ignore` names this account."""

        return any(self.named(account, item) for item in self.ignore or [])

    def worker_accounts(self) -> list[Account]:
        """Accounts that unpinned jobs may use, in configuration order.

        From a Claude Code session, the session's own folder (and other folders with its
        login) never takes its delegated jobs: the list may then be empty.
        """

        if self.runtime in OPT_IN_FAILOVER and self.failover == "off":
            return [self.primary_account()]
        found: list[Account] = []
        logins: set[str] = set()
        order = self.order or []

        def rank(item: Account) -> int:
            return next((index for index, name in enumerate(order) if self.named(item, name)), len(order))

        # One folder per login; with `order`, the one it lists first.
        ranked = sorted(self.found_accounts(), key=rank)
        for account in ranked:
            if account.logged_in is not True or (account.login and account.login in logins):
                continue  # logged out or unknown; or a second folder with a login already listed
            if account.login:
                logins.add(account.login)
            found.append(account)
        registered = [account for account in self.accounts if account.enabled]
        if self.session_home is not None and self.runtime == "claude":
            login = self.session_login()
            pool = [account for account in (*registered, *found)
                    if not self.is_session(account) and not (account.found and login and account.login == login)]
            if self.picked:
                return [account for account in pool if self._picked(account)]
            return pool
        if self.picked:
            chosen = [account for account in (self.primary_account(), *self.accounts, *found)
                      if self._picked(account) and account.enabled]
            if chosen:
                from agents_router.profiles import same_folder

                return [account for index, account in enumerate(chosen)
                        if not any(same_folder(account.home, other.home) for other in chosen[:index])]
        accounts = [*registered, *found]
        if self.include_primary or not accounts:
            primary = self.primary_account()
            if self.include_primary or not self.ignores(primary):  # `ignore` keeps it out of the fallback too
                from agents_router.profiles import same_folder

                accounts = [primary, *(account for account in accounts if not same_folder(account.home, primary.home))]
        return accounts

    def _picked(self, account: Account) -> bool:
        return any(self.named(account, name) for name in self.picked or [])

    def pool_accounts(self) -> list[Account]:
        """Accounts that may run workers, pinned or not (doctor --live checks these)."""

        if self.runtime in OPT_IN_FAILOVER:
            return [self.primary_account(), *(account for account in self.accounts if account.enabled)]
        return self.worker_accounts()

    def listed_accounts(self) -> list[Account]:
        """Accounts shown in reports: for opt-in runtimes the primary and every extra account, disabled ones too.

        For Claude, also the calling session's own profile and every found profile, so reports show them all.
        """

        if self.runtime in OPT_IN_FAILOVER:
            return [self.primary_account(), *self.accounts]
        listed = self.worker_accounts()
        if self.runtime == "claude" and (self.session_home is not None or self.found_accounts()):
            from agents_router.profiles import same_folder

            extra = []
            if self.session_home is not None:
                session = next((account for account in self.all_accounts() if self.is_session(account)), None)
                extra.append(session or Account(name="primary", home=self.session_home, is_primary=True))
            extra += self.all_accounts()
            for account in extra:
                if not any(same_folder(account.home, other.home) for other in listed):
                    listed.append(account)
        return listed

    def find(self, name: str) -> Account | None:
        """An account by name: `primary`, a registered name, a found profile, or a unique short form (`ntb`)."""

        if name == "primary":
            return self.primary_account()
        accounts = self.all_accounts()
        exact = next((account for account in accounts if account.name == name), None)
        if exact is not None:
            return exact
        short = [account for account in self.found_accounts() if account.name in (f"claude-{name}", f"claude{name}")]
        if len(short) == 1:
            return short[0]
        return None

    def matches(self, name: str) -> list[str]:
        """Profile names that end with `name`, for a hint when a short form is ambiguous or unknown."""

        return [account.name for account in self.all_accounts() if name and name in account.name]


@dataclass
class SubagentsConfig:
    prefer_roles: bool = True


@dataclass
class Config:
    runtimes: dict[str, RuntimeConfig]
    job_retention_days: int = 7
    max_depth: int = 1
    max_jobs: int | None = None  # cap on running jobs across all runtimes
    notes: list[str] = field(default_factory=list)  # settings agt ignored, each with its file and line
    roles: dict[str, Any] = field(default_factory=dict)  # name -> roles.Role, merged over every level
    subagents: SubagentsConfig = field(default_factory=SubagentsConfig)

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


def _room_steps(name: str, value: Any) -> list[list[float | int]]:
    if value is None:
        return []
    if not isinstance(value, list) or not value:
        raise ValueError(f"[{name}] max_jobs_by_room needs descending [percent, jobs] steps")
    previous, previous_cap = 101.0, None
    steps = []
    for step in value:
        if (not isinstance(step, list) or len(step) != 2
                or type(step[0]) not in (int, float) or not 0 <= step[0] <= 100
                or type(step[1]) is not int or step[1] < 0
                or step[0] >= previous or (previous_cap is not None and step[1] > previous_cap)):
            raise ValueError(f"[{name}] max_jobs_by_room needs descending percentages and nonnegative job caps")
        steps.append(list(step))
        previous, previous_cap = step
    if steps[-1][0] != 0:
        raise ValueError(f"[{name}] max_jobs_by_room must end with a 0 percent step")
    return steps


def _quota_pools(name: str, value: Any) -> list[QuotaPool]:
    if not isinstance(value, list):
        raise ValueError(f"[{name}] pools must be an array of tables")
    pools = []
    names = set()
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not NAME_RE.fullmatch(item["name"]):
            raise ValueError(f"[{name}] pool entries need a simple name")
        pool_name = item["name"]
        if pool_name in names:
            raise ValueError(f"[{name}] duplicate pool name '{pool_name}'")
        names.add(pool_name)
        models = item.get("models")
        if not isinstance(models, list) or not models or any(not isinstance(m, str) or not m.strip() for m in models):
            raise ValueError(f"[{name}] pool '{pool_name}' needs a nonempty models glob list")
        fields = {}
        for key in ("per_account", "total", "weekly_jobs", "cooldown_minutes"):
            value = item.get(key)
            if value is not None:
                if type(value) is not int or value < 1:
                    raise ValueError(f"[{name}] pool '{pool_name}' {key} must be a positive integer")
                fields[key] = value
        pools.append(QuotaPool(pool_name, [m.strip() for m in models], **fields))
    return pools


def _setup(runtime: str, value: Any) -> str:
    if value is None:
        return "linked"
    if value not in ("linked", "own") or runtime != "claude":
        raise ValueError(f"[{runtime}] account setup must be \"linked\" or \"own\" (Claude accounts only)")
    return str(value)


def _names(runtime: str, key: str, value: Any) -> list[str] | None:
    if value is None:
        return None
    if runtime != "claude" or not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"[{runtime}] {key} must be a list of Claude profile names, under [claude] only")
    return [item.strip() for item in value]


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
                setup=_setup(name, item.get("setup")),
            )
        )
    opt_in = name in OPT_IN_FAILOVER
    # On by default everywhere; for Antigravity, "off" keeps unpinned jobs on the primary account.
    failover = str(data.get("failover", "on-limit"))
    if failover not in FAILOVER_MODES:
        raise ValueError(f"[{name}] failover must be one of {FAILOVER_MODES}")
    connectors = str(data.get("connectors", "account"))
    if connectors not in ("account", "off"):
        raise ValueError(f"[{name}] connectors must be 'account' or 'off'")
    windows_sandbox = str(data.get("windows_sandbox", "unelevated"))
    if windows_sandbox not in WINDOWS_SANDBOX_MODES:
        raise ValueError(f"[{name}] windows_sandbox must be one of {WINDOWS_SANDBOX_MODES}")
    reasoning_summary = data.get("reasoning_summary", "detailed")
    if reasoning_summary not in REASONING_SUMMARY_MODES or ("reasoning_summary" in data and name != "codex"):
        raise ValueError(f"[{name}] reasoning_summary must be under [codex] and one of {REASONING_SUMMARY_MODES}")
    kiro_engine = data.get("engine", "auto")
    if kiro_engine not in KIRO_ENGINES or ("engine" in data and name != "kiro"):
        raise ValueError(f"[{name}] engine must be under [kiro] and one of {KIRO_ENGINES}")
    when_full = str(data.get("when_full", "refuse"))
    if when_full not in WHEN_FULL_MODES:
        raise ValueError(f"[{name}] when_full must be one of {WHEN_FULL_MODES}")
    reviewer = str(data.get("reviewer", "agy"))
    if reviewer not in REVIEWERS:
        raise ValueError(f"[{name}] reviewer must be one of {REVIEWERS}")
    web_search = data.get("web_search")
    if web_search is not None and (name != "codex" or web_search not in WEB_SEARCH_MODES):
        raise ValueError(f"[{name}] web_search must be under [codex] and one of {WEB_SEARCH_MODES}")
    network_access = data.get("network_access", True)
    if not isinstance(network_access, bool) or ("network_access" in data and name != "codex"):
        raise ValueError(f"[{name}] network_access must be true or false, under [codex] only")
    ceiling = float(data.get("usage_ceiling", 0.95))
    if not 0 < ceiling <= 1:
        raise ValueError(f"[{name}] usage_ceiling must be above 0 and at most 1")
    capacity_retries = data.get("capacity_retries", 3)
    capacity_retry_seconds = data.get("capacity_retry_seconds", 30)
    for key, value, minimum in (("capacity_retries", capacity_retries, 0),
                                ("capacity_retry_seconds", capacity_retry_seconds, 1)):
        if type(value) is not int or value < minimum:
            raise ValueError(f"[{name}] {key} must be an integer at least {minimum}")
    per_account = data.get("max_jobs_per_account")
    if per_account is not None and (type(per_account) is not int or per_account < 1):
        raise ValueError(f"[{name}] max_jobs_per_account must be an integer at least 1; remove the line for no cap")
    return RuntimeConfig(
        runtime=name,
        primary=_path(data.get("primary"), default_primary(name)),
        primary_from_settings=bool(isinstance(data.get("primary"), str) and data["primary"].strip()),
        binary=data.get("binary") or None,
        # These runtimes rotate through the primary account too, unless told otherwise.
        include_primary=bool(data.get("include_primary", opt_in)),
        failover=failover,
        max_jobs_per_account=per_account,
        connectors=connectors,
        edit_permission_mode=str(data.get("edit_permission_mode", "accept-edits" if name == "antigravity" else "auto")),
        default_model=data.get("default_model") or None,
        cooldown_minutes=max(1, int(data.get("cooldown_minutes", 60))),
        capacity_retries=capacity_retries,
        capacity_retry_seconds=capacity_retry_seconds,
        windows_sandbox=windows_sandbox,
        reasoning_summary=reasoning_summary,
        engine=kiro_engine,
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
        max_jobs_by_room=_room_steps(name, data.get("max_jobs_by_room")),
        pools=_quota_pools(name, data.get("pools", [])),
        web_search=web_search,
        network_access=network_access,
        accounts=accounts,
        order=_names(name, "order", data.get("order")),
        ignore=_names(name, "ignore", data.get("ignore")),
    )


TOP_KEYS = ("job_retention_days", "max_depth", "max_jobs")
USER_ONLY = ("primary", "binary", "accounts", "ignore")  # machine-wide: a workspace file can't set them
_legacy_noted = False


def _top_from(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_retention_days": max(1, int(data.get("job_retention_days", 7))),
        "max_depth": max(0, int(data.get("max_depth", 1))),
        "max_jobs": _opt_int(data.get("max_jobs")),
    }


def _fails(check) -> bool:
    try:
        check()
    except (TypeError, ValueError):
        return True
    return False


def _checked(layer: levels.Layer, data: dict[str, Any]) -> None:
    """Refuse a file with a bad value, naming the file and the value's line."""

    for name in RUNTIMES:
        table = data.get(name, {})
        if not isinstance(table, dict):
            raise levels.SettingsError(f"{levels.where(layer, None, name)}: {name} must be a [{name}] table")
        try:
            _runtime_from(name, table)
        except (TypeError, ValueError) as exc:
            culprit = next((key for key in table if _fails(lambda key=key: _runtime_from(name, {key: table[key]}))), None)
            raise levels.SettingsError(f"{levels.where(layer, name, culprit)}: {exc}") from None
    top = {key: data[key] for key in TOP_KEYS if key in data}
    try:
        _top_from(top)
    except (TypeError, ValueError) as exc:
        culprit = next((key for key in top if _fails(lambda key=key: _top_from({key: top[key]}))), None)
        raise levels.SettingsError(f"{levels.where(layer, None, culprit)}: {culprit}: {exc}") from None


def _note_legacy(notes: list[str]) -> None:
    legacy = legacy_config_path()
    if not legacy.exists():
        return
    note = f"{legacy} is no longer read; move its lines into {user_settings_path()}"
    notes.append(note)
    global _legacy_noted
    if not _legacy_noted:  # once per process, so an upgrade that dropped your accounts doesn't go unseen
        _legacy_noted = True
        print(f"agt: {note}", file=sys.stderr)


def load_config(path: Path | None = None, cwd: Path | str | None = None) -> Config:
    """The settings in effect in `cwd`'s workspace (the current folder by default).

    The user, workspace and local files are merged key by key, later levels
    winning. A workspace file can't set `primary`, `binary` or account
    tables; it can pick accounts by name (`accounts = ["max-2"]`). With
    `path`, only that file is read, as the user level.
    """

    notes: list[str] = []
    _note_legacy(notes)
    if path is not None:
        found = [layer for layer in [levels.read("user", path)] if layer]
    else:
        found = levels.layers(user_settings_path(), levels.find_workspace(cwd))
    config = from_layers(found, notes)
    set_session(config, session_folder())
    return config


def session_folder(host: str | None = None, env: dict[str, str] | None = None) -> Path | None:
    """The Claude folder of the Claude Code session that runs agt, or None when no Claude Code session does.

    Claude Code passes its environment to the commands and MCP servers it starts: `CLAUDECODE`
    says a session runs this, and `CLAUDE_CONFIG_DIR`, when set, names its folder; without it
    the session uses `~/.claude`.
    """

    from agents_router.hosts import detect_host

    env = dict(os.environ if env is None else env)
    if (host or detect_host(env=env)) != "claude":
        return None
    configured = env.get("CLAUDE_CONFIG_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".claude"


def set_session(config: Config, folder: Path | str | None) -> None:
    """Name the calling Claude Code session's folder (the orchestrator), or None for no session."""

    config.runtime("claude").session_home = Path(folder).expanduser() if folder else None


def defaults() -> Config:
    """Every setting at its default, as with no settings file."""

    return Config(runtimes={name: _runtime_from(name, {}) for name in RUNTIMES}, **_top_from({}))


def subagents_from_layers(found: list[levels.Layer]) -> SubagentsConfig:
    """The subagent settings alone, without loading runtimes or discovering accounts."""

    settings = SubagentsConfig()
    for layer in found:
        table = layer.data.get("subagents", {})
        if not isinstance(table, dict):
            raise levels.SettingsError(f"{levels.where(layer, None, 'subagents')}: subagents must be a [subagents] table")
        if "prefer_roles" in table:
            value = table["prefer_roles"]
            if not isinstance(value, bool):
                raise levels.SettingsError(f"{levels.where(layer, 'subagents', 'prefer_roles')}: "
                                           "[subagents] prefer_roles must be true or false")
            settings.prefer_roles = value
    return settings


def from_layers(found: list[levels.Layer], notes: list[str] | None = None) -> Config:
    """The settings these files give, lowest level first (see load_config)."""

    notes = [] if notes is None else notes
    top: dict[str, Any] = {}
    tables: dict[str, dict[str, Any]] = {name: {} for name in RUNTIMES}
    picks: dict[str, tuple[levels.Layer, list[str]]] = {}
    for layer in found:
        data = dict(layer.data)
        for name in RUNTIMES if layer.level != "user" else ():
            if not isinstance(data.get(name), dict):
                continue
            table = data[name] = dict(data[name])
            for key in [key for key in USER_ONLY if key in table]:
                value = table.pop(key)
                if key == "accounts" and isinstance(value, list) and all(isinstance(item, str) for item in value):
                    picks[name] = (layer, value)
                else:
                    notes.append(f"{levels.where(layer, name, key)}: [{name}] {key} is set in your user settings "
                                 f"only ({user_settings_path()}); ignored here")
        _checked(layer, data)
        top.update({key: data[key] for key in TOP_KEYS if key in data})
        for name in RUNTIMES:
            tables[name].update(data.get(name) or {})
    runtimes = {name: _runtime_from(name, tables[name]) for name in RUNTIMES}
    for name, (layer, chosen) in picks.items():
        known = {"primary", *(account.name for account in runtimes[name].accounts)}
        known |= {account.name for account in runtimes[name].found_accounts()} if any(
            item not in known for item in chosen) else set()
        for unknown in [item for item in chosen if item not in known]:
            notes.append(f"{levels.where(layer, name, 'accounts')}: [{name}] accounts names '{unknown}', "
                         f"which isn't one of your {name} accounts; ignored")
        runtimes[name].picked = [item for item in chosen if item in known] or None
    return Config(runtimes=runtimes, notes=notes, roles=roles.from_layers(found),
                  subagents=subagents_from_layers(found), **_top_from(top))


def _account_lines(runtime: str, account: Account) -> list[str]:
    lines = [
        f"[[{runtime}.accounts]]",
        f"name = {_toml_value(account.name)}",
        f"home = {_toml_value(str(account.home))}",
        f"enabled = {_toml_value(account.enabled)}",
    ]
    if account.fallback:
        lines.append("fallback = true")
    if account.setup != "linked":
        lines.append(f"setup = {_toml_value(account.setup)}")
    for key, value in (("plan", account.plan), ("max_jobs", account.max_jobs), ("max_effort", account.max_effort)):
        if value is not None:
            lines.append(f"{key} = {_toml_value(value)}")
    if account.models:
        lines.append(f"models = {json.dumps(account.models)}")
    return lines


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(str(value))


def dump_config(config: Config) -> str:
    """Serialize the config back to TOML (only the keys this tool understands)."""

    lines = [
        "# Agents Router configuration. See docs/configuration.md.",
        f"job_retention_days = {config.job_retention_days}",
        f"max_depth = {config.max_depth}",
    ]
    if config.max_jobs:
        lines.append(f"max_jobs = {config.max_jobs}")
    if not config.subagents.prefer_roles:
        lines += ["", "[subagents]", "prefer_roles = false"]
    for name in RUNTIMES:
        runtime = config.runtimes[name]
        lines += [
            "",
            f"[{name}]",
            f"primary = {_toml_value(str(runtime.primary))}",
            f"include_primary = {_toml_value(runtime.include_primary)}",
            f"failover = {_toml_value(runtime.failover)}",
            f"connectors = {_toml_value(runtime.connectors)}",
            f"edit_permission_mode = {_toml_value(runtime.edit_permission_mode)}",
            f"cooldown_minutes = {runtime.cooldown_minutes}",
            f"capacity_retries = {runtime.capacity_retries}",
            f"capacity_retry_seconds = {runtime.capacity_retry_seconds}",
        ]
        if runtime.binary:
            lines.append(f"binary = {_toml_value(runtime.binary)}")
        if runtime.default_model:
            lines.append(f"default_model = {_toml_value(runtime.default_model)}")
        if name == "codex" and runtime.windows_sandbox != "unelevated":
            lines.append(f"windows_sandbox = {_toml_value(runtime.windows_sandbox)}")
        if name == "kiro" and runtime.engine != "auto":
            lines.append(f"engine = {_toml_value(runtime.engine)}")
        if name == "codex" and runtime.reasoning_summary != "detailed":
            lines.append(f"reasoning_summary = {_toml_value(runtime.reasoning_summary)}")
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
            ("max_jobs_per_account", runtime.max_jobs_per_account),
        ):
            if value is not None:
                lines.append(f"{key} = {_toml_value(value)}")
        if runtime.primary_models:
            lines.append(f"primary_models = {json.dumps(runtime.primary_models)}")
        if runtime.usage_ceiling != 0.95:
            lines.append(f"usage_ceiling = {runtime.usage_ceiling}")
        if runtime.when_full != "refuse":
            lines.append(f"when_full = {_toml_value(runtime.when_full)}")
        if runtime.max_jobs_by_room:
            lines.append(f"max_jobs_by_room = {json.dumps(runtime.max_jobs_by_room)}")
        for key, value in (("order", runtime.order), ("ignore", runtime.ignore)):
            if value:
                lines.append(f"{key} = {json.dumps(value)}")
        if name == "codex" and runtime.web_search is not None:
            lines.append(f"web_search = {_toml_value(runtime.web_search)}")
        if name == "codex" and not runtime.network_access:
            lines.append("network_access = false")
        for pool in runtime.pools:
            lines += ["", f"[[{name}.pools]]", f"name = {_toml_value(pool.name)}", f"models = {json.dumps(pool.models)}"]
            for key in ("per_account", "total", "weekly_jobs", "cooldown_minutes"):
                value = getattr(pool, key)
                if value is not None:
                    lines.append(f"{key} = {value}")
        for account in runtime.accounts:
            lines += ["", *_account_lines(name, account)]
    return "\n".join(lines) + "\n"


def save_accounts(config: Config, path: Path | None = None) -> Path:
    """Write every runtime's `[[<runtime>.accounts]]` tables to the user settings, keeping every other line."""

    from agents_router import synclog

    location = path or user_settings_path()
    tables = {f"{name}.accounts" for name in RUNTIMES}
    blocks = ["\n".join(_account_lines(name, account)) for name in RUNTIMES for account in config.runtimes[name].accounts]
    rendered = "\n\n".join(blocks) + "\n" if blocks else ""
    synclog.notice_file(location, "user", None)
    written = levels.replace_tables(location, lambda header: header in tables, rendered)
    synclog.saved(written)
    return written


def save_config(config: Config, path: Path | None = None) -> Path:
    """Write every key agt knows as the user settings, keeping the file's `[share]` and `[roles.*]` tables.

    It writes `primary` and the defaults too, so agt's own commands don't use
    it: they change one key, or the account tables (`save_accounts`).
    """

    location = path or user_settings_path()
    try:
        text = location.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    share = levels.split(text, lambda header: header.split(".")[0] in ("share", "roles"))[1]
    return levels.write(location, dump_config(config) + ("\n" + "\n".join(share) + "\n" if share else ""))
