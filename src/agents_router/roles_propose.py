"""Propose a role table from installed CLIs, accounts and quota.

Interface (fixed; other modules call it):
    propose(loaded: config.Config, mode: str = "setup") -> dict
        mode "setup": a table for the base roles (Q5): read-only roles mix vendors, the Builder lists
        the strongest runtime with room first, the Decision Maker gets one provider.
        mode "rebalance": the current table with moves from remaining quota (Q8): an account under
        20% left in its current window, or cooling down, moves back in an edit or full role's order, and a
        read-only role swaps it for another account of that runtime with room (or drops it when
        other providers stay). Antigravity and Kiro print no usage figures, so only their cooldowns
        and recent jobs count (D11). Roles that are off are kept as they are.
        Returns {"roles": {name: {"access"?, "providers": [...], ...}}, "reasons": {name: "one line"}}.
        Rebalance returns every role, off ones included, so writing the table back loses nothing, and
        "quota": [usage_view.account_quota dicts], one per account it weighed: left_percent, resets_at,
        observed_at, cooling_until and text, in the words the room line uses.
        This dict, as JSON, is the file `agt roles config --from FILE` reads (it reads only "roles").

agt has no ranking of models, so "strongest" is a fixed order of runtimes (STRENGTH) and no
model is proposed: each provider runs its runtime's default model unless the user picks one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import time
from typing import Any

from agents_router import levels, roles, usage_view

LOW_LEFT = 0.20  # Q8: fixed, no setting; the hard stop stays usage_ceiling
RECENT_SECONDS = 5 * 3600  # "recent jobs": the last five-hour window
STRENGTH = ("claude", "codex", "antigravity", "kiro")  # Builder and Decision Maker: first that has room
READ_ORDER = ("codex", "antigravity", "claude", "kiro")  # read-only roles: other vendors before the Builder's
NO_USAGE = usage_view.NO_USAGE  # D11: quota unknown


@dataclass
class Candidate:
    runtime: str
    name: str | None  # the provider's account part; None when the runtime has one account
    account: str  # the account's own name, for messages and job counts
    left: float | None  # share left in the busiest open window; None when unknown
    cooling: float | None  # cooldown end
    recent: int  # jobs in the last five hours
    quota: dict[str, Any] = field(default_factory=dict)  # usage_view.account_quota: the figures and their words

    @property
    def low(self) -> bool:
        return self.cooling is not None or (self.left is not None and self.left < LOW_LEFT)

    def provider(self, effort: str | None = None) -> str:
        return str(roles.Provider(self.runtime, self.name, None, effort))

    def why(self) -> str:
        if self.cooling is not None:
            return f"{self.label()} is at its limit until {time.strftime('%H:%M', time.localtime(self.cooling))}"
        if self.left is not None:
            left = self.quota.get("left_percent")
            left = round(self.left * 100) if left is None else left
            text = f"{self.label()} has {left}% left (under {round(LOW_LEFT * 100)}%)"
            resets = self.quota.get("resets_at")
            return text + (f" until it resets at {time.strftime('%H:%M', time.localtime(resets))}" if resets else "")
        return f"{self.label()} has no usage data"

    def label(self) -> str:
        return f"{self.runtime}:{self.account}" if self.name else self.runtime


def _available(loaded: Any, runtime: str) -> bool:
    from agents_router.runtimes import get_runtime

    config = loaded.runtime(runtime)
    if get_runtime(runtime).find_binary(config):
        return runtime != "kiro" or Path(config.primary).expanduser().is_dir()
    return runtime == "claude" and Path(config.primary).expanduser().is_dir()


def _recent_jobs(now: float) -> dict[tuple[str, str], int]:
    from agents_router import jobs

    counts: dict[tuple[str, str], int] = {}
    for job in jobs.list_jobs(limit=500):
        if now - float(job.get("created_at") or 0) > RECENT_SECONDS:
            continue
        key = (str(job.get("runtime") or ""), str(job.get("account") or "primary"))
        counts[key] = counts.get(key, 0) + 1
    return counts


def candidates(loaded: Any, now: float | None = None) -> dict[str, list[Candidate]]:
    """Each installed runtime's accounts that may take jobs, best first."""

    from agents_router import accounts, limits

    now = now or time.time()
    recent = _recent_jobs(now)
    found: dict[str, list[Candidate]] = {}
    for runtime in roles.RUNTIMES:
        if not _available(loaded, runtime):
            continue
        config = loaded.runtime(runtime)
        listed = [account for account in config.worker_accounts() if account.enabled]
        usage = accounts.load_usage(runtime)
        items = []
        for account in listed:
            record = accounts.account_record(config, account, usage)
            used = None if runtime in NO_USAGE else limits.utilization(record, now)
            items.append(Candidate(runtime, account.name if len(listed) > 1 else None, account.name,
                                   None if used is None else max(0.0, 1.0 - used),
                                   accounts.cooldown_until(record, now), recent.get((runtime, account.name), 0),
                                   usage_view.account_quota(runtime, account, record, now)))
        items.sort(key=_rank)
        if items:
            found[runtime] = items
    return found


def _rank(item: Candidate) -> tuple[bool, float, int]:
    # Cooling or under 20% last; then most left (unknown counts as half); then fewest recent jobs.
    return (item.low, -(item.left if item.left is not None else 0.5), item.recent)


def _pick(found: dict[str, list[Candidate]], order: tuple[str, ...], count: int,
          avoid: tuple[str, ...] = ()) -> list[Candidate]:
    """The best account of `count` runtimes, by `order`, runtimes with room before those without, `avoid` last."""

    best = [found[runtime][0] for runtime in order if runtime in found]
    best.sort(key=lambda item: (item.low, item.runtime in avoid))  # stable: keeps `order` within each group
    return best[:count]


def _setup(loaded: Any) -> dict[str, Any]:
    found = candidates(loaded)
    table: dict[str, dict[str, Any]] = {}
    reasons: dict[str, str] = {}
    if not found:
        return {"roles": table, "reasons": reasons}
    builder = _pick(found, STRENGTH, 2)
    if len(builder) == 1 and len(found[builder[0].runtime]) > 1:
        builder.append(found[builder[0].runtime][1])  # one runtime: its second account is the fallback
    reviewer = _pick(found, READ_ORDER, 2, avoid=(builder[0].runtime,))
    verifier = _pick(found, READ_ORDER, 2, avoid=tuple(item.runtime for item in reviewer))
    decider = _pick(found, ("codex", "claude", "antigravity", "kiro"), 1, avoid=(builder[0].runtime,))
    strong = lambda item: "high" if item.runtime in ("claude", "codex") else None  # noqa: E731
    table["decision-maker"] = {"providers": [decider[0].provider(strong(decider[0]))]}
    reasons["decision-maker"] = "one provider, so it settles a conflict instead of adding another view"
    table["reviewer"] = {"providers": [item.provider() for item in reviewer]}
    reasons["reviewer"] = _mix_reason(reviewer)
    table["builder"] = {"providers": [item.provider() for item in builder]}
    reasons["builder"] = ("strongest runtime with room first" + (", then " + builder[1].label() if len(builder) > 1 else "")
                          + _low_note(builder))
    table["verifier"] = {"providers": [item.provider() for item in verifier]}
    reasons["verifier"] = _mix_reason(verifier)
    check(table)
    return {"roles": table, "reasons": reasons}


def _mix_reason(items: list[Candidate]) -> str:
    if len(items) > 1:
        return "two vendors, for independent views" + _low_note(items)
    return "the only runtime with an account here" + _low_note(items)


def _low_note(items: list[Candidate]) -> str:
    low = [item.why() for item in items if item.low]
    return f" ({'; '.join(low)})" if low else ""


def _state(loaded: Any, provider: roles.Provider, found: dict[str, list[Candidate]]) -> Candidate | None:
    """The candidate a provider runs on, or None when its runtime or account isn't usable here."""

    items = found.get(provider.runtime, [])
    if provider.account is None:
        return items[0] if items else None
    account = loaded.runtime(provider.runtime).find(provider.account)
    name = account.name if account is not None else provider.account
    return next((item for item in items if item.account == name), None)


def _rebalance(loaded: Any) -> dict[str, Any]:
    found = candidates(loaded)
    table: dict[str, dict[str, Any]] = {}
    reasons: dict[str, str] = {}
    for role in roles.ordered(loaded.roles):
        current = role.as_table()
        table[role.name] = current
        if not role.enabled:
            reasons[role.name] = "off; skipped"
            continue
        states = [(provider, _state(loaded, provider, found)) for provider in role.providers]
        low = [(provider, state) for provider, state in states if state is not None and state.low]
        if not low:
            reasons[role.name] = "no change"
            continue
        if role.access in roles.ONE_AT_A_TIME:
            order = [p for p, s in states if s is None or not s.low] + [p for p, _ in low]
            if order == role.providers:
                reasons[role.name] = "no change (" + "; ".join(state.why() for _, state in low) + ", but nothing has more room)"
                continue
            table[role.name] = dict(current, providers=[str(p) for p in order])
            reasons[role.name] = "; ".join(state.why() for _, state in low) + ": moved back in the order"
            continue
        new, why = _read_moves(role, states, found)
        if new != [str(p) for p in role.providers]:
            table[role.name] = dict(current, providers=new)
        reasons[role.name] = "; ".join(why)
    check({name: item for name, item in table.items()})
    quota = []  # each runtime's accounts in the order the settings list them, not ranked
    for runtime, items in found.items():
        listed = [account.name for account in loaded.runtime(runtime).worker_accounts()]
        for item in sorted(items, key=lambda item: listed.index(item.account)):
            quota.append({key: value for key, value in item.quota.items() if key != "known"})
    return {"quota": quota, "roles": table, "reasons": reasons}


def _read_moves(role: roles.Role, states: list[tuple[roles.Provider, Candidate | None]],
                found: dict[str, list[Candidate]]) -> tuple[list[str], list[str]]:
    """A read-only role: swap a low account for another of its runtime with room, else drop it if others stay."""

    used = {(p.runtime, s.account) for p, s in states if s is not None}
    keep = sum(1 for _, s in states if s is None or not s.low)
    new: list[str] = []
    why: list[str] = []
    for provider, state in states:
        if state is None or not state.low:
            new.append(str(provider))
            continue
        spare = next((item for item in found.get(provider.runtime, [])
                      if not item.low and (provider.runtime, item.account) not in used), None)
        if spare is not None:
            used.add((provider.runtime, spare.account))
            swapped = roles.Provider(provider.runtime, spare.account, provider.model, provider.effort)
            new.append(str(swapped))
            why.append(f"{state.why()}: swapped for {spare.label()}")
        elif keep > 0:
            why.append(f"{state.why()}: left out while the others run")
        else:
            new.append(str(provider))
            why.append(f"{state.why()}, but no other provider has room: no change")
    return new, why


def check(table: dict[str, dict[str, Any]]) -> dict[str, roles.Role]:
    """The roles a proposed table gives, checked as a settings file would be. Raises SettingsError."""

    layer = levels.parse("user", Path("<proposed roles>"), roles.render_tables(table))
    return roles.from_layers([layer])


def propose(loaded, mode="setup"):
    if mode == "setup":
        return _setup(loaded)
    if mode == "rebalance":
        return _rebalance(loaded)
    raise ValueError(f"unknown proposal mode {mode!r}: setup or rebalance")
