"""Roles: named jobs (Reviewer, Builder, ...) that pick their own provider.

A role lives in `[roles.<name>]` in any settings file:

    [roles.reviewer]
    access    = "read"                                   # read | edit | full
    providers = ["codex:work/gpt-6.1-sol:high", "antigravity:primary"]

    [roles.builder]
    access    = "edit"
    providers = ["claude:claude-work/opus", "codex:work"]   # order = priority

A read role runs every provider in parallel; an edit or full role runs one,
the first in its order that has room. Full access is edit plus every tool, the
shell included, with no sandbox or permission prompts; only a custom role can
have it. `enabled = false` turns a role off, base roles included, so you can
use your own agent instead. A custom role's `prompt_file` may be relative: to
the folder of the settings file that names it.

Levels merge per role and per key, later levels winning; a `providers` list
replaces the one below it. Loading checks only the shape: a provider naming
an account you don't have is skipped when a job runs, with the reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import os
import re
from typing import Any

from agents_router import levels
EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max")  # plans.EFFORTS; plans imports config, which imports this

RUNTIMES = ("claude", "codex", "antigravity", "kiro")
ACCESS = ("read", "edit", "full")
ONE_AT_A_TIME = ("edit", "full")  # roles that change things: one provider, the first in their order with room
BASE_ROLES: dict[str, str] = {"decision-maker": "read", "reviewer": "read", "builder": "edit", "verifier": "read"}
BASE_DESCRIPTIONS: dict[str, str] = {
    "decision-maker": "Use only when reviewers or providers disagree: send just the points in conflict and it decides.",
    "reviewer": ("Use after every bug fix, feature or new tests (not a typo or one-line edit), before your final "
                 "message and without being asked: the user wants those changes reviewed. Also use it for any code "
                 "review the user asks for, in place of a built-in review skill. Reviews the change and returns each "
                 "provider's findings."),
    "builder": "Use for edits: makes the change on the first provider with room.",
    "verifier": "Use after the Builder: checks the change works and the task is met.",
}
# Every earlier default of a base role's "when to call" sentence (append only): an agent file still holding one was
# never edited, so a sync moves it to today's default instead of keeping it as yours (role_agents.unedited).
EARLIER_DESCRIPTIONS: dict[str, tuple[str, ...]] = {
    "reviewer": ("Use before calling non-trivial work done: reviews a change and returns each provider's findings.",),
}
RESERVED = RUNTIMES  # the built-in relay agents agt-claude, agt-codex, ... own these names
KEYS = ("access", "providers", "enabled", "description", "prompt_file")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")


class RoleError(ValueError):
    """A roles mistake by the caller (exit 2)."""


@dataclass(frozen=True)
class Provider:
    runtime: str
    account: str | None = None  # None: the runtime's only or first account
    model: str | None = None
    effort: str | None = None

    def __str__(self) -> str:
        text = self.runtime
        if self.account or self.model:
            text += f":{self.account or ''}"
        if self.model:
            text += f"/{self.model}"
        if self.effort:
            text += f":{self.effort}"
        return text

    def entry(self) -> str:
        """The provider without model and effort: what a grid row and `--set-model` name."""

        return f"{self.runtime}:{self.account}" if self.account else self.runtime

    def same_entry(self, other: "Provider") -> bool:
        return self.runtime == other.runtime and (self.account or None) == (other.account or None)


def parse_provider(text: Any) -> Provider:
    """`<runtime>[:<account>][/<model>][:<effort>]`; the last `:` part is effort only when it's a known level."""

    if not isinstance(text, str) or not text.strip():
        raise RoleError("a provider is a string like codex:work/gpt-6.1-sol:high")
    raw = text.strip()
    runtime, _, rest = raw.partition(":")
    if runtime not in RUNTIMES:
        raise RoleError(f"provider {raw!r}: runtime must be one of {', '.join(RUNTIMES)}")
    effort = None
    head, sep, tail = rest.rpartition(":")
    if sep and tail in EFFORTS:
        rest, effort = head, tail
    elif not sep and rest in EFFORTS and "/" not in rest:
        rest, effort = "", rest
    account, slash, model = rest.partition("/")
    if slash and not model:
        raise RoleError(f"provider {raw!r}: nothing after /")
    if account and not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", account):
        raise RoleError(f"provider {raw!r}: {account!r} isn't an account name")
    return Provider(runtime, account or None, model or None, effort)


@dataclass
class Role:
    name: str
    access: str
    providers: list[Provider] = field(default_factory=list)
    enabled: bool = True
    description: str | None = None
    prompt_file: str | None = None
    sources: dict[str, levels.Layer] = field(default_factory=dict)  # key -> the layer that set it

    @property
    def base(self) -> bool:
        return self.name in BASE_ROLES

    @property
    def use_when(self) -> str:
        return self.description or BASE_DESCRIPTIONS.get(self.name) or f"Use for the {self.name} role."

    def level(self) -> str | None:
        """The highest level that sets anything for this role (where a change goes by default)."""

        found = [layer.level for layer in self.sources.values()]
        return max(found, key=levels.LEVELS.index) if found else None

    def prompt(self) -> str | None:
        if not self.prompt_file:
            return None
        location = Path(os.path.expandvars(self.prompt_file)).expanduser()
        layer = self.sources.get("prompt_file")
        if not location.is_absolute() and layer is not None:  # relative to the settings file that names it
            location = layer.path.parent / location
        try:
            return location.read_text(encoding="utf-8")
        except OSError:
            return None

    def as_table(self) -> dict[str, Any]:
        table: dict[str, Any] = {}
        if not self.base:
            table["access"] = self.access
        table["providers"] = [str(p) for p in self.providers]
        if not self.enabled:
            table["enabled"] = False
        if self.description:
            table["description"] = self.description
        if self.prompt_file:
            table["prompt_file"] = self.prompt_file
        return table


def check_name(name: str) -> None:
    if name in RESERVED:
        raise RoleError(f"the role name {name} belongs to the built-in relay agent agt-{name}; pick another name")
    if not NAME_RE.match(name):
        raise RoleError(f"{name!r} isn't a role name: lowercase letters, digits, - and _, starting with a letter or digit")


def _check_key(name: str, key: str, value: Any) -> None:
    if key not in KEYS:
        raise RoleError(f"[roles.{name}] has no key {key}; keys: {', '.join(KEYS)}")
    if key == "access":
        if value not in ACCESS:
            raise RoleError(f"[roles.{name}] access must be \"read\", \"edit\" or \"full\", not {value!r}")
        if name in BASE_ROLES and value != BASE_ROLES[name]:
            raise RoleError(f"{name} is a base role with {BASE_ROLES[name]} access, which can't change; "
                            "make a custom role for other access")
    elif key == "providers":
        if not isinstance(value, list):
            raise RoleError(f"[roles.{name}] providers must be a list of provider strings")
        for item in value:
            parse_provider(item)
    elif key == "enabled":
        if not isinstance(value, bool):
            raise RoleError(f"[roles.{name}] enabled must be true or false")
    elif not isinstance(value, str):
        raise RoleError(f"[roles.{name}] {key} must be a string")


def _fix(layer: levels.Layer, command: str) -> str:
    """`; fix: <command> --<level>`, the agt command that repairs this file; nothing for a table not in a file."""

    return "" if str(layer.path).startswith("<") else f"; fix: agt roles config {command} --{layer.level}"


def check_layer(layer: levels.Layer) -> None:
    """Refuse a file whose [roles.*] has a bad shape, naming the file and line and the command that fixes it."""

    tables = layer.data.get("roles", {})
    if not isinstance(tables, dict):
        raise levels.SettingsError(f"{levels.where(layer, None, 'roles')}: roles must be [roles.<name>] tables")
    for name, table in tables.items():
        try:
            check_name(name)
        except RoleError as exc:
            raise levels.SettingsError(f"{levels.where(layer, 'roles.' + name, None)}: {exc}") from None
        if not isinstance(table, dict):
            raise levels.SettingsError(f"{layer.path}: roles.{name} must be a [roles.{name}] table")
        for key, value in table.items():
            try:
                _check_key(name, key, value)
            except RoleError as exc:
                fix = _fix(layer, f"{name} --only <providers>" if key == "providers" else f"{name} --unset {key}")
                raise levels.SettingsError(f"{levels.where(layer, 'roles.' + name, key)}: {exc}{fix}") from None


def from_layers(found: list[levels.Layer]) -> dict[str, Role]:
    """The roles these files give, lowest level first. Raises SettingsError on a bad shape."""

    merged: dict[str, dict[str, Any]] = {}
    sources: dict[str, dict[str, levels.Layer]] = {}
    for layer in found:
        check_layer(layer)
        for name, table in (layer.data.get("roles") or {}).items():
            merged.setdefault(name, {}).update(table)
            sources.setdefault(name, {}).update({key: layer for key in table})
    out: dict[str, Role] = {}
    for name, table in merged.items():
        access = BASE_ROLES.get(name) or table.get("access")
        last = sources[name].get("providers") or next(iter(sources[name].values()))
        enabled = table.get("enabled", True)
        if access is None:
            if not enabled:
                continue  # a custom role turned off before it was ever made: nothing to run
            raise levels.SettingsError(f"{levels.where(last, 'roles.' + name, None)}: custom role {name} needs "
                                       "access = \"read\", \"edit\" or \"full\"" + _fix(last, f"{name} --access read"))
        providers = [parse_provider(item) for item in table.get("providers", [])]
        if enabled and not providers:
            hint = (f"role {name} has {access} access, so its providers need an order" if access in ONE_AT_A_TIME
                    else f"role {name} has no providers")
            raise levels.SettingsError(f"{levels.where(last, 'roles.' + name, 'providers')}: {hint}; "
                                       f"to stop using it: agt roles config --disable {name}")
        out[name] = Role(name, access, providers, enabled, table.get("description"), table.get("prompt_file"),
                         dict(sources[name]))
    return out


def ordered(roles: dict[str, Role]) -> list[Role]:
    """Base roles first, in their fixed order, then custom roles by name."""

    base = [roles[name] for name in BASE_ROLES if name in roles]
    return base + sorted((role for name, role in roles.items() if name not in BASE_ROLES), key=lambda r: r.name)


def render_tables(roles: dict[str, dict[str, Any]]) -> str:
    """TOML for `[roles.<name>]` tables, in the order given."""

    from agents_router.share import codex_toml

    blocks = []
    for name, table in roles.items():
        lines = [f"[roles.{name}]"]
        for key in KEYS:
            if key in table:
                lines.append(f"{key} = {codex_toml.render_value(table[key])}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def unknown_role(name: str, roles: dict[str, Role]) -> RoleError:
    known = ", ".join(role.name for role in ordered(roles)) or "none yet (agt setup or agt roles config sets them up)"
    return RoleError(f"there's no role {name}. Roles: {known}")
