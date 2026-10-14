"""Account pools: usage state, cooldowns, per-account job slots and selection."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, BinaryIO

from cli_subagents.config import Account, RuntimeConfig, state_dir
from cli_subagents.fsutil import file_lock, read_json, try_lock, unlock, write_json

# Above this utilization (0..1) an account is only used when nothing else is free.
NEARLY_FULL = 0.97


def _usage_path(runtime: str):
    return state_dir() / f"{runtime}-usage.json"


def load_usage(runtime: str) -> dict[str, dict[str, Any]]:
    data = read_json(_usage_path(runtime), {})
    return data if isinstance(data, dict) else {}


def update_usage(runtime: str, account: str, **fields: Any) -> dict[str, Any]:
    """Merge fields into one account's usage record under a lock."""

    path = _usage_path(runtime)
    with file_lock(path.with_suffix(".lock")):
        data = load_usage(runtime)
        record = dict(data.get(account) or {})
        record.update({key: value for key, value in fields.items() if value is not None})
        record["updated"] = time.time()
        data[account] = record
        write_json(path, data)
    return record


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def record_rate_limit_event(runtime: str, account: str, info: dict[str, Any]) -> None:
    """Store the windows reported by a `rate_limit_event` (Claude) or equivalent."""

    windows: dict[str, Any] = {}
    unified = info.get("unifiedWindows")
    if isinstance(unified, dict):
        for name, window in unified.items():
            if isinstance(window, dict):
                windows[str(name)] = {
                    "utilization": _number(window.get("utilization")),
                    "resets_at": _number(window.get("resetsAt")),
                }
    elif info.get("rateLimitType"):
        windows[str(info["rateLimitType"])] = {
            "utilization": _number(info.get("utilization")),
            "resets_at": _number(info.get("resetsAt")),
        }
    fields: dict[str, Any] = {"status": info.get("status"), "windows": windows or None}
    resets_at = _number(info.get("resetsAt"))
    if info.get("status") == "rejected" and resets_at:
        fields["cooldown_until"] = resets_at
    update_usage(runtime, account, **fields)


def set_cooldown(runtime: str, account: str, until: float, reason: str) -> None:
    update_usage(runtime, account, cooldown_until=until, cooldown_reason=reason)


def max_utilization(record: dict[str, Any] | None) -> float | None:
    if not record:
        return None
    values = [
        float(window["utilization"])
        for window in (record.get("windows") or {}).values()
        if isinstance(window, dict) and isinstance(window.get("utilization"), (int, float))
    ]
    return max(values) if values else None


def cooldown_until(record: dict[str, Any] | None, now: float | None = None) -> float | None:
    now = now or time.time()
    until = (record or {}).get("cooldown_until")
    return float(until) if isinstance(until, (int, float)) and until > now else None


@dataclass
class Lease:
    runtime: str
    account: Account
    handle: BinaryIO

    def release(self) -> None:
        unlock(self.handle)


def try_lease(runtime: RuntimeConfig, account: Account) -> Lease | None:
    """Take one of the account's job slots, or None if all are busy."""

    for slot in range(runtime.max_jobs_per_account):
        path = state_dir() / "leases" / runtime.runtime / account.name / f"{slot}.lock"
        handle = try_lock(path)
        if handle is not None:
            return Lease(runtime.runtime, account, handle)
    return None


def busy_slots(runtime: RuntimeConfig, account: Account) -> int:
    busy = 0
    for slot in range(runtime.max_jobs_per_account):
        path = state_dir() / "leases" / runtime.runtime / account.name / f"{slot}.lock"
        handle = try_lock(path)
        if handle is None:
            busy += 1
        else:
            unlock(handle)
    return busy


def ranked_accounts(
    runtime: RuntimeConfig,
    *,
    pinned: str | None = None,
    exclude: set[str] | None = None,
    now: float | None = None,
) -> tuple[list[Account], float | None]:
    """Usable accounts best-first, plus the earliest cooldown end if none are usable."""

    now = now or time.time()
    usage = load_usage(runtime.runtime)
    candidates = runtime.worker_accounts()
    if pinned:
        account = runtime.find(pinned)
        if account is None:
            raise ValueError(f"no {runtime.runtime} account named '{pinned}'")
        candidates = [account]
    exclude = exclude or set()
    usable: list[Account] = []
    earliest: float | None = None
    for account in candidates:
        if account.name in exclude:
            continue
        until = cooldown_until(usage.get(account.name), now)
        if until is not None:
            earliest = until if earliest is None else min(earliest, until)
            continue
        usable.append(account)

    def key(account: Account) -> tuple[bool, float, float]:
        record = usage.get(account.name) or {}
        utilization = max_utilization(record)
        return (
            utilization is not None and utilization >= NEARLY_FULL,
            utilization if utilization is not None else 0.0,
            float(record.get("last_used") or 0.0),
        )

    usable.sort(key=key)
    return usable, (earliest if not usable else None)


def acquire(
    runtime: RuntimeConfig,
    *,
    pinned: str | None = None,
    exclude: set[str] | None = None,
) -> tuple[Lease | None, float | None, bool]:
    """Lease the best free account.

    Returns (lease, earliest_cooldown_end, all_busy). A None lease with
    all_busy=True means accounts exist but every slot is taken right now.
    """

    accounts, earliest = ranked_accounts(runtime, pinned=pinned, exclude=exclude)
    for account in accounts:
        lease = try_lease(runtime, account)
        if lease is not None:
            update_usage(runtime.runtime, account.name, last_used=time.time())
            return lease, None, False
    return None, earliest, bool(accounts)
