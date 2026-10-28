"""Sharing hooks between your CLIs, with translation (`agt share kinds add hooks`).

A hook is one matcher group of a CLI's user hooks: its event, its matcher and
its handlers, named `hooks/<event>-<script>`. agt reads them from Claude Code's
`settings.json`, Codex's `hooks.json` and Antigravity's `config/hooks.json`
(Kiro's hooks aren't read or written yet), and works out for each other CLI
whether the hook can run there (its verdict).

Nothing is copied until you approve it for that CLI (`agt share pick
hooks/<name> --in codex`). The approval holds the hook's identity: its event,
matcher and commands, with every script it calls by content hash. When the
hook or one of its scripts changes, its copies go quiet until you approve it
again.

A copy is one entry at the end of the target's hooks file that runs
`agt share hook-run` (hook_run.py) through the launcher. That translates the
target's payload into the source's, runs the handlers as they are in the
source CLI, and translates the answer back. Every sync makes the copies match
your approvals: an entry agt no longer wants goes, and the others keep their
place, since Codex trusts hooks by position.

Never copied: agt's own hooks, the Antigravity review gate, a hook that isn't
a plain command (prompt, agent, HTTP or MCP handlers, or one with an `if`), and
a hook with a secret written into its command.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import shlex
from typing import Any

from agents_router import fsutil
from agents_router.mcp_translate import ENV_REF_RE, SECRET_FLAG_RE, URL_CREDENTIALS_RE, _secret_in_args
from agents_router.share import providers, triggers
from agents_router.share.hook_run import identity, words
from agents_router.share.state import share_dir

KIND = "hooks"
COPY_SET = "agents-router-share"  # the Antigravity hook set agt owns
REVIEW_SET = "agents-router-review"
COPY_RE = re.compile(r"/bin/agt-run'? share hook-run\b")
OWN_RE = re.compile(r"agt-run|agents[-_]router|(^|[\s/])agt\s+(hook|share|mcp)\b")
EVENTS = {
    "claude": {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "SessionEnd"},
    "codex": {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "SessionEnd"},
    "antigravity": {"SessionStart", "PreToolUse", "PostToolUse"},
}
TOOL_EVENTS = {"PreToolUse", "PostToolUse"}
# A tool in a matcher, in each CLI's name; only the shell maps (other tools' arguments differ between CLIs).
TOOLS = {"Bash": {"claude": "Bash", "codex": "Bash", "antigravity": "run_command"},
         "run_command": {"claude": "Bash", "codex": "Bash", "antigravity": "run_command"}}
PLAIN_MATCHER_RE = re.compile(r"^[A-Za-z0-9_]+(\|[A-Za-z0-9_]+)*$")
ASSIGNMENT_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.+)$")
INTERPRETER_RE = re.compile(r"^(python[0-9.]*|node|bash|sh|zsh|ruby|perl|deno|bun|uv|uvx|npx|env|exec)$")
MAX_TIMEOUT = 600
DEFAULT_TIMEOUT = 60


@dataclass
class Hook:
    name: str
    event: str
    matcher: str
    handlers: list[dict[str, Any]]
    cli: str  # the CLI it's written in
    folder: Path  # the first folder it's in
    identity: str
    labels: list[str] = field(default_factory=list)  # every folder it's in (Claude folders with the same hook count once)
    verdicts: dict[str, str | None] = field(default_factory=dict)  # label -> why it can't go there (None: it can)
    secret: str | None = None
    default_folder: bool = False  # Claude's ~/.claude, which runs without CLAUDE_CONFIG_DIR

    @property
    def key(self) -> str:
        return f"{KIND}/{self.name}"

    @property
    def short(self) -> str:
        return self.identity[:12]


def hooks_file(provider: providers.Provider) -> Path | None:
    return {"claude": provider.folder / "settings.json", "codex": provider.folder / "hooks.json",
            "antigravity": provider.folder / "config" / "hooks.json"}.get(provider.cli)


# Reading -------------------------------------------------------------------------------------


def _groups(provider: providers.Provider) -> tuple[list[tuple[str, dict[str, Any]]], str | None]:
    """(event, matcher group) pairs in the provider's hooks file, and a problem when it can't be read."""

    path = hooks_file(provider)
    if path is None or not path.exists():
        return [], None
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError) as exc:
        return [], f"{path}: can't be read ({exc})"
    if not isinstance(data, dict):
        return [], f"{path}: isn't a JSON object"
    tables = [data.get("hooks")] if provider.cli != "antigravity" else \
        [value for name, value in data.items() if name not in (REVIEW_SET, COPY_SET)]
    found = []
    for table in tables:
        for event, groups in (table or {}).items() if isinstance(table, dict) else []:
            for group in groups if isinstance(groups, list) else []:
                if isinstance(group, dict) and isinstance(group.get("hooks"), list):
                    found.append((str(event), group))
    return found, None


def _slug(handlers: list[dict[str, Any]]) -> str:
    """A name from the first handler: its script, past an interpreter (`python3 ~/hooks/guard.py` is `guard`)."""

    command = str(handlers[0].get("command") or "") if handlers else ""
    pieces = [word for word in words(command) if not word.startswith("-") and not ASSIGNMENT_RE.match(word)]
    while len(pieces) > 1 and INTERPRETER_RE.match(Path(pieces[0]).name):
        pieces = pieces[1:]
    files = [word for word in pieces if "/" in word]
    chosen = files[0] if files else pieces[0] if pieces else "hook"
    stem = Path(chosen).stem or Path(chosen).name
    return re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-") or "hook"


def secret(handlers: list[dict[str, Any]]) -> str | None:
    """Why a hook's commands look like they hold a secret, or None."""

    for handler in handlers:
        pieces = words(str(handler.get("command") or ""))
        reason = _secret_in_args(pieces)
        if reason:
            return reason
        for word in pieces:
            if URL_CREDENTIALS_RE.search(word):
                return "a URL in it has a password"
            literal = not ENV_REF_RE.search(word) and not re.search(r"\$[A-Za-z_]", word)
            assignment = ASSIGNMENT_RE.match(word)
            if assignment and SECRET_FLAG_RE.search(assignment.group(1)) and literal:
                return f"it sets {assignment.group(1)} to a literal value"
            if SECRET_FLAG_RE.search(word) and ":" in word and literal and " " in word:
                return "a header in it holds a literal value"
    return None


def verdict(hook: Hook, cli: str) -> str | None:
    """Why this hook can't run in that CLI, or None when it can."""

    if hook.secret:
        return f"it looks like it has a secret written into it ({hook.secret})"
    if hook.event not in EVENTS.get(cli, set()):
        return f"{cli} has no {hook.event} event" if cli in EVENTS else f"agt doesn't write {cli} hooks yet"
    for handler in hook.handlers:
        if handler.get("type", "command") != "command":
            return f"its {handler.get('type')} handler only runs where it was written"
        if "if" in handler:
            return "it has an `if` condition"
    if hook.cli == cli:
        return None  # another folder of the same CLI runs it as it is
    if hook.event in TOOL_EVENTS:
        if hook.matcher in ("", "*"):
            return "it matches every tool, and the tools differ between CLIs"
        if not PLAIN_MATCHER_RE.match(hook.matcher):
            return "its matcher is a regular expression"
        for tool in hook.matcher.split("|"):
            if tool.startswith("mcp__"):
                if cli != "codex" or hook.cli == "antigravity":
                    return f"its MCP tool {tool} has no counterpart in {cli}"
            elif cli not in TOOLS.get(tool, {}):
                return f"{tool} has no counterpart in {cli}"
    elif cli == "antigravity" and hook.matcher not in ("", "*"):
        return f"its matcher ({hook.matcher}) has no counterpart in Antigravity"
    return None


def matcher_for(hook: Hook, cli: str) -> str:
    if hook.event not in TOOL_EVENTS:
        return "*" if cli == "antigravity" else hook.matcher
    return "|".join(tool if tool.startswith("mcp__") else TOOLS[tool][cli] for tool in hook.matcher.split("|"))


def read(included: list[providers.Provider]) -> tuple[list[Hook], list[str]]:
    """Every hook in these folders (the same hook in two Claude folders counts once), and the files agt can't read."""

    from agents_router.runtimes.claude import _default_dir

    found: dict[str, Hook] = {}
    problems = []
    for provider in included:
        groups, problem = _groups(provider)
        if problem:
            problems.append(problem)
        for event, group in groups:
            handlers = [dict(handler) for handler in group["hooks"] if isinstance(handler, dict)]
            commands = [str(handler.get("command") or "") for handler in handlers]
            if not handlers or any(COPY_RE.search(command) or OWN_RE.search(command) for command in commands):
                continue  # agt's own entries, and its copies of your hooks
            matcher = str(group.get("matcher") or "")
            folder = str(provider.folder)
            key = identity(event, matcher, handlers, folder)
            if key in found:
                found[key].labels.append(provider.label)
                continue
            found[key] = Hook("", event, matcher, handlers, provider.cli, provider.folder, key, [provider.label],
                              secret=secret(handlers),
                              default_folder=provider.cli == "claude" and fsutil.same_path(provider.folder, _default_dir()))
    hooks = sorted(found.values(), key=lambda hook: (hook.event, _slug(hook.handlers), hook.identity))
    taken: dict[str, int] = {}
    for hook in hooks:
        base = f"{hook.event.lower()}-{_slug(hook.handlers)}"
        taken[base] = taken.get(base, 0) + 1
        hook.name = base if taken[base] == 1 else f"{base}-{hook.identity[:6]}"
    by_label = {provider.label: provider.cli for provider in included}
    for hook in hooks:
        hook.verdicts = {label: verdict(hook, cli) for label, cli in by_label.items() if label not in hook.labels}
    return hooks, problems


# The plan --------------------------------------------------------------------------------------


@dataclass
class Copy:
    hook: Hook
    label: str
    approved: str  # the hook's identity prefix when you approved it: the entry carries this, never the current one
    waiting: bool  # changed since you approved it: the entry stays as it was, and hook-run answers neutrally


@dataclass
class Plan:
    hooks: list[Hook] = field(default_factory=list)
    copies: list[Copy] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    by_label: dict[str, providers.Provider] = field(default_factory=dict)
    refused: list[str] = field(default_factory=list)  # approvals for a CLI where the hook can't run

    @property
    def waiting(self) -> list[Copy]:
        return [copy for copy in self.copies if copy.waiting]

    def entries(self, label: str) -> dict[str, list[tuple[str, str, int]]]:
        """The entries agt wants in that folder's hooks file: event -> (matcher, command, timeout)."""

        cli = self.by_label[label].cli
        wanted: dict[str, list[tuple[str, str, int]]] = {}
        for copy in sorted((copy for copy in self.copies if copy.label == label), key=lambda copy: copy.hook.name):
            hook = copy.hook
            wanted.setdefault(hook.event, []).append((matcher_for(hook, cli), command(hook, cli, copy.approved), _timeout(hook)))
        return wanted

    def summary(self) -> str:
        if not self.hooks:
            return "Hooks: none in the folders sharing."
        live = [copy for copy in self.copies if not copy.waiting]
        lines = [f"Hooks: {len(self.hooks)} found, {len(live)} cop{'y' if len(live) == 1 else 'ies'} running in other CLIs."]
        unapproved = [hook for hook in self.hooks
                      if any(why is None and not self._approved(hook, label) for label, why in hook.verdicts.items())]
        if unapproved:
            lines.append("  Waiting for your approval (`agt share pick hooks/<name> --in <label>`): "
                         + ", ".join(hook.name for hook in unapproved) + ".")
        lines += [f"  {copy.hook.name} in {copy.label}: changed since you approved it; it does nothing there until you "
                  f"approve it again" for copy in self.waiting]
        lines += [f"  {line}" for line in self.refused + self.problems]
        return "\n".join(lines)

    def _approved(self, hook: Hook, label: str) -> bool:
        return any(copy.hook is hook and copy.label == label for copy in self.copies)

    def digest_content(self) -> list[Any]:
        return sorted([copy.hook.name, copy.label, copy.approved, copy.waiting] for copy in self.copies)


def build(included: list[providers.Provider], approvals: dict[str, dict[str, str]]) -> Plan:
    plan = Plan(by_label={provider.label: provider for provider in included})
    plan.hooks, plan.problems = read(included)
    by_name = {hook.name: hook for hook in plan.hooks}
    for name, approved in sorted(approvals.items()):
        hook = by_name.get(name)
        if hook is None:
            continue  # gone from its CLI: its copies go too
        for label, short in sorted(approved.items()):
            if label not in plan.by_label or label in hook.labels:
                continue
            why = hook.verdicts.get(label)
            if why is not None:
                plan.refused.append(f"{name} in {label}: approved, but {why}")
                continue
            plan.copies.append(Copy(hook, label, short, waiting=not hook.identity.startswith(short)))
    return plan


def command(hook: Hook, cli: str, approved: str) -> str:
    """The copy's entry. It names the hook as you approved it: a changed hook never re-arms itself on a sync."""

    launcher = shlex.quote(str(triggers.launcher_path()))
    run = f"{launcher} share hook-run {shlex.quote(hook.name)} --to {cli} --hash {shlex.quote(approved)}"
    otherwise = "printf '%s\\n' '{\"decision\": \"allow\"}'" if cli == "antigravity" else "exit 0"
    return f"[ -x {launcher} ] && exec {run} || {otherwise}"


def _timeout(hook: Hook) -> int:
    total = sum(int(handler.get("timeout") or DEFAULT_TIMEOUT) for handler in hook.handlers)
    return min(MAX_TIMEOUT, total + 2)


# Writing -----------------------------------------------------------------------------------------


def _spec(hook: Hook, approved: list[str]) -> dict[str, Any]:
    """The hook as it is now, for hook_run, with the identities you approved (it runs only when they still match)."""

    return {"name": hook.name, "event": hook.event, "matcher": hook.matcher, "handlers": hook.handlers, "cli": hook.cli,
            "folder": str(hook.folder), "default_folder": hook.default_folder,
            "identity": hook.identity, "approved": approved, "deadline": _timeout(hook) - 1}


def _read_sets(path: Path) -> tuple[dict[str, Any], str]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, ""
    data = json.loads(text) if text.strip() else {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} isn't a JSON object; it was left as it is")
    return data, text


def _set_antigravity(path: Path, wanted: dict[str, list[tuple[str, str, int]]], label: str) -> bool:
    data, text = _read_sets(path)
    groups = {event: [{"matcher": matcher, "hooks": [{"type": "command", "command": entry, "timeout": timeout}]}
                      for matcher, entry, timeout in entries] for event, entries in wanted.items()}
    if (data.get(COPY_SET) or {}) == groups or (not groups and COPY_SET not in data):
        return False
    updated = {key: value for key, value in data.items() if key != COPY_SET}
    if groups:
        updated[COPY_SET] = groups
    if text:
        triggers._backup(path, text, label)
    if not updated and not text:
        return False
    rendered = json.dumps(updated, indent=2, ensure_ascii=False) + "\n"
    if path.exists():
        fsutil.write_preserving(path, rendered.encode("utf-8"))
    else:
        fsutil.make_private_dir(path.parent)
        fsutil.write_text(path, rendered)
    return True


def write_entries(provider: providers.Provider, wanted: dict[str, list[tuple[str, str, int]]]) -> bool:
    """Make agt's hook copies in this folder exactly `wanted`. Returns whether its hooks file changed."""

    path = hooks_file(provider)
    if path is None:
        return False
    if provider.cli == "antigravity":
        return _set_antigravity(path, wanted, provider.label)
    return triggers.set_entries(path, wanted, provider.label, pattern=COPY_RE)


@dataclass
class Written:
    name: str
    label: str
    path: Path
    ok: bool
    message: str = ""


def apply(plan: Plan) -> list[Written]:
    """Write each hook's spec and every folder's copies. Call it holding the share lock."""

    folder = share_dir() / "hooks"
    keep = set()
    for copy in plan.copies:
        target = folder / f"{copy.hook.name}.json"  # where hook_run.spec_path looks
        keep.add(target.name)
        spec = _spec(copy.hook, sorted({other.approved for other in plan.copies if other.hook is copy.hook and not other.waiting}))
        text = json.dumps(spec, indent=2) + "\n"
        if not target.exists() or target.read_text(encoding="utf-8") != text:
            fsutil.make_private_dir(target.parent)
            fsutil.write_text(target, text)
    done = []
    for label, provider in plan.by_label.items():
        wanted = plan.entries(label)
        try:
            changed = write_entries(provider, wanted)
        except (OSError, ValueError, triggers.TriggerError) as exc:
            done.append(Written(", ".join(sorted({c.hook.name for c in plan.copies if c.label == label})) or "hooks",
                                label, hooks_file(provider), False, str(exc)))
            continue
        if changed:
            names = sorted({copy.hook.name for copy in plan.copies if copy.label == label}) or ["(none)"]
            done.append(Written(", ".join(names), label, hooks_file(provider), True))
    for stale in folder.glob("*.json") if folder.is_dir() else []:
        if stale.name not in keep:
            stale.unlink(missing_ok=True)
    return done


def remove_all(included: list[providers.Provider]) -> list[str]:
    """Take every copy out (hooks turned off, or sharing off). Returns one line per folder that changed."""

    lines = []
    for provider in included:
        try:
            if write_entries(provider, {}):
                lines.append(f"Removed agt's hook copies from {hooks_file(provider)}.")
        except (OSError, ValueError, triggers.TriggerError) as exc:
            lines.append(f"Couldn't remove agt's hook copies from {hooks_file(provider)}: {exc}")
    folder = share_dir() / "hooks"
    for spec in folder.glob("*.json") if folder.is_dir() else []:
        spec.unlink(missing_ok=True)
    return lines


def attention(plan: Plan) -> list[dict[str, Any]]:
    found = []
    for copy in plan.waiting:
        found.append({"id": f"changed:{copy.hook.key}:{copy.label}", "kind": "changed", "item": copy.hook.key,
                      "who": "choice", "message": f"hook {copy.hook.name} changed since you approved it for {copy.label}; "
                                                  "its copy there does nothing until you approve it again",
                      "fix": [f"agt share pick {copy.hook.key} --in {copy.label}"], "label": copy.label, "why": "changed"})
    for problem in plan.problems:
        found.append({"id": f"unreadable:hooks:{problem.partition(':')[0]}", "kind": "unreadable", "item": "hooks",
                      "who": "action", "message": f"agt can't read hooks in {problem}", "fix": []})
    return found


def watched_files(included: list[providers.Provider]) -> list[Path]:
    """The hooks files, and every script a hook calls (a changed script is a changed hook)."""

    files = [path for path in (hooks_file(provider) for provider in included) if path is not None]
    hooks, _ = read(included)
    for hook in hooks:
        for handler in hook.handlers:
            for word in words(str(handler.get("command") or "")):
                path = os.path.expanduser(word)
                if os.path.isabs(path) and os.path.isfile(path):
                    files.append(Path(path))
    return sorted(set(files))


def describe(hook: Hook, plan: Plan, approvals: dict[str, str]) -> list[str]:
    """`pick hooks/<name>` with no flags: the hook, and its verdict and approval in each other CLI."""

    lines = [f"Hook {hook.name}: {hook.event}" + (f" on {hook.matcher}" if hook.matcher else "")
             + f", in {', '.join(hook.labels)}."]
    lines += [f"  runs: {handler.get('command')}" for handler in hook.handlers]
    for label, why in sorted(hook.verdicts.items()):
        approved = approvals.get(label)
        state = (f"can't: {why}" if why else "approved" if approved and hook.identity.startswith(approved)
                 else "approved, but it changed since" if approved else "works; not approved")
        lines.append(f"  {label:<20} {state}")
    return lines
