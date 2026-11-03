"""`agt roles show|propose|config`: see and change which providers each role uses.

`agt roles` (or `show`) prints the roles table: provider entries as rows,
roles as columns, a tick for a read role and the place in the order for an
edit role, with roles that are off and providers agt can't use marked.
`propose` prints a role table from your CLIs, accounts and quota (`--json`
gives the file `config --from` reads). `config` changes roles: with no flags a
person at a terminal gets the grid, anyone else the same table read-only.

Where a change goes: `--user`, `--workspace` or `--local`; with none named,
the file where the role is set now (your `settings.local.toml` when it's the
committed workspace file), else your user file; `--from` too. One command
writes one file. `<role> --unset <key>` removes a key from that file, the fix
agt's load error names for a bad hand edit.
Every write takes a backup first, goes to agt's log, and prints one undo
command, which is refused once the file has changed since.

From an agent, a change that widens edit access (a provider joins an edit role,
`--order` brings in a new entry, a custom role made or turned edit) and every
`--enable`/`--disable` needs `--user-said "<the user's words>"`; without it the
command exits 3 and writes nothing. A refused part refuses the whole command
(exit 2), so the file stays as it was.
"""

from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass, field, replace
import hashlib
import json
from pathlib import Path
import shlex
import sys
import time
from typing import Any

from agents_router import config, fsutil, levels, plans, roles, roles_grid, settings_write, synclog, ui

LEVEL_FLAGS = tuple(f"--{level}" for level in levels.LEVELS)
RoleError = roles.RoleError

Tables = dict[str, dict[str, Any]]


# Command line ---------------------------------------------------------------------------------------


def add_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("roles", help="See and change roles: which providers each role uses (show, propose, config).",
                            description=__doc__.split("\n\n", 1)[0])
    actions = parser.add_subparsers(dest="roles_action")
    show = actions.add_parser("show", help="The roles table (the same as `agt roles`).")
    show.add_argument("--json", action="store_true")
    propose = actions.add_parser("propose", help="A role table from your CLIs, accounts and quota; writes nothing.")
    propose.add_argument("--rebalance", action="store_true", help="Move providers by the quota left, not a fresh table.")
    propose.add_argument("--json", action="store_true", help="The proposal as the file `agt roles config --from` reads.")
    change = actions.add_parser("config", help="Change roles: the grid at a terminal, or flags.")
    change.add_argument("role", nargs="?", help="The role the provider flags change (a new name makes a custom role).")
    change.add_argument("--from", dest="from_file", metavar="FILE",
                        help="Write the role tables in this JSON file (from `agt roles propose --json`).")
    for flag, text in (("only", "The role's providers, replacing the list."), ("add", "Providers to add."),
                       ("remove", "Providers to remove."), ("order", "An edit role's providers in priority order.")):
        change.add_argument(f"--{flag}", metavar="ENTRIES", help=text + " Comma-separated.")
    change.add_argument("--set-model", action="append", default=[], metavar="ENTRY=MODEL",
                        help="The model of an entry (codex:work=gpt-6.1-sol) in every role using it.")
    change.add_argument("--set-effort", action="append", default=[], metavar="ENTRY=EFFORT",
                        help="The effort of an entry in every role using it.")
    change.add_argument("--enable", action="append", default=[], metavar="ROLES", help="Turn roles on.")
    change.add_argument("--disable", action="append", default=[], metavar="ROLES", help="Turn roles off.")
    change.add_argument("--access", choices=roles.ACCESS, help="A custom role's access.")
    change.add_argument("--prompt", metavar="TEXT", help="A custom role's instructions, added before each task.")
    change.add_argument("--description", metavar="TEXT", help="A custom role's 'use me when' line.")
    change.add_argument("--delete", action="store_true", help="Delete a custom role from the file.")
    change.add_argument("--unset", action="append", default=[], metavar="KEYS",
                        help="Remove keys from the role's table in this file (a bad hand edit, or an override of a "
                             "lower level).")
    change.add_argument("--user", action="store_true", help="Your user settings (every workspace).")
    change.add_argument("--workspace", action="store_true", help="This workspace's .agt/settings.toml (committed).")
    change.add_argument("--local", action="store_true", help="This workspace's .agt/settings.local.toml (yours).")
    change.add_argument("--dry-run", action="store_true", help="Print the change and write nothing.")
    change.add_argument("--json", action="store_true")
    change.add_argument("--user-said", metavar="WORDS",
                        help="From an agent: the user's own words, for a change that widens edit access or turns a role "
                             "on or off.")
    change.add_argument("--if-unchanged", metavar="HASH", help=argparse.SUPPRESS)  # in the undo command


def command(args: argparse.Namespace) -> int:
    action = args.roles_action or "show"
    if action == "show":
        return _show(getattr(args, "json", False))
    if action == "propose":
        return _propose(args)
    edit = _edit_from_args(args)
    if edit.empty():
        return _no_flags(args)
    with settings_write.locked():  # re-read and write under one lock, so two writers never interleave
        return _write(args, edit)


def _items(values: str | list[str] | None) -> list[str] | None:
    if values is None:
        return None
    values = [values] if isinstance(values, str) else values
    return list(dict.fromkeys(item.strip() for value in values for item in value.split(",") if item.strip()))


# What a command changes -----------------------------------------------------------------------------


@dataclass
class Edit:
    """One `agt roles config` change, from flags, `--from` or the grid."""

    role: str | None = None
    only: list[roles.Provider] | None = None
    add: list[roles.Provider] | None = None
    remove: list[roles.Provider] | None = None
    order: list[roles.Provider] | None = None
    set_model: list[tuple[roles.Provider, str | None]] = field(default_factory=list)
    set_effort: list[tuple[roles.Provider, str | None]] = field(default_factory=list)
    enable: list[str] = field(default_factory=list)
    disable: list[str] = field(default_factory=list)
    access: str | None = None
    prompt: str | None = None
    description: str | None = None
    delete: bool = False
    unset: list[str] = field(default_factory=list)  # keys removed from the role's table in the target file
    tables: dict[str, dict[str, Any] | None] | None = None  # --from: whole tables (None removes one)
    assign: dict[str, dict[str, Any]] | None = None  # the grid: providers and enabled per role
    grid: bool = False

    def role_ops(self) -> bool:
        return any(value is not None for value in (self.only, self.add, self.remove, self.order, self.access,
                                                   self.prompt, self.description)) or self.delete or bool(self.unset)

    def empty(self) -> bool:
        return not (self.role_ops() or self.set_model or self.set_effort or self.enable or self.disable
                    or self.tables is not None or self.assign)


def _providers(values: str | None, flag: str) -> list[roles.Provider] | None:
    items = _items(values)
    if items is None:
        return None
    if not items and flag != "only":
        raise RoleError(f"--{flag} needs at least one provider, like codex:work")
    return [roles.parse_provider(item) for item in items]


def _settings(values: list[str], flag: str) -> list[tuple[roles.Provider, str | None]]:
    found = []
    for value in values:
        entry, sep, chosen = value.partition("=")
        if not sep:
            raise RoleError(f"--{flag} takes <entry>=<value>, like codex:work=high")
        provider = roles.parse_provider(entry)
        if provider.model or provider.effort:
            raise RoleError(f"--{flag}: name the entry without model or effort, like {provider.entry()}")
        chosen = chosen.strip() or None
        if flag == "set-effort" and chosen is not None and chosen not in roles.EFFORTS:
            raise RoleError(f"--set-effort: {chosen} isn't an effort level; levels: {', '.join(roles.EFFORTS)}")
        found.append((provider, chosen))
    return found


def _read_from(path: str) -> dict[str, dict[str, Any] | None]:
    try:
        data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except OSError as exc:
        raise RoleError(f"--from: {path} can't be read ({exc.strerror or exc})") from None
    except ValueError as exc:
        raise RoleError(f"--from: {path} isn't JSON ({exc})") from None
    tables = data.get("roles") if isinstance(data, dict) else None
    if not isinstance(tables, dict):
        raise RoleError(f"--from: {path} needs {{\"roles\": {{<name>: <table>}}}}, as `agt roles propose --json` prints")
    for name, table in tables.items():
        roles.check_name(name)
        if table is not None and not isinstance(table, dict):
            raise RoleError(f"--from: roles.{name} must be a table (or null to remove it)")
    return tables


def _edit_from_args(args: argparse.Namespace) -> Edit:
    edit = Edit(role=args.role, only=_providers(args.only, "only"), add=_providers(args.add, "add"),
                remove=_providers(args.remove, "remove"), order=_providers(args.order, "order"),
                set_model=_settings(args.set_model, "set-model"), set_effort=_settings(args.set_effort, "set-effort"),
                enable=_items(args.enable) or [], disable=_items(args.disable) or [], access=args.access,
                prompt=args.prompt, description=args.description, delete=args.delete,
                unset=_items(args.unset) or [])
    if args.from_file:
        if not edit.empty() or args.role:
            raise RoleError("--from writes the tables in its file; use it without other changes")
        edit.tables = _read_from(args.from_file)
        return edit
    if edit.only is not None and (edit.add is not None or edit.remove is not None):
        raise RoleError("--only replaces the whole list; use it alone, or --add and --remove")
    if edit.delete and any(value is not None for value in (
            edit.only, edit.add, edit.remove, edit.order, edit.access, edit.prompt, edit.description)):
        raise RoleError("--delete removes the role; use it without other changes to that role")
    if edit.unset and (edit.delete or any(value is not None for value in (
            edit.only, edit.add, edit.remove, edit.order, edit.access, edit.prompt, edit.description))):
        raise RoleError("--unset removes keys from the role's table; use it without other changes to that role")
    if edit.role_ops() and not args.role:
        raise RoleError("name the role first, e.g. agt roles config builder --add codex:work")
    if args.role and not edit.role_ops():
        raise RoleError(f"say what to change in {args.role}: --only, --add, --remove or --order its providers,"
                        f" --unset a key (or --enable/--disable {args.role})")
    both = set(edit.enable) & set(edit.disable)
    if both:
        raise RoleError(f"{', '.join(sorted(both))}: --enable and --disable at once")
    if args.role:
        roles.check_name(args.role)
    return edit


# Raw tables, before anything is checked (so a broken hand edit can be fixed) ---------------------


def _raw(layer: levels.Layer | None) -> Tables:
    data = layer.data.get("roles") if layer else None
    return {name: table for name, table in data.items() if isinstance(table, dict)} if isinstance(data, dict) else {}


def _merge(found: list[Tables]) -> Tables:
    merged: Tables = {}
    for tables in found:
        for name, table in tables.items():
            merged.setdefault(name, {}).update(table)
    return merged


def _access(name: str, table: dict[str, Any] | None) -> str | None:
    return roles.BASE_ROLES.get(name) or (table or {}).get("access")


def _parse(text: Any) -> roles.Provider | None:
    try:
        return roles.parse_provider(text)
    except RoleError:
        return None


def _listed(table: dict[str, Any] | None) -> list[Any]:
    """A table's providers list; empty when a hand edit made it something else."""

    value = (table or {}).get("providers", [])
    return value if isinstance(value, list) else []


def _fits(wanted: roles.Provider, have: roles.Provider | None) -> bool:
    """`codex:work` names any codex:work provider; a model or effort given must match too."""

    return have is not None and wanted.same_entry(have) and wanted.model in (None, have.model) \
        and wanted.effort in (None, have.effort)


def _match(wanted: roles.Provider, current: list[str]) -> str:
    return next((text for text in current if _fits(wanted, _parse(text))), str(wanted))


def _entries(table: dict[str, Any] | None) -> list[str]:
    found = [_parse(text) for text in _listed(table)]
    return list(dict.fromkeys(provider.entry() for provider in found if provider))


def _order_names(names: Any) -> list[str]:
    names = list(dict.fromkeys(names))
    return [name for name in roles.BASE_ROLES if name in names] + sorted(name for name in names if name not in roles.BASE_ROLES)


def _unknown(name: str, known: list[str]) -> RoleError:
    listed = ", ".join(_order_names(known)) or "none yet (agt setup or agt roles config sets them up)"
    return RoleError(f"there's no role {name}. Roles: {listed}. To make a custom role: "
                     f"agt roles config {name} --access read --only <provider>")


def prompt_path(name: str, level: str, location: Path) -> tuple[Path, str]:
    """Where --prompt's text goes, and what prompt_file says: in the workspace file, `roles/<name>.md` beside it
    (relative, so the committed file works in every clone); in the local file, `roles/<name>.local.md` beside it,
    kept out of git; otherwise your user folder, by its full path."""

    if level == "workspace":
        return location.parent / "roles" / f"{name}.md", f"roles/{name}.md"
    if level == "local":
        return location.parent / "roles" / f"{name}.local.md", f"roles/{name}.local.md"
    target = config.user_settings_path().parent / "roles" / f"{name}.md"
    return target, str(target)


def _apply(edit: Edit, tables: Tables, lower: Tables, known: list[str], level: str, location: Path) -> list[str]:
    """Make `edit` in `tables` (the target file's own [roles.*]); `lower` is what the files below it set.
    Returns the summary lines of --set-model and --set-effort."""

    def view(name: str) -> dict[str, Any]:
        return {**lower.get(name, {}), **tables.get(name, {})}

    def own(name: str) -> dict[str, Any]:
        return tables.setdefault(name, {})

    def check_known(name: str) -> None:
        roles.check_name(name)
        if name not in roles.BASE_ROLES and name not in known and name not in tables:
            raise _unknown(name, known)

    def switch(name: str, on: bool) -> None:
        if not on:
            own(name)["enabled"] = False
        elif lower.get(name, {}).get("enabled", True) is False:
            own(name)["enabled"] = True
        else:
            tables.get(name, {}).pop("enabled", None)

    summary: list[str] = []
    for name, table in (edit.tables or {}).items():
        if table is None:
            tables.pop(name, None)
            continue
        clean = dict(table)
        if name in roles.BASE_ROLES and clean.get("access") == roles.BASE_ROLES[name]:
            clean.pop("access")
        for key, value in clean.items():
            roles._check_key(name, key, value)
        tables[name] = clean
    if edit.role:
        summary += _apply_role(edit, tables, lower, known, level, location, view, own)
    for flag, pairs, attribute in (("model", edit.set_model, "model"), ("effort", edit.set_effort, "effort")):
        for entry, value in pairs:
            used = []
            for name in _order_names([*lower, *tables]):
                current = _listed(view(name))
                parsed = [_parse(text) for text in current]
                if not any(provider and provider.same_entry(entry) for provider in parsed):
                    continue
                changed = [str(replace(provider, **{attribute: value})) if provider and provider.same_entry(entry)
                           else text for text, provider in zip(current, parsed)]
                if changed != current:
                    own(name)["providers"] = list(dict.fromkeys(changed))
                used.append(name)
            if not used:
                raise RoleError(f"no role uses {entry.entry()}" + (f" in the {level} file's view" if level != "local" else ""))
            summary.append(f"{flag:<9} {entry.entry()} → {value or 'default'} in {', '.join(used)}")
    for name in edit.disable:
        check_known(name)
        switch(name, False)
    for name in edit.enable:
        check_known(name)
        switch(name, True)
    for name, table in (edit.assign or {}).items():
        if "providers" in table:
            own(name)["providers"] = list(table["providers"])
        if "enabled" in table:
            switch(name, table["enabled"])
    for name in [name for name, table in tables.items() if not table]:
        del tables[name]
    return summary


def _apply_role(edit: Edit, tables: Tables, lower: Tables, known: list[str], level: str, location: Path, view,
                own) -> list[str]:
    """Make the role flags' change; returns the summary lines of --unset."""

    name = edit.role
    base = name in roles.BASE_ROLES
    if edit.unset:
        if not base and name not in known and name not in tables:
            raise _unknown(name, known)
        lines = []
        for key in edit.unset:
            if key not in tables.get(name, {}):
                elsewhere = "; it's set in another file: name that one with --user, --workspace or --local" \
                    if key in lower.get(name, {}) else ""
                raise RoleError(f"{name} has no {key} in the {level} file{elsewhere}")
            value = tables[name].pop(key)
            lines.append(f"{name:<9} unset {key} (was {json.dumps(value, default=str)})")
        return lines
    if edit.delete:
        if base:
            raise RoleError(f"{name} is a base role, so it can't be deleted; to stop using it: "
                            f"agt roles config --disable {name}")
        if name not in tables:
            if name not in known:
                raise _unknown(name, known)
            raise RoleError(f"{name} isn't set in the {level} file; name the file it's in with --user, --workspace or --local")
        del tables[name]
        return []
    if not base and name not in known and name not in tables and not (edit.access and edit.only):
        raise _unknown(name, known)
    for flag, value in (("access", edit.access), ("prompt", edit.prompt), ("description", edit.description)):
        if value is not None and base:
            if flag == "access":
                raise RoleError(f"{name} is a base role with {roles.BASE_ROLES[name]} access, which can't change; "
                                "make a custom role for other access")
            raise RoleError(f"--{flag} is for custom roles; {name} is a base role")
    if edit.access:
        own(name)["access"] = edit.access
    if edit.description is not None:
        own(name)["description"] = edit.description
    if edit.prompt is not None:
        own(name)["prompt_file"] = prompt_path(name, level, location)[1]
    if all(value is None for value in (edit.only, edit.add, edit.remove, edit.order)):
        return []
    current = list(_listed(view(name)))
    access = _access(name, view(name))
    chosen = list(current)
    if edit.order is not None:
        if access != "edit":
            raise RoleError(f"{name} has read access, so its providers all run at once and have no order.\n"
                            "Use --only, --add or --remove to choose them.")
        first = [_match(provider, current) for provider in edit.order]
        chosen = first + [text for text in current if text not in first]
    if edit.only is not None:
        chosen = [_match(provider, current) for provider in edit.only]
    for provider in edit.add or []:
        chosen.append(_match(provider, current))
    for provider in edit.remove or []:
        hits = [text for text in chosen if _fits(provider, _parse(text)) or text == str(provider)]
        if not hits:
            raise RoleError(f"{provider} isn't one of {name}'s providers ({', '.join(chosen) or 'none'})")
        chosen = [text for text in chosen if text not in hits]
    chosen = list(dict.fromkeys(chosen))
    if not chosen and view(name).get("enabled", True):
        raise RoleError(f"{name} would have no providers left. To stop using it: agt roles config --disable {name}")
    own(name)["providers"] = chosen
    return []


# The change, worked out before anything is written ---------------------------------------------------


@dataclass
class Change:
    level: str
    location: Path
    old_text: str
    new_text: str
    names: list[str]  # roles whose table in this file changes
    own_before: Tables  # the file's own tables
    own_after: Tables
    before: Tables  # the roles as the files up to this level give them
    after: Tables
    full_before: Tables  # and as every level gives them
    full_after: Tables
    higher: list[levels.Layer]
    summary: list[str]
    loaded: config.Config | None = None

    @property
    def widens(self) -> list[str]:
        return list(dict.fromkeys(_widens(self.before, self.after) + _widens(self.full_before, self.full_after)))


def _render(old_text: str, tables: Tables, names: list[str]) -> str:
    def ours(header: str) -> bool:
        bare = header.replace('"', "").replace("'", "").replace(" ", "")
        prefix, _, name = bare.partition(".")
        return prefix == "roles" and name in names

    kept = levels.split(old_text, ours)[0]
    while kept and not kept[-1].strip():
        kept.pop()
    if not kept:
        kept = ["# agt settings. See docs/configuration.md."]
    rendered = roles.render_tables({name: tables[name] for name in _order_names(names) if tables.get(name)})
    return "\n".join(kept) + "\n" + ("\n" + rendered if rendered else "")


def plan_change(edit: Edit, user: Path, workspace: levels.Workspace | None, found: list[levels.Layer], level: str,
                location: Path) -> Change:
    """The whole change, checked, with nothing written. Raises RoleError (or SettingsError) on any refused part."""

    upto = levels.LEVELS.index(level)
    target = next((layer for layer in found if layer.path == location), None)
    old_text = target.text if target else settings_write.read_text(location)
    below = [layer for layer in found if levels.LEVELS.index(layer.level) < upto]
    higher = [layer for layer in found if levels.LEVELS.index(layer.level) > upto]
    own_before = copy.deepcopy(_raw(target))
    lower = _merge([_raw(layer) for layer in below])
    full_before = _merge([_raw(layer) for layer in found])
    own_after = copy.deepcopy(own_before)
    summary = _apply(edit, own_after, lower, list(full_before), level, location)
    names = [name for name in dict.fromkeys([*own_before, *own_after]) if own_before.get(name) != own_after.get(name)]
    if names:  # rewriting this file: each of its [roles.*] tables loses the keys a hand edit broke, saying so,
        for name in list(own_after):  # so a second broken table can't block the repair of the first
            for key, value in list(own_after[name].items()):
                try:
                    roles._check_key(name, key, value)
                except RoleError as exc:
                    del own_after[name][key]
                    summary.append(f"{name:<9} dropped {key} = {json.dumps(value, default=str)}: {exc}")
            if not own_after[name]:
                del own_after[name]
        names = [name for name in dict.fromkeys([*own_before, *own_after]) if own_before.get(name) != own_after.get(name)]
    new_text = _render(old_text, own_after, names) if names else old_text
    change = Change(level, location, old_text, new_text, names, own_before, own_after,
                    _merge([lower, own_before]), _merge([lower, own_after]), full_before,
                    _merge([lower, own_after, *(_raw(layer) for layer in higher)]), higher, summary)
    if names:
        after_layers = levels.layers(user, workspace, override={location: new_text})
        change.loaded = config.from_layers(after_layers)  # the shape of every role, as agt will load it
        for name in names:
            before = {text for text in _listed(change.before.get(name)) if isinstance(text, str)}
            for text in change.after.get(name, {}).get("providers", []):
                if text not in before:
                    problem = account_problem(change.loaded, roles.parse_provider(text))
                    if problem:
                        raise RoleError(problem)
    return change


def account_problem(loaded: config.Config, provider: roles.Provider) -> str | None:
    """Why agt can't use this provider's account or model, or None."""

    runtime = loaded.runtime(provider.runtime)
    account = None
    if provider.account:
        account = runtime.find(provider.account)
        if account is None:
            return f"{provider}: {provider.runtime} has no account {provider.account} (agt accounts lists them)"
        if not account.enabled:
            return f"{provider}: the {provider.runtime} account {provider.account} is turned off"
    if provider.model:
        account = account or runtime.primary_account()
        limits = plans.account_limits(runtime, account)
        if not plans.model_allowed(runtime, limits, provider.model):
            return (f"{provider}: {account.name} doesn't allow the model {provider.model} "
                    f"(allowed: {', '.join(limits.models)})")
    return None


def show_problem(loaded: config.Config, provider: roles.Provider) -> str | None:
    """What `agt roles show` reports: account_problem, plus a pinned account that is logged out (as `agt run --role` skips it)."""

    problem = account_problem(loaded, provider)
    if problem or not provider.account:
        return problem
    from agents_router import role_run  # role_run loads jobs and the runner, which a write never needs

    runtime = loaded.runtime(provider.runtime)
    account = runtime.find(provider.account)
    if account is not None and role_run.logged_in(runtime, account) is False:
        return f"{provider}: {provider.account} is logged out · log in with {role_run._login_fix(provider.runtime, account.name)}"
    return None


def _widens(before: Tables, after: Tables) -> list[str]:
    """Why this change widens edit access or turns a role on or off: what an agent needs the user's words for."""

    reasons = []
    for name in _order_names([*before, *after]):
        old, new = before.get(name), after.get(name)
        if new is None:
            continue
        on_before, on_after = (old or {}).get("enabled", True), new.get("enabled", True)
        access = _access(name, new)
        if on_before != on_after:
            reasons.append(f"{name} would be turned {'on' if on_after else 'off'}"
                           + (", and it edits files" if on_after and access == "edit" else ""))
            continue
        if not on_after or access != "edit":
            continue
        if old is None and name not in roles.BASE_ROLES:
            reasons.append(f"the new role {name} would edit files")
        elif _access(name, old) != "edit":
            reasons.append(f"{name} would go from read to edit access")
        else:
            had = _entries(old)
            reasons += [f"{entry} would join {name}, which edits files" for entry in _entries(new) if entry not in had]
    return reasons


def _lines(change: Change, covered: set[str]) -> list[str]:
    """What moved, one line per role: "verifier  - kiro:default  + antigravity:default"."""

    lines = []
    for name in _order_names(change.names):
        old, new = change.before.get(name), change.after.get(name)
        access = _access(name, new or old)
        if old is None and new is not None and name not in roles.BASE_ROLES:
            count = len(new.get("providers", []))
            lines.append(f"Added [roles.{name}] ({access}, {count} provider{'s' if count != 1 else ''}, "
                         f"{'in parallel' if access == 'read' else 'in this order'})")
            continue
        if new is None:
            lines.append(f"{name:<9} deleted from this file")
            continue
        old = old or {}
        parts = []
        if old.get("enabled", True) != new.get("enabled", True):
            parts.append("on" if new.get("enabled", True) else "off (enabled = false)")
        if _access(name, old) != access:
            parts.append(f"access {_access(name, old)} → {access}")
        was, now = _listed(old), _listed(new)
        if was != now:
            removed = [text for text in was if text not in now]
            added = [text for text in now if text not in was]
            for text in list(removed):
                provider = _parse(text)
                pair = next((other for other in added if provider and _parse(other) and provider.same_entry(_parse(other))), None)
                if pair is not None:
                    removed.remove(text)
                    added.remove(pair)
                    if provider.entry() not in covered:
                        parts.append(f"{text} → {pair}")
            parts += [f"- {text}" for text in removed] + [f"+ {text}" for text in added]
            kept_was = [entry for entry in _entries(old) if entry in _entries(new)]
            kept_now = [entry for entry in _entries(new) if entry in _entries(old)]
            if access == "edit" and kept_was != kept_now:
                parts.append("order  " + "   ".join(f"{index} {text}" for index, text in enumerate(now, 1)))
        for key in ("description", "prompt_file"):
            if old.get(key) != new.get(key):
                parts.append(f"{key} → {new.get(key) or 'removed'}")
        if parts:
            lines.append(f"{name:<9} " + "  ".join(parts))
    return change.summary + lines


def _overridden(change: Change) -> list[dict[str, Any]]:
    found = []
    for name in _order_names(change.names):
        old, new = change.own_before.get(name, {}), change.own_after.get(name, {})
        for key in dict.fromkeys([*old, *new]):
            if old.get(key) == new.get(key):
                continue
            setter = next((layer for layer in reversed(change.higher) if key in _raw(layer).get(name, {})), None)
            if setter is not None:
                found.append({"role": name, "key": key, "level": setter.level, "file": str(setter.path),
                              "in_effect": _raw(setter)[name][key], "fix": _command(level=setter.level)})
    return found


# Writing ---------------------------------------------------------------------------------------------


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _command(level: str | None = None, extra: str = "") -> str:
    """This command as typed, without --json and --dry-run; with `level`, pointed at that file instead."""

    argv = [word for word in ui.ARGV if word not in ("--json", "--dry-run")]
    if level:
        argv = [word for word in argv if word not in LEVEL_FLAGS] + [f"--{level}"]
    return "agt " + shlex.join(argv) + extra


def _named_level(args: argparse.Namespace, workspace: levels.Workspace | None) -> str | None:
    named = [level for level in levels.LEVELS if getattr(args, level, False)]
    if len(named) > 1:
        raise RoleError("name one of --user, --workspace and --local")
    if named and named[0] != "user" and workspace is None:
        raise RoleError(f"there's no workspace here ({Path.cwd()} is your home folder); use --user")
    return named[0] if named else None


def _default_level(names: list[str], found: list[levels.Layer], workspace: levels.Workspace | None) -> str:
    """Where the role is set now (local for the committed workspace file), else your user file."""

    if workspace is None:
        return "user"
    picked = {}
    for name in names:
        level = "user"
        for layer in found:
            if name in _raw(layer):
                level = layer.level
        picked[name] = "local" if level == "workspace" else level
    if len(set(picked.values())) > 1:
        where = ", ".join(f"{name}: {level}" for name, level in picked.items())
        raise RoleError(f"the roles this changes are set in different files ({where}); one command writes one file, "
                        "so name it with --user or --local")
    return next(iter(picked.values()), "user")


def _touched(edit: Edit, found: list[levels.Layer]) -> list[str]:
    full = _merge([_raw(layer) for layer in found])
    names = [edit.role] if edit.role else []
    names += edit.enable + edit.disable + list(edit.assign or {}) + list(edit.tables or {})
    for entry, _ in edit.set_model + edit.set_effort:
        names += [name for name, table in full.items()
                  if any(provider and provider.same_entry(entry) for provider in map(_parse, _listed(table)))]
    return list(dict.fromkeys(names))


def _write(args: argparse.Namespace, edit: Edit) -> int:
    workspace = levels.find_workspace()
    user = config.user_settings_path()
    found = levels.layers(user, workspace)
    level = _named_level(args, workspace) or _default_level(_touched(edit, found), found, workspace)
    location = settings_write.files(workspace)[level]
    if args.if_unchanged and _hash(settings_write.read_text(location)) != args.if_unchanged:
        raise RoleError(f"{location} changed since that change (agt log shows what changed), so this undo would "
                        f"overwrite it; nothing was written. Backups: {config.state_dir() / 'backups' / 'settings'}")
    change = plan_change(edit, user, workspace, found, level, location)
    covered = {entry.entry() for entry, _ in edit.set_model + edit.set_effort}
    lines = _lines(change, covered)
    who = settings_write.who(args.user_said)
    widens = change.widens if not edit.grid else []
    needs = bool(widens) and who["by"] == "agent" and not args.user_said
    retry = _command(extra=' --user-said "<the user\'s words>"')
    if not change.names and edit.prompt is not None and edit.role:  # same prompt_file: only its text may change
        target = prompt_path(edit.role, level, location)[0]
        text = edit.prompt if edit.prompt.endswith("\n") else edit.prompt + "\n"
        if settings_write.read_text(target) != text:
            if not args.dry_run:
                _write_prompt(target, text, level)
            if args.json:
                print(json.dumps({"changes": {}, "prompt_text": str(target), "file": str(location), "level": level,
                                  "dry_run": args.dry_run}, indent=2))
            else:
                print(f"{edit.role:<9} prompt text {'would change' if args.dry_run else 'changed'} in {ui.tilde(str(target))}")
            return 0
    if not change.names:
        if args.json:
            print(json.dumps({"changes": {}, "file": str(location), "level": level, "dry_run": args.dry_run}, indent=2))
        else:
            print(f"Nothing to change: {ui.tilde(str(location))} ({level}) already has that.")
        return 0
    ask = f"{'; '.join(widens)}. Ask the user, and pass their own words." if widens else None
    report: dict[str, Any] = {
        "changes": {name: {"old": change.before.get(name), "new": change.after.get(name)} for name in change.names},
        "lines": lines, "file": str(location), "level": level, "dry_run": args.dry_run,
        "needs_user_words": needs}
    if needs and not args.dry_run:
        refusal = {"error": "needs_user_words", "role": _order_names(change.names)[0], "reasons": widens,
                   "ask": ask, "retry": retry}
        if args.json:
            print(json.dumps(refusal, indent=2, ensure_ascii=False))
        else:
            print(f"agt: {ask}\nThen run: {retry}", file=sys.stderr)
        return settings_write.NEEDS_WORDS
    if args.dry_run:
        report["overridden_by"] = _overridden(change) or None
        if needs:
            report.update(ask=ask, retry=retry)
        if args.json:
            print(json.dumps(report, indent=2, ensure_ascii=False))
            return 0
        for line in lines:
            print(line)
        print(f"Dry run: nothing written to {ui.tilde(str(location))} ({level}). Run again without --dry-run to save.")
        if needs:
            print(f"Saving it from an agent needs the user's words: {retry}")
        return 0
    if edit.tables is not None:  # --from: the one write path setup and rebalance use too
        saved = _write_table(edit.tables, level, who, _command())
    else:
        saved = _commit(edit, change, lines, who, workspace, "agt roles config (grid)" if edit.grid else _command())
    from agents_router import role_agents

    # Agent files are user-wide: only a description written to your user settings replaces the sentence in them.
    described = edit.role if edit.role and level == "user" and (
        edit.description is not None or "description" in (edit.unset or [])) else None
    saved["agents"] = role_agents.sync(config.load_config(), fresh=[described] if described else ())
    return _print_saved(args, edit, {**report, **saved})


def write_table(table: dict[str, dict[str, Any] | None], level: str | None = None,
                who: dict[str, Any] | None = None, command_text: str = "agt setup") -> dict[str, Any]:
    """Write role tables (`{name: table}`, None removes one) the way `agt roles config --from` does, without printing
    and without syncing role agents (the caller does, with the folders it picked).

    Under the settings lock: re-read, check every role, back up, write, log, keep an undo file. `level` None follows
    the level rule (where the roles are set now, else your user file). Returns the report `--json` prints;
    `changes` is empty when the files already say this. Raises RoleError (or SettingsError) and writes nothing when
    any part is refused."""

    with settings_write.locked():
        return _write_table(table, level, who or settings_write.who(None), command_text)


def _write_table(table: dict[str, dict[str, Any] | None], level: str | None, who: dict[str, Any],
                 command_text: str) -> dict[str, Any]:
    edit = Edit(tables=table)
    workspace = levels.find_workspace()
    user = config.user_settings_path()
    found = levels.layers(user, workspace)
    level = level or _default_level(_touched(edit, found), found, workspace)
    location = settings_write.files(workspace)[level]
    change = plan_change(edit, user, workspace, found, level, location)
    lines = _lines(change, set())
    if not change.names:
        return {"changes": {}, "lines": [], "file": str(location), "level": level}
    return _commit(edit, change, lines, who, workspace, command_text)


def _write_prompt(target: Path, text: str, level: str) -> str | None:
    """Write --prompt's text to its file, ending in a newline. Every local prompt file is kept out of git, even beside
    a settings file kept out already; returns the exclude file when that added its line."""

    fsutil.make_private_dir(target.parent)
    fsutil.write_text(target, text if text.endswith("\n") else text + "\n")
    return settings_write.exclude_local(target) if level == "local" else None


def _commit(edit: Edit, change: Change, lines: list[str], who: dict[str, Any], workspace: levels.Workspace | None,
            command_text: str) -> dict[str, Any]:
    """Write a checked change: backup, the file, agt's log and the undo file. Prints nothing."""

    location, level = change.location, change.level
    synclog.notice(workspace)  # a hand edit made before this change is logged as one
    backup = settings_write.backup(location, level)
    prompt = prompt_path(edit.role, level, location)[0] if edit.prompt is not None and edit.role else None
    excluded = settings_write.exclude_local(location) if level == "local" and not change.old_text else None
    if prompt is not None:  # before the settings file, which names it
        excluded = _write_prompt(prompt, edit.prompt, level) or excluded
    levels.write(location, change.new_text)
    synclog.saved(location)
    undo_file = (config.state_dir() / "backups" / "settings"
                 / f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 10**9}-{level}-roles-undo.json")
    fsutil.make_private_dir(undo_file.parent)
    fsutil.write_text(undo_file, json.dumps({"roles": {name: change.own_before.get(name) for name in change.names}},
                                            indent=2) + "\n")
    undo = f"agt roles config --from {shlex.quote(str(undo_file))} --{level} --if-unchanged {_hash(change.new_text)}"
    for name in change.names:
        synclog.append({"type": "setting", **synclog.where(workspace if level != "user" else None), "file": str(location),
                        "level": level, "key": f"roles.{name}", "old": change.own_before.get(name),
                        "new": change.own_after.get(name), "command": command_text, **who})
    overridden = _overridden(change)
    in_effect = None
    if not overridden and len(change.names) == 1 and change.loaded:
        role = change.loaded.roles.get(change.names[0])
        if role is not None:
            in_effect = ", ".join(map(str, role.providers)) if role.enabled else "off"
    return {"changes": {name: {"old": change.before.get(name), "new": change.after.get(name)} for name in change.names},
            "lines": lines, "file": str(location), "level": level, "overridden_by": overridden or None,
            "in_effect": in_effect, "backup": str(backup) if backup else None, "undo": undo, "excluded_in": excluded}


def _print_saved(args: argparse.Namespace, edit: Edit, report: dict[str, Any]) -> int:
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    for line in report["lines"]:
        print(line)
    if not report["changes"]:
        return 0
    backup = report.get("backup")
    print(f"Saved to {ui.tilde(report['file'])} ({report['level']})." + (f" Backup: {ui.tilde(backup)}" if backup else ""))
    for item in report["overridden_by"] or []:
        print(f"Overridden here by {item['level']} ({ui.tilde(item['file'])}); in effect: {_shown(item['in_effect'])}")
        if not edit.grid:
            print(f"To change it here: {item['fix']}")
    if report.get("in_effect"):
        print(f"In effect here: {report['in_effect']}")
    if report.get("excluded_in"):
        print(f"Kept out of git: {report['excluded_in']}")
    for line in report.get("agents") or []:
        print(line)
    print(f"Undo: {report['undo']}")
    return 0


def _shown(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(map(str, value))
    if isinstance(value, bool):
        return "on" if value else "off (enabled = false)"
    return str(value)


# Showing ---------------------------------------------------------------------------------------------


def _loaded() -> config.Config:
    return config.load_config()


def _choices(loaded: config.Config):
    used: dict[str, set[str]] = {}
    for role in loaded.roles.values():
        for provider in role.providers:
            if provider.model:
                used.setdefault(provider.runtime, set()).add(provider.model)

    def choices(entry: roles_grid.Entry) -> tuple[list[str | None], list[str | None]]:
        runtime = loaded.runtime(entry.runtime)
        account = runtime.find(entry.account) if entry.account else None
        limits = plans.account_limits(runtime, account or runtime.primary_account())
        models = list(limits.models) if limits.models else sorted(
            used.get(entry.runtime, set()) | ({runtime.default_model} if runtime.default_model else set()))
        top = limits.max_effort if limits.max_effort in roles.EFFORTS else roles.EFFORTS[-1]
        efforts = list(roles.EFFORTS[: roles.EFFORTS.index(top) + 1])
        return [None, *models], [None, *efforts]

    return choices


def _table_json(loaded: config.Config) -> dict[str, Any]:
    from agents_router import role_agents

    workspace = levels.find_workspace()
    rows = []
    for role in roles.ordered(loaded.roles):
        providers = []
        for provider in role.providers:
            problem = show_problem(loaded, provider)
            providers.append({"provider": str(provider), "entry": provider.entry(), "model": provider.model,
                              "effort": provider.effort, "usable": problem is None, "reason": problem})
        mine = role_agents.here(role.name, loaded)
        rows.append({"name": role.name, "access": role.access, "enabled": role.enabled, "base": role.base,
                     "use_when": role.use_when, "level": role.level(), "providers": providers,
                     **({"prompt_file": role.prompt_file} if role.prompt_file else {}),
                     "when_to_call": (mine or {}).get("when_to_call") or role.use_when,
                     "rules_chars": (mine or {}).get("rules_chars", 0),
                     "rules_state": (mine or {}).get("state"),
                     "agent_files": role_agents.agent_files(role.name, loaded),
                     "archived": [str(path) for path in role_agents.archived(role.name)]})
    return {"roles": rows, "files": {level: str(path) for level, path in settings_write.files(workspace).items()}}


def _table_lines(loaded: config.Config) -> list[ui.Line]:
    found = roles.ordered(loaded.roles)
    if not found:
        return ["No roles yet. agt setup proposes a role table; or make one: "
                "agt roles config <name> --access read --only <provider>"]
    lines = roles_grid.RoleGrid(found).table()
    notes = [f"  ! {role.name}: {problem}" for role in found for provider in role.providers
             if (problem := show_problem(loaded, provider))]
    notes += _agent_notes(found)
    notes += _parts_notes(loaded, found)
    return lines + ([""] + notes if notes else [])


def _parts_notes(loaded: config.Config, found: list[roles.Role]) -> list[str]:
    """What your agt-<role> file in this CLI adds: your sentence, your Role rules, or the line to fix; and archives."""

    from agents_router import role_agents

    out = []
    for role in found:
        mine = role_agents.here(role.name, loaded) if role.enabled else None
        if mine and mine["state"] == "broken":
            out.append(f"  ! {role.name}: {mine['problem']}")
        elif mine and mine["state"] != "absent":
            parts = ([f"Role rules ({mine['rules_chars']} characters)"] if mine["rules_chars"] else []) + (
                ["its own \"when to call\" sentence"] if mine["when_to_call"] not in (None, role.use_when) else [])
            if parts:
                out.append(f"  {role.name}: {ui.tilde(mine['file'])} has {' and '.join(parts)}")
        for path in role_agents.archived(role.name) if not role.enabled else []:
            out.append(f"  {role.name}: your sentence and Role rules wait in {ui.tilde(str(path))} until it's on again")
    return out


def _agent_notes(found: list[roles.Role]) -> list[str]:
    """A line for each enabled role that gets no agt-<role> agent: agent files come from your user settings alone."""

    from agents_router import role_agents

    try:
        mine, problems = role_agents.user_roles()
    except (levels.SettingsError, RoleError):
        return []
    out = []
    for role in found:
        if not role.enabled or role.name in mine:
            continue
        if not any(layer.level == "user" for layer in role.sources.values()):
            where = ui.tilde(str(next(iter(role.sources.values())).path))
            out.append(f"  ! {role.name}: no agt-{role.name} agent: defined only in {where}; "
                       "add --user to define it for your agent")
        else:
            out += [f"  ! {role.name}: {note}" for note in problems if note.startswith(f"no agt-{role.name} agent:")]
    return out


def _show(as_json: bool) -> int:
    loaded = _loaded()
    if as_json:
        print(json.dumps(_table_json(loaded), indent=2, ensure_ascii=False))
        return 0
    ui.out(*_table_lines(loaded))
    return 0


def _propose(args: argparse.Namespace) -> int:
    from agents_router import roles_propose

    result = roles_propose.propose(_loaded(), mode="rebalance" if args.rebalance else "setup")
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    for item in result.get("quota") or []:  # rebalance: what each account has left, before the moves
        print(f"{item['runtime']} {item['text']}")
    tables = result.get("roles") or {}
    if result.get("quota") and tables:
        print()
    if not tables:
        print("No change to propose.")
        return 0
    for name, table in tables.items():
        print(f"{name:<9} {', '.join(table.get('providers', []))}" + ("  (off)" if table.get("enabled") is False else ""))
        if (result.get("reasons") or {}).get(name):
            print(f"          {result['reasons'][name]}")
    flag = " --rebalance" if args.rebalance else ""
    print(f"Nothing written. To save it: agt roles propose{flag} --json > roles.json, then agt roles config --from roles.json")
    return 0


def _interactive() -> bool:
    """A person at a terminal. An agent never gets a prompt, even with a terminal (share/commands.py `_interactive`)."""

    try:
        return settings_write.terminal() and sys.stdout.isatty() and not settings_write.agent()
    except (AttributeError, ValueError):
        return False


def _no_flags(args: argparse.Namespace) -> int:
    loaded = _loaded()
    if args.json:
        print(json.dumps(_table_json(loaded), indent=2, ensure_ascii=False))
        return 0
    if not _interactive():
        ui.out(*_table_lines(loaded), roles_grid.HINT)
        return 0
    return _grid(args, loaded)


def _grid(args: argparse.Namespace, loaded: config.Config) -> int:
    from agents_router.share import picker

    workspace = levels.find_workspace()
    watched = {path: settings_write.read_text(path) for path in settings_write.files(workspace).values()}
    grid = roles_grid.RoleGrid(roles.ordered(loaded.roles), _choices(loaded))
    if picker.choose(grid) != roles_grid.DONE or not grid.tables():
        print("Nothing was changed.")
        return 0
    edit = Edit(assign=grid.tables(), grid=True)
    with settings_write.locked():
        moved = [str(path) for path, text in watched.items() if settings_write.read_text(path) != text]
        if moved:
            raise RoleError(f"{', '.join(moved)} changed while the grid was open, so nothing was saved; "
                            "run agt roles config again")
        return _write(args, edit)
