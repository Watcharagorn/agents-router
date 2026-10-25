"""`agt config show|set|unset`: see and change agt's settings, one key at a time.

`show` lists every setting in effect in this workspace and where each value
comes from: the default, an environment variable, or a file (user, workspace,
local). `set` and `unset` change one line of one file, keep your comments,
save a backup in `<AGT_HOME>/state/backups/settings/`, and log the change.

Where a change goes: `.agt/settings.toml` in a workspace, your user file in
your home folder, `--local`, `--workspace` or `--user` to pick. Account picks
go to `.agt/settings.local.toml`, because they're yours; `primary`, `binary`
and the `share.*` keys go to your user file, because they're machine-wide (a
repo's own share settings come with sharing a repo's items). `unset` removes
the key from the file whose value is in effect, unless you name a level.

Some keys raise a limit or loosen safety. Typed by you in a terminal, a change
applies at once: the command is your words. From an agent (agt sees
CLAUDECODE, CODEX_THREAD_ID and the like, terminal or not), it needs
`--user-said "<the user's words>"`, which goes to the log; without it, it's
refused and exits 3. Lowering a limit never needs words.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import difflib
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import time
from typing import Any

from agents_router import config, fsutil, hosts, levels, plans, synclog
from agents_router.share import codex_toml, gitexclude, toolmap
from agents_router.share import settings as share_settings

JOB = "the next job start (running jobs keep theirs)"
SYNC = "the next sync (agt share sync)"
AT_ONCE = "once (agt updates the watcher and its hook entries now)"
TRIGGER_KEYS = ("share.watcher", "share.push_on_save", "share.codex_session")
NEEDS_WORDS = 3


@dataclass(frozen=True)
class Key:
    name: str
    kind: str  # int, float, bool, choice, text, names, auto_copy, or fixed (shown, changed elsewhere)
    description: str
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    cap: bool = False  # unset means no cap
    guard: str = ""  # "raise": higher (or no cap) is looser; "any": any change; "loosen": a value in `loose`
    loose: tuple[Any, ...] = ()
    user_only: bool = False
    personal: bool = False
    changed_by: str = ""  # for a fixed key: the command that changes it
    applies: str = JOB

    @property
    def table(self) -> str | None:
        return self.name.rpartition(".")[0] or None

    @property
    def field(self) -> str:
        return self.name.rpartition(".")[2]


def _runtime_keys(runtime: str) -> list[Key]:
    r = f"{runtime}."
    keys = [
        Key(r + "primary", "text", "The CLI folder of your main account.", user_only=True),
        Key(r + "binary", "text", "The CLI to run, when it isn't found on PATH.", user_only=True),
        Key(r + "accounts", "names", "The only accounts this workspace's jobs use, by name (yours, not the team's).",
            personal=True),
        Key(r + "include_primary", "bool", "Unpinned jobs may also use your main account."),
        Key(r + "failover", "choice", "Move a job to another account when one hits a usage limit.", config.FAILOVER_MODES),
        Key(r + "max_jobs", "int", "Jobs at a time on this CLI.", minimum=1, cap=True, guard="raise"),
        Key(r + "max_jobs_per_account", "int", "Jobs at a time on one account.", minimum=1, cap=True, guard="raise"),
        Key(r + "usage_ceiling", "float", "Accounts at or above this share of a usage window get no new jobs.",
            guard="raise"),
        Key(r + "when_full", "choice", "When every account is full: refuse the job, or queue it.", config.WHEN_FULL_MODES),
        Key(r + "plan", "text", "Your main account's plan, which sets its caps.", guard="any"),
        Key(r + "primary_max_jobs", "int", "Jobs at a time on your main account.", minimum=1, cap=True, guard="raise"),
        Key(r + "primary_max_effort", "choice", "The highest effort a job on your main account may use.", plans.EFFORTS,
            cap=True, guard="raise"),
        Key(r + "primary_models", "names", "The only models a job on your main account may use.", guard="any"),
        Key(r + "max_jobs_by_room", "fixed", "Jobs at a time by how much of the usage window is left.", guard="any",
            changed_by=f"by hand, under [{runtime}] in a settings file"),
        Key(r + "edit_permission_mode", "text", "The permission mode of edit jobs.", guard="any"),
        Key(r + "default_model", "text", "The model a job uses when it names none."),
        Key(r + "connectors", "choice", "Give workers the account's own claude.ai connectors, or none.", ("account", "off")),
        Key(r + "cooldown_minutes", "int", "Minutes an account rests after a usage limit.", minimum=1),
        Key(r + "capacity_retries", "int", "Retries when the service is out of capacity.", minimum=0),
        Key(r + "capacity_retry_seconds", "int", "The first wait before a capacity retry; it doubles each time.", minimum=1),
    ]
    if runtime in ("codex", "antigravity"):
        keys.append(Key(r + "share_parent_mcp", "bool", "Give workers started from Claude Code its MCP servers."))
    if runtime == "codex":
        keys += [
            Key(r + "network_access", "bool", "Network inside the sandbox for edit jobs.", guard="loosen", loose=(True,)),
            Key(r + "web_search", "choice", "Codex's web search for jobs (unset: Codex's default).", config.WEB_SEARCH_MODES),
            Key(r + "windows_sandbox", "choice", "The sandbox on Windows.", config.WINDOWS_SANDBOX_MODES, guard="loosen",
                loose=("off",)),
            Key(r + "reasoning_summary", "choice", "The reasoning summary jobs ask for.", config.REASONING_SUMMARY_MODES),
        ]
    if runtime == "antigravity":
        keys += [
            Key(r + "reviewer", "choice", "Who reviews read and edit jobs' tool calls.", config.REVIEWERS, guard="loosen",
                loose=("off",)),
            Key(r + "reviewer_model", "text", "The reviewer's model."),
            Key(r + "reviewer_account", "text", "The Claude account of reviewer = \"claude\"."),
            Key(r + "sandbox", "bool", "Run edit jobs with agy --sandbox.", guard="loosen", loose=(False,)),
        ]
    return keys


KEYS: dict[str, Key] = {key.name: key for key in [
    Key("max_jobs", "int", "Jobs at a time across every CLI.", minimum=1, cap=True, guard="raise"),
    Key("max_depth", "int", "How deep workers may start workers of their own.", minimum=0, guard="raise"),
    Key("job_retention_days", "int", "Days agt keeps finished jobs' records.", minimum=1),
    *(key for runtime in config.RUNTIMES for key in _runtime_keys(runtime)),
    Key("share.enabled", "fixed", "Whether agt shares agents and skills between your CLIs.",
        changed_by="agt share enable / agt share disable", applies=SYNC),
    Key("share.kinds", "fixed", "The kinds agt shares.", changed_by="agt share kinds add|remove <kind>", applies=SYNC),
    Key("share.auto_copy", "auto_copy", "New items go to every CLI on their own (on), wait for you (off), or a list of kinds.",
        user_only=True, applies=SYNC),
    Key("share.tool_widening", "choice", "What a copy's tools become where they can't be kept exactly.",
        toolmap.TOOL_WIDENING, user_only=True, applies=SYNC),
    Key("share.instructions", "choice", "Share each CLI's instruction file: off, block (agt's marked block) or whole.",
        share_settings.INSTRUCTIONS, user_only=True, applies=SYNC),
    Key("share.worktrees", "choice", "A listed repo's worktrees share by themselves (auto), or by `agt share repo add .`.",
        share_settings.WORKTREES, user_only=True, applies=SYNC),
    Key("share.mcp", "bool", "Share your user MCP servers between your CLIs (one with a secret written into it stays put).",
        user_only=True, applies=SYNC),
    Key("share.hooks", "bool", "List your hooks for sharing; each copy runs only after you approve it for that CLI.",
        user_only=True, applies=SYNC),
    Key("share.watcher", "bool", "The background watcher (macOS): a sync every minute and when a shared folder changes.",
        user_only=True, applies=AT_ONCE),
    Key("share.push_on_save", "bool", "A save in Claude Code inside a shared folder spreads within seconds.",
        user_only=True, applies=AT_ONCE),
    Key("share.notify", "bool", "macOS notifications for conflicts and other urgent items, at most once an hour.",
        user_only=True, applies="the watcher's next run"),
    Key("share.codex_session", "bool", "agt's session-start entry in Codex's hooks.json (Codex asks you to trust it in /hooks).",
        user_only=True, applies=AT_ONCE),
]}


# Values -----------------------------------------------------------------------


def _auto_copy(kinds: tuple[str, ...]) -> Any:
    return True if kinds == share_settings.KINDS else False if not kinds else list(kinds)


def _value(key: Key, loaded: config.Config, share: share_settings.Settings) -> Any:
    if key.table == "share":
        return _auto_copy(share.auto_copy) if key.field == "auto_copy" else getattr(share, key.field)
    if key.table is None:
        return getattr(loaded, key.field)
    runtime = loaded.runtime(key.table)
    value = runtime.picked if key.field == "accounts" else getattr(runtime, key.field)
    return str(value) if isinstance(value, Path) else list(value) if isinstance(value, tuple) else value


def _text(key: Key, value: Any) -> str:
    if value is None:
        return "no cap" if key.cap else "unset"
    if key.kind == "auto_copy":
        return "on" if value is True else "off" if value is False else ", ".join(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value) or "none"
    return f"{value:g}" if isinstance(value, float) else str(value)


def _parse(key: Key, text: str) -> Any:
    """The command line's text as the value the file gets."""

    if "\n" in text:
        raise ValueError(f"{key.name} takes one line")
    word = text.strip()
    if key.kind == "int" or key.kind == "float":
        try:
            value = int(word) if key.kind == "int" else float(word)
        except ValueError:
            raise ValueError(f"{key.name} takes a number, not {text!r}") from None
        if key.minimum is not None and value < key.minimum:
            extra = "; `agt config unset` it for no cap" if key.cap else ""
            raise ValueError(f"{key.name} must be at least {key.minimum:g}{extra}")
        return value
    if key.kind == "bool":
        if word.lower() in ("true", "on", "yes"):
            return True
        if word.lower() in ("false", "off", "no"):
            return False
        raise ValueError(f"{key.name} takes true or false, not {text!r}")
    if key.kind == "choice":
        if word not in key.choices:
            raise ValueError(f"{key.name} takes {', '.join(key.choices)}, not {text!r}")
        return word
    if key.kind == "names":
        names = [item.strip() for item in word.split(",") if item.strip()]
        if not names:
            raise ValueError(f"{key.name} takes one name or more, separated by commas")
        return names
    if key.kind == "auto_copy":
        return _auto_copy(share_settings.parse_auto_copy(word))
    if not word:
        raise ValueError(f"{key.name} can't be empty; `agt config unset {key.name}` removes it")
    return word


def _looser(key: Key, old: Any, new: Any) -> bool:
    if old == new or not key.guard:
        return False
    if key.guard == "any":
        return True
    if key.guard == "loosen":
        return new in key.loose
    if new is None:
        return True  # no cap is looser than any cap
    if old is None:
        return False
    if key.kind == "choice":
        return key.choices.index(new) > key.choices.index(old)
    return new > old


# Files ------------------------------------------------------------------------


def _files(workspace: levels.Workspace | None) -> dict[str, Path]:
    found = {"user": config.user_settings_path()}
    if workspace is not None:
        found.update(workspace=workspace.settings, local=workspace.local)
    return found


def _source(key: Key, layers: list[levels.Layer]) -> tuple[str, levels.Layer | None]:
    """Where the value in effect comes from: a level and its file, or the default (or an environment variable)."""

    found: tuple[str, levels.Layer | None] = ("default", None)
    for layer in layers:
        if (layer.level == "user") == key.personal and (key.user_only or key.personal):
            continue  # machine-wide keys count only in your user file, account picks only in a workspace's
        table = layer.data.get(key.table, {}) if key.table else layer.data
        if isinstance(table, dict) and key.field in table:
            if key.personal and not all(isinstance(item, str) for item in table[key.field]):
                continue
            found = (layer.level, layer)
    if found[1] is None and key.name in ("claude.primary", "codex.primary"):
        variable = "CLAUDE_CONFIG_DIR" if key.name == "claude.primary" else "CODEX_HOME"
        if os.environ.get(variable):
            return f"environment ({variable})", None
    return found


def _effective(layers: list[levels.Layer]) -> tuple[config.Config, share_settings.Settings]:
    user = next((layer for layer in layers if layer.level == "user"), None)
    share = share_settings.parse(user.data.get("share", {}), user.path) if user else share_settings.Settings()
    return config.from_layers(layers), share


def _backup(location: Path, level: str) -> Path | None:
    if not location.exists():
        return None
    folder = config.state_dir() / "backups" / "settings"
    fsutil.make_private_dir(folder)
    target = folder / f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}-{level}.toml"
    shutil.copy2(location, target)
    return target


def _exclude_local(location: Path) -> str | None:
    """Keep a new `.agt/settings.local.toml` out of git: one line in the repo's info/exclude."""

    found = gitexclude.repo_of(location)
    if not found:
        return None
    top, exclude = found
    line = gitexclude.entry(top, location)
    try:
        text = exclude.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    if not line or line in text.splitlines():
        return None
    exclude.parent.mkdir(parents=True, exist_ok=True)
    body = text + ("" if not text or text.endswith("\n") else "\n") + "# agt: your own settings for this workspace\n" + line + "\n"
    fsutil.write_preserving(exclude, body.encode("utf-8"), new=not exclude.exists())
    return str(exclude)


# Commands ---------------------------------------------------------------------


def _key(name: str) -> Key:
    key = KEYS.get(name)
    if key is None:
        close = [known for known in KEYS if known.split(".")[-1] == name] or difflib.get_close_matches(name, KEYS, 3, 0.8)
        hint = f"; did you mean {' or '.join(close)}?" if close else "; `agt config show` lists them"
        raise ValueError(f"agt has no setting {name}{hint}")
    return key


def _agent() -> bool:
    return hosts.detect_host() != "unknown"


def _who(words: str | None) -> dict[str, Any]:
    if _agent():
        return {"by": "agent", "host": hosts.detect_host(), "user_said": words}
    try:
        typed = sys.stdin.isatty()
    except (AttributeError, ValueError):
        typed = False
    return {"by": "terminal" if typed else "command", "user_said": words}


def _level(args: argparse.Namespace, key: Key, workspace: levels.Workspace | None,
           layers: list[levels.Layer], unset: bool) -> str:
    named = [level for level in levels.LEVELS if getattr(args, level, False)]
    if len(named) > 1:
        raise ValueError("name one of --user, --workspace and --local")
    machine = key.user_only
    if named:
        level = named[0]
        if level != "user" and machine:
            what = "agt's share settings apply to your user-level folders" if key.table == "share" else "it's machine-wide"
            raise ValueError(f"{key.name} goes in your user settings only ({config.user_settings_path()}): {what}")
        if level != "user" and workspace is None:
            raise ValueError(f"there's no workspace here ({Path.cwd()} is your home folder); use --user")
    elif unset:
        level = _source(key, layers)[0]
        if level not in levels.LEVELS:
            level = "user" if machine or workspace is None else "workspace"
    elif machine or workspace is None:
        level = "user"
    else:
        level = "local" if key.personal else "workspace"
    if key.personal and level == "user":
        raise ValueError(f"{key.name} picks accounts for one workspace; run it in one, or turn an account off "
                         f"everywhere with `agt accounts disable <name>`")
    return level


def _limits(key: Key, loaded: config.Config, new: Any, layers: list[levels.Layer]) -> list[str]:
    """The caps that still hold a raised cap down."""

    if key.field not in ("max_jobs", "max_jobs_per_account", "primary_max_jobs") or key.table is None:
        return []
    runtime = loaded.runtime(key.table)
    above = [(f"{key.table}.max_jobs", runtime.max_jobs)] if key.field != "max_jobs" else []
    above.append(("max_jobs", loaded.max_jobs))
    found = []
    for name, value in above:
        if value is not None and (new is None or value < new):
            level, layer = _source(KEYS[name], layers)
            found.append(f"{name} = {value} ({level}{', ' + str(layer.path) if layer else ''})")
    return found


def _change(args: argparse.Namespace, unset: bool) -> int:
    key = _key(args.key)
    if key.kind == "fixed":
        raise ValueError(f"{key.name} changes with: {key.changed_by}")
    workspace = levels.find_workspace()
    user = config.user_settings_path()
    layers = levels.layers(user, workspace)
    level = _level(args, key, workspace, layers, unset)
    location = _files(workspace)[level]
    try:
        old_text = location.read_text(encoding="utf-8")
    except FileNotFoundError:
        old_text = ""
    level_old = levels.read_value(old_text, key.table, key.field)
    if unset:
        if level_old is levels.MISSING:
            print(f"{key.name} isn't set in {location} ({level}); nothing to unset.")
            return 0
        new_text = levels.drop_line(old_text, key.table, key.field, location)
    else:
        new_text = levels.set_line(old_text, key.table, key.field, codex_toml.render_value(_parse(key, args.value)), location)
    after = levels.layers(user, workspace, override={location: new_text})
    upto = levels.LEVELS.index(level)
    before_cfg, before_share = _effective(layers)
    after_cfg, after_share = _effective(after)
    view_before = _effective([layer for layer in layers if levels.LEVELS.index(layer.level) <= upto])
    view_after = _effective([layer for layer in after if levels.LEVELS.index(layer.level) <= upto])
    old, new = _value(key, before_cfg, before_share), _value(key, after_cfg, after_share)
    looser = _looser(key, old, new) or _looser(key, _value(key, *view_before), _value(key, *view_after))
    who = _who(args.user_said)
    command = f"agt config {'unset' if unset else 'set'} {key.name}" + ("" if unset else f" {shlex.quote(args.value)}")
    if looser and who["by"] == "agent" and not args.user_said:
        report = {"error": "needs_user_words", "key": key.name, "old": old, "new": new,
                  "ask": f"{key.name} goes from {_text(key, old)} to {_text(key, new)}, which raises a limit or "
                         "loosens safety. Ask the user, and pass their own words.",
                  "retry": f"{command} --{level} --user-said \"<the user's words>\""}
        if args.json:
            print(json.dumps(report, indent=2, ensure_ascii=False))
        else:
            print(f"agt: {report['ask']}\nThen run: {report['retry']}", file=sys.stderr)
        return NEEDS_WORDS
    synclog.notice(workspace)  # a hand edit made before this change is logged as one
    backup = _backup(location, level)
    levels.write(location, new_text)
    synclog.saved(location)
    excluded = _exclude_local(location) if level == "local" and not old_text else None
    if level_old is levels.MISSING:
        undo = f"agt config unset {key.name} --{level}"
    else:
        undo = f"agt config set {key.name} {_argument(key, level_old)} --{level}"
    overridden = None
    source_level, source_layer = _source(key, after)
    if source_level in levels.LEVELS and levels.LEVELS.index(source_level) > upto:
        overridden = {"level": source_level, "file": str(source_layer.path), "value": new,
                      "fix": f"agt config unset {key.name} --{source_level}"}
    limited = _limits(key, after_cfg, new, after)
    synclog.append({"type": "setting", **synclog.where(workspace), "file": str(location), "level": level,
                    "key": key.name, "old": _plain(level_old), "new": None if unset else _plain(levels.read_value(new_text, key.table, key.field)),
                    "command": command, **who})
    triggers = _refresh_share(key)
    report = {"key": key.name, "old": old, "new": new, "file": str(location), "level": level, "applies": key.applies,
              "effective": new, "overridden_by": overridden, "still_limited_by": limited, "undo": undo,
              "backup": str(backup) if backup else None, "excluded_in": excluded, "triggers": triggers}
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    shown = "removed" if unset else _text(key, _plain(levels.read_value(new_text, key.table, key.field)))
    print(f"{key.name}: {_text(key, _plain(level_old)) if level_old is not levels.MISSING else 'not set'} → {shown} "
          f"in {location} ({level})")
    if overridden:
        print(f"In effect here: {_text(key, new)}, from {overridden['file']} ({overridden['level']}). "
              f"To use this one: {overridden['fix']}")
    else:
        print(f"In effect here: {_text(key, new)}. Applies at {key.applies}.")
    for line in limited:
        print(f"Still limited by {line}.")
    if excluded:
        print(f"Kept out of git: {excluded}")
    for line in triggers:
        print(line)
    print(f"Undo: {undo}")
    return 0


def _refresh_share(key: Key) -> list[str]:
    """A switch for the background side changes the watcher and the hook entries at once, while sharing is on."""

    if key.name not in TRIGGER_KEYS:
        return []
    from agents_router.share import commands as share_commands

    saved = share_settings.load()
    return share_commands._refresh_triggers(saved) if saved.enabled else []


def _argument(key: Key, value: Any) -> str:
    """A value as `agt config set` takes it on the command line."""

    if isinstance(value, list):
        text = ",".join(str(item) for item in value)
    elif key.kind == "auto_copy" or isinstance(value, bool):
        text = _text(key, value)
    else:
        text = str(value)
    return shlex.quote(text)


def _plain(value: Any) -> Any:
    return None if value is levels.MISSING else value


def _show(args: argparse.Namespace) -> int:
    workspace = levels.find_workspace()
    files = _files(workspace)
    error = None
    layers: list[levels.Layer] = []
    try:
        layers = levels.layers(files["user"], workspace)
        loaded, share = _effective(layers)
    except levels.SettingsError as exc:
        error = str(exc)
        loaded, share = config.defaults(), share_settings.Settings()
    notes = list(loaded.notes)
    for name, runtime in loaded.runtimes.items():
        if name == "antigravity" and plans.no_parent_channel(runtime):
            notes.append("Antigravity workers have no parent channel; set `max_jobs_per_account = 1` under "
                         f"[antigravity] in {files['user']} to get it.")
    rows = []
    for key in KEYS.values():
        value = _value(key, loaded, share)
        level, layer = _source(key, layers)
        line = levels.line_of(layer.text, key.table, key.field) if layer else None
        rows.append({"key": key.name, "value": value, "shown": _text(key, value), "source": level,
                     "file": str(layer.path) if layer else None, "line": line, "description": key.description,
                     "needs_user_words": bool(key.guard), "user_only": key.user_only,
                     "changed_by": key.changed_by or None, "applies": key.applies})
    shown_files = [{"level": level, "file": str(path), "exists": path.exists()} for level, path in files.items()]
    if workspace is not None and not workspace.local.exists() and workspace.local_fallback() is not None:
        shown_files[-1].update(read_from=str(workspace.local_fallback()))
    if args.json:
        print(json.dumps({"workspace": str(workspace.root) if workspace else None, "files": shown_files,
                          "error": error, "notes": notes, "settings": rows}, indent=2, ensure_ascii=False, default=str))
        return 2 if error else 0
    print(f"Workspace: {workspace.root}" if workspace else "No workspace here (your home folder): user settings only.")
    for entry in shown_files:
        state = "" if entry["exists"] else " (not there yet)"
        if entry.get("read_from"):
            state = f" (none; reads the main checkout's {entry['read_from']})"
        print(f"  {entry['level']:<9}  {entry['file']}{state}")
    if error:
        print(f"\nProblem: {error}\nUntil it's fixed, jobs don't start here; the values below are agt's defaults.")
    width = min(44, max(len(row["key"]) + 3 + len(row["shown"]) for row in rows))
    print()
    for row in rows:
        where = row["source"] if row["file"] is None else f"{row['source']} ({row['file']}{':' + str(row['line']) if row['line'] else ''})"
        if row["changed_by"]:
            where += f"; change it {'with ' if row['changed_by'].startswith('agt') else ''}{row['changed_by']}"
        print(f"{row['key']} = {row['shown']}".ljust(width) + "  " + where)
    for note in notes:
        print(f"Note: {note}")
    return 2 if error else 0


SHARE_COMMANDS = {
    "share.kinds": "agt share kinds add|remove <kind>",
    "share.auto_copy": "agt config set share.auto_copy on|off|<kinds>",
    "share.tool_widening": f"agt config set share.tool_widening {'|'.join(toolmap.TOOL_WIDENING)}",
    **{f"share.{name}": f"agt config set share.{name} true|false" for name in share_settings.SWITCHES},
    "share.instructions": "agt share kinds add|remove instructions, or agt config set share.instructions block|whole|off",
    "share.worktrees": "agt config set share.worktrees auto|command",
    "share.mcp": "agt share kinds add|remove mcp, or agt config set share.mcp true|false",
    "share.hooks": "agt share kinds add|remove hooks; approve one with agt share pick hooks/<name> --in <label>",
}


def share_command(args: argparse.Namespace) -> int:
    """`agt share settings`: every share setting, where it comes from, and the command that changes it."""

    user = config.user_settings_path()
    layer = levels.read("user", user)
    share = share_settings.parse(layer.data.get("share", {}), user) if layer else share_settings.Settings()
    rows = []
    for key in (KEYS[name] for name in ("share.enabled", "share.kinds", "share.auto_copy", "share.tool_widening",
                                         "share.instructions", "share.worktrees", "share.mcp", "share.hooks",
                                         *(f"share.{name}" for name in share_settings.SWITCHES))):
        value = _value(key, config.defaults(), share)
        level, found = _source(key, [layer] if layer else [])
        command = SHARE_COMMANDS.get(key.name) or ("agt share disable" if share.enabled else "agt share enable")
        rows.append({"key": key.name, "value": value, "shown": _text(key, value), "source": level, "command": command})
    picks = len(share.off | set(share.not_in) | set(share.only))
    rows += [
        {"key": "share.folders", "value": [folder.label for folder in share.folders],
         "shown": ", ".join(folder.label for folder in share.folders) or "none", "source": "user" if share.folders else "default",
         "command": "agt share cli add|remove <label>"},
        {"key": "share.targets", "value": picks, "shown": f"{picks} item pick{'s' if picks != 1 else ''}",
         "source": "user" if picks else "default", "command": "agt share pick <item> --in|--not-in|--only <label>"},
    ]
    if args.json:
        print(json.dumps({"file": str(user), "settings": rows}, indent=2, ensure_ascii=False, default=str))
        return 0
    print(f"Share settings, from {user}{'' if layer else ' (not there yet)'}:")
    width = min(44, max(len(row["key"]) + 3 + len(row["shown"]) for row in rows))
    for row in rows:
        print(f"{row['key']} = {row['shown']}".ljust(width) + f"  {row['source']}; change it with {row['command']}")
    return 0


def add_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("config", help="See and change agt's settings (show, set, unset).",
                            description=__doc__.split("\n\n", 1)[0])
    actions = parser.add_subparsers(dest="config_action", required=True)
    show = actions.add_parser("show", help="Every setting in effect here, with where it comes from.")
    show.add_argument("--json", action="store_true")
    for name, help_text in (("set", "Change one setting."), ("unset", "Remove one setting from one file.")):
        action = actions.add_parser(name, help=help_text)
        action.add_argument("key")
        if name == "set":
            action.add_argument("value")
        action.add_argument("--user", action="store_true", help="Your user settings (every workspace).")
        action.add_argument("--workspace", action="store_true", help="This workspace's .agt/settings.toml (committed).")
        action.add_argument("--local", action="store_true", help="This workspace's .agt/settings.local.toml (yours).")
        action.add_argument("--user-said", metavar="WORDS",
                            help="From an agent: the user's own words, for a change that raises a limit or loosens safety.")
        action.add_argument("--json", action="store_true")


def command(args: argparse.Namespace) -> int:
    if args.config_action == "show":
        return _show(args)
    return _change(args, unset=args.config_action == "unset")
