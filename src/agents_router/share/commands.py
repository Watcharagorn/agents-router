"""`agt share` commands.

`enable` shows what sharing would do (`--dry-run`) or saves your answers and
does it (`--yes`, plus `--if-plan` from a dry run). `sync` makes the copies
match again from the saved settings, `status` says where things stand, and
`scan` lists every choice setup would offer.

The item commands act on one item at once: `pick` changes which CLIs get it,
`resolve` picks the version that wins a conflict, `rm` removes it everywhere,
`mv` renames it everywhere, and `undo` puts back what the last change to it
replaced.

`cli add|remove|list` and `kinds add|remove` change what shares; turning one
off asks whether the copies agt made there stay as your own files or go to
the backups (`--keep-copies`, `--remove-copies`). `disable` stops sharing and
keeps every copy; `disable --remove-copies` also removes agt's copies. Every copy they
replace or remove goes to the backups first. Nothing writes inside agt's own
workers, and a command that needs a choice it wasn't given exits 3 with
`needs_choice` instead of asking an agent.

Turning sharing on also sets up what runs it without you (triggers.py): the
background watcher, and agt's session-start and save entries in each Claude
folder. `watch`, `session` and `saved` are what those run (watch.py), and
`badge` prints a short marker for a status line.

A repo you list with `repo add` is a scope of its own (scope.py): `sync`,
`status` and `enable --projects` cover every listed repo and its worktrees,
and an item command run inside a listed repo acts on that repo's item.
"""

from __future__ import annotations

import argparse
import dataclasses
import difflib
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path
from typing import Any, Callable

from agents_router import helptext, hosts, synclog, ui
from agents_router.config import default_primary, load_config
from agents_router.share import (
    apply, attention, engine, gitexclude, hooks, instructions, mcp, model, plugin_view, plugins, names, normalize, picker, plan as planner, providers, scope,
    settings, state, status_view, toolmap, triggers,
)
from agents_router.share.providers import _short as short

SUPPORTED_KINDS = settings.KINDS
LATER_KINDS = ("commands",)
# Runs started by these leave the slow writes to later: `claude mcp` starts and plugin installs don't fit a session
# hook's few seconds.
MCP_LATER_TRIGGERS = ("session", "save", "mcp-start")
IN_WORKER = "agt share doesn't write inside agt's workers; run it from your own terminal or session."
NOT_ENABLED = "agt share isn't enabled yet. `agt share enable --dry-run` shows what it would do."
TURNED_OFF = "agt share is off. `agt share enable` turns it on again, with your saved answers."


def _say_off(args: argparse.Namespace) -> None:
    """Sharing isn't on: never set up, or turned off with `disable` (your answers are still saved)."""

    turned_off = bool(settings.load().folders)
    if getattr(args, "json", False):
        _print({"enabled": False, "message": TURNED_OFF if turned_off else NOT_ENABLED})
    elif turned_off:
        ui.out([ui.bold("agt share"), "  off; your answers are saved"],
               *ui.columns([[ui.muted("Turn it on again"), ui.cmd("agt share enable")]], gap=6))
    else:
        ui.out([ui.bold("agt share"), "  not set up yet"],
               *ui.columns([[ui.muted("See what it would do"), ui.cmd("agt share enable --dry-run")]]))


def _folder_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--claude-dir", action="append", default=[], metavar="PATH", help="A Claude folder to share (repeatable).")
    parser.add_argument("--codex-dir", action="append", default=[], metavar="PATH")
    parser.add_argument("--kiro-dir", action="append", default=[], metavar="PATH")
    parser.add_argument("--agy-dir", action="append", default=[], metavar="PATH", help="Antigravity's ~/.gemini folder.")
    parser.add_argument("--no-agy", action="store_true", help="Leave Antigravity out.")
    parser.add_argument("--kinds", help="What to share: agents,skills (the default, or what you saved).")
    parser.add_argument("--json", action="store_true", help="Print the result as JSON.")


def add_parser(subparsers) -> None:
    share = subparsers.add_parser(
        "share", help="Keep agents and skills the same across Claude Code, Codex, Antigravity and Kiro.",
        description="agt share keeps your agents and skills the same in every CLI folder you pick.", page=helptext.SHARE,
    )
    actions = share.add_subparsers(dest="share_command", metavar="<command>")  # none: the status, then the common commands
    enable = actions.add_parser("enable", help="Turn sharing on: --dry-run shows the plan, --yes saves your answers and applies it.")
    _folder_flags(enable)
    enable.add_argument("--projects", default="none", metavar="none|all-with-items|PATH[,PATH]",
                        help="Repos to share too: none (the default), every repo your CLIs know that has agents or skills, "
                             "or these paths.")
    enable.add_argument("--only", action="append", default=[], metavar="ITEM=LABEL[,LABEL]", help="Keep an item in these CLIs only.")
    enable.add_argument("--skip", action="append", default=[], metavar="ITEM@LABEL", help="Untick one cell (repeatable).")
    enable.add_argument("--resolve", action="append", default=[], metavar="ITEM=LABEL|skip",
                        help="Which version wins for an item that differs between folders; skip doesn't share it.")
    enable.add_argument("--auto-copy", help="New items go to every target: on (the default), off or a list of kinds.")
    enable.add_argument("--instructions", choices=settings.INSTRUCTIONS,
                        help="Share each CLI's instruction file too: block (agt's marked block of it) or whole; off by default.")
    enable.add_argument("--mcp", choices=("on", "off"), help="Share your user MCP servers too; off by default.")
    enable.add_argument("--hooks", choices=("on", "off"),
                        help="List your hooks for sharing; each copy waits for your approval. Off by default.")
    enable.add_argument("--plugins", choices=("on", "off"),
                        help="Install your Claude Code and Codex plugins in each other's folders where they fit; off by default.")
    enable.add_argument("--tool-widening", choices=toolmap.TOOL_WIDENING,
                        help="Codex agents can't limit tools: allow-if-shell (the default) copies agents that have a shell, "
                             "skip copies none with a tool list.")
    enable.add_argument("--dry-run", action="store_true", help="Show the plan; write nothing.")
    enable.add_argument("--yes", action="store_true", help="Save your answers and apply the plan.")
    enable.add_argument("--if-plan", metavar="ID", help="Apply only if the plan is still the one a dry run showed.")
    enable.add_argument("--accept-suggested", action="store_true", help="Take the suggested answer for every open choice.")
    scan = actions.add_parser("scan", help="List every choice setup would offer; writes nothing.")
    _folder_flags(scan)
    sync = actions.add_parser("sync", help="Make every copy match again, from your saved settings.")
    sync.add_argument("--dry-run", action="store_true", help="Show what the sync would do; write nothing.")
    sync.add_argument("--allow-deletes", action="append", default=[], metavar="LABEL",
                      help="A frozen folder: many items disappeared from it at once, and you meant it. This run only.")
    sync.add_argument("--refill", action="append", default=[], metavar="LABEL",
                      help="A frozen folder: put back the copies that disappeared from it. This run only.")
    sync.add_argument("--json", action="store_true")
    sync.add_argument("--if-stale", action="store_true", help="Only when something moved since the last sync.")
    sync.add_argument("--quiet", action="store_true", help="Print nothing (a background run).")
    sync.add_argument("--trigger", default="command", help=argparse.SUPPRESS)
    hook_run = actions.add_parser("hook-run", help=argparse.SUPPRESS)  # the launcher runs hook_run.py directly
    plugins_parser = actions.add_parser("plugins", help="Plugins on in one CLI that another could install; `apply` installs "
                                                        "the ones you say yes to.")
    plugins_parser.add_argument("--json", action="store_true")
    plugin_actions = plugins_parser.add_subparsers(dest="plugins_command")
    plugins_apply = plugin_actions.add_parser("apply", help="Install plugins in the CLIs that don't have them, after you say yes.")
    plugins_apply.add_argument("offers", nargs="*", metavar="PLUGIN:LABEL",
                               help="Only these (as `agt share plugins` lists them); needed with --yes.")
    plugins_apply.add_argument("--yes", action="store_true", help="Install the plugins you named without asking.")
    plugins_apply.add_argument("--json", action="store_true")
    hook_run.add_argument("hook_args", nargs=argparse.REMAINDER)
    watch = actions.add_parser("watch", help="One run of the background watcher (the LaunchAgent runs it every minute).")
    watch.add_argument("--once", action="store_true", help="Run once and exit (the only mode).")
    watch.add_argument("--quiet", action="store_true")
    session = actions.add_parser("session", help="What a CLI's session-start entry runs: catch up, then say what waits.")
    session.add_argument("--label", help="The folder whose session this is.")
    session.add_argument("--event", default="SessionStart", help=argparse.SUPPRESS)
    actions.add_parser("saved", help="What Claude Code's save entry runs: a save in a shared folder starts a sync.")
    actions.add_parser("badge", help="Print share:N! when N things wait for you, else nothing (for a status line).")
    shown = actions.add_parser("settings", help="Every share setting, where it comes from and the command that changes it.")
    shown.add_argument("--json", action="store_true")
    status = actions.add_parser("status", help="What's in sync, what differs and what waits for you; writes nothing.")
    status.add_argument("--diff", action="store_true", help="Show how the copies of each conflict differ.")
    status.add_argument("--json", action="store_true")
    pick = actions.add_parser("pick", help="Change which CLIs get an item; with no flags, show what goes where.")
    pick.add_argument("item", nargs="?", help="The agent or skill: a bare name, or agents/<name> or skills/<name>.")
    pick.add_argument("--in", dest="in_labels", action="append", default=[], metavar="LABEL[,LABEL]",
                      help="Share it to these CLIs (again).")
    pick.add_argument("--not-in", dest="not_in", action="append", default=[], metavar="LABEL[,LABEL]",
                      help="Stop sharing it there; the copy agt made is removed, with a backup.")
    pick.add_argument("--only", dest="only_in", action="append", default=[], metavar="LABEL[,LABEL]",
                      help="Keep it in these CLIs only.")
    pick.add_argument("--keep-copies", action="store_true", help="Keep the copies you leave out as your own files.")
    pick.add_argument("--json", action="store_true")
    resolve = actions.add_parser("resolve", help="Pick the version that wins when an item's copies differ.")
    resolve.add_argument("item", nargs="?")
    resolve.add_argument("--all", action="store_true", help="Every conflict.")
    resolve.add_argument("--prefer", metavar="LABEL|newest|skip",
                         help="The copy that wins: a CLI's label, newest, or skip to stop sharing the item.")
    resolve.add_argument("--kind", choices=SUPPORTED_KINDS, help="With --all, only this kind.")
    resolve.add_argument("--json", action="store_true")
    remove = actions.add_parser("rm", help="Remove an item from every CLI, with backups.")
    remove.add_argument("item")
    remove.add_argument("--json", action="store_true")
    disable = actions.add_parser("disable", help="Stop sharing. Every copy stays; `agt share enable` turns it on again.")
    disable.add_argument("--remove-copies", action="store_true",
                         help="Also remove the copies agt made that nobody edited since, with backups.")
    disable.add_argument("--json", action="store_true")
    clis = actions.add_parser("cli", help="The CLI folders that share: list, add or remove one.")
    cli_actions = clis.add_subparsers(dest="cli_command", required=True)
    cli_list = cli_actions.add_parser("list", help="The folders sharing, and the others agt found.")
    cli_list.add_argument("--json", action="store_true")
    cli_add = cli_actions.add_parser("add", help="Share a CLI folder too; it's filled from your items at once.")
    cli_add.add_argument("cli", choices=("claude", "codex", "kiro", "antigravity", "agy"))
    cli_add.add_argument("path", nargs="?", help="The folder (needed for Claude; the usual one otherwise).")
    cli_add.add_argument("--dry-run", action="store_true")
    cli_add.add_argument("--json", action="store_true")
    cli_remove = cli_actions.add_parser("remove", help="Stop sharing a CLI folder.")
    cli_remove.add_argument("label")
    kinds = actions.add_parser("kinds", help="Which kinds share: add or remove one.")
    kind_actions = kinds.add_subparsers(dest="kinds_command", required=True)
    kind_add = kind_actions.add_parser("add", help="Share a kind too.")
    kind_add.add_argument("kind")
    kind_add.add_argument("--dry-run", action="store_true")
    kind_add.add_argument("--yes", action="store_true", help="plugins: install what it shows without asking.")
    kind_add.add_argument("--json", action="store_true")
    kind_remove = kind_actions.add_parser("remove", help="Stop sharing a kind.")
    kind_remove.add_argument("kind")
    for parser in (cli_remove, kind_remove):
        choice = parser.add_mutually_exclusive_group()
        choice.add_argument("--keep-copies", action="store_true", help="The copies agt made stay, as your own files.")
        choice.add_argument("--remove-copies", action="store_true", help="The copies agt made go to the backups.")
        parser.add_argument("--dry-run", action="store_true", help="Show what it would leave behind; change nothing.")
        parser.add_argument("--json", action="store_true")
    adopt = actions.add_parser("adopt", help="Add agt's instructions block to CLIs' instruction files, so they share it.")
    adopt.add_argument("labels", nargs="*", metavar="LABEL", help="The folders (default: every folder sharing).")
    adopt.add_argument("--wrap", action="store_true", help="Put the file's own text inside the block, instead of an empty block.")
    adopt.add_argument("--dry-run", action="store_true")
    adopt.add_argument("--json", action="store_true")
    repo = actions.add_parser("repo", help="The repos that share their own agents and skills: list, add or remove one.")
    repo_actions = repo.add_subparsers(dest="repo_command", required=True)
    repo_list = repo_actions.add_parser("list", help="The repos sharing, with their worktrees.")
    repo_list.add_argument("--json", action="store_true")
    repo_add = repo_actions.add_parser("add", help="Share a repo's own agents and skills between its CLI folders.")
    repo_add.add_argument("path", nargs="?", default=".")
    repo_add.add_argument("--json", action="store_true")
    repo_remove = repo_actions.add_parser("remove", help="Stop sharing a repo.")
    repo_remove.add_argument("path", nargs="?", default=".")
    choice = repo_remove.add_mutually_exclusive_group()
    choice.add_argument("--keep-copies", action="store_true", help="The copies agt made there stay, as your own files.")
    choice.add_argument("--remove-copies", action="store_true", help="The copies agt made there go to the backups.")
    repo_remove.add_argument("--dry-run", action="store_true")
    repo_remove.add_argument("--json", action="store_true")
    move = actions.add_parser("mv", help="Rename an item in every CLI, keeping each copy's own fields.")
    move.add_argument("item")
    move.add_argument("new_name", metavar="NEW-NAME")
    move.add_argument("--json", action="store_true")
    undo = actions.add_parser("undo", help="Put back what the last change to an item replaced, from the backups.")
    undo.add_argument("item")
    undo.add_argument("--in", dest="in_label", metavar="LABEL", help="Bring back the copy removed from this CLI.")
    undo.add_argument("--json", action="store_true")
    for parser in (pick, resolve, remove, move, undo):
        parser.add_argument("--repo", metavar="PATH", help="A listed repo's item (inside a listed repo, it's that repo's).")
    for parser in (enable, sync, pick, resolve, remove, disable, cli_add, cli_remove, kind_add, kind_remove, adopt, repo_add,
                   repo_remove, move, undo):
        parser.add_argument("-v", "--verbose", action="store_true", help="List the file of each copy written.")


def _kinds(value: str) -> tuple[str, ...]:
    kinds = tuple(dict.fromkeys(kind.strip() for kind in value.split(",") if kind.strip()))
    if "instructions" in kinds:
        raise ValueError("instructions aren't a kind of item: use --instructions block|whole, or `agt share kinds add instructions`")
    if "mcp" in kinds:
        raise ValueError("MCP servers aren't a kind of item: use --mcp on, or `agt share kinds add mcp`")
    if "hooks" in kinds:
        raise ValueError("hooks aren't a kind of item: use --hooks on, or `agt share kinds add hooks`")
    if "plugins" in kinds:
        raise ValueError("plugins aren't a kind of item: use --plugins on, or `agt share kinds add plugins`")
    later = [kind for kind in kinds if kind in LATER_KINDS]
    if later:
        raise ValueError(f"agt doesn't share {', '.join(later)} yet; it shares agents and skills")
    unknown = [kind for kind in kinds if kind not in SUPPORTED_KINDS]
    if unknown or not kinds:
        raise ValueError(f"unknown kind {', '.join(unknown) or '(none)'}; use agents, skills")
    return kinds


def _explicit_folders(args: argparse.Namespace) -> list[tuple[str, Path]] | None:
    given = [("claude", path) for path in args.claude_dir] + [("codex", path) for path in args.codex_dir]
    given += [("kiro", path) for path in args.kiro_dir] + [("antigravity", path) for path in args.agy_dir]
    if not given:
        return None
    missing = [path for _, path in given if not Path(path).expanduser().is_dir()]
    if missing:
        raise ValueError(f"no such folder: {', '.join(missing)}")
    return [(cli, Path(path).expanduser().absolute()) for cli, path in given]


def _check_label(label: str, labels: set[str], flag: str) -> None:
    if label not in labels:
        raise ValueError(f"{flag}: no folder labelled {label}; the labels are {', '.join(sorted(labels))}")


def _options(args: argparse.Namespace, labels: set[str], saved: settings.Settings | None,
             resolve: dict[str, str] | None = None, keep_copies: bool = False) -> planner.Options:
    """Saved picks first, then the flags on top of them."""

    # A flag you give wins; otherwise what you saved, otherwise the default (so `enable` again resumes).
    kinds = getattr(args, "kinds", None)
    widening = getattr(args, "tool_widening", None)
    auto = getattr(args, "auto_copy", None)
    options = planner.Options(
        kinds=_kinds(kinds) if kinds else saved.kinds if saved else planner.KINDS,
        tool_widening=widening or (saved.tool_widening if saved else "allow-if-shell"),
        auto_copy=settings.parse_auto_copy(auto) if auto is not None else saved.auto_copy if saved else planner.KINDS)
    if saved is not None:
        options.off |= saved.off
        options.skip |= {(key, label) for key, chosen in saved.not_in.items() for label in chosen if label in labels}
        options.only.update({key: {label for label in chosen if label in labels} for key, chosen in saved.only.items()})
    for value in getattr(args, "skip", []):
        item, at, label = value.rpartition("@")
        if not at or not item:
            raise ValueError(f"--skip takes ITEM@LABEL, not {value}")
        _check_label(label, labels, "--skip")
        options.skip.add((item, label))
    for value in getattr(args, "only", []):
        item, sign, rest = value.partition("=")
        chosen = {label.strip() for label in rest.split(",") if label.strip()}
        if not sign or not item or not chosen:
            raise ValueError(f"--only takes ITEM=LABEL[,LABEL], not {value}")
        for label in chosen:
            _check_label(label, labels, "--only")
        options.only[item] = chosen
    for value in getattr(args, "resolve", []):
        item, sign, label = value.partition("=")
        if not sign or not item or not label:
            raise ValueError(f"--resolve takes ITEM=LABEL or ITEM=skip, not {value}")
        if label == "skip":
            options.off.add(item)
            continue
        _check_label(label, labels, "--resolve")
        if item == "instructions":
            options.instructions_from = label
            continue
        options.resolve[item] = label
        options.off -= {item, *(f"{kind}/{item}" for kind in SUPPORTED_KINDS)}
    options.resolve.update(resolve or {})
    options.keep_copies = keep_copies
    options.project = scope.current().root
    for answer, flag in (("apply", "allow_deletes"), ("refill", "refill")):
        for label in getattr(args, flag, []):
            _check_label(label, labels, "--" + flag.replace("_", "-"))
            if options.deletes.get(label, answer) != answer:
                raise ValueError(f"{label} can't take both --allow-deletes and --refill")
            options.deletes[label] = answer
    return options


def _command_line(args: argparse.Namespace, digest: str) -> str:
    parts = ["agt", "share", "enable", "--yes", "--if-plan", digest]
    for flag, values in (("--claude-dir", args.claude_dir), ("--codex-dir", args.codex_dir), ("--kiro-dir", args.kiro_dir),
                         ("--agy-dir", args.agy_dir), ("--only", args.only), ("--skip", args.skip), ("--resolve", args.resolve)):
        for value in values:
            parts += [flag, value]
    if args.no_agy:
        parts.append("--no-agy")
    if getattr(args, "projects", "none") not in (None, "none"):
        parts += ["--projects", args.projects]
    for flag, value in (("--kinds", args.kinds), ("--tool-widening", args.tool_widening), ("--auto-copy", args.auto_copy),
                        ("--instructions", getattr(args, "instructions", None)), ("--mcp", getattr(args, "mcp", None)),
                        ("--hooks", getattr(args, "hooks", None)), ("--plugins", getattr(args, "plugins", None))):
        if value is not None:
            parts += [flag, value]
    return " ".join(shlex.quote(part) for part in parts)


def _folders(config: Any, explicit: list[tuple[str, Path]] | None, no_agy: bool,
             saved: settings.Settings | None) -> tuple[list[providers.Candidate], list[providers.Provider]]:
    """The folder rows and the ones sharing: the flags, else the saved folders, else the suggested ones.

    A saved folder keeps sharing when its CLI's binary isn't found, so a run with a smaller PATH (the
    watcher's) sees the same folders as yours. `agt share cli remove` stops one.
    """

    from_saved = explicit is None and saved is not None and bool(saved.folders)
    if from_saved:  # saved folders, even while sharing is off
        explicit = [(folder.cli, folder.path) for folder in saved.folders]
    rows = providers.candidates(config, explicit, no_agy=no_agy)
    if explicit is None:
        return rows, [row.provider for row in rows if row.suggested]
    given = {(cli, str(folder.resolve())) for cli, folder in explicit}
    included = [row.provider for row in rows if (row.reason is None or (from_saved and row.missing_binary))
                and (row.provider.cli, str(row.provider.folder.resolve())) in given]
    if saved is not None:
        # The state is keyed by label, so the saved labels win over freshly worked-out ones.
        for provider in included:
            for folder in saved.folders:
                if folder.cli == provider.cli and providers.same(folder.path, provider.folder):
                    provider.label = folder.label
    return rows, included


def _plan(args: argparse.Namespace, current: state.State, saved: settings.Settings | None, *,
          resolve: dict[str, str] | None = None, keep_copies: bool = False, own: frozenset[str] = frozenset()) -> planner.Plan:
    config = load_config()
    explicit = _explicit_folders(args) if hasattr(args, "claude_dir") else None
    rows, included = _folders(config, explicit, getattr(args, "no_agy", False), saved)
    options = _options(args, {provider.label for provider in included}, saved, resolve, keep_copies)
    plan = planner.build(config, rows, included, options, current, own=own)
    mode = getattr(args, "instructions", None) or (saved.instructions if saved is not None else "off")
    root = scope.current().root
    if mode != "off" and root is None:
        plan.instructions = instructions.build(included, mode, current,
                                               prefer=getattr(options, "instructions_from", None))
    if root is not None:  # a repo's instructions are its AGENTS.md; Claude Code reads it through a CLAUDE.md import
        plan.claude_import = instructions.claude_import(
            root, mode != "off" and any(provider.cli == "claude" for provider in included), current)
        if plan.claude_import is not None and plan.claude_import.action == "release":
            plan.notices.append(f"{plan.claude_import.note}; agt no longer looks after it")
    if _mcp_on(args, saved) and not scope.current().project:
        later = frozenset({"claude"}) if getattr(args, "trigger", "command") in MCP_LATER_TRIGGERS else frozenset()
        plan.mcp = mcp.build(included, current, options, later=later)
    if _hooks_on(args, saved) and not scope.current().project:
        plan.hooks = hooks.build(included, saved.hook_approvals if saved else {})
    if _plugins_on(args, saved) and not scope.current().project:
        # Installs clone repos and take a while: a session hook or a save leaves them to the watcher.
        later = frozenset(plugins.NATIVE) if getattr(args, "trigger", "command") in MCP_LATER_TRIGGERS else frozenset()
        plan.plugins = plugins.build(included, current, options, saved.plugin_approvals if saved else {}, later=later)
    return plan


def _mcp_on(args: argparse.Namespace, saved: settings.Settings | None) -> bool:
    flag = getattr(args, "mcp", None)
    return flag == "on" if flag else bool(saved and saved.mcp)


def _hooks_on(args: argparse.Namespace, saved: settings.Settings | None) -> bool:
    flag = getattr(args, "hooks", None)
    return flag == "on" if flag else bool(saved and saved.hooks)


def _plugins_on(args: argparse.Namespace, saved: settings.Settings | None) -> bool:
    flag = getattr(args, "plugins", None)
    return flag == "on" if flag else bool(saved and saved.plugins)


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False) if not isinstance(value, str) else value)


def _item_key(plan: planner.Plan, name: str) -> str:
    """`<kind>/<name>`, as the folders spell it, for an item named on the command line (bare when it's unique)."""

    kind, slash, bare = name.partition("/")
    if kind in (mcp.KIND, hooks.KIND, plugins.KIND) and bare:
        return name
    if not slash:
        kind, bare = "", name
    elif kind not in SUPPORTED_KINDS:
        raise ValueError(f"{name}: say agents/<name> or skills/<name>")
    wanted = names.identity(bare)
    found = sorted({(item.kind, item.name) for item in plan.items if names.identity(item.name) == wanted}
                   | {(entry.kind, entry.name) for entry in plan.excluded if names.identity(entry.name) == wanted})
    found = [entry for entry in found if not kind or entry[0] == kind]
    if not found:
        if kind:
            return name
        raise _no_item(plan, name)
    if len({entry[0] for entry in found}) > 1:
        raise ValueError(f"{name} is both an agent and a skill; say agents/{name} or skills/{name}")
    return f"{found[0][0]}/{found[0][1]}"


def _identity(key: str) -> str:
    kind, _, name = key.partition("/")
    return f"{kind}/{names.identity(name)}"


def _find(plan: planner.Plan, key: str) -> planner.Item | None:
    return next((item for item in plan.items if item.key == _identity(key)), None)


def _saved_answers(plan: planner.Plan, args: argparse.Namespace, saved: settings.Settings | None = None) -> settings.Settings:
    """Your answers from this plan; the switches and settings no flag sets stay as you saved them."""

    options = plan.options
    switches = {name: getattr(saved, name) for name in settings.SWITCHES} if saved is not None else {}
    result = settings.Settings(enabled=True, kinds=options.kinds, tool_widening=options.tool_widening,
                               auto_copy=options.auto_copy,
                               folders=[settings.Folder(p.cli, p.folder, p.label) for p in plan.included],
                               instructions=getattr(args, "instructions", None) or (saved.instructions if saved else "off"),
                               worktrees=saved.worktrees if saved else "auto", mcp=_mcp_on(args, saved),
                               hooks=_hooks_on(args, saved), hook_approvals=dict(saved.hook_approvals) if saved else {},
                               plugins=_plugins_on(args, saved),
                               plugin_approvals=dict(saved.plugin_approvals) if saved else {}, **switches)
    result.off = {_item_key(plan, name) for name in options.off}
    for name, label in options.skip:
        result.not_in.setdefault(_item_key(plan, name), []).append(label)
    for name, chosen in options.only.items():
        result.only[_item_key(plan, name)] = sorted(chosen)
    for name in options.resolve:
        _item_key(plan, name)
    return result


def _needs_choice(plan: planner.Plan, command: str, as_json: bool) -> int:
    proposal = planner.proposal(plan, command)
    asks = proposal["needs_answer"]
    if as_json:
        _print({"error": "needs_choice", "ask": [ask["ask"] for ask in asks], "choices": asks,
                "retry": command + " " + " ".join(ask["flag"] for ask in asks)})
    else:
        count = len(asks)
        lines: list[ui.Line] = [[ui.bold(command.split(" --")[0]), "  ", ui.warn(
            f"! nothing written: {count} item{'s need' if count != 1 else ' needs'} your choice")]]
        kinds = {item.name: item.kind for item in plan.conflicts}
        for ask in asks:
            name = ask["flag"].split()[1].partition("=")[0]
            versions = [choice for choice in ask["choices"] if choice["value"] != "skip"]
            labels = planner._and([choice["value"] + (" (newer)" if choice.get("newer") else "") for choice in versions])
            what = ("The instructions differ" if name == "instructions" else
                    f"{name if '/' in name else kinds.get(name, 'skills') + '/' + name} differs")
            lines += ["", f"  {what} between {labels}.",
                      "  Which version should every CLI get? Add one of these to the command:"]
            lines += ui.columns([[ui.muted(choice["value"] if choice["value"] != "skip" else "neither"),
                                  ui.cmd(f"--resolve {name}={choice['value']}"),
                                  ui.muted("every copy stays as it is") if choice["value"] == "skip" else ""]
                                 for choice in ask["choices"]], indent=4, gap=3)
        ui.out(*lines)
    return 3


MARKS = {"copy": "+", "link": "+", "update": "~", "relink": "~", "remove": "-", "rename": ">", "rename-folder": ">",
         "rename-link": ">"}
MARK_STYLES = {"+": "ok", "~": "warn", "-": "muted", ">": "warn"}
_verbose = False  # -v: each item line is followed by the file of each copy written


def _what(outcome: apply.Outcome) -> str:
    return "instructions" if outcome.kind == "instructions" else f"{outcome.kind}/{outcome.name}"


def _item_lines(result: apply.Result, hide: set[str] = frozenset(), held: list[list[ui.Line]] | None = None, *,
                again: Callable[[apply.Outcome, list[str]], ui.Line] | None = None) -> list[ui.Line]:
    """One line per item and change, naming its folders; a failed write in red, with its error. With -v, the files.
    `hide`: kinds whose writes the report shows another way; `held`: rows for items held back, lined up with the rest."""

    by_label = all(outcome.kind == "instructions" for outcome in result.written + result.failed)
    groups: dict[tuple[str, str, str, bool], list[apply.Outcome]] = {}
    for outcome in result.written:
        if outcome.kind in hide:
            continue
        # A plugin's message shows only with -v ("installed from tools"), so without it one row holds every CLI.
        message = "" if outcome.kind == plugins.KIND and not _verbose else outcome.message
        groups.setdefault((MARKS.get(outcome.action, "?"), _what(outcome), message, False), []).append(outcome)
    for outcome in result.failed:
        groups.setdefault(("✗", _what(outcome), outcome.message, True), []).append(outcome)
    rows: list[list[ui.Line]] = []
    under: list[list[ui.Line]] = []
    for (mark, what, message, failed), outcomes in groups.items():
        sign = ui.Span("error-mark" if failed else MARK_STYLES.get(mark, "muted"), mark)
        labels = list(dict.fromkeys(label for outcome in outcomes for label in outcome.labels))
        if by_label:  # instruction files: each CLI keeps its own somewhere else, so the path is the news
            for outcome in outcomes:
                rows.append([[sign, " ", ", ".join(outcome.labels)], ui.muted(short(outcome.path))])
                under.append([])
        else:
            plugin = outcomes[0].kind == plugins.KIND  # its message is where it installed from: -v shows it
            rows.append([[sign, " ", what], [ui.muted(", ".join(labels)), *([ui.muted(f" ({message})")]
                                                                          if message and not failed and not plugin else [])]])
            files = [outcome for outcome in outcomes if not plugin] if _verbose and not failed else []
            under.append(ui.columns([[ui.muted(", ".join(outcome.labels)), ui.muted(short(outcome.path))] for outcome in files],
                                    indent=6) if files else [["      ", ui.muted(message)]] if _verbose and plugin and message
                         and not failed else [])
        if failed:
            under[-1] = [["      ", *ui.inline(message)], (again or _again)(outcomes[0], labels)]
    for row in held or []:
        rows.append(row)
        under.append([])
    lines: list[ui.Line] = []
    for line, extra in zip(ui.columns(rows, gap=3), under):
        lines += [line, *extra]
    return lines


def _again(outcome: apply.Outcome, labels: list[str]) -> ui.Line:
    """Under a failed write: what tries it again. A plugin install waits for a new version, or a pick; an uninstall
    only for the command you ran."""

    if outcome.kind != plugins.KIND:
        return ["      ", ui.muted("The next sync tries again.")]
    if outcome.action == "remove":
        ran = "agt " + shlex.join(ui.ARGV) if ui.ARGV else None
        return ["      ", ui.muted("Try again"), "   ", ui.cmd(ran)] if ran else ["      ", ui.muted("Run the same command to try again.")]
    return ["      ", ui.muted("agt tries again when its version changes; "),
            ui.code(f"agt share pick {_what(outcome)} --in {','.join(labels)}"), ui.muted(" tries now.")]


def _kept(plan: planner.Plan) -> list[str]:
    """Copies in folders you left out that stay, with why: your own files now, or your original."""

    return [f"{item.name} in {label}: {cell.note}" for item in plan.items for label, cell in item.cells.items()
            if item.drops.get(label) == "release" or (cell.state == planner.HAS and cell.note)]


def _repo_lines(entry: dict[str, Any], title: str) -> list[ui.Line]:
    target = entry["scope"]
    name = target.name + (f" (a worktree of {short(target.main)})" if target.main else "")
    if entry.get("skipped"):
        return [f"{title} in {name}: left alone; {entry['skipped']}."]
    plan = entry["plan"]
    with scope.use(target):
        if "result" not in entry:
            changes = [f"  {change.action} {change.kind}/{change.name} in {', '.join(change.labels)}: {short(change.path)}"
                       for change in plan.changes] + _import_lines(plan)
            return [f"In {name}: {len(changes)} change{'s' if len(changes) != 1 else ''}, "
                    f"{len(plan.conflicts)} conflict{'s' if len(plan.conflicts) != 1 else ''}.", *changes]
        return _report_lines(plan, entry["result"], None, f"{title} in {name}", None)


def _import_lines(plan: planner.Plan) -> list[str]:
    change = plan.claude_import
    if change is None or change.action not in ("make", "remove"):
        return []
    return [f"  {'copy' if change.action == 'make' else 'remove'} CLAUDE.md holding @AGENTS.md: {short(change.path)}"]


def _repo_json(entry: dict[str, Any]) -> dict[str, Any]:
    target = entry["scope"]
    data: dict[str, Any] = {"root": str(target.root), "worktree_of": str(target.main) if target.main else None}
    if entry.get("skipped"):
        return {**data, "skipped": entry["skipped"]}
    plan = entry["plan"]
    with scope.use(target):
        if "result" not in entry:
            return {**data, "changes": len(plan.changes) + len(_import_lines(plan)),
                    "conflicts": [item.name for item in plan.conflicts]}
        return {**data, **_report_json(plan, entry["result"], None, None)}


def _report(plan: planner.Plan, result: apply.Result, saved_at: Path | None, as_json: bool, *,
            title: str = "agt share", notes: list[str] | None = None, repos: list[dict[str, Any]] | None = None,
            plugin_blocks: bool = False, found: triggers.Refreshed | None = None, headline: ui.Line | None = None,
            body: list[ui.Line] | None = None, rows: list[list[ui.Line]] | None = None,
            text_notes: list[str] | None = None, hide: set[str] = frozenset(), named: int = 0, setup: bool = False) -> None:
    """A writing run's report. --json gets `notes` then the trigger lines; a person gets `text_notes` when it's given
    (what `rows` and `body` already say), the custom `headline` unless something failed, and `rows` (label, command)
    lined up with the backup folder at the end."""

    if as_json:
        data = _report_json(plan, result, saved_at, (notes or []) + (found.lines if found else []))
        if repos:
            data["repos"] = [_repo_json(entry) for entry in repos]
        _print(data)
        return
    if setup:
        ui.out(*_setup_lines(plan, result, found), *(line for entry in repos or [] for line in ["", *_repo_lines(entry, title)]))
        return
    held = _held(plan) if title in ITEM_COMMANDS else []
    lines = _report_lines(plan, result, saved_at, title, notes if text_notes is None else text_notes,
                          plugin_blocks=plugin_blocks, found=found, headline=headline, body=body, hide=hide,
                          held=[row for row, _ in held], rows=[*(rows or []), *(action for _, action in held)])
    if plugin_blocks and plan.plugins is not None:  # the report lists the plugin waits in full
        named += len(plugin_view.waits(plan.plugins))
    lines[1:1] = _waiting_pointer(len(held) + named)
    for entry in repos or []:
        lines += ["", *_repo_lines(entry, title)]
    ui.out(*lines)


def _waiting_pointer(named: int = 0) -> list[ui.Line]:
    """What waits for you, as one line under a report's headline: `status` lists it, so a report doesn't repeat it.
    `named`: the waits the report itself names, which the line leaves out."""

    from agents_router.share import watch

    saved = settings.load()
    if not saved.enabled:
        return []
    count = watch.wait_count(saved) - named
    if count <= 0:
        return []
    more = " more" if named else ""
    return [["  ", ui.warn(f"{count}{more} thing{'s wait' if count != 1 else ' waits'} for you"), ": ",
             ui.code("agt share status"), f" lists {'them' if count != 1 else 'it'}."]]


ITEM_COMMANDS = ("agt share pick", "agt share resolve", "agt share rm", "agt share mv", "agt share undo")


def _held(plan: planner.Plan) -> list[tuple[list[ui.Line], list[ui.Line]]]:
    """An item command's own items held back: a row saying why, and the action row that settles it."""

    def row(key: str, why: str) -> list[ui.Line]:
        return [[ui.Span("error-mark", "!"), " ", key], ui.muted(why)]

    found = []
    for item in plan.conflicts:
        labels = list(dict.fromkeys(label for copy in item.copies for label in copy.labels))
        found.append((row(f"{item.kind}/{item.name}", f"differs between {planner._and(labels)}"),
                      [ui.muted("Pick a version"), ui.cmd(f"agt share resolve {item.name}")]))
    for item in plan.items:
        if item.decision and item.decision.action == engine.PENDING_DELETE:
            found.append((row(f"{item.kind}/{item.name}", "its original was deleted"),
                          [ui.muted("Remove its copies"), ui.cmd(f"agt share rm {item.name}")]))
        elif item.decision and item.decision.reason == planner.NEW_WAITING:
            found.append((row(f"{item.kind}/{item.name}", "it's new and waits for you to pick where it goes"),
                          [ui.muted("Pick where it goes"), ui.cmd(f"agt share pick {item.name}")]))
    for server in plan.mcp.conflicts if plan.mcp is not None else []:
        found.append((row(server.key, f"differs between {planner._and(list(server.decision.labels))}"),
                      [ui.muted("Pick a version"), ui.cmd(f"agt share resolve {server.key}")]))
    return found


def _report_json(plan: planner.Plan, result: apply.Result, saved_at: Path | None, notes: list[str] | None) -> dict[str, Any]:
    extra = _report_notes(plan, result, notes)
    return {
        "run": result.run, "written": [_outcome(o) for o in result.written], "failed": [_outcome(o) for o in result.failed],
        "conflicts": [item.name for item in plan.conflicts], "waiting": [item.name for item in plan.waiting],
        "notes": extra + plan.notices + [note for item in plan.items for note in item.notes], "replayed": result.replayed,
        "settings": str(saved_at) if saved_at else None,
        "backups": str(state.share_dir() / "backups" / result.run) if any(o.backup for o in result.written) else None,
        "git_exclude": [{"file": str(path), "lines": lines} for path, lines in result.excluded],
        "git_exclude_kept": [{"file": str(path), "lines": lines} for path, lines in result.unlisted],
    }


def _report_notes(plan: planner.Plan, result: apply.Result, notes: list[str] | None) -> list[str]:
    return _renamed(result) + (notes or []) + _kept(plan)


def _renamed(result: apply.Result) -> list[str]:
    return [f"{old.partition('/')[2]} is {new.partition('/')[2]} now, in every CLI. "
            f"`agt share mv {new.partition('/')[2]} {old.partition('/')[2]}` renames it back." for old, new in result.renamed]


def _kept_text(plan: planner.Plan) -> list[str]:
    """`_kept` for a person: the items with the same folder and reason in one sentence."""

    found: dict[tuple[str, str], list[str]] = {}
    for item in plan.items:
        for label, cell in item.cells.items():
            if item.drops.get(label) == "release" or (cell.state == planner.HAS and cell.note):
                found.setdefault((label, cell.note), []).append(item.name)
    lines = []
    for (label, note), names_found in found.items():
        many = len(names_found) > 1
        lines.append(f"{planner._and(names_found)} stay{'' if many else 's'} in {label} as your own file{'s' if many else ''}; "
                     f"agt no longer updates {'them' if many else 'it'}." if note.startswith("kept as your own file")
                     else f"{planner._and(names_found)} in {label}: {note}")
    return lines


def _written_headline(result: apply.Result) -> list[ui.Piece] | None:
    """What a run wrote and what failed, as a report's headline; None when it did neither."""

    count, failed = len(result.written), len(result.failed)
    fail: list[ui.Piece] = [ui.error(f"{failed} failed")]
    if count:
        folders = {label for outcome in result.written for label in outcome.labels}
        text = (f"removed {count} cop{'y' if count == 1 else 'ies'}" if all(o.action == "remove" for o in result.written)
                else f"wrote {count} change{'s' if count != 1 else ''} in {len(folders)} folder{'s' if len(folders) != 1 else ''}")
        return [text, ", ", *fail] if failed else [ui.ok("✓ " + text)]
    return ["nothing written, ", *fail] if failed else None


def _headline(plan: planner.Plan, result: apply.Result, saved_at: Path | None) -> list[ui.Piece]:
    written = _written_headline(result)
    if written is not None:
        return written
    held = plan.conflicts or plan.frozen or any(item.decision and item.decision.action == engine.PENDING_DELETE
                                                for item in plan.items) \
        or (plan.mcp is not None and (plan.mcp.conflicts or plan.mcp.pending)) \
        or (plan.instructions is not None and plan.instructions.conflict) or (plan.plugins is not None and plan.plugins.pending)
    if saved_at:  # an answer saved; ✓ only when nothing is held back
        return ["saved, nothing to write"] if held else [ui.ok("✓ saved, nothing to write")]
    return ["nothing to write"] if held else [ui.ok("✓ everything is in sync, nothing to write")]


def _report_lines(plan: planner.Plan, result: apply.Result, saved_at: Path | None, title: str,
                  notes: list[str] | None, *, plugin_blocks: bool = False, found: triggers.Refreshed | None = None,
                  headline: ui.Line | None = None, body: list[ui.Line] | None = None,
                  rows: list[list[ui.Line]] | None = None, hide: set[str] = frozenset(),
                  held: list[list[ui.Line]] | None = None) -> list[ui.Line]:
    """`plugin_blocks`: the plugins that can't go somewhere and those waiting for an answer, in full. Turning
    plugins on is when they're news; every other run points to `status`, as it does for conflicts and other waits."""

    if headline is None or result.failed:
        headline = _headline(plan, result, saved_at)
    lines: list[ui.Line] = [[ui.bold(title), "  ", *(headline if isinstance(headline, list) else [headline])]]
    items = _item_lines(result, hide, held)
    lines += ["", *items] if items else []
    lines += ["", *body] if body else []
    if plugin_blocks and plan.plugins is not None:
        for block in (plugin_view.wait_lines(plan, plan.plugins), plugin_view.cant_lines(plan.plugins)):
            lines += ["", *block] if block else []
    lines += _on_its_own(found)
    shown = _renamed(result) + (notes or []) + _kept_text(plan) + result.replayed + planner.note_lines(plan)
    shown += plan.mcp.notes() if plan.mcp is not None else []
    shown += plan.plugins.notes(restart=False, done={(o.name, label) for o in result.written if o.kind == plugins.KIND
                                                     for label in o.labels}) if plan.plugins is not None else []
    shown += [f"Listed {len(changed)} of agt's copies in {short(path)}, so that repo's `git status` doesn't show them."
              for path, changed in result.excluded]
    shown += [f"Took {len(changed)} cop{'y' if len(changed) == 1 else 'ies'} you kept out of {short(path)}, "
              "so that repo's `git status` shows them now." for path, changed in result.unlisted]
    if shown:
        lines += ["", ui.bold("Notes"), *(["  ", *ui.inline(note)] for note in shown)]
    end: list[ui.Line] = []
    if any(outcome.action in ("copy", "link") and outcome.kind in (*planner.KINDS, plugins.KIND) for outcome in result.written):
        end.append(plugin_view.next_line(result.written, plan.plugins.by_label if plan.plugins is not None else {}))
    rows = list(rows or [])
    if any(outcome.backup for outcome in result.written):
        rows.append([ui.muted("Backup"), ui.muted(short(state.share_dir() / "backups" / result.run))])
    end += ui.columns(rows, gap=3) if rows else []
    if saved_at:
        end.append(ui.muted(f"Saved your answers in {short(saved_at)}."))
    return lines + (["", *end] if end else [])


def _setup_lines(plan: planner.Plan, result: apply.Result, found: triggers.Refreshed | None) -> list[ui.Line]:
    """Sharing as one row of `agt setup`'s report: what it wrote, what keeps it the same, and the notes, unheaded."""

    count, failed = len(result.written), len(result.failed)
    folders = {label for outcome in result.written for label in outcome.labels}
    what: list[ui.Piece] = [f"{count} cop{'y' if count == 1 else 'ies'} in {len(folders)} folder{'s' if len(folders) != 1 else ''}"
                            if count else ui.muted("every folder already has them")]
    what += [", ", ui.error(f"{failed} failed")] if failed else []
    sign = ui.Span("error-mark", "✗") if failed and not count else ui.ok("+") if count else ui.muted("·")
    lines: list[ui.Line] = [["  ", sign, "  ", "Sharing".ljust(len("Claude Code")), "  ", *what]]
    lines += [["    ", *line] for line in _item_lines(result)]
    said = []
    if found is not None:
        starts = " at each session start in Claude Code" if found.catch_up else ""
        if found.watcher == "runs":
            said.append(f"agt keeps them the same from now on: every minute{', and' + starts if starts else ''}.")
        elif found.watcher == "missing":
            said.append(f"There's no background watcher here: {found.missing}." + (f" agt catches up{starts}." if starts else ""))
        elif found.watcher == "off":
            said.append("The background watcher is off (share.watcher = false)." + (f" agt catches up{starts}." if starts else ""))
        said += found.problems
    notes = _kept_text(plan) + planner.note_lines(plan) + (plan.mcp.notes() if plan.mcp is not None else [])
    return lines + ([""] if said or notes else []) + [["  ", *ui.inline(line)] for line in said] \
        + [["  ", ui.muted(note)] for note in notes]


def _on_its_own(found: triggers.Refreshed | None) -> list[ui.Line]:
    """What keeps sharing going without you, when this run started or stopped some of it."""

    if found is None or not found.news:
        return []
    if not found.enabled:
        return ["", *(["  ", *ui.inline(line)] for line in _removed_lines(found.lines))]
    said = {"runs": ["The background watcher checks every minute, and at once when an agents or skills folder changes."],
            "missing": [f"There's no background watcher here: {found.missing}."],
            "off": ["The background watcher is off (share.watcher = false)."]}.get(found.watcher, [])
    saves = [label for label, also in found.catch_up.items() if also]
    starts = [label for label, also in found.catch_up.items() if not also]
    if saves:
        said.append(f"In {planner._and(saves)}, agt also catches up at each session start and each save in Claude Code.")
    if starts:
        said.append(f"In {planner._and(starts)}, agt also catches up at each session start.")
    return ["", ui.bold("Syncs on its own"), *(["  ", *ui.inline(line)] for line in found.problems + said)]


def _outcome(outcome: apply.Outcome) -> dict[str, Any]:
    return {"kind": outcome.kind, "name": outcome.name, "labels": outcome.labels, "action": outcome.action,
            "path": str(outcome.path), "message": outcome.message}


def _remember(plan: planner.Plan, current: state.State, result: apply.Result, keys: set[str] | None = None) -> None:
    """After a writing run: log what passed, and update the attention list (only these items' entries, with `keys`)."""

    for outcome in result.failed:
        kind = "skipped" if outcome.message.startswith(apply.SKIPPED) else "write-failed"
        attention.event(kind, f"{outcome.kind}/{names.identity(outcome.name)}", f"{outcome.path}: {outcome.message}")
    for line in result.replayed:
        attention.event("replayed", "journal", line)
    attention.save(plan, current, keys)


def _move_picks(saved: settings.Settings, old: str, new: str) -> bool:
    """Your picks for an item follow it to its new name (`<kind>/<identity>` keys). Returns whether any moved."""

    moved = False
    for chosen in (saved.not_in, saved.only):
        for key in [key for key in chosen if _identity(key) == old]:
            chosen[new] = chosen.pop(key)
            moved = True
    for key in [key for key in saved.off if _identity(key) == old]:
        saved.off.discard(key)
        saved.off.add(new)
        moved = True
    return moved


def _after_renames(args: argparse.Namespace, saved: settings.Settings, current: state.State,
                   result: apply.Result) -> Path | None:
    """After a rename agt spotted: your picks move to the new name, then the item is filled where it's missing.

    Call it holding the lock. Returns where the settings were saved, if they changed.
    """

    if not result.renamed:
        return None
    saved_at = settings.save(saved) if any([_move_picks(saved, old, new) for old, new in result.renamed]) else None
    again = _plan(args, current, saved).restricted({new for _, new in result.renamed})
    more = apply.apply(again, current)
    result.outcomes += more.outcomes
    result.excluded += more.excluded
    return saved_at


def _user_folders(saved: settings.Settings | None) -> list[Path]:
    """Your CLIs' own folders, which can't be repos too."""

    found = [folder.path for folder in saved.folders] if saved else []
    return found + [path for _, path in providers.discovered_folders(load_config())]


def _project_choice(args: argparse.Namespace, saved: settings.Settings | None) -> list[Path]:
    """The repos `--projects` adds: none, every repo your CLIs know that has agents or skills, or these paths."""

    value = getattr(args, "projects", "none") or "none"
    if value == "none":
        return []
    listed = {os.path.realpath(entry["root"]) for entry in scope.listed()}
    mine = _user_folders(saved)
    if value == "all-with-items":
        config = load_config()
        claude = [path for cli, path in providers.discovered_folders(config) if cli == "claude"]
        found = scope.known_projects(claude, default_primary("codex"), mine)
        return [root for root in found if scope.has_items(root) and os.path.realpath(root) not in listed]
    roots = []
    for part in value.split(","):
        root = Path(part.strip()).expanduser().absolute()
        why = scope.refuse(root, mine)
        if why:
            raise ValueError(f"--projects: {why}")
        if os.path.realpath(root) not in listed:
            roots.append(Path(os.path.realpath(root)))
    return roots


def _enable(args: argparse.Namespace) -> int:
    saved = settings.load()
    if not (args.dry_run or args.yes or args.json) and _interactive():
        return _enable_screen(args, saved)
    if args.dry_run or not args.yes:
        plan = _plan(args, state.load(), saved)
        for name in [*args.resolve, *args.skip, *args.only]:
            if name.partition("=")[0] == "instructions":
                continue
            _item_key(plan, name.partition("=")[0].rpartition("@")[0] or name.partition("=")[0])
        line = _command_line(args, plan.digest)
        repos = _project_choice(args, saved)
        if args.json:
            _print({**planner.proposal(plan, line), "repos": [str(root) for root in repos]})
        else:
            ui.out(*planner.render(plan, line, intro=None if saved.enabled else _intro(plan)),
                   *(["", "Repos to share too: " + ", ".join(short(root) for root in repos)] if repos else []))
        if not args.dry_run and not args.json:
            print("\nNothing was written. Add --yes to save these answers and apply the plan.")
        return 0 if args.dry_run else 2
    return _enable_write(args, saved)


def _intro(plan: planner.Plan) -> str:
    """What sharing does, in one sentence, for someone who hasn't turned it on yet. Only the kinds every CLI gets:
    hooks wait for your approval, and plugins install where they fit."""

    kinds = [*plan.options.kinds] + (["MCP servers"] if plan.mcp is not None else []) \
        + (["shared instructions"] if plan.instructions is not None else [])
    return f"agt copies your {planner._and(kinds)} between these CLI folders, so every CLI has the same ones."


def _enable_write(args: argparse.Namespace, saved: settings.Settings, *, screen: bool = False,
                  setup: bool = False) -> int | None:
    """`enable --yes`: save your answers and apply the plan. On the screen, None means the plan changed meanwhile.
    `setup`: the report is part of `agt setup`'s, under its headline."""

    if apply.in_worker():
        print(IN_WORKER)
        return 2
    config = load_config()
    _rows, included = _folders(config, _explicit_folders(args), args.no_agy, saved)
    with apply.lock([provider.folder for provider in included]):
        current = state.load()
        replayed = apply.replay(current)
        plan = _plan(args, current, saved)
        line = _command_line(args, plan.digest)
        if args.if_plan and args.if_plan != plan.digest:
            if screen:
                return None
            ui.out(*planner.render(plan, line, header=[ui.bold("agt share enable"), "  ", ui.warn(
                "! nothing written: the plan changed since your dry run")]))
            return 1
        if (plan.conflicts or (plan.mcp is not None and plan.mcp.conflicts)) and not (args.if_plan or args.accept_suggested):
            return _needs_choice(plan, line, args.json)
        answers = _saved_answers(plan, args, saved)
        saved_at = settings.save(answers)
        result = apply.apply(plan, current, command="agt share enable")
        result.replayed = replayed
        _after_renames(argparse.Namespace(**{key: value for key, value in vars(args).items() if key != "kinds"}),
                       answers, current, result)
        _remember(plan, current, result)
    repos = []
    for root in _project_choice(args, saved):
        scope.add(root, "sweep" if args.projects == "all-with-items" else "command")
        repos.append(_sync_scope(argparse.Namespace(json=args.json, dry_run=False), scope.Scope(root), "agt share enable"))
    _report(plan, result, saved_at, args.json, title="agt share enable", found=_triggers(answers, plan.included), repos=repos,
            setup=setup)
    return 1 if result.failed or any(entry.get("result") and entry["result"].failed for entry in repos) else 0


def _enable_screen(args: argparse.Namespace, saved: settings.Settings) -> int:
    """`agt share enable` for a person at a terminal: the plan, then Enter applies it, `p` changes it, `q` quits.

    A conflict is asked first. Enter applies exactly the plan on screen (`--yes --if-plan <id>`): if it changed
    meanwhile, the new one is shown instead.
    """

    while True:
        plan = _plan(args, state.load(), saved)
        answered = {value.partition("=")[0] for value in args.resolve}
        open_conflicts = [item for item in plan.conflicts if item.name not in answered and f"{item.kind}/{item.name}" not in answered]
        if open_conflicts:
            ui.out(_first_choice("agt share enable", len(open_conflicts)), "")
            for item in open_conflicts:
                choice = _ask_version(item)
                if choice is None:
                    print("Nothing was written.")
                    return 0
                args.resolve.append(f"{item.name}={choice}")
            continue
        line = _command_line(args, plan.digest)
        ui.out(*planner.render(plan, line, header=[ui.bold("agt share enable"), "  ", ui.muted(
            "what sharing would do; nothing is written until you press Enter")], intro=None if saved.enabled else _intro(plan),
                               apply=False))
        try:
            answer = input("\nApply it? Enter yes · p change it · q quit: ").strip().lower()
        except EOFError:
            answer = "q"
        if answer == "q":
            print("Nothing was written.")
            return 0
        if answer == "p":
            _enable_lists(args, saved, plan)
            continue
        if answer:
            continue
        args.yes, args.if_plan = True, plan.digest
        code = _enable_write(args, saved, screen=True)
        if code is not None:
            return code
        args.yes, args.if_plan = False, None
        print("\nThe plan changed while you looked at it; here's the new one.")


def _enable_lists(args: argparse.Namespace, saved: settings.Settings, plan: planner.Plan) -> None:
    """`p` on the enable screen: kinds, folders, the on/off choices, then what goes where.

    Your answers become the same ones the flags give. `q` in any list drops every change from these lists.
    """

    before = {key: list(value) if isinstance(value, list) else value for key, value in vars(args).items()}
    picks = (dict(saved.not_in), dict(saved.only), set(saved.off))

    def drop() -> None:
        vars(args).update(before)
        saved.not_in, saved.only, saved.off = dict(picks[0]), dict(picks[1]), set(picks[2])

    kinds = picker.ListState("Which kinds share?", [picker.Choice(kind, kind, kind in plan.options.kinds) for kind in SUPPORTED_KINDS],
                             verbs=("share", "stop sharing"), enter="next")
    if picker.choose(kinds) == picker.QUIT or not kinds.checked():
        return drop()
    args.kinds = ",".join(kind for kind in SUPPORTED_KINDS if kind in kinds.checked())
    # Every folder agt can find, not only the ones in this plan, so a CLI installed later shows up unticked.
    config = load_config()
    given = [(provider.cli, provider.folder) for provider in plan.included]
    found = given + [entry for entry in providers.discovered_folders(config)
                     if not any(entry[0] == cli and providers.same(entry[1], path) for cli, path in given)]
    rows = providers.candidates(config, found)
    sharing = {(folder.cli, str(folder.path.resolve())) for folder in saved.folders} if saved.enabled else set()
    in_plan = {(provider.cli, str(provider.folder.resolve())) for provider in plan.included}
    labels = {(provider.cli, str(provider.folder.resolve())): provider.label for provider in plan.included}
    choices = []
    named = [labels.get((row.provider.cli, str(row.provider.folder.resolve())), row.provider.label) for row in rows]
    widths = (max(map(len, named), default=0), max((len(short(row.provider.folder)) for row in rows), default=0))
    for index, row in enumerate(rows):
        provider = row.provider
        where = (provider.cli, str(provider.folder.resolve()))
        label = named[index]
        text = (f"{label:<{widths[0]}}  {short(provider.folder):<{widths[1]}}  "
                f"{row.holds.get('agents', 0)} agents, {row.holds.get('skills', 0)} skills")
        fixed = "sharing now; `agt share cli remove` stops it" if where in sharing else None
        choices.append(picker.Choice(str(index), text, where in in_plan, row.reason, fixed, name=label))
    folders = picker.ListState("Which CLI folders share?", choices, verbs=("joins", "leaves"), enter="next")
    if picker.choose(folders) == picker.QUIT or not folders.checked():
        return drop()
    chosen = [rows[int(key)].provider for key in sorted(folders.checked(), key=int)]
    for cli, flag in (("claude", "claude_dir"), ("codex", "codex_dir"), ("kiro", "kiro_dir"), ("antigravity", "agy_dir")):
        setattr(args, flag, [str(provider.folder) for provider in chosen if provider.cli == cli])
    args.no_agy = not any(provider.cli == "antigravity" for provider in chosen)
    switches = picker.ListState("Choices", [
        picker.Choice("widen", "Codex agents get every tool (Codex can't limit an agent's tools)",
                      plan.options.tool_widening == "allow-if-shell", name="tool widening"),
        picker.Choice("auto", "New items go to every CLI (auto copy)", bool(plan.options.auto_copy), name="auto copy")],
        enter="next")
    if picker.choose(switches) == picker.QUIT:
        return drop()
    # Only a switch you flipped changes (auto copy for some kinds stays as it is otherwise).
    if ("widen" in switches.checked()) != (plan.options.tool_widening == "allow-if-shell"):
        args.tool_widening = "allow-if-shell" if "widen" in switches.checked() else "skip"
    if ("auto" in switches.checked()) != bool(plan.options.auto_copy):
        args.auto_copy = "on" if "auto" in switches.checked() else "off"
    shown = _plan(args, state.load(), saved)
    current = state.load()
    rows = [picker.Row(f"{item.kind}/{item.name}", item.name, {
        label: _grid_cell(item, label, set((current.items.get(item.key) or {}).get("made") or []))
        for label in (provider.label for provider in shown.included)}) for item in shown.items]
    grid = picker.GridState([ui.bold("What goes where"), "  ", ui.muted("Enter takes your changes back to the plan")],
                            [p.label for p in shown.included], rows, enter="done", leave="back")
    if picker.choose(grid) == picker.QUIT:
        return drop()
    try:
        for (key, label), ticked in sorted(grid.changes().items()):
            _change_picks(saved, key, [label] if ticked else [], [] if ticked else [label], None, _find(shown, key))
    except ValueError as exc:
        print(f"Not changed: {exc}.")
        return drop()


def _triggers(saved: settings.Settings, included: list[providers.Provider] | None = None) -> triggers.Refreshed:
    """Bring the watcher and the hook entries in line with the settings: what runs sharing now, and whether that changed."""

    if scope.current().project:
        return triggers.Refreshed()  # the watcher and the hook entries belong to your user folders
    try:
        if included is None and saved.enabled:
            included = _folders(load_config(), None, False, saved)[1]
        return triggers.refreshed(saved, included or [])
    except (OSError, triggers.TriggerError) as exc:
        line = f"The background side couldn't be set up ({exc}); `agt doctor --fix` tries again."
        return triggers.Refreshed([line], news=True, watcher="failed", problems=[line])


def _refresh_triggers(saved: settings.Settings, included: list[providers.Provider] | None = None) -> list[str]:
    """The lines saying what runs sharing now, after bringing it in line with the settings."""

    return _triggers(saved, included).lines


def _sync(args: argparse.Namespace) -> int:
    if args.if_stale or args.quiet or args.trigger != "command":
        from agents_router.share import watch

        return watch.sync_command(args)
    saved = settings.load()
    if not saved.enabled:
        _say_off(args)
        return 0
    return _run_sync(args, saved, "agt share sync")


def _sync_scope(args: argparse.Namespace, target: scope.Scope, command: str,
                user: settings.Settings | None = None) -> dict[str, Any]:
    """One repo's sync (or dry run): its plan and result, or why it was left alone. `user`: your new settings, not yet saved."""

    with scope.use(target):
        why = scope.busy(target.root)
        if why:
            return {"scope": target, "skipped": f"git is in the middle of a {why} there, so agt catches up afterwards"}
        saved = settings.load(user=user)
        if getattr(args, "dry_run", False):
            return {"scope": target, "plan": _plan(args, state.load(), saved)}
        included = _folders(load_config(), None, False, saved)[1]
        with apply.lock([provider.folder for provider in included]):
            current = state.load()
            replayed = apply.replay(current)
            plan = _plan(args, current, saved)
            if any(item.decision.reason == "saved less than 3 s ago" for item in plan.waiting):
                time.sleep(planner.FRESH_SECONDS)
                plan = _plan(args, current, saved)
            result = apply.apply(plan, current, command=command, trigger=getattr(args, "trigger", "command"))
            result.replayed = replayed
            _after_renames(args, saved, current, result)
            _remember(plan, current, result)
        return {"scope": target, "plan": plan, "result": result}


def _repo_scopes(saved: settings.Settings) -> list[scope.Scope]:
    return scope.scopes(worktrees_join=saved.worktrees == "auto") if saved.enabled else []


def _run_sync(args: argparse.Namespace, saved: settings.Settings, command: str, *,
              save: bool = False, notes: list[str] | None = None,
              shown: Callable[[planner.Plan, apply.Result], dict[str, Any]] | None = None,
              preview: Callable[[planner.Plan], list[ui.Line]] | None = None) -> int:
    """A whole sync from these settings: `sync`, and `cli add` / `kinds add`, which `save` theirs first. Listed repos too.

    `shown(plan, result)` gives the report's own headline, body, rows or text notes (`_report`); `preview(plan)`, a
    dry run's own lines.
    """

    def repo_runs(dry_run: bool) -> list[dict[str, Any]]:
        if scope.current().project:
            return []
        # `cli add` and `kinds add` reach the repos at once, with the settings they save.
        return [_sync_scope(argparse.Namespace(json=args.json, dry_run=dry_run), target, command, saved if save else None)
                for target in _repo_scopes(saved)]

    if getattr(args, "dry_run", False):
        repos = repo_runs(True)
        plan = _plan(args, state.load(), saved)
        if args.json:
            _print({**planner.proposal(plan, command, saves=False), "repos": [_repo_json(entry) for entry in repos]})
        else:
            ui.out(*(preview(plan) if preview else
                     planner.render(plan, command, header=[ui.bold(command), "  ", ui.muted("dry run, nothing written")],
                                    saves=False)),
                   *(line for entry in repos for line in ["", *_repo_lines(entry, command)]))
        return 0
    if apply.in_worker():
        print(IN_WORKER)
        return 2
    included = _folders(load_config(), None, False, saved)[1]
    with apply.lock([provider.folder for provider in included]):
        current = state.load()
        replayed = apply.replay(current)
        plan = _plan(args, current, saved)
        if any(item.decision.reason == "saved less than 3 s ago" for item in plan.waiting):
            time.sleep(planner.FRESH_SECONDS)  # wait the edit out instead of skipping it
            plan = _plan(args, current, saved)
        saved_at = settings.save(saved) if save else None  # saved only now, holding the lock
        result = apply.apply(plan, current)
        result.replayed = replayed
        saved_at = _after_renames(args, saved, current, result) or saved_at
        _remember(plan, current, result)
    repos = repo_runs(False)  # after your folders, so no repo gets ahead of the settings saved above
    _report(plan, result, saved_at, args.json, title=command, notes=notes, repos=repos,
            plugin_blocks=command == "agt share kinds add plugins", found=_triggers(saved, plan.included) if save else None,
            **(shown(plan, result) if shown else {}))
    return 1 if result.failed or any(entry.get("result") and entry["result"].failed for entry in repos) else 0


def _core_text(copy: planner.Physical, kind: str) -> list[str]:
    if copy.obj is None:
        return [f"(can't be read: {copy.problem})"]
    if kind == "agents":
        core = copy.obj.core
        return [f"name: {core['name']}", f"description: {core['description']}", "", *core["body"].splitlines()]
    text = normalize.text((Path(copy.obj.real) / "SKILL.md").read_text(encoding="utf-8"))
    return text.splitlines()


def _diff(item: planner.Item, toward: planner.Physical | None = None) -> list[str]:
    """How the copies differ: the first against each other one (`status --diff --json` has always given that), or each
    other one against `toward`, the newest, so its lines are the green + side for a person."""

    pairs = [(item.copies[0], other) for other in item.copies[1:]] if toward is None else \
        [(other, toward) for other in item.copies if other is not toward]
    lines = []
    for first, other in pairs:
        lines += list(difflib.unified_diff(_core_text(first, item.kind), _core_text(other, item.kind),
                                           fromfile=f"{'/'.join(first.labels)} {short(first.path)}",
                                           tofile=f"{'/'.join(other.labels)} {short(other.path)}", lineterm=""))
        if item.kind == "skills" and first.obj and other.obj:
            files = lambda copy: {p.relative_to(copy.obj.real).as_posix() for p in Path(copy.obj.real).rglob("*")
                                  if p.is_file() and not any(part.startswith(".") for part in p.relative_to(copy.obj.real).parts)}
            only_first, only_other = files(first) - files(other), files(other) - files(first)
            if only_first or only_other:
                lines.append(f"files only in {'/'.join(first.labels)}: {', '.join(sorted(only_first)) or 'none'}; "
                             f"only in {'/'.join(other.labels)}: {', '.join(sorted(only_other)) or 'none'}")
    return lines


def _home(args: argparse.Namespace) -> int:
    """`agt share` on its own: the status, then what people come here to do. It's a screen you open to look
    around, so it exits 0 even when something waits; `agt share status` exits 1 then."""

    _status(argparse.Namespace(**{**vars(args), "diff": False, "json": False}))
    if settings.load().enabled:
        ui.out("", *ui.columns([
            [ui.muted("What goes where"), ui.cmd("agt share pick")],
            [ui.muted("Take one out of a CLI"), ui.cmd("agt share pick <name> --not-in <cli>")],
            [ui.muted("Share more kinds"), ui.cmd("agt share kinds add plugins|mcp|hooks|instructions")],
            [ui.muted("Every command"), ui.cmd("agt share --help")],
        ], gap=3))
    return 0


def _status(args: argparse.Namespace) -> int:
    saved = settings.load()
    if not saved.enabled:
        _say_off(args)
        return 0
    current = state.load()
    plan = _plan(args, current, saved)
    pending = [item for item in plan.items if item.decision and item.decision.action == engine.PENDING_DELETE]
    in_sync = [item for item in plan.items if item.decision and item.decision.action == engine.NOTHING and item.decision.version]
    waiting = [item for item in plan.waiting if item.decision.reason == "saved less than 3 s ago"]
    needs = attention.entries(plan, current)
    from agents_router.share import watch

    health = watch.health()
    if saved.watcher and health["stopped"]:
        needs.append({"id": "watcher", "kind": "watcher", "item": attention.FOLDER, "who": "agent",
                      "message": "the background watcher stopped", "fix": ["agt doctor --fix"]})
    shared = plan.instructions
    servers, shared_hooks, shared_plugins = plan.mcp, plan.hooks, plan.plugins
    code = 1 if plan.conflicts or pending or plan.frozen or (shared is not None and shared.conflict) \
        or (servers is not None and (servers.conflicts or servers.pending)) \
        or (shared_plugins is not None and (shared_plugins.pending or shared_plugins.waiting)) else 0
    repos = [] if scope.current().project else [_repo_status(target) for target in _repo_scopes(saved)]
    if any(entry.get("conflicts") or entry.get("pending") for entry in repos):
        code = 1
    if args.json:
        _print({"enabled": True, "folders": [{"label": p.label, "cli": p.cli, "path": str(p.folder)} for p in plan.included],
                "in_sync": len(in_sync), "to_write": len(plan.changes), "conflicts": [i.name for i in plan.conflicts],
                "waiting": [i.name for i in plan.waiting], "pending_delete": [i.name for i in pending],
                "frozen": plan.frozen, "not_shared": len(plan.excluded), "notes": plan.notices, "attention": needs,
                "watcher": health, "instructions": shared.summary() if shared is not None else None,
                "mcp": servers.summary() if servers is not None else None,
                "hooks": shared_hooks.summary() if shared_hooks is not None else None,
                "plugins": shared_plugins.summary() if shared_plugins is not None else None,
                "repos": [{key: value for key, value in entry.items() if key != "scope"} for entry in repos],
                "diffs": {item.name: _diff(item) for item in plan.conflicts} if args.diff else None})
        return code
    by_key = {item.key: item for item in plan.items}

    def diff(entry: dict[str, Any]) -> list[str]:
        item = by_key.get(entry["item"]) if entry["kind"] == "conflict" else None
        return _diff(item, planner.newest(item)) if item is not None else []  # toward the newest, as the version question

    ui.out(*status_view.lines(plan, saved, in_sync=len(in_sync), needs=needs, health=health, fresh=waiting, repos=repos,
                              diff=diff if args.diff else None))
    return code


# Item commands -----------------------------------------------------------------------


def _repo_status(target: scope.Scope) -> dict[str, Any]:
    """One listed repo for `status`: a line, the counts, and what waits there."""

    name = target.name + (f" (a worktree of {short(target.main)})" if target.main else "")
    data: dict[str, Any] = {"scope": target, "root": str(target.root), "worktree_of": str(target.main) if target.main else None}
    why = scope.busy(target.root)
    if why:
        return {**data, "line": f"Repo {name}: left alone while git is in the middle of a {why}.", "busy": why}
    with scope.use(target):
        current = state.load()
        plan = _plan(argparse.Namespace(json=False), current, settings.load())
        in_sync = [item for item in plan.items if item.decision and item.decision.action == engine.NOTHING and item.decision.version]
        pending = [item.name for item in plan.items if item.decision and item.decision.action == engine.PENDING_DELETE]
        needs = attention.entries(plan, current)
    return {**data, "in_sync": len(in_sync), "to_write": len(plan.changes), "conflicts": [i.name for i in plan.conflicts],
            "pending": pending, "attention": needs,
            "line": f"Repo {name}: {len(in_sync)} items in sync, {len(plan.changes)} changes for the next sync, "
                    f"{len(plan.conflicts)} conflicts."}


def _interactive() -> bool:
    """A person at a terminal. An agent never gets a prompt, even with a terminal (decision 3)."""

    try:
        return sys.stdin.isatty() and sys.stdout.isatty() and hosts.detect_host() == "unknown"
    except (AttributeError, ValueError):
        return False


def _enabled() -> settings.Settings | None:
    saved = settings.load()
    return saved if saved.enabled else None


def _label_list(values: list[str], labels: set[str], flag: str) -> list[str]:
    found = [label.strip() for value in values for label in value.split(",") if label.strip()]
    for label in found:
        _check_label(label, labels, flag)
    return list(dict.fromkeys(found))


def _locked_plan(args: argparse.Namespace, saved: settings.Settings, current: state.State, keys: set[str],
                 **kwargs: Any) -> planner.Plan:
    """The plan for these items, after waiting out an edit saved less than 3 s ago (like `sync`)."""

    plan = _plan(args, current, saved, **kwargs).restricted(keys)
    if any(item.decision.reason == "saved less than 3 s ago" for item in plan.waiting):
        time.sleep(planner.FRESH_SECONDS)
        plan = _plan(args, current, saved, **kwargs).restricted(keys)
    _no_rename_pending(plan)
    return plan


def _no_rename_pending(plan: planner.Plan) -> None:
    """An item renamed by hand and not synced yet: `sync` finishes the rename and moves your picks first."""

    for item in plan.items:
        if item.decision and item.decision.action == "rename":
            raise ValueError(f"{item.name} was renamed from {item.renamed_from.partition('/')[2]} by hand; "
                             "`agt share sync` finishes the rename, and your picks follow it")


def _not_shared(plan: planner.Plan, key: str) -> str | None:
    entry = next((entry for entry in plan.excluded if _identity(f"{entry.kind}/{entry.name}") == _identity(key)), None)
    return entry.reason if entry else None


def _existing(plan: planner.Plan, name: str) -> str:
    """The key of an item agt can see: shared, or left out with a reason."""

    key = _item_key(plan, name)
    if _find(plan, key) is None and _not_shared(plan, key) is None:
        raise _no_item(plan, name)
    return key


def _no_item(plan: planner.Plan, name: str) -> ui.Mistake:
    """No item by that name: the closest one there is, and the command with it."""

    known = sorted({item.name for item in plan.items} | {entry.name for entry in plan.excluded})
    close = difflib.get_close_matches(name.rpartition("/")[2], known, 1, 0.6)
    prog = "agt share " + ui.ARGV[1] if ui.ARGV[:1] == ["share"] and len(ui.ARGV) > 1 else "agt share"
    return ui.Mistake(prog, f"no agent or skill named {name} in the folders sharing", close[0] if close else None, name,
                      [ui.code("agt share pick"), " lists every item and where it goes."])


def _show_picks(args: argparse.Namespace, plan: planner.Plan, key: str | None) -> int:
    """`pick` with no flags: the grid for a person at a terminal; the table and `needs_choice` otherwise."""

    if key is None:
        shown = sorted(plan.items, key=lambda item: (not any(c.state == planner.WAITING for c in item.cells.values()),
                                                     planner.KINDS.index(item.kind), item.name))
    else:
        item = _find(plan, key)
        if item is None:
            print(f"{key.partition('/')[2]} isn't shared: {_not_shared(plan, key)}. "
                  f"`agt share pick {key.partition('/')[2]} --in <cli>` shares it again.")
            return 0
        shown = [item]
    if not args.json and _interactive():
        return _pick_grid(args, plan, shown)
    retry = f"agt share pick {key.partition('/')[2] if key else '<item>'} --in <label> | --not-in <label> | --only <label>[,<label>]"
    if args.json:
        _print({"error": "needs_choice", "ask": "Which CLIs should get it?", "labels": [p.label for p in plan.included],
                "choices": [{"kind": item.kind, "name": item.name,
                             "cells": {label: {"state": cell.state, "note": cell.note} for label, cell in item.cells.items()}}
                            for item in shown], "retry": retry})
        return 3
    name = key.partition("/")[2] if key else "<name>"
    lines: list[ui.Line] = [[ui.bold("agt share pick"), "  ", ui.muted("what goes where; nothing changed")], "",
                            *planner.table(plan, shown), "", ui.bold("Change one" if key is None else "Change it")]
    lines += ui.columns([[ui.muted("Add a CLI"), ui.cmd(f"agt share pick {name} --in <cli>")],
                         [ui.muted("Take a CLI out"), ui.cmd(f"agt share pick {name} --not-in <cli>")],
                         [ui.muted("Only these CLIs"), ui.cmd(f"agt share pick {name} --only <cli>[,<cli>]")]], gap=3)
    lines.append(ui.muted("--not-in removes the copy agt made there, with a backup; --keep-copies keeps it as your file."))
    ui.out(*lines)
    return 0  # a listing; --json still answers needs_choice with 3


def _grid_cell(item: planner.Item, label: str, made: set[str]) -> picker.Cell:
    """One cell of the pick grid: ticked where the item is or goes, and fixed where agt never changes it."""

    cell = item.cells.get(label, planner.Cell(planner.BLOCKED, "not a folder agt shares"))
    state, note = cell.state, cell.note
    if state == planner.HAS:
        if note:  # your original, or a folder another CLI reads
            return picker.Cell(picker.FIXED, note)
        if label not in made:
            return picker.Cell(picker.FIXED, "your original: agt never removes it")
        return picker.Cell(picker.ON, "agt's copy; untick to remove it, with a backup", there=True)
    if state in (planner.COPY, planner.UPDATE, planner.CONFLICTED):
        return picker.Cell(picker.ON, note or "agt copies it there", there=state != planner.COPY and label in made)
    if state == planner.WAITING:
        if note.startswith(planner.NEW_WAITING):
            return picker.Cell(picker.OFF, "new: tick where it should go")
        if note == "its folder is missing or frozen this run":
            return picker.Cell(picker.FIXED, note)
        return picker.Cell(picker.ON, note, there=label in made)
    if state == planner.BLOCKED:
        return picker.Cell(picker.BLOCKED, note)
    return picker.Cell(picker.OFF, note or "left out")  # unticked, kept as your own file, or a removal still to finish


def _pick_grid(args: argparse.Namespace, plan: planner.Plan, shown: list[planner.Item]) -> int:
    current = state.load()
    columns = [provider.label for provider in plan.included]
    rows = []
    for item in shown:
        made = set((current.items.get(item.key) or {}).get("made") or [])
        rows.append(picker.Row(f"{item.kind}/{item.name}", item.name, {label: _grid_cell(item, label, made) for label in columns}))
    grid = picker.GridState([ui.bold("agt share pick"), "  ", ui.muted("which CLIs get what; Enter applies your changes")],
                            columns, rows)
    changes = grid.changes() if picker.choose(grid) == picker.DONE else {}
    if not changes:
        print("Nothing was changed.")
        return 0
    picks: dict[str, tuple[list[str], list[str]]] = {}
    for (key, label), ticked in sorted(changes.items()):
        to_in, to_out = picks.setdefault(key, ([], []))
        (to_in if ticked else to_out).append(label)
    return _apply_picks(args, picks, keep_copies=False)


def _change_picks(saved: settings.Settings, key: str, to_in: list[str], to_out: list[str], only: list[str] | None,
                  item: planner.Item | None) -> None:
    """One item's picks in the settings: `--in`, `--not-in` and `--only`."""

    name = key.partition("/")[2]
    not_in = set(saved.not_in.get(key, []))
    chosen = sorted(only) if only is not None else saved.only.get(key)
    if item is not None and item.decision and item.decision.reason == planner.NEW_WAITING and chosen is None and not to_out:
        # A new item waiting for targets (auto copy off): it goes where it is now, plus where you pick.
        chosen = sorted({label for copy in item.copies for label in copy.labels})
    for label in to_in:
        not_in.discard(label)
        if chosen is not None and label not in chosen:
            chosen = sorted([*chosen, label])
    for label in to_out:
        not_in.add(label)
        if chosen is not None and label in chosen:
            chosen = [entry for entry in chosen if entry != label]
    if chosen is not None and not chosen:
        raise ValueError(f"that leaves {name} in no CLI; `agt share rm {name}` removes it everywhere")
    if only is not None:
        not_in -= set(only)
    saved.not_in.pop(key, None)
    saved.only.pop(key, None)
    if not_in:
        saved.not_in[key] = sorted(not_in)
    if chosen is not None:
        saved.only[key] = sorted(chosen)
    if to_in or only:
        saved.off.discard(key)


def _apply_picks(args: argparse.Namespace, picks: dict[str, tuple[list[str], list[str]]], *, keep_copies: bool,
                 only: dict[str, list[str]] | None = None) -> int:
    """Save these items' picks and act on them at once, in one run. `picks`: key -> (labels in, labels out).

    The settings are read again holding the lock, so a pick made meanwhile elsewhere isn't lost.
    """

    if apply.in_worker():
        print(IN_WORKER)
        return 2
    only = only or {}
    saved = settings.load()
    included = _folders(load_config(), None, False, saved)[1]
    keys = {_identity(key) for key in picks}
    with apply.lock([provider.folder for provider in included]):
        current = state.load()
        replayed = apply.replay(current)
        saved = settings.load()
        before = _plan(args, current, saved)
        _no_rename_pending(before.restricted(keys))
        for key, (to_in, to_out) in picks.items():
            _change_picks(saved, key, to_in, to_out, only.get(key), _find(before, key))
            record = current.items.get(_identity(key))
            if record:
                # A CLI picked again gets the item again, whatever stopped it there.
                back = set(to_in) | set(only.get(key) or [])
                for field_name in ("stopped", "released"):
                    left = [label for label in record.get(field_name) or [] if label not in back]
                    if left:
                        record[field_name] = left
                    else:
                        record.pop(field_name, None)
        saved_at = settings.save(saved)
        plan = _locked_plan(args, saved, current, keys, keep_copies=keep_copies)
        result = apply.apply(plan, current, op="pick")
        result.replayed = replayed
        _remember(plan, current, result, keys)
    removed = [(outcome.name, label) for outcome in result.written if outcome.action == "remove" for label in outcome.labels]
    undo = [f"`agt share undo {name} --in {label}` brings a removed copy back." for name, label in removed]
    rows = [[ui.muted("Bring it back" if not index else ""), ui.cmd(f"agt share undo {name} --in {label}")]
            for index, (name, label) in enumerate(removed)]
    _report(plan, result, saved_at, args.json, title="agt share pick", notes=undo, text_notes=[], rows=rows)
    return 1 if result.failed else 0


def _pick(args: argparse.Namespace) -> int:
    saved = _enabled()
    if saved is None:
        _say_off(args)
        return 0
    plan = _plan(args, state.load(), saved)
    labels = {provider.label for provider in plan.included}
    to_in = _label_list(args.in_labels, labels, "--in")
    to_out = _label_list(args.not_in, labels, "--not-in")
    only = _label_list(args.only_in, labels, "--only") if args.only_in else None
    if not args.item:
        if to_in or to_out or only:
            raise ValueError("name the item: agt share pick <name> --in <cli>")
        return _show_picks(args, plan, None)
    if args.item.startswith(f"{mcp.KIND}/"):
        return _pick_mcp(args, saved, plan, to_in, to_out, only)
    if args.item.startswith(f"{hooks.KIND}/"):
        return _pick_hook(args, saved, plan, to_in, to_out, only)
    if args.item.startswith(f"{plugins.KIND}/"):
        return _pick_plugin(args, saved, plan, to_in, to_out, only)
    key = _existing(plan, args.item)
    name = key.partition("/")[2]
    if not (to_in or to_out or only):
        return _show_picks(args, plan, key)
    both = sorted(set(to_in) & set(to_out))
    if both:
        raise ValueError(f"{', '.join(both)} is in both --in and --not-in")
    if _find(plan, key) is None and not (to_in or only):
        print(f"{name} isn't shared ({_not_shared(plan, key)}), so there's nothing to take out. "
              f"`agt share pick {name} --in <cli>` shares it again.")
        return 0
    return _apply_picks(args, {key: (to_in, to_out)}, keep_copies=args.keep_copies,
                        only={key: only} if only is not None else None)


SHORT_DIFF = 20  # longer differences show as a summary, and `d` shows them


def _first_choice(title: str, count: int) -> ui.Line:
    """The headline above the version questions a screen asks before it shows its plan."""

    return [ui.bold(title), "  ", ui.warn(f"! {count} item{'s need' if count != 1 else ' needs'} your choice first")]


def _or(choices: list[str]) -> str:
    return ", ".join(choices[:-1]) + " or " + choices[-1] if len(choices) > 1 else choices[0]


def _ask_label(question: str, answers: list[str]) -> str | None:
    """One of `answers`, asked until it comes; q, Enter alone or the end of input is None."""

    while True:
        try:
            answer = input(question).strip()
        except EOFError:
            return None
        if answer.lower() in ("", "q"):
            return None
        if answer in answers:
            return answer




def _ask_version(item: planner.Item) -> str | None:
    """In a terminal: show the difference (or its summary when it's long) and ask which copy wins. None means quit."""

    latest = planner.newest(item)
    diff = _diff(item, latest)
    changed = [line for line in diff if line[:1] in "+-" and not line.startswith(("+++", "---"))]
    ui.out([ui.bold(f"{item.kind}/{item.name}"), " differs between its copies"],
           *ui.columns([[str(number), ", ".join(copy.labels), ui.muted(short(copy.path)), ui.ok("newer") if copy is latest else ""]
                        for number, copy in enumerate(item.copies, 1)], gap=3),
           *(status_view.diff_lines(diff) if len(diff) <= SHORT_DIFF else [["    ", ui.muted(f"{len(changed)} lines differ; d shows them.")]]))
    numbers = _or([str(number) for number in range(1, len(item.copies) + 1)])
    while True:
        try:
            answer = input(f"Which version should every copy get? {numbers} · s stop sharing it · "
                           f"{'d see the difference · ' if len(diff) > SHORT_DIFF else ''}q quit: ").strip().lower()
        except EOFError:
            return None
        if answer == "q":
            return None
        if answer == "d":
            ui.out(*status_view.diff_lines(diff))
            continue
        if answer == "s":
            return "skip"
        if answer.isdigit() and 1 <= int(answer) <= len(item.copies):
            return item.copies[int(answer) - 1].labels[0]


def _resolve_needs_choice(items: list[planner.Item], as_json: bool) -> int:
    asks = []
    for item in items:
        latest = planner.newest(item)
        choices = [{"value": copy.labels[0], "labels": copy.labels, "path": str(copy.path), "newer": copy is latest}
                   for copy in item.copies]
        choices.append({"value": "skip", "means": "don't share it; every copy stays as it is"})
        asks.append({"id": f"resolve:{item.name}", "choices": choices,
                     "ask": f"{item.name} differs between {', '.join(c['value'] for c in choices[:-1])}. Which version should every copy get?",
                     "retry": f"agt share resolve {item.name} --prefer <choice>"})
    if as_json:
        _print({"error": "needs_choice", "ask": [ask["ask"] for ask in asks], "choices": asks,
                "retry": asks[0]["retry"] if len(asks) == 1 else "agt share resolve <item> --prefer <choice>"})
    else:
        print("Nothing was changed: agt needs to know which version wins.")
        for ask in asks:
            options = ", ".join(choice["value"] + (" (newer)" if choice.get("newer") else "") for choice in ask["choices"])
            print(f"  {ask['ask']} Choices: {options}. Answer with {ask['retry']}.")
    return 3


def _resolve_instructions(args: argparse.Namespace, saved: settings.Settings) -> int:
    """`resolve instructions --prefer <label>`: that file's shared text goes to every other one."""

    if saved.instructions == "off":
        raise ValueError("instructions don't share; `agt share kinds add instructions` turns them on")
    plan = _plan(args, state.load(), saved)
    shared = plan.instructions
    if not shared.conflict and not args.prefer:
        print("agt share resolve: the instructions have no conflict; they agree.")
        return 0
    if not args.prefer:
        labels = shared.decision.labels
        if args.json or not _interactive():
            _print({"error": "needs_choice", "ask": f"The instructions differ between {', '.join(labels)}. Which version "
                    "should every CLI get?", "choices": labels, "retry": "agt share resolve instructions --prefer <label>"}
                   if args.json else f"The instructions differ between {', '.join(labels)}. Answer with "
                   "`agt share resolve instructions --prefer <cli>`.")
            return 3
        answer = _ask_label(f"Which version should every CLI get? {_or(labels)} · q quit: ", labels)
        if answer is None:
            print("Nothing was changed.")
            return 0
        args.prefer = answer
    if apply.in_worker():
        print(IN_WORKER)
        return 2
    included = _folders(load_config(), None, False, saved)[1]
    with apply.lock([provider.folder for provider in included]):
        current = state.load()
        apply.replay(current)
        plan = _plan(args, current, saved)
        plan.instructions = instructions.build(plan.included, saved.instructions, current, prefer=args.prefer)
        locked = plan.restricted(set())
        locked.instructions = plan.instructions
        result = apply.apply(locked, current, op="resolve")
        _remember(plan, current, result)
    _report(locked, result, None, args.json, title="agt share resolve")
    return 1 if result.failed else 0


def _mcp_server(plan: planner.Plan, key: str) -> Any:
    if plan.mcp is None:
        raise ValueError("MCP servers don't share; `agt share kinds add mcp` turns them on")
    server = next((server for server in plan.mcp.servers if server.key == key), None)
    if server is None:
        raise ValueError(f"no MCP server named {key.partition('/')[2]} in the folders sharing")
    return server


_REMOVE_ALL = object()


def _mcp_run(args: argparse.Namespace, saved: settings.Settings, key: str, title: str, op: str, *,
             resolve: dict[str, str] | None = None, removing: Any = _REMOVE_ALL, save: bool = False) -> int:
    """One MCP server's run, holding the lock: `resolve`, `pick` (`save` first) and `rm` (`removing=None`)."""

    if apply.in_worker():
        print(IN_WORKER)
        return 2
    included = _folders(load_config(), None, False, saved)[1]
    with apply.lock([provider.folder for provider in included]):
        current = state.load()
        replayed = apply.replay(current)
        saved_at = settings.save(saved) if save else None  # saved only now, holding the lock
        plan = _plan(args, current, saved, resolve=resolve)
        locked = plan.restricted(set())
        locked.mcp = dataclasses.replace(plan.mcp, servers=[server for server in plan.mcp.servers if server.key == key])
        if removing is not _REMOVE_ALL:
            locked.mcp.removing = {key.partition("/")[2]: removing}
        result = apply.apply(locked, current, op=op)
        result.replayed = replayed
        _remember(plan, current, result)
    _report(locked, result, saved_at, args.json, title=title)
    return 1 if result.failed else 0


def _resolve_mcp(args: argparse.Namespace, saved: settings.Settings) -> int:
    """`resolve mcp/<name> --prefer <label>`: that CLI's version of the server goes to the others."""

    plan = _plan(args, state.load(), saved)
    server = _mcp_server(plan, args.item)
    if server not in plan.mcp.conflicts:
        print(f"agt share resolve: {server.name} has no conflict; it's the same in every CLI that has it.")
        return 0
    labels = server.decision.labels
    if not args.prefer:
        if args.json or not _interactive():
            ask = f"The MCP server {server.name} differs between {', '.join(labels)}. Which version should every CLI get?"
            _print({"error": "needs_choice", "ask": ask, "choices": [*labels, "skip"],
                    "retry": f"agt share resolve {server.key} --prefer <label>"} if args.json else
                   f"{ask} Answer with `agt share resolve {server.key} --prefer <cli>` (or skip).")
            return 3
        answer = _ask_label(f"Which version should every CLI get? {_or(labels)} · s stop sharing it · q quit: ", [*labels, "s"])
        if answer is None:
            print("Nothing was changed.")
            return 0
        args.prefer = "skip" if answer == "s" else answer
    if args.prefer == "skip":
        saved.off.add(server.key)
        settings.save(saved)
        print(f"agt share: {server.name} isn't shared any more; every CLI keeps its own version.")
        return 0
    if args.prefer not in server.copies:
        raise ValueError(f"--prefer: {args.prefer} has no copy of {server.name}; its copies are in {', '.join(labels)}")
    return _mcp_run(args, saved, server.key, "agt share resolve", "resolve", resolve={server.key: args.prefer})


def _pick_mcp(args: argparse.Namespace, saved: settings.Settings, plan: planner.Plan, to_in: list[str],
              to_out: list[str], only: list[str] | None) -> int:
    """`pick mcp/<name>`: where the server goes. The copies agt made in CLIs you leave out are taken out (with a
    backup), unless `--keep-copies`; a server you added yourself somewhere stays there."""

    server = _mcp_server(plan, args.item)
    key = server.key
    if not (to_in or to_out or only):
        print(f"MCP server {server.name}:")
        for label in sorted(plan.mcp.by_label):
            where = ("has it" if label in server.copies else f"can't: {server.blocked[label]}" if label in server.blocked
                     else "doesn't have it")
            print(f"  {label:<20} {where}")
        print(f"  {server.held}." if server.held else f"  {server.decision.reason or server.decision.action}.")
        return 0
    both = sorted(set(to_in) & set(to_out))
    if both:
        raise ValueError(f"{', '.join(both)} is in both --in and --not-in")
    if only:
        saved.only[key] = sorted(only)
        saved.not_in.pop(key, None)
    for label in to_in:
        saved.not_in[key] = [entry for entry in saved.not_in.get(key, []) if entry != label]
        if key in saved.only and label not in saved.only[key]:
            saved.only[key] = sorted([*saved.only[key], label])
    for label in to_out:
        saved.not_in[key] = sorted({*saved.not_in.get(key, []), label})
        if key in saved.only:
            saved.only[key] = [entry for entry in saved.only[key] if entry != label]
    for chosen in (saved.not_in, saved.only):
        if key in chosen and not chosen[key]:
            del chosen[key]
    if to_in or only:
        saved.off.discard(key)
    allowed = set(saved.only.get(key, plan.mcp.by_label)) - set(saved.not_in.get(key, []))
    left = {label for label in server.copies if label not in allowed}
    removing = set() if args.keep_copies else left & mcp.agt_made(server, state.load())
    if left - removing:
        print(f"{server.name} stays in {', '.join(sorted(left - removing))}: "
              + ("you kept it there." if args.keep_copies else "it's yours there (you added or changed it)."))
    return _mcp_run(args, saved, key, "agt share pick", "pick", removing=removing, save=True)


def _pick_hook(args: argparse.Namespace, saved: settings.Settings, plan: planner.Plan, to_in: list[str],
               to_out: list[str], only: list[str] | None) -> int:
    """`pick hooks/<name>`: approve a hook for CLIs (as it is now), or withdraw that; no flags shows its verdicts."""

    if plan.hooks is None:
        raise ValueError("hooks don't share; `agt share kinds add hooks` lists them for approval")
    name = args.item.partition("/")[2]
    hook = next((hook for hook in plan.hooks.hooks if hook.name == name), None)
    if hook is None:
        raise ValueError(f"no hook named {name} in the folders sharing; `agt share status` lists them")
    approved = dict(saved.hook_approvals.get(name, {}))
    if not (to_in or to_out or only):
        print("\n".join(hooks.describe(hook, plan.hooks, approved)))
        return 0
    for label in sorted(set(to_in) | set(only or [])):
        if label in hook.labels:
            continue  # it's there already
        why = hook.verdicts.get(label)
        if why:
            raise ValueError(f"{name} can't run in {label}: {why}")
        approved[label] = hook.short
    for label in set(to_out) | (set(approved) - set(only) if only else set()):
        approved.pop(label, None)
    if approved:
        saved.hook_approvals[name] = approved
    else:
        saved.hook_approvals.pop(name, None)
    if apply.in_worker():
        print(IN_WORKER)
        return 2
    included = _folders(load_config(), None, False, saved)[1]
    with apply.lock([provider.folder for provider in included]):
        current = state.load()
        saved_at = settings.save(saved)
        plan = _plan(args, current, saved)
        locked = plan.restricted(set())
        locked.hooks = plan.hooks
        result = apply.apply(locked, current, op="pick")
        _remember(plan, current, result)
    notes = [f"{name} runs in {', '.join(sorted(approved))} as it is now; if it or its script changes, its copies do "
             "nothing until you approve it again."] if approved else [f"{name} isn't copied anywhere now."]
    _report(locked, result, saved_at, args.json, title="agt share pick", notes=notes)
    return 1 if result.failed else 0


def _plugin_entry(plan: planner.Plan, key: str) -> Any:
    if plan.plugins is None:
        raise ValueError("plugins don't share; `agt share kinds add plugins` shows what it would install")
    entry = next((entry for entry in plan.plugins.entries if entry.key == key), None)
    if entry is None:
        raise ValueError(f"no plugin {key.partition('/')[2]} in the Claude Code and Codex folders sharing "
                         "(name it as name@marketplace)")
    return entry


def _plugin_run(args: argparse.Namespace, saved: settings.Settings, key: str, title: str, op: str, *,
                removing: Any = _REMOVE_ALL, save: bool = False, unstop: frozenset[str] = frozenset(),
                notes: list[str] | None = None) -> int:
    """One plugin's run, holding the lock: `pick` (`save` first, and `unstop` the CLIs you picked back) and `rm`."""

    if apply.in_worker():
        print(IN_WORKER)
        return 2
    included = _folders(load_config(), None, False, saved)[1]
    with apply.lock([provider.folder for provider in included]):
        current = state.load()
        replayed = apply.replay(current)
        saved_at = settings.save(saved) if save else None  # saved only now, holding the lock
        record = current.items.get(key)
        if record and unstop:  # you uninstalled agt's copy there once; picking it back in installs it again
            record["stopped"] = [label for label in record.get("stopped") or [] if label not in unstop]
        plan = _plan(args, current, saved)
        locked = plan.restricted(set())
        locked.plugins = dataclasses.replace(plan.plugins, entries=[entry for entry in plan.plugins.entries if entry.key == key])
        if removing is not _REMOVE_ALL:
            locked.plugins.removing = {key.partition("/")[2]: removing}
        result = apply.apply(locked, current, op=op)
        result.replayed = replayed
        _remember(plan, current, result)
    _report(locked, result, saved_at, args.json, title=title, notes=notes)
    return 1 if result.failed else 0


def _pick_plugin(args: argparse.Namespace, saved: settings.Settings, plan: planner.Plan, to_in: list[str],
                 to_out: list[str], only: list[str] | None) -> int:
    """`pick plugins/<id>`: where the plugin goes. Picking a CLI where it waits for your answer is your yes, as it is
    now; agt uninstalls its own copies from CLIs you leave out, unless `--keep-copies`."""

    entry = _plugin_entry(plan, args.item)
    key, shared = entry.key, plan.plugins
    if not (to_in or to_out or only):
        ui.out(*plugin_view.pick_lines(entry, shared))
        return 0
    both = sorted(set(to_in) & set(to_out))
    if both:
        raise ValueError(f"{', '.join(both)} is in both --in and --not-in")
    approvals = dict(saved.plugin_approvals.get(entry.id, {}))
    picked = sorted(set(to_in) | set(only or []))
    for label in picked:
        if label in entry.copies:
            continue  # it's there already
        if label not in shared.by_label:
            raise ValueError(f"{label} installs no plugins; only Claude Code and Codex folders do")
        if label in entry.stays:
            raise ValueError(f"{entry.name} can't go to {label}: {entry.stays[label]}")
        if label in entry.asks:
            approvals[label] = plugins.signature(entry.asks[label])
        plugins._forget(entry.id, label)  # a pick tries a failed install again
    for label in to_out:
        approvals.pop(label, None)
    if approvals:
        saved.plugin_approvals[entry.id] = approvals
    else:
        saved.plugin_approvals.pop(entry.id, None)
    if only:
        saved.only[key] = sorted(only)
        saved.not_in.pop(key, None)
    for label in to_in:
        saved.not_in[key] = [other for other in saved.not_in.get(key, []) if other != label]
        if key in saved.only and label not in saved.only[key]:
            saved.only[key] = sorted([*saved.only[key], label])
    for label in to_out:
        saved.not_in[key] = sorted({*saved.not_in.get(key, []), label})
        if key in saved.only:
            saved.only[key] = [other for other in saved.only[key] if other != label]
    for chosen in (saved.not_in, saved.only):
        if key in chosen and not chosen[key]:
            del chosen[key]
    if picked:
        saved.off.discard(key)
    allowed = set(saved.only.get(key, shared.by_label)) - set(saved.not_in.get(key, []))
    left = {label for label in entry.copies if label not in allowed}
    removing = set() if args.keep_copies else left & plugins.agt_made(entry, state.load())
    stays = [f"{entry.name} stays in {', '.join(sorted(left - removing))}: "
             + ("you kept it there." if args.keep_copies else "you installed it there yourself.")] if left - removing else []
    return _plugin_run(args, saved, key, "agt share pick", "pick", removing=removing, save=True, unstop=frozenset(picked),
                       notes=stays)


def _plugins(args: argparse.Namespace) -> int:
    """`plugins`: where each plugin goes, or (with plugins off) what another CLI could install. `plugins apply`:
    install them now, after you say yes."""

    saved = _enabled()
    if saved is None:
        _say_off(args)
        return 0
    included = _folders(load_config(), None, False, saved)[1]
    found = plugins.offers(included, saved.off)
    # The plugins an "x" turned off: named in `apply`, one installs and is offered again (the undo for "x").
    turned_off = [offer for offer in plugins.offers(included) if f"{plugins.KIND}/{offer.plugin.id}" in saved.off] \
        if any(key.startswith(f"{plugins.KIND}/") for key in saved.off) else []
    shown = [{"plugin": offer.plugin.id, "from": offer.source_label, "to": offer.label, "key": offer.key,
              "commands": [" ".join([offer.cli, *step]) for step in offer.steps], "why_not": offer.why_not,
              "asks": offer.asks, "failed_before": offer.failed} for offer in found]
    if getattr(args, "plugins_command", None) != "apply":
        plan = _plan(args, state.load(), saved) if saved.plugins else None
        shared = plan.plugins if plan is not None else None
        if args.json:
            _print({"sharing": saved.plugins, "summary": shared.summary() if shared else None, "offers": shown})
        elif shared is not None:
            ui.out(*plugin_view.page(plan, shared))
        else:
            ui.out(*plugin_view.offers_page(found, turned_off))
        return 0
    by_key = {offer.key: offer for offer in found}
    by_key.update({offer.key: offer for offer in turned_off if offer.key in args.offers})
    unknown = [key for key in args.offers if key not in by_key]
    if unknown:
        raise ValueError(f"{', '.join(unknown)}: not a plugin another CLI could install; `agt share plugins` lists them")
    chosen = [by_key[key] for key in args.offers] if args.offers else \
        [offer for offer in found if not offer.why_not and not offer.failed]
    blocked = [offer for offer in chosen if offer.why_not]
    if blocked:
        raise ValueError("; ".join(f"{offer.key}: {offer.why_not}" for offer in blocked))
    if not chosen:
        ui.out([ui.bold("agt share plugins apply"), "  ", "nothing to install"])
        return 0
    if apply.in_worker():
        print(IN_WORKER)
        return 2
    if args.yes and not args.offers:
        raise ValueError("--yes installs only plugins you name: agt share plugins apply <plugin>:<label> --yes")
    if not args.yes:
        if args.json or not _interactive():
            ask = "Install these plugins? " + ", ".join(f"{offer.plugin.name} in {offer.label}" for offer in chosen)
            if args.json:
                _print({"error": "needs_choice", "ask": ask, "choices": [entry for entry in shown if entry["key"] in
                                                                        {offer.key for offer in chosen}],
                        "retry": "agt share plugins apply " + " ".join(offer.key for offer in chosen) + " --yes"})
            else:
                ui.out(*_apply_choices(chosen))
            return 3
        approved, never = [], []
        for index, offer in enumerate(chosen):
            ui.out(*([""] if index else []), *_apply_offer(offer))
            try:
                answer = input("Install it? y yes · n no · x never offer it · q stop: ").strip().lower()
            except EOFError:
                answer = "q"
            if answer == "q":
                break
            if answer == "y":
                approved.append(offer)
            elif answer == "x":
                never.append(offer)
        if never:
            saved.off |= {f"plugins/{offer.plugin.id}" for offer in never}
            settings.save(saved)
            ui.out("", *(_never_line(offer, saved.plugins) for offer in {offer.plugin.id: offer for offer in never}.values()))
        chosen = approved
    if not chosen:
        ui.out("", [ui.bold("agt share plugins apply"), "  ", "nothing installed"])
        return 0
    by_label = {provider.label: provider for provider in included}
    outcomes = []
    asked = not args.yes  # the questions above it
    with apply.lock([provider.folder for provider in included]):  # never at the same time as a sync's installs
        current = state.load()
        try:
            with ui.Steps([ui.bold("agt share plugins apply"), "  ",
                           ui.muted(f"installing {len(chosen)} plugin{'s' if len(chosen) != 1 else ''}")]) as display:
                for offer in chosen:
                    provider = by_label[offer.label]
                    display.start(f"{plugins.KIND}/{offer.plugin.id}", ui.muted(offer.label))
                    ok, said = plugins.run_steps(offer.plugin, offer.label, provider, offer.steps, display)
                    display.finish(ok)
                    if ok:
                        plugins.record_install(current, offer.plugin.id, offer.label)
                    outcomes.append(apply.Outcome(plugins.KIND, offer.plugin.id, [offer.label], "copy", provider.folder, ok,
                                                  f"installed from {offer.plugin.marketplace}" if ok else said[-1]))
        except KeyboardInterrupt:
            left = " ".join(offer.key for offer in chosen[len(outcomes):])  # the ones done aren't on offer any more
            ui.out(["Stopped. Run ", ui.code(f"agt share plugins apply {left} --yes"), " again to finish."], stream=sys.stderr)
            raise
        finally:
            state.save(current)  # what finished is agt's, as a sync's install would be
    was_off = {offer.key for offer in turned_off}  # before this run: an "x" given just now stays
    back = {f"{plugins.KIND}/{outcome.name}" for outcome in outcomes
            if outcome.ok and f"{outcome.name}:{outcome.labels[0]}" in was_off} & saved.off
    if back:  # installed by name after an "x": offered again from now on
        saved.off -= back
        saved_at = settings.save(saved)
    result = apply.Result("apply", outcomes)
    lines: list[ui.Line] = [[ui.bold("agt share plugins apply"), "  ", *_written_headline(result)],
                            "", *_item_lines(result, again=_apply_again)]
    if result.written:
        lines += ["", plugin_view.next_line(result.written, by_label)]
    if back:
        lines.append(ui.muted(f"Saved your answers in {short(saved_at)}."))
    ui.out(*([""] if asked else []), *lines)
    return 1 if result.failed else 0


def _apply_again(outcome: apply.Outcome, labels: list[str]) -> ui.Line:
    """Under a failed `plugins apply` install: the command that tries every CLI of that row again (nothing else
    retries it while plugins don't share)."""

    keys = " ".join(f"{outcome.name}:{label}" for label in labels)
    return ["      ", ui.muted("Try again"), "   ", ui.cmd(f"agt share plugins apply {keys} --yes")]


def _never_line(offer: plugins.Offer, sharing: bool) -> ui.Line:
    """An "x" answer stops the plugin in every CLI: say so, and how to take it back."""

    if sharing:
        return [f"{offer.plugin.name} isn't offered again in any CLI; ",
                ui.code(f"agt share pick {plugins.KIND}/{offer.plugin.id} --in <cli>"), " brings it back."]
    return [f"{offer.plugin.name} isn't offered again in any CLI; ", ui.code(f"agt share plugins apply {offer.key} --yes"),
            " installs it and offers it again."]


def _apply_offer(offer: plugins.Offer) -> list[ui.Line]:
    """One plugin `plugins apply` asks about: the commands it runs, each on its own line, and what it would miss."""

    runs = [" ".join([offer.cli, *step]) for step in offer.steps]
    lines: list[ui.Line] = [ui.bold(f"{offer.plugin.name} in {offer.label}")]
    lines += [["  ", ui.muted("runs " if index == 0 else "     "), "  ", ui.cmd(command)] for index, command in enumerate(runs)]
    lines += [["  ", ui.muted("note "), "  ", status_view._sentence("; ".join(offer.asks))]] if offer.asks else []
    return lines


def _apply_choices(chosen: list[plugins.Offer]) -> list[ui.Line]:
    """`plugins apply` with no answer to give: the command that installs each, and all of them."""

    rows: list[list[ui.Line]] = []
    for offer in chosen:
        rows.append([f"{offer.plugin.name} in {offer.label}", ui.cmd(f"agt share plugins apply {offer.key} --yes")])
        if offer.asks:
            rows.append(["", ui.muted(status_view._sentence("; ".join(offer.asks)))])
    if len(chosen) > 1:
        rows.append(["All of them", ui.cmd("agt share plugins apply " + " ".join(offer.key for offer in chosen) + " --yes")])
    return [[ui.bold("agt share plugins apply"), "  ", ui.warn("! nothing installed: name the ones to install")], "",
            *ui.columns(rows, gap=3)]


def _adopt(args: argparse.Namespace) -> int:
    """`adopt`: agt's block in these folders' instruction files, then a sync that fills it."""

    saved = _enabled()
    if saved is None:
        _say_off(args)
        return 0
    included = _folders(load_config(), None, False, saved)[1]
    labels = {provider.label: provider for provider in included}
    for label in args.labels:
        _check_label(label, set(labels), "adopt")
    chosen = [labels[label] for label in args.labels] if args.labels else included
    if args.dry_run:
        found = [{"label": provider.label, "file": str(instructions.file_for(provider))} for provider in chosen
                 if instructions.file_for(provider)]
        if args.json:
            _print({"dry_run": True, "adopt": found})
        else:
            order = [provider.label for provider in included]
            ui.out([ui.bold("agt share adopt"), "  ", ui.muted("dry run, nothing written")], "",
                   "It would add a shared section to:",
                   *ui.columns([[entry["label"], ui.muted(short(Path(entry["file"])))]
                                for entry in sorted(found, key=lambda entry: order.index(entry["label"]))], gap=3))
        return 0
    if apply.in_worker():
        print(IN_WORKER)
        return 2
    notes = []
    done: list[tuple[str, Path, bool]] = []  # label, file, whether it had the section already
    with apply.lock([provider.folder for provider in included]):
        for provider in chosen:
            path, what = instructions.adopt(provider, wrap=args.wrap)
            notes.append(f"{provider.label}: {what} ({short(path)})")
            done.append((provider.label, path, what == "already has agt's block"))
    turned_on = saved.instructions == "off"
    if turned_on:
        saved.instructions = "block"
        notes.append("Instructions share now (`instructions = \"block\"`).")

    def shown(plan: planner.Plan, result: apply.Result) -> dict[str, Any]:
        added = sum(not had for _, _, had in done)
        text = plan.instructions is not None and any(copy.text.strip() for copy in plan.instructions.copies)
        order = [provider.label for provider in included]
        rows = [[[ui.muted("·") if had else ui.warn("~"), " ", label], ui.muted(short(path)), ui.muted("already has it") if had else ""]
                for label, path, had in sorted(done, key=lambda entry: order.index(entry[0]))]
        said = ("Each one now holds your shared instructions; an edit to the section in any one file reaches the others."
                if text else "The sections are empty: write your shared instructions in any one of them, and the others get them.")
        return {"headline": [ui.ok(f"✓ added a shared section to {added} instruction file{'s' if added != 1 else ''}")
                             if added else "every file already has the shared section"],
                "body": [*ui.columns(rows, gap=3), "", f"  {said}", *(["  Instructions share now."] if turned_on else [])],
                "text_notes": [], "hide": {"instructions"}}

    return _run_sync(argparse.Namespace(json=args.json, dry_run=False), saved, "agt share adopt", save=True, notes=notes,
                     shown=shown)


def _resolve(args: argparse.Namespace) -> int:
    saved = _enabled()
    if saved is None:
        _say_off(args)
        return 0
    if args.item == "instructions":
        return _resolve_instructions(args, saved)
    if (args.item or "").startswith(f"{mcp.KIND}/"):
        return _resolve_mcp(args, saved)
    if (args.item or "").startswith(f"{plugins.KIND}/"):
        print(f"agt share resolve: a plugin has no versions to pick; it's installed in a CLI or it isn't. "
              f"`agt share pick {args.item}` shows where it goes.")
        return 0
    plan = _plan(args, state.load(), saved)
    if args.all == bool(args.item):
        raise ValueError("name one item to resolve, or --all")
    if args.all:
        targets = [item for item in plan.conflicts if not args.kind or item.kind == args.kind]
        if not targets:
            print("agt share resolve: no conflicts to resolve.")
            return 0
    else:
        key = _existing(plan, args.item)
        item = _find(plan, key)
        if item is None:
            print(f"{key.partition('/')[2]} isn't shared ({_not_shared(plan, key)}), so it has nothing to resolve. "
                  f"`agt share pick {key.partition('/')[2]} --in <cli>` shares it again.")
            return 0
        if item not in plan.conflicts:
            print(f"agt share resolve: {item.name} has no conflict; its copies agree.")
            return 0
        targets = [item]
    choices: dict[str, str] = {}
    skipped: list[str] = []
    for item in targets:
        if args.prefer == "newest":
            choice = planner.newest(item).labels[0]
        elif args.prefer:
            choice = args.prefer
            if choice != "skip" and not any(choice in copy.labels for copy in item.copies):
                if args.all:
                    skipped.append(f"{item.name}: {choice} has no copy, so it's left as it is")
                    continue
                where = ", ".join("/".join(copy.labels) for copy in item.copies)
                raise ValueError(f"{choice} has no copy of {item.name}; its copies are in {where}")
        elif _interactive():
            choice = _ask_version(item)
            if choice is None:
                print("Nothing was changed.")
                return 0
        else:
            return _resolve_needs_choice(targets, args.json)
        choices[item.key] = choice
    if apply.in_worker():
        print(IN_WORKER)
        return 2
    by_key = {item.key: item for item in targets}
    winners = {by_key[key].name: label for key, label in choices.items() if label != "skip"}
    off = [f"{by_key[key].kind}/{by_key[key].name}" for key, label in choices.items() if label == "skip"]
    included = _folders(load_config(), None, False, saved)[1]
    with apply.lock([provider.folder for provider in included]):
        current = state.load()
        replayed = apply.replay(current)
        saved_at = None
        if off:
            saved.off |= set(off)
            saved_at = settings.save(saved)
        plan = _locked_plan(args, saved, current, set(choices), resolve=winners)
        result = apply.apply(plan, current, op="resolve")
        result.replayed = replayed
        _remember(plan, current, result, set(choices))
    notes = skipped + [f"{key.partition('/')[2]}: not shared any more; every copy stays as it is" for key in off]
    _report(plan, result, saved_at, args.json, title="agt share resolve", notes=notes)
    return 1 if result.failed or plan.conflicts else 0


def _rm(args: argparse.Namespace) -> int:
    saved = _enabled()
    if saved is None:
        _say_off(args)
        return 0
    if args.item.startswith(f"{mcp.KIND}/"):
        server = _mcp_server(_plan(args, state.load(), saved), args.item)
        if server.held:
            print(f"{server.name} isn't shared ({server.held}), so agt leaves it alone in every CLI.")
            return 1
        return _mcp_run(args, saved, args.item, "agt share rm", "rm", removing=None)
    if args.item.startswith(f"{plugins.KIND}/"):
        entry = _plugin_entry(_plan(args, state.load(), saved), args.item)
        if entry.held:
            print(f"{entry.name} isn't shared ({entry.held}), so agt leaves it alone in every CLI.")
            return 1
        return _plugin_run(args, saved, entry.key, "agt share rm", "rm", removing=None)
    plan = _plan(args, state.load(), saved)
    key = _existing(plan, args.item)
    name = key.partition("/")[2]
    if _find(plan, key) is None:
        print(f"{name} isn't shared ({_not_shared(plan, key)}), so agt leaves its copies alone. "
              f"Delete them by hand, or share it again first with `agt share pick {name} --in <cli>`.")
        return 1
    if apply.in_worker():
        print(IN_WORKER)
        return 2
    included = _folders(load_config(), None, False, saved)[1]
    with apply.lock([provider.folder for provider in included]):
        current = state.load()
        replayed = apply.replay(current)
        plan = _locked_plan(args, saved, current, {_identity(key)})
        item = plan.items[0] if plan.items else None
        if item is None:
            print(f"agt share rm: {name} is already gone.")
            return 0
        last = current.last(item.kind, names.identity(item.name))
        edited = sorted(label for copy in item.copies for label in copy.labels
                        if copy.version and (last is None or last.copies.get(label) != copy.version))
        item.decision = engine.Decision("remove", reason="agt share rm")
        plan.changes = planner.removal_changes(item)
        record = dict(current.items.get(item.key) or {})
        result = apply.apply(plan, current, op="rm", records={item.key: record} if record else None)
        result.replayed = replayed
        if not result.failed:
            current.items.pop(item.key, None)  # gone everywhere; the manifest keeps the record for undo
            state.save(current)
        _remember(plan, current, result, {item.key})
    notes = [f"{name} in {', '.join(edited)} had an edit the other copies didn't have; it's in the backups too"] if edited and last else []
    if result.written:
        notes.append(f"`agt share undo {name}` puts it back.")
    _report(plan, result, None, args.json, title="agt share rm", notes=notes)
    return 1 if result.failed else 0


def _not_movable(item: planner.Item, record: dict) -> str | None:
    """Why `mv` can't rename the item now, or None: every copy has to match the last sync."""

    decision = item.decision
    if decision is None or decision.action == engine.CONFLICT:
        return f"its copies differ; `agt share resolve {item.name}` first"
    if decision.action == engine.WAIT:
        return decision.reason
    if decision.action != engine.NOTHING or not decision.version or item.drops:
        return "it has changes that haven't reached every copy yet; `agt share sync` spreads them"
    if record.get("released"):
        return f"the copy in {', '.join(record['released'])} is your own file now; rename or remove it by hand first"
    if item.ignored:
        return f"{', '.join(sorted(item.ignored))} is frozen or left out this run"
    return None


def _mv(args: argparse.Namespace) -> int:
    saved = _enabled()
    if saved is None:
        _say_off(args)
        return 0
    new = args.new_name
    problem = names.problem(new)
    if problem:
        raise ValueError(f"{new}: {problem}")
    plan = _plan(args, state.load(), saved)
    key = _existing(plan, args.item)
    kind, _, name = key.partition("/")
    if _find(plan, key) is None:
        print(f"{name} isn't shared ({_not_shared(plan, key)}); rename its files by hand.")
        return 1
    if names.identity(new) == names.identity(name):
        print(f"agt share mv: {name} is already called that.")
        return 0
    taken = [entry for entry in [*(i.name for i in plan.items if i.kind == kind), *(e.name for e in plan.excluded if e.kind == kind)]
             if names.identity(entry) == names.identity(new)]
    if taken:
        raise ValueError(f"there's already an item named {taken[0]} in {kind}")
    if apply.in_worker():
        print(IN_WORKER)
        return 2
    included = _folders(load_config(), None, False, saved)[1]
    with apply.lock([provider.folder for provider in included]):
        current = state.load()
        replayed = apply.replay(current)
        plan = _locked_plan(args, saved, current, {_identity(key)})
        item = plan.items[0] if plan.items else None
        record = current.items.get(_identity(key)) or {}
        why = _not_movable(item, record) if item else "it's gone"
        changes, problems = planner.rename_changes(item, new, plan.included, set(record.get("made") or [])) if item else ([], [])
        if why or problems:
            print(f"agt share mv: nothing was renamed: {why or '; '.join(problems)}.")
            return 1
        if not record.get("shape"):
            # Saved first, so a sync after a cut-off mv sees the rename and finishes it.
            record["shape"] = next(copy.shape for copy in item.copies if copy.shape)
            current.items[item.key] = record
            state.save(current)
        renamed = planner.Item(kind, new, copies=item.copies, decision=engine.Decision("rename", reason="agt share mv"),
                               renamed_from=item.key)
        plan.items, plan.changes = [renamed], changes
        result = apply.apply(plan, current, op="mv")
        result.replayed = replayed
        _remember(plan, current, result, {item.key, renamed.key})
        saved_at = settings.save(saved) if result.renamed and _move_picks(saved, _identity(key), f"{kind}/{new}") else None
    _report(plan, result, saved_at, args.json, title="agt share mv")
    return 1 if result.failed else 0


TURN_OFF = ("cli-remove", "kinds-remove", "disable")


@dataclasses.dataclass
class _Part:
    """Where one turn-off reaches: your user folders, or a listed repo, with what it turns off there."""

    target: scope.Scope
    saved: settings.Settings
    kinds: set[str]
    labels: set[str]
    change: Callable[[settings.Settings, state.State], None]
    left: list[planner.Left] = dataclasses.field(default_factory=list)


STAYS = [("original", "Your originals stay either way:"), ("edited", "Copies you edited stay as your own files:"),
         ("shared", "Copies another CLI reads stay:")]


def _left_rows(entries: list[planner.Left], by_kind: bool, indent: int) -> list[ui.Line]:
    """The copies, one row per item with its folders (or, for one CLI, one row per kind with its items)."""

    if by_kind:
        rows = [[ui.muted(kind), ", ".join(sorted({entry.item.name for entry in entries if entry.item.kind == kind}))]
                for kind in (*planner.KINDS, "instructions") if any(entry.item.kind == kind for entry in entries)]
        return ui.columns(rows, indent=indent, gap=3)
    bare = len({entry.item.kind for entry in entries}) == 1
    found: dict[str, tuple[list[str], str]] = {}
    for entry in entries:
        name = entry.item.name if bare else f"{entry.item.kind}/{entry.item.name}"
        labels, note = found.get(name, ([], ""))
        labels += [label for label in entry.labels if label not in labels]
        if entry.status == "shared":  # "kiro reads claude's folder, so it stays": who depends on it
            note = entry.note.removesuffix(", so it stays")
        found[name] = (labels, note)
    return ui.columns([[name, ui.muted(", ".join(labels)), ui.muted(f"({note})") if note else ""]
                       for name, (labels, note) in found.items()], indent=indent, gap=3)


def _left_lines(parts: list[_Part], user: _Part, heading: str, by_kind: bool) -> list[ui.Line]:
    """What a turn-off leaves: agt's copies under `heading`, then what stays either way, a repo's under its name."""

    copies: list[ui.Line] = []
    stays: dict[str, list[ui.Line]] = {}
    for part in [user, *(part for part in parts if part is not user and part.left)]:
        indent = 2 if part is user else 4
        inside = [] if part is user else [f"  In {part.target.name}:"]
        agt = [entry for entry in part.left if entry.status == "agt"]
        if agt:
            copies += inside + _left_rows(agt, by_kind, indent)
        for status, _ in STAYS:
            found = [entry for entry in part.left if entry.status == status]
            if found:
                stays.setdefault(status, []).extend(inside + _left_rows(found, False, indent))
    lines: list[ui.Line] = [heading, *copies] if copies else [heading]
    for status, title in STAYS:
        if status in stays:
            lines += ["", title, *stays[status]]
    return lines


def _stays_lines(left: list[planner.Left], choice: str) -> list[str]:
    """What stays, for the report: your originals, entries a CLI still sharing reads, and a kept `CLAUDE.md` import.
    (Copies kept as your own files are in the plan, and the report lists them from there.)"""

    return [f"{entry.item.name} in {', '.join(entry.labels)}: "
            + (planner.KEPT if entry.status == "agt" else entry.note)
            for entry in left if entry.status in ("original", "shared")
            or (entry.item.kind == "instructions" and choice == "keep")]


def _import_left(part: _Part, current: state.State) -> list[planner.Left]:
    """agt's `CLAUDE.md` holding `@AGENTS.md` in a repo, when the turn-off takes Claude Code out of it for every kind."""

    root = part.target.root
    if root is None or "claude" not in part.labels or not set(part.saved.kinds) <= part.kinds:
        return []
    path = instructions.turn_off_import(root, current)
    return [planner.Left(planner.Item("instructions", "CLAUDE.md"), ["claude"], path, "agt", "agt made it")] if path else []


def _survey(args: argparse.Namespace, part: _Part) -> None:
    """What the turn-off leaves behind in this part, before anything is written."""

    with scope.use(part.target):
        current = state.load()
        plan = _plan(args, current, part.saved)
        _no_rename_pending(plan)
        part.left = planner.leave(plan, current, part.kinds, part.labels) + _import_left(part, current)


def _repo_parts(saved: settings.Settings,
                repos: Callable[[settings.Settings], tuple[set[str], set[str], Callable]] | None) -> list[_Part]:
    if repos is None or scope.current().project:
        return []
    found = []
    for target in _repo_scopes(saved):
        with scope.use(target):
            inner = settings.load()
        kinds, labels, change = repos(inner)
        if kinds and labels:
            found.append(_Part(target, inner, kinds, labels, change))
    return found


def _turn_off(args: argparse.Namespace, saved: settings.Settings, kinds: set[str], labels: set[str], op: str,
              what: str, retry: str, change, done, repos=None, *, off: str = "", again: tuple[str, str] | None = None) -> int:
    """Turn off some kinds in some CLIs. `change(saved, current)` makes the settings and state change afterwards.

    A person's report says `off` ("skills don't share any more") as its headline and gives `again` (label, command),
    the way back, as a row; --json gets `done(removed)` as a note.

    The copies agt made there, unchanged since the last sync, stay as your own files or go to the backups:
    `--keep-copies` / `--remove-copies`, or the question in a terminal. An agent without either gets
    `needs_choice`. Nothing in the settings changes until every removal worked.

    `repos(inner)` gives, from a listed repo's settings, the kinds and labels the turn-off reaches there and the
    change to that repo's state: a CLI or kind your folders stop sharing leaves the repos too. One answer covers
    every repo, and the repos go first, while their settings still include what's being turned off.
    """

    user = _Part(scope.current(), saved, kinds, labels, change)
    parts = [*_repo_parts(saved, repos), user]
    for part in parts:
        _survey(args, part)
    left = [entry for part in parts for entry in part.left]
    agt = [entry for entry in left if entry.status == "agt"]
    choice = "remove" if getattr(args, "remove_copies", False) else "keep" if getattr(args, "keep_copies", False) or not agt else None
    ask = (f"Turning off {what} leaves {len(agt)} cop{'y' if len(agt) == 1 else 'ies'} agt made there. "
           "Keep them as your own files, or remove them (with a backup)?")
    copies = [{"kind": entry.item.kind, "name": entry.item.name, "labels": entry.labels, "path": str(entry.path),
               "status": entry.status, "note": entry.note, **({"repo": str(part.target.root)} if part.target.project else {})}
              for part in parts for entry in part.left]
    title = retry.split(" --")[0]
    by_kind = op == "cli-remove"
    heading = (f"Turning off {what} leaves the {len(agt)} cop{'y' if len(agt) == 1 else 'ies'} agt made{' there' if by_kind else ''}:"
               if agt else f"agt made no copies there; turning off {what} removes nothing.")
    if args.dry_run:
        if args.json:
            _print({"dry_run": True, "turn_off": what, "copies": copies,
                    "needs_answer": [] if choice else [{"ask": ask, "choices": ["--keep-copies", "--remove-copies"]}]})
        else:
            ui.out([ui.bold(title), "  ", ui.muted("dry run, nothing changed")], "", *_left_lines(parts, user, heading, by_kind))
        return 0
    busy = [(part, why) for part in parts if part is not user and part.left and (why := scope.busy(part.target.root))]
    if busy:
        raise ValueError(f"git is in the middle of a {busy[0][1]} in {busy[0][0].target.name}; finish it there, "
                         "then run this again")
    if choice is None:
        if _interactive():
            ui.out(*_left_lines(parts, user, heading, by_kind), "")
            while choice is None:
                try:
                    answer = input("Keep them as your own files, or remove them with a backup? k keep · r remove · q quit: ")
                    answer = answer.strip().lower()
                except EOFError:
                    answer = "q"
                if answer == "q":
                    print("Nothing was changed.")
                    return 0
                choice = {"k": "keep", "r": "remove"}.get(answer)
        else:
            if args.json:
                _print({"error": "needs_choice", "ask": ask, "choices": [
                    {"value": "--keep-copies", "means": "the copies stay as your own files; agt stops updating them"},
                    {"value": "--remove-copies", "means": "the copies go to the backups"}], "copies": copies,
                    "retry": f"{retry} --keep-copies | --remove-copies"})
            else:
                count = f"{len(agt)} cop{'y' if len(agt) == 1 else 'ies'}"
                ui.out([ui.bold(title), "  ", ui.warn(f"! nothing changed: keep or remove {count}?")], "",
                       *_left_lines(parts, user, heading, by_kind), "",
                       *ui.columns([[ui.muted("Keep them as your own files"), ui.cmd(f"{retry} --keep-copies")],
                                    [ui.muted("Remove them, with a backup"), ui.cmd(f"{retry} --remove-copies")]], gap=3))
            return 3
    if apply.in_worker():
        print(IN_WORKER)
        return 2
    repo_entries: list[dict[str, Any]] = []
    for part in parts:  # the repos first, so a failure there leaves your settings as they were
        off_plan, result, saved_at = _turn_off_writes(args, part, op, what, title, choice)
        if part is not user:
            off_plan.notices += _stays_lines(part.left, choice)
        if part is user:
            break
        repo_entries.append({"scope": part.target, "plan": off_plan, "result": result})
        if result.failed:
            message = (f"Nothing else changed: a removal failed in {part.target.name}, so {what} still shares. "
                       "Run it again to finish.")
            if args.json:
                _print({"failed": True, "notes": [message], "repos": [_repo_json(entry) for entry in repo_entries]})
            else:
                ui.out(*(line for entry in repo_entries for line in [*_repo_lines(entry, title), ""]), message)
            return 1
    notes = ([done(choice == "remove")] if not result.failed else
             [f"Nothing else changed: a removal failed, so {what} still shares. Run it again to finish."])
    stays = _stays_lines(user.left, choice)
    removed = len(result.written)
    sections: list[ui.Line] = []
    for status, heading in STAYS:  # what stays, as the dry run listed it (Notes name the edited copies kept)
        found = [entry for entry in user.left if entry.status == status and status != "edited"]
        sections += ["", heading.replace(" either way", ""), *_left_rows(found, False, 2)] if found else []
    _report(off_plan, result, saved_at, args.json, title=title, notes=notes + stays, repos=repo_entries,
            found=_triggers(saved) if not result.failed else None,
            headline=[ui.ok(f"✓ {off}" + (f", removed {removed} cop{'y' if removed == 1 else 'ies'}" if removed else ""))]
            if off else None, rows=[[ui.muted(again[0]), ui.cmd(again[1])]] if again and not result.failed else None,
            body=sections[1:] if off and not result.failed else None,
            text_notes=notes[1:] if off and not result.failed else None)
    return 1 if result.failed else 0


def _turn_off_writes(args: argparse.Namespace, part: _Part, op: str, what: str, command: str,
                     choice: str) -> tuple[planner.Plan, apply.Result, Path | None]:
    """One part's writes under its own lock: agt's copies removed or kept, then its settings and state change."""

    with scope.use(part.target):
        saved = part.saved
        included = _folders(load_config(), None, False, saved)[1]
        with apply.lock([provider.folder for provider in included]):
            current = state.load()
            replayed = apply.replay(current)
            plan = _plan(args, current, saved)
            _no_rename_pending(plan)
            part.left = planner.leave(plan, current, part.kinds, part.labels)
            off = planner.turn_off_plan(plan, part.left, choice == "remove")
            for entry in _import_left(part, current):
                off.claude_import = instructions.Import(entry.path, choice if choice == "remove" else "release",
                                                        f"sharing {what} stopped")
                part.left.append(entry)
            if op == "disable" and choice == "remove" and plan.mcp is not None:
                off.mcp = mcp.removal_only(plan.mcp, {server.name: mcp.agt_made(server, current) for server in plan.mcp.servers})
            if op == "disable" and choice == "remove" and plan.plugins is not None:
                off.plugins = plugins.removal_only(plan.plugins, {entry.id: plugins.agt_made(entry, current)
                                                                  for entry in plan.plugins.entries})
            copies = [{"item": entry.item.key, "labels": entry.labels, "path": str(entry.path), "status": entry.status}
                      for entry in part.left]
            result = apply.apply(off, current, op=op, command=command,
                                 log={"turned_off": what, "answer": choice, "copies": copies})
            result.replayed = replayed
            saved_at = None
            if not result.failed:
                part.change(saved, current)
                state.save(current)
                # A repo's picks stay as they are: its `.agt/settings.toml` may be committed.
                saved_at = None if part.target.project else settings.save(saved)
                after = _plan(args, current, saved) if saved.enabled else None
                if after is not None:
                    attention.save(after, current)
                else:
                    attention.path().unlink(missing_ok=True)
    return off, result, saved_at


def _drop_label(saved: settings.Settings, current: state.State, label: str) -> None:
    """A CLI that stops sharing: its folder, your picks for it, and its place in every state record go."""

    saved.folders = [folder for folder in saved.folders if folder.label != label]
    for chosen in (saved.not_in, saved.only):
        for key in list(chosen):
            chosen[key] = [entry for entry in chosen[key] if entry != label]
            if not chosen[key]:
                del chosen[key]
                if chosen is saved.only:
                    saved.off.add(key)  # it was only in that CLI: now it's in none, so it isn't shared
    _drop_records(current, label)


def _drop_records(current: state.State, label: str) -> None:
    """A CLI's place in every state record goes (in a repo, its CLI name).

    Copies kept as your own files stay `released`, so if the CLI comes back they aren't mistaken for originals.
    """

    for record in current.items.values():
        record.get("copies", {}).pop(label, None)
        for field_name in ("made", "stopped"):
            if label in (record.get(field_name) or []):
                record[field_name] = [entry for entry in record[field_name] if entry != label]


def _disable(args: argparse.Namespace) -> int:
    saved = settings.load()
    if not saved.enabled:
        if args.json:
            print("agt share is already off.")
        else:
            ui.out([ui.bold("agt share disable"), "  ", ui.muted("already off")], "", *_turn_on_again())
        return 0
    if not args.remove_copies:
        if apply.in_worker():
            print(IN_WORKER)
            return 2
        saved.enabled = False
        at = settings.save(saved)
        attention.path().unlink(missing_ok=True)
        synclog.append({"type": "turn-off", **synclog.where(None), "what": "sharing", "answer": "keep", "by": "command",
                        "command": "agt share disable", "copies": synclog.share_copies()})
        removed = triggers.remove_all() + hooks.remove_all(_folders(load_config(), None, False, saved)[1])
        message = (f"agt share is off. Every copy stays as it is, and your answers stay in {short(at)}: "
                   "`agt share enable` turns it on again.")
        if args.json:
            _print({"enabled": False, "settings": str(at), "message": message, "notes": removed})
            return 0
        ui.out([ui.bold("agt share disable"), "  ", ui.ok("✓ off")], "",
               f"  Every copy stays as it is, and your answers stay in {short(at)}.",
               *(["  ", *ui.inline(line)] for line in _removed_lines(removed)), "", *_turn_on_again())
        return 0
    args.dry_run, args.keep_copies = False, False
    labels = {folder.label for folder in saved.folders}

    def change(saved: settings.Settings, current: state.State) -> None:
        saved.enabled = False
        _put_back_replaced(current)

    def in_repo(inner: settings.Settings):
        return set(inner.kinds), {folder.label for folder in inner.folders}, lambda saved, current: setattr(saved, "enabled", False)

    hooks.remove_all(_folders(load_config(), None, False, saved)[1])
    return _turn_off(args, saved, set(saved.kinds), labels, "disable", "sharing", "agt share disable --remove-copies", change,
                     lambda removed: "agt share is off; `agt share enable` turns it on again and fills the copies again.",
                     repos=in_repo, off="off", again=("Turn it on again", "agt share enable"))


REMOVED = re.compile(r"Removed (the background watcher|agt's entries from (.+)|agt's hook copies from (.+))\.")


def _removed_lines(lines: list[str]) -> list[str]:
    """What turning sharing off took out, as one sentence; anything else (a file agt couldn't change) as it is."""

    watcher, entries, copies, rest = False, [], [], []
    for line in lines:
        match = REMOVED.fullmatch(line)
        if match is None:
            rest.append(ui.tilde(line))
        elif match[2]:
            entries.append(ui.tilde(match[2]))
        elif match[3]:
            copies.append(ui.tilde(match[3]))
        else:
            watcher = True
    parts = (["the background watcher"] if watcher else []) + ([f"agt's entries in {planner._and(entries)}"] if entries else []) \
        + ([f"agt's hook copies in {planner._and(copies)}"] if copies else [])
    return ([f"Removed {planner._and(parts)}."] if parts else []) + rest


def _turn_on_again() -> list[ui.Line]:
    return ui.columns([[ui.muted("Turn it on again"), ui.cmd("agt share enable")]], gap=3)


def _put_back_replaced(current: state.State) -> None:
    """`disable --remove-copies`: a skill folder of yours that `resolve` replaced with a link comes back.

    Only while the link is still the one agt made; the item's record forgets its version, so if sharing
    comes back the two versions are a conflict again, never an edit.
    """

    run = apply.new_run_id()
    done: set[str] = set()
    for manifest in apply.manifests():
        if manifest.get("op") != "resolve":
            continue
        for entry in manifest["changes"]:
            path, backup = Path(entry["path"]), Path(entry["backup"]) if entry.get("backup") else None
            if entry.get("action") != "relink" or backup is None or str(path) in done or not backup.is_dir() \
                    or os.path.islink(backup) or not os.path.islink(path) or not os.path.isabs(os.readlink(path)):
                continue
            done.add(str(path))
            if apply.restore(entry, run).ok:
                record = current.items.get(entry["key"])
                if record:
                    record.pop("version", None)
                    record.pop("copies", None)


def _cli(args: argparse.Namespace) -> int:
    saved = settings.load()
    config = load_config()
    if args.cli_command == "list":
        folders = [(folder.cli, folder.path) for folder in saved.folders]
        found = folders + [entry for entry in providers.discovered_folders(config)
                           if not any(entry[0] == cli and providers.same(entry[1], path) for cli, path in folders)]
        rows = providers.candidates(config, found)
        sharing = {(folder.cli, str(folder.path.resolve())): folder.label for folder in saved.folders} if saved.enabled else {}
        listed = []
        for row in rows:
            label = sharing.get((row.provider.cli, str(row.provider.folder.resolve())))
            status = "sharing" if label else row.reason or (
                "not sharing; `agt share cli add` adds it" if saved.enabled else "not sharing")
            listed.append({"label": label or row.provider.label, "cli": row.provider.cli, "path": str(row.provider.folder),
                           "sharing": bool(label), "status": status})
        if args.json:
            _print({"enabled": saved.enabled, "folders": listed})
        else:
            rows = [[f"{'[x]' if entry['sharing'] else '[ ]'} {entry['label']}", ui.muted(short(Path(entry["path"]))),
                     ui.ok("sharing") if entry["sharing"] else ui.inline(entry["status"])] for entry in listed]
            ui.out(*([] if saved.enabled else [TURNED_OFF if saved.folders else NOT_ENABLED]),
                   *(ui.columns(rows, gap=3) if rows else ["agt found no CLI folders."]))
        return 0
    if not saved.enabled:
        _say_off(args)
        return 0
    if args.cli_command == "remove":
        labels = {folder.label for folder in saved.folders}
        _check_label(args.label, labels, "cli remove")
        if len(labels) == 1:
            raise ValueError(f"{args.label} is the last folder sharing; `agt share disable` turns sharing off")
        folder = next(entry for entry in saved.folders if entry.label == args.label)
        # Repos share between one folder per CLI, so they lose this CLI only with its last folder.
        leaves = not any(entry.cli == folder.cli for entry in saved.folders if entry.label != args.label)

        def in_repo(inner: settings.Settings):
            return (set(inner.kinds), {entry.label for entry in inner.folders if entry.cli == folder.cli},
                    lambda saved, current: _drop_records(current, folder.cli))

        return _turn_off(args, saved, set(saved.kinds), {args.label}, "cli-remove", args.label,
                         f"agt share cli remove {args.label}", lambda saved, current: _drop_label(saved, current, args.label),
                         lambda removed: f"{args.label} doesn't share any more; `agt share cli add {folder.cli} "
                         f"{short(folder.path)}` shares it again" + (" and fills it." if removed else
                                                                     ", and the copies you kept stay your own files."),
                         repos=in_repo if leaves else None, off=f"{args.label} doesn't share any more",
                         again=("Share it again", f"agt share cli add {folder.cli} {short(folder.path)}"))
    cli = "antigravity" if args.cli == "agy" else args.cli
    if args.path:
        folder = Path(args.path).expanduser().absolute()
    elif cli == "claude":
        raise ValueError("say which Claude folder: agt share cli add claude ~/.claude-other")
    else:
        folder = {"codex": default_primary("codex"), "antigravity": default_primary("antigravity").parent,
                  "kiro": Path.home() / ".kiro"}[cli]
    if not folder.is_dir():
        raise ValueError(f"no such folder: {folder}")
    if any(entry.cli == cli and providers.same(entry.path, folder) for entry in saved.folders):
        print(f"agt share: {short(folder)} is already sharing.")
        return 0
    rows = providers.candidates(config, [(entry.cli, entry.path) for entry in saved.folders] + [(cli, folder)])
    row = next(row for row in rows if row.provider.cli == cli and providers.same(row.provider.folder, folder))
    if row.reason:
        raise ValueError(f"{short(folder)} can't share: {row.reason}")
    taken = {entry.label for entry in saved.folders}
    label = row.provider.label if row.provider.label not in taken else f"{cli}@{short(folder)}"
    saved.folders.append(settings.Folder(cli, folder, label))
    # With auto copy off for a kind, your items don't go to the new folder until you pick them.
    current = state.load()
    held = sorted(f"{kind}/{key.partition('/')[2]}" for key in current.items if (kind := key.partition("/")[0]) in saved.kinds
                  and kind not in saved.auto_copy and current.items[key].get("version"))
    for key in held:
        saved.not_in.setdefault(key, [])
        if label not in saved.not_in[key]:
            saved.not_in[key].append(label)
    notes = [f"auto copy is off, so these don't go to {label} until you pick them "
             f"(`agt share pick <item> --in {label}`): {', '.join(name.partition('/')[2] for name in held)}"] if held else []
    notes += _kept_there(current, saved.kinds, {label})
    return _run_sync(args, saved, "agt share cli add", save=True, notes=notes)


def _repo(args: argparse.Namespace) -> int:
    """`repo list|add|remove`: the repos that share their own agents and skills."""

    saved = settings.load()
    if args.repo_command == "list":
        found = scope.scopes(worktrees_join=saved.worktrees == "auto")
        entries = [{"root": str(target.root), "worktree_of": str(target.main) if target.main else None} for target in found]
        if args.json:
            _print({"enabled": saved.enabled, "repos": entries})
        elif found:
            ui.out(*(["  ", short(target.root), *([ui.muted(f"  a worktree of {short(target.main)}")] if target.main else [])]
                     for target in found))
        else:
            ui.out("No repo shares yet.", *ui.columns([[ui.muted("Add one"), ui.cmd("agt share repo add <path>")]], gap=3))
        return 0
    if not saved.enabled:
        _say_off(args)
        return 0
    root = Path(args.path).expanduser().absolute()
    if args.repo_command == "add":
        why = scope.refuse(root, _user_folders(saved))
        if why:
            raise ValueError(why)
        top = Path(os.path.realpath(root))
        if not scope.add(top):
            print(f"agt share: {short(top)} already shares.")
            return 0
        entry = _sync_scope(argparse.Namespace(json=args.json, dry_run=False), scope.Scope(top), "agt share repo add")
        if args.json:
            _print({"added": str(top), **_repo_json(entry)})
        else:
            ui.out(*_repo_lines(entry, "agt share repo add"), "",
                   ui.inline(f"{short(top)} shares its own agents and skills now; `agt share repo remove {short(top)}` stops it."))
        return 1 if entry.get("result") and entry["result"].failed else 0
    real = os.path.realpath(root)
    target = next((entry for entry in scope.scopes(worktrees_join=False) if os.path.realpath(entry.root) == real), None)
    if target is None:
        raise ValueError(f"{short(root)} isn't a listed repo; `agt share repo list` shows them")

    def change(saved: settings.Settings, current: state.State) -> None:
        scope.remove(target.root)
        current.items.clear()  # if it comes back, it starts afresh

    with scope.use(target):
        inner = settings.load()
        labels = {folder.label for folder in inner.folders}
        return _turn_off(args, inner, set(inner.kinds), labels, "repo-remove", f"sharing in {target.name}",
                         f"agt share repo remove {short(target.root)}", change,
                         lambda removed: f"{target.name} doesn't share any more; `agt share repo add {short(target.root)}` "
                                         "shares it again.", off=f"{target.name} doesn't share any more",
                         again=("Share it again", f"agt share repo add {short(target.root)}"))


def _item_scope(args: argparse.Namespace) -> scope.Scope:
    """Where an item command acts: `--repo`, else the listed repo you're in when the item is there, else your user folders."""

    if getattr(args, "repo", None):
        real = os.path.realpath(Path(args.repo).expanduser())
        found = next((entry for entry in scope.scopes() if os.path.realpath(entry.root) == real), None)
        if found is None:
            raise ValueError(f"--repo: {args.repo} isn't a listed repo; `agt share repo list` shows them")
        return found
    name = getattr(args, "item", None)
    if not name or name == "instructions":
        return scope.USER
    here = scope.scope_of(Path.cwd())
    if here is None:
        return scope.USER
    with scope.use(here):
        try:
            _existing(_plan(argparse.Namespace(json=False), state.load(), settings.load()), name)
        except (ValueError, settings.SettingsError):
            return scope.USER
    return here


def _kept_there(current: state.State, kinds: tuple[str, ...], labels: set[str]) -> list[str]:
    """Copies you kept as your own files when this CLI or kind stopped sharing: they stay yours until you pick them."""

    found = sorted((key.partition("/")[2], label) for key, record in current.items.items()
                   if key.partition("/")[0] in kinds for label in record.get("released") or [] if label in labels)
    return [f"{name} in {label} is your own file (you kept it); `agt share pick {name} --in {label}` takes it back"
            for name, label in found]


def _kinds_command(args: argparse.Namespace) -> int:
    saved = settings.load()
    if not saved.enabled:
        _say_off(args)
        return 0
    kind = args.kind
    title = f"agt share kinds {args.kinds_command} {kind}"
    word = {mcp.KIND: "MCP servers"}.get(kind, kind)

    def already(how: str = "") -> int:
        ui.out([ui.bold(title), "  ", ui.muted(("already on" if args.kinds_command == "add" else "already off") + how)])
        return 0

    def turned_on(sentence: str = "", body: Callable[[], list[ui.Line]] | None = None):
        """The report of a kind turned on: its headline, then what it means (`body`, or one sentence)."""

        def shown(plan: planner.Plan, result: apply.Result) -> dict[str, Any]:
            said = body() if body else [f"  {sentence}"] if sentence else []
            return {"headline": [ui.ok("✓ hooks are on" if kind == hooks.KIND else f"✓ {word} share now")], "body": said,
                    "text_notes": [] if sentence or body else None}
        return shown

    if kind == hooks.KIND:
        if args.kinds_command == "add":
            if saved.hooks:
                return already()
            saved.hooks = True

            def found() -> list[ui.Line]:
                built = hooks.build(_folders(load_config(), None, False, saved)[1], saved.hook_approvals)
                running: dict[str, list[str]] = {}
                for copy in built.copies:
                    if not copy.waiting:
                        running.setdefault(copy.hook.name, []).append(copy.label)
                waiting = [hook for hook in built.hooks if any(why is None and not built._approved(hook, label)
                                                               for label, why in hook.verdicts.items())]
                rows = [[ui.muted("Found"), ", ".join(hook.name for hook in built.hooks) if built.hooks
                         else ui.muted("none in the folders sharing")]]
                rows += [[ui.muted("Running"), "; ".join(f"{name} in {planner._and(labels)}" for name, labels in running.items())]] \
                    if running else []
                lines = ui.columns(rows, gap=3)
                if waiting:
                    lines += [f"  {'The others wait' if running else 'None is copied to another CLI'} until you approve "
                              f"{'them' if running else 'it'} there.", "",
                              *ui.columns([[ui.muted("Approve one"), ui.cmd("agt share pick hooks/<name> --in <cli>")]], gap=3)]
                return lines + [["  ", *ui.inline(line)] for line in built.refused + built.problems]

            return _run_sync(args, saved, title, save=True, notes=[
                "agt lists your hooks and what each one can do in your other CLIs; none is copied until you approve it "
                "there: `agt share pick hooks/<name> --in <label>`."], shown=turned_on(body=found))
        was_on, saved.hooks = saved.hooks, False
        at = settings.save(saved)
        removed = hooks.remove_all(_folders(load_config(), None, False, saved)[1])
        if args.json:
            _print({"hooks": "off", "settings": str(at), "removed": removed})
        elif not was_on and not removed:
            already()
        else:
            ui.out([ui.bold(title), "  ", ui.ok("✓ hooks don't share any more")], "",
                   "  agt's copies of them are gone, and your approvals stay for next time.",
                   *(["  ", *ui.inline(line)] for line in _removed_lines(removed)))
        return 0
    if kind == mcp.KIND:
        if args.kinds_command == "add":
            if saved.mcp:
                return already()
            saved.mcp = True
            return _run_sync(args, saved, title, save=True, notes=[
                "MCP servers share between your CLIs' user settings; one with a secret written into it stays where it is."],
                shown=turned_on("They share between your CLIs' user settings; one with a secret written into it stays "
                                "where it is."))
        was_on, saved.mcp = saved.mcp, False
        at = settings.save(saved)
        if args.json:
            _print({"mcp": "off", "settings": str(at)})
        elif not was_on:
            already()
        else:
            ui.out([ui.bold(title), "  ", ui.ok("✓ MCP servers don't share any more")], "", "  Every CLI keeps the servers it has.")
        return 0
    if kind == "instructions":
        if args.kinds_command == "add":
            if saved.instructions != "off":
                return already(f" ({saved.instructions})")
            saved.instructions = "block"
            def adopt(plan: planner.Plan, result: apply.Result) -> dict[str, Any]:
                waits = [entry for entry in attention.grouped(attention.entries(plan, state.load())) if entry["kind"] == "adopt"]
                return {**turned_on("agt keeps a shared section in each CLI's instruction file the same everywhere.")(plan, result),
                        "rows": [[ui.muted("Add the section"), ui.cmd(attention.commands(entry)[0])] for entry in waits],
                        "named": len(waits)}

            return _run_sync(args, saved, title, save=True, notes=[
                "Instructions share agt's block of each CLI's instruction file; `agt share adopt` adds the block to a file."],
                shown=adopt)
        if apply.in_worker():
            print(IN_WORKER)
            return 2
        was_on, saved.instructions = saved.instructions != "off", "off"
        at = settings.save(saved)
        # A repo's CLAUDE.md that only points Claude Code at its AGENTS.md is agt's, and goes now.
        repos = [_sync_scope(argparse.Namespace(json=args.json, dry_run=False), target, "agt share kinds remove")
                 for target in _repo_scopes(saved)]
        if args.json:
            _print({"instructions": "off", "settings": str(at), "repos": [_repo_json(entry) for entry in repos]})
        elif not was_on and not any(entry.get("skipped") or entry["result"].outcomes for entry in repos):
            already()
        else:
            ui.out([ui.bold(title), "  ", ui.ok("✓ instructions don't share any more")], "",
                   "  Every instruction file keeps its text.",
                   *(line for entry in repos if entry.get("skipped") or entry["result"].outcomes
                     for line in ["", *_repo_lines(entry, "agt share kinds remove")]))
        return 1 if any(entry.get("result") and entry["result"].failed for entry in repos) else 0
    if kind in LATER_KINDS:
        raise ValueError(f"agt doesn't share {kind} yet; it shares agents and skills")
    if kind == plugins.KIND:
        return _kinds_plugins(args, saved)
    if kind not in SUPPORTED_KINDS:
        raise ValueError(f"unknown kind {kind}; use agents or skills")
    if args.kinds_command == "add":
        if kind in saved.kinds:
            return already()
        saved.kinds = tuple(entry for entry in SUPPORTED_KINDS if entry in saved.kinds or entry == kind)
        return _run_sync(args, saved, title, save=True,
                         notes=_kept_there(state.load(), (kind,), {folder.label for folder in saved.folders}), shown=turned_on())
    if kind not in saved.kinds:
        return already()
    if len(saved.kinds) == 1:
        raise ValueError(f"{kind} is the last kind sharing; `agt share disable` turns sharing off")

    def change(saved: settings.Settings, current: state.State) -> None:
        saved.kinds = tuple(entry for entry in saved.kinds if entry != kind)

    def in_repo(inner: settings.Settings):
        return {kind}, {folder.label for folder in inner.folders}, change

    labels = {folder.label for folder in saved.folders}
    return _turn_off(args, saved, {kind}, labels, "kinds-remove", kind, f"agt share kinds remove {kind}", change,
                     lambda removed: f"{kind} don't share any more; `agt share kinds add {kind}` shares them again"
                     + (" and fills the copies." if removed else ", and the copies you kept stay your own files."),
                     repos=in_repo, off=f"{kind} don't share any more",
                     again=("Share them again", f"agt share kinds add {kind}"))


def _kinds_plugins(args: argparse.Namespace, saved: settings.Settings) -> int:
    """`kinds add plugins`: what it would install, and only after your yes. `kinds remove plugins`: every CLI keeps
    the plugins it has, unless `--remove-copies` uninstalls the ones agt installed."""

    title = f"agt share kinds {args.kinds_command} plugins"
    if args.kinds_command == "remove":
        if not saved.plugins:
            ui.out([ui.bold(title), "  ", "already off"])
            return 0
        again = [ui.muted("Share them again"), ui.cmd("agt share kinds add plugins")]
        remove = getattr(args, "remove_copies", False)
        if remove and not getattr(args, "dry_run", False):
            if apply.in_worker():
                print(IN_WORKER)
                return 2
            included = _folders(load_config(), None, False, saved)[1]
            with apply.lock([provider.folder for provider in included]):
                current = state.load()
                plan = _plan(args, current, saved)
                off = plan.restricted(set())
                off.plugins = plugins.removal_only(plan.plugins, {entry.id: plugins.agt_made(entry, current)
                                                                  for entry in plan.plugins.entries})
                result = apply.apply(off, current, op="kinds-remove", command=f"{title} --remove-copies")
                if result.failed:
                    _report(off, result, None, args.json, title=title)
                    return 1
                saved.plugins = False
                at = settings.save(saved)
                attention.save(_plan(args, current, saved), current)
            count = len(result.written)
            _report(off, result, at, args.json, title=title, text_notes=[],
                    notes=["plugins don't share any more; agt uninstalled the copies it installed."],
                    headline=[ui.ok("✓ plugins don't share any more" + (f", removed {count} cop{'y' if count == 1 else 'ies'}"
                                                                        if count else ""))], rows=[again])
            return 0
        if getattr(args, "dry_run", False):
            current = state.load()
            shared = _plan(args, current, saved).plugins
            made = {entry.name: sorted(plugins.agt_made(entry, current)) for entry in shared.entries}
            made = {name: labels for name, labels in made.items() if labels}
            count = sum(len(labels) for labels in made.values())
            lines: list[ui.Line] = [[ui.bold(title), "  ", ui.muted("dry run, nothing changed")], ""]
            if not made:
                lines.append("agt installed no plugins, so every CLI keeps the plugins it has.")
            else:
                lines.append(f"Turning off plugins {'removes' if remove else 'leaves'} the {count} install"
                             f"{'s' if count != 1 else ''} agt made:")
                lines += ui.columns([[name, ui.muted(", ".join(labels))] for name, labels in made.items()], gap=3)
                lines += ["", "To apply it:", ["  ", ui.cmd(f"{title} --remove-copies")]] if remove else \
                    ["Every CLI keeps them.", "",
                     *ui.columns([[ui.muted("Uninstall them too"), ui.cmd(f"{title} --remove-copies")]], gap=3)]
            ui.out(*lines)
            return 0
        saved.plugins = False
        at = settings.save(saved)
        if args.json:
            _print({"plugins": "off", "settings": str(at)})
        else:
            ui.out([ui.bold(title), "  ", ui.ok("✓ plugins don't share any more")], "",
                   "  Every CLI keeps the plugins it has; agt installs none from now on.", "", *ui.columns([again], gap=3))
        return 0
    if saved.plugins:
        ui.out([ui.bold(title), "  ", "already on"])
        return 0
    saved.plugins = True
    note = ["Plugins go between your Claude Code and Codex folders, installed with each CLI's own commands. One that "
            "a CLI would load only part of waits for your answer: `agt share pick plugins/<id> --in <label>`."]
    text = ["Each sync now installs plugins between your Claude Code and Codex folders, with each CLI's own commands."]
    if getattr(args, "dry_run", False):
        return _run_sync(args, saved, title, notes=note, preview=lambda plan: _plugins_preview(plan, title))
    shared = _plan(args, state.load(), saved).plugins
    go = [ui.muted("Go ahead"), ui.cmd(f"{title} --yes")]
    if shared.writes and not args.yes:
        installs = ", ".join(f"{entry.name} in {label}" for entry, label in shared.writes)
        if args.json or not _interactive():
            ask = f"Turning plugins on installs {len(shared.writes)} now: {installs}. Go ahead?"
            if args.json:
                _print({"error": "needs_choice", "ask": ask, "summary": shared.summary(), "retry": f"{title} --yes"})
            else:
                ui.out([ui.bold(title), "  ", ui.warn(f"! nothing installed: turning plugins on installs {len(shared.writes)} now")],
                       "", *plugin_view.install_rows(shared), "",
                       *ui.columns([go, [ui.muted("See everything"), ui.cmd(f"{title} --dry-run")]], gap=3))
            return 3
        ui.out(*plugin_view.install_rows(shared))
        try:
            answer = input("\nInstall these, and share plugins from now on? y yes · n no: ")
        except EOFError:
            answer = ""
        if answer.strip().lower() != "y":
            ui.out([ui.bold(title), "  ", "nothing installed; plugins don't share"], "", *ui.columns([go], gap=3))
            return 0
    return _run_sync(args, saved, title, save=True, notes=note, shown=lambda plan, result: {"text_notes": text})


def _plugins_preview(plan: planner.Plan, command: str) -> list[ui.Line]:
    """`kinds add plugins --dry-run`: what it would install and what waits. The agents and skills it would change
    come first, when there are any; the folders and what isn't shared aren't changes, so they stay out."""

    lines: list[ui.Line] = [[ui.bold(command), "  ", ui.muted("dry run, nothing written")]]
    grid = planner.table(plan, [item for item in plan.items if any(cell.state != planner.HAS for cell in item.cells.values())]) \
        if plan.changes else []
    lines += ["", *grid] if grid else []
    lines += ["", *planner.conflict_lines(plan, False)] if plan.conflicts else []
    for block in (plugin_view.install_rows(plan.plugins), plugin_view.wait_lines(plan, plan.plugins),
                  plugin_view.short_lines(plan.plugins)):
        lines += ["", *block] if block else []
    count = len(plan.changes)
    total = (f"{count} agent and skill change{'s' if count != 1 else ''} in {len(plan.folders_changed)} folder"
             f"{'s' if len(plan.folders_changed) != 1 else ''}." if count else "No agent or skill changes.")
    return lines + ["", [ui.bold(total), " ", ui.muted("To apply it:")], ["  ", ui.cmd(command)]]


def _undo_entries(key: str | None, bare: str, label: str | None) -> tuple[dict | None, list[dict], str | None]:
    """The newest run that left a backup of the item (with `label`, of a copy it removed there).

    Returns (that run's manifest, its entries for the item, the item's key).
    """

    wanted = names.identity(bare)
    for manifest in apply.manifests():
        renames = [rename for rename in manifest.get("renames") or []
                   if wanted in (names.identity(rename["from"].partition("/")[2]), names.identity(rename["to"].partition("/")[2]))]
        if renames:
            # Backups from before a rename are under the other name: putting them back would bring that name back.
            old, new = renames[0]["from"].partition("/")[2], renames[0]["to"].partition("/")[2]
            raise ValueError(f"{old} was renamed to {new}; `agt share mv {new} {old}` renames it back")
        entries = []
        for change in manifest["changes"]:
            kind, _, name = str(change.get("key", "")).partition("/")
            if names.identity(name) != wanted or (key and kind != key.partition("/")[0]) or not change.get("backup"):
                continue
            if label and (change.get("action") != "remove" or label not in change.get("labels", [])):
                continue
            entries.append(change)
        if entries:
            return manifest, entries, f"{entries[0]['key'].partition('/')[0]}/{entries[0].get('name') or bare}"
    return None, [], key


def _left_out_in(saved: settings.Settings, key: str, labels: set[str]) -> set[str]:
    """The CLIs your picks leave this item out of."""

    found = set(saved.not_in.get(key, []))
    if key in saved.only:
        found |= labels - set(saved.only[key])
    return found


def _undo(args: argparse.Namespace) -> int:
    saved = _enabled()
    if saved is None:
        _say_off(args)
        return 0
    plan = _plan(args, state.load(), saved)
    labels = {provider.label for provider in plan.included}
    label = args.in_label
    if label:
        _check_label(label, labels, "--in")
    try:
        key = _item_key(plan, args.item)
    except ValueError:
        key = None  # gone from every folder after `rm`: the backups still know it
    bare = args.item.partition("/")[2] if "/" in args.item else args.item
    manifest, entries, key = _undo_entries(key, bare, label)
    if key is None:
        raise ValueError(f"agt has no agent or skill named {args.item} and no backup of one")
    name = key.partition("/")[2]
    if manifest and manifest.get("op") in TURN_OFF:
        what, back = {"cli-remove": ("turning off a CLI", "`agt share cli add <cli>`"),
                      "kinds-remove": (f"turning off {key.partition('/')[0]}", f"`agt share kinds add {key.partition('/')[0]}`"),
                      "disable": ("turning sharing off", "`agt share enable`")}[manifest["op"]]
        print(f"agt share undo: {name}'s last backup is from {what}, which undo doesn't reverse; "
              f"{back} fills the copies again.")
        return 0
    gone_labels = sorted({entry_label for entry in entries for entry_label in entry["labels"]} - labels)
    if gone_labels:
        print(f"agt share undo: {', '.join(gone_labels)} isn't sharing now; `agt share cli add` it first.")
        return 0
    if label and manifest and manifest.get("op") == "rm":
        # One copy back after `rm` would leave the others looking deleted; rm's undo is all or nothing.
        print(f"agt share undo: `agt share rm` removed {name} from every CLI, so it comes back everywhere or not at all: "
              f"`agt share undo {name}`.")
        return 0
    record = state.load().items.get(_identity(key)) or {}
    held = bool(label) and label in (set(record.get("stopped") or []) | set(record.get("released") or [])
                                     | _left_out_in(saved, key, labels))
    if manifest is None and not held:
        if label:
            print(f"agt share undo: nothing to bring back; {name} wasn't removed from {label}. "
                  f"`agt share pick {name} --in {label}` shares it there.")
        else:
            print(f"agt share undo: nothing to put back; agt has no backup from a change to {name} "
                  f"(a run that only made new copies leaves none). `agt share pick {name} --not-in <cli>` removes a copy.")
        return 0
    if apply.in_worker():
        print(IN_WORKER)
        return 2
    included = _folders(load_config(), None, False, saved)[1]
    with apply.lock([provider.folder for provider in included]):
        current = state.load()
        replayed = apply.replay(current)
        run = apply.new_run_id()
        restored = [apply.restore(entry, run) for entry in entries]
        came_back = [(outcome, entry) for outcome, entry in zip(restored, entries) if outcome.ok and entry["action"] == "remove"]
        back = {entry_label for _, entry in came_back for entry_label in entry["labels"]} | ({label} if label else set())
        dropped = (manifest or {}).get("records", {}).get(_identity(key)) if (manifest or {}).get("op") == "rm" else None
        if dropped and _identity(key) not in current.items:
            current.items[_identity(key)] = dict(dropped)  # undoing rm: the item is back as it was
        record = current.items.get(_identity(key))
        if record is not None:
            if manifest and manifest.get("op") == "resolve":
                # Undoing a resolve brings the conflict back: the copies differ the way they did.
                record.pop("version", None)
                record.pop("copies", None)
            for field_name in ("stopped", "released"):
                left = [entry_label for entry_label in record.get(field_name) or [] if entry_label not in back]
                if left:
                    record[field_name] = left
                else:
                    record.pop(field_name, None)
            if manifest and manifest.get("op") == "pick":
                # A copy a pick removed comes back as agt's copy, at the version it had, so later edits reach it.
                for outcome, entry in came_back:
                    record["made"] = sorted(set(record.get("made") or []) | set(entry["labels"]))
                    version = _version_at(key.partition("/")[0], included, entry["labels"][0], outcome.path)
                    if version:
                        record.setdefault("copies", {}).update({entry_label: version for entry_label in entry["labels"]})
        saved_at = None
        if back & _left_out_in(saved, key, labels):
            not_in = [entry_label for entry_label in saved.not_in.get(key, []) if entry_label not in back]
            saved.not_in.pop(key, None)
            if not_in:
                saved.not_in[key] = not_in
            if key in saved.only:
                saved.only[key] = sorted(set(saved.only[key]) | back)
            saved_at = settings.save(saved)
        own = frozenset(os.path.realpath(outcome.path) for outcome in restored if outcome.ok)
        plan = _plan(args, current, saved, own=own).restricted({_identity(key)})
        _no_rename_pending(plan)
        result = apply.apply(plan, current, op="undo", run=run, prior=[outcome for outcome in restored if outcome.ok])
        result.outcomes += [outcome for outcome in restored if not outcome.ok]
        result.replayed = replayed
        # Copies agt made that came back go in their repo's exclude file again; your own files never do.
        made = set((current.items.get(_identity(key)) or {}).get("made") or [])
        result.excluded += gitexclude.update([outcome.path for outcome, entry in came_back if set(entry["labels"]) & made])
        _remember(plan, current, result, {_identity(key)})
    _report(plan, result, saved_at, args.json, title="agt share undo",
            notes=[f"`agt share undo {name}` again puts back what this undo replaced."]
            if any(outcome.backup for outcome in result.written) else [])
    return 1 if result.failed else 0


def _version_at(kind: str, included: list[providers.Provider], label: str, path: Path) -> str | None:
    cli = next((provider.cli for provider in included if provider.label == label), None)
    try:
        if kind == "agents":
            return model.read_agent(cli, path).digest if cli else None
        return model.read_skill(path).digest
    except model.ItemError:
        return None


def _background(args: argparse.Namespace) -> int:
    """`watch`, `session`, `saved` and `badge`: what runs sharing without you. They never fail what started them."""

    from agents_router.share import watch

    action = args.share_command
    if action == "watch":
        return watch.run_once()
    if action == "badge":
        text = watch.badge()
        if text:
            print(text)
        return 0
    try:
        stdin = sys.stdin.read() if not sys.stdin.isatty() else ""
    except (OSError, ValueError):
        stdin = ""
    try:
        if action == "session":
            line = watch.session(args.label, stdin, args.event)
            if line:
                print(line)
        else:
            watch.saved_hook(stdin)
    except Exception as exc:  # a hook entry never fails the session that runs it
        print(f"agt share {action}: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 0


def command(args: argparse.Namespace) -> int:
    action = args.share_command
    if action in ("watch", "session", "saved", "badge"):
        return _background(args)
    if action == "hook-run":
        from agents_router.share import hook_run

        return hook_run.main(list(args.hook_args))
    global _verbose
    _verbose = bool(getattr(args, "verbose", False))
    try:
        return _command(args, action)
    finally:
        _verbose = False


def _command(args: argparse.Namespace, action: str | None) -> int:
    try:
        if action is None:
            return _home(args)
        if action == "enable":
            return _enable(args)
        if action == "sync":
            return _sync(args)
        if action == "status":
            return _status(args)
        if action == "settings":
            from agents_router import config_cli

            return config_cli.share_command(args)
        if action in ("pick", "resolve", "rm", "mv", "undo"):
            with scope.use(_item_scope(args)):
                return {"pick": _pick, "resolve": _resolve, "rm": _rm, "mv": _mv, "undo": _undo}[action](args)
        if action == "plugins":
            return _plugins(args)
        if action in ("disable", "cli", "kinds", "adopt", "repo"):
            return {"disable": _disable, "cli": _cli, "kinds": _kinds_command, "adopt": _adopt, "repo": _repo}[action](args)
    except (settings.SettingsError, state.StateError, apply.Busy) as exc:
        print(f"agt share: {exc}", file=sys.stderr)
        return 1
    if action == "scan":
        plan = _plan(argparse.Namespace(**{**vars(args), "skip": [], "only": [], "resolve": [],
                                           "tool_widening": None, "auto_copy": None}), state.load(), None)
        proposal = planner.proposal(plan, "")
        if args.json:
            _print({"kinds": [{"kind": kind, "suggested": kind in SUPPORTED_KINDS, "available": True} for kind in SUPPORTED_KINDS]
                    + [{"kind": kind, "suggested": False, "available": False} for kind in LATER_KINDS],
                    **{key: proposal[key] for key in ("folders", "items", "needs_answer", "excluded")}})
        else:
            ui.out(*planner.render(plan, "agt share enable --dry-run", header=[ui.bold("agt share scan"), "  ", ui.muted(
                "what setup would offer; nothing written")]))
        return 0
    raise ValueError(f"unknown share command {action}")
