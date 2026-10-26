"""Sharing instructions: one block of text kept the same in every CLI's instruction file.

Each CLI folder has one user-level instruction file:

| CLI | File |
|---|---|
| Claude Code | `<folder>/CLAUDE.md` |
| Codex | `~/.codex/AGENTS.md` |
| Antigravity | `~/.gemini/GEMINI.md` |
| Kiro | `~/.kiro/steering/AGENTS.md` (Kiro's user steering; unverified for the CLI) |

With `instructions = "block"` (the usual choice), only the text between
`<!-- agents-router:share:start -->` and `<!-- agents-router:share:end -->`
is shared; everything else in each file stays that CLI's own. A file takes
part once it has the markers: `agt share adopt <label>` adds an empty block
(or, with `--wrap`, puts the file's text inside one). With `"whole"`, the
whole file is shared and a missing file is made. Either way, agt's own
delegation rules block and Claude's `@path` import lines are never copied:
a Claude file keeps its own imports, and the other CLIs never get them.

It's one item, `instructions`, in agt share's state, decided by the same
3-way engine as agents and skills: an edit in any file spreads, two different
edits are a conflict (`agt share resolve instructions --prefer <label>`), and
an empty block is filled from the others. Writes back the file up first, keep
its mode and hard links, change only the block's bytes, and are read back.

In a listed repo, instructions are the repo's own `AGENTS.md`, which Codex,
Antigravity and Kiro read already. When the repo has only `AGENTS.md`, agt
makes a `CLAUDE.md` holding `@AGENTS.md`, so Claude Code reads it too
(`claude_import`). It goes in the repo's `.git/info/exclude`, and agt takes it
away when `AGENTS.md` goes, as long as it still holds only that line.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import re
import time
from typing import Any

from agents_router import fsutil, rules
from agents_router.share import engine, providers
from agents_router.share.state import State, share_dir

START = "<!-- agents-router:share:start -->"
END = "<!-- agents-router:share:end -->"
KEY = "instructions/instructions"
MODES = ("off", "block", "whole")
FRESH_SECONDS = 3
IMPORT_RE = re.compile(r"^@\S")


def file_for(provider: providers.Provider) -> Path | None:
    folder = provider.folder
    return {"claude": folder / "CLAUDE.md", "codex": folder / "AGENTS.md", "antigravity": folder / "GEMINI.md",
            "kiro": folder / "steering" / "AGENTS.md"}.get(provider.cli)


def _rules_span(text: str) -> tuple[int, int] | None:
    if rules.START in text and rules.END in text and text.index(rules.END) > text.index(rules.START):
        return text.index(rules.START), text.index(rules.END) + len(rules.END)
    return None


def _span(text: str) -> tuple[int, int] | None:
    """Where the block's body is (between the markers), None without markers; ValueError when they're broken."""

    if START not in text and END not in text:
        return None
    if text.count(START) != 1 or text.count(END) != 1 or text.index(END) < text.index(START):
        raise ValueError(f"its {START} / {END} markers are incomplete or repeated")
    return text.index(START) + len(START), text.index(END)


def _strip_rules(text: str) -> str:
    span = _rules_span(text)
    return text if span is None else text[:span[0]] + text[span[1]:]


def normalize(text: str) -> str:
    """The shared text: no agt rules block, no `@` imports, LF line ends, no trailing spaces, single blank lines
    (Markdown reads several as one), and no blank lines at either end."""

    lines = [line.rstrip() for line in _strip_rules(text.replace("\r\n", "\n").replace("\r", "\n")).split("\n")]
    lines = [line for line in lines if not IMPORT_RE.match(line)]
    lines = [line for index, line in enumerate(lines) if line or index == 0 or lines[index - 1]]  # one blank line at most
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def version_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class Copy:
    label: str
    cli: str
    path: Path
    state: str  # "shared", "empty", "unadopted", "missing" or "broken"
    text: str = ""  # the normalized shared text
    raw: str = ""
    problem: str | None = None
    fresh: bool = False
    newline: str = "\n"


def read(provider: providers.Provider, mode: str) -> Copy | None:
    path = file_for(provider)
    if path is None:
        return None
    try:
        raw = path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        return Copy(provider.label, provider.cli, path, "missing")
    except (OSError, UnicodeDecodeError) as exc:
        return Copy(provider.label, provider.cli, path, "broken", problem=f"{path}: can't be read ({exc})")
    found = re.search(r"\r\n|\n|\r", raw)
    copy = Copy(provider.label, provider.cli, path, "shared", raw=raw, newline=found.group() if found else "\n")
    try:
        copy.fresh = time.time() - path.stat().st_mtime < FRESH_SECONDS
    except OSError:
        pass
    if mode == "whole":
        body = raw
    else:
        try:
            span = _span(raw)
        except ValueError as exc:
            copy.state, copy.problem = "broken", f"{path}: {exc}"
            return copy
        if span is None:
            copy.state = "unadopted"
            return copy
        body = raw[span[0]:span[1]]
    copy.text = normalize(body)
    if not copy.text:
        copy.state = "empty"
    return copy


@dataclass
class Plan:
    mode: str
    copies: list[Copy] = field(default_factory=list)
    decision: engine.Decision | None = None
    source: Copy | None = None

    @property
    def writes(self) -> list[Copy]:
        if self.decision is None or self.decision.action != engine.WRITE:
            return []
        wanted = set(self.decision.writes)
        return [copy for copy in self.copies if copy.label in wanted]

    @property
    def unadopted(self) -> list[Copy]:
        return [copy for copy in self.copies if copy.state == "unadopted"]

    @property
    def conflict(self) -> bool:
        return self.decision is not None and self.decision.action == engine.CONFLICT

    def summary(self) -> str:
        """One line for `status` and the dry run."""

        if self.decision is None:
            return "Instructions: no instruction file takes part yet; `agt share adopt <label>` adds agt's block to one."
        shared = [copy.label for copy in self.copies if copy.state in ("shared", "empty")]
        if self.conflict:
            return (f"Instructions differ between {', '.join(self.decision.labels)} ({self.decision.reason}): "
                    "`agt share resolve instructions --prefer <label>`.")
        if self.decision.action == engine.WAIT:
            return f"Instructions wait: {self.decision.reason}."
        if self.writes:
            return f"Instructions: {len(self.writes)} to write ({', '.join(copy.label for copy in self.writes)}), from {self.source.label}."
        return f"Instructions: the same in {', '.join(shared)}."


def build(included: list[providers.Provider], mode: str, current: State, *, prefer: str | None = None) -> Plan:
    """What the instruction files need: the engine's decision over the files that take part."""

    plan = Plan(mode)
    if mode == "off":
        return plan
    plan.copies = [copy for copy in (read(provider, mode) for provider in included) if copy is not None]
    taking_part = [copy for copy in plan.copies if copy.state in ("shared", "empty", "broken")
                   or (mode == "whole" and copy.state == "missing")]
    if not taking_part:
        return plan
    targets = {copy.label for copy in taking_part}
    seen = [engine.Copy(copy.label, None if copy.state == "broken" else version_of(copy.text), copy.fresh, copy.problem)
            for copy in taking_part if copy.state in ("shared", "broken")]
    record = current.items.get(KEY) or {}
    last = engine.Last(version=record["version"], copies=dict(record.get("copies") or {})) if record.get("version") else None
    if prefer:
        chosen = next((copy for copy in taking_part if copy.label == prefer and copy.state == "shared"), None)
        if chosen is None:
            raise ValueError(f"{prefer} has no shared instructions to prefer")
        version = version_of(chosen.text)
        others = sorted(copy.label for copy in taking_part if copy.label != prefer
                        and (copy.state != "shared" or version_of(copy.text) != version))
        plan.decision = engine.Decision(engine.WRITE, version=version, source=prefer, updates=others,
                                        reason=f"you picked {prefer}")
    else:
        plan.decision = engine.decide(seen, targets, last)
    if plan.decision.source:
        plan.source = next((copy for copy in plan.copies if copy.label == plan.decision.source), None)
    return plan


def render(target: Copy, text: str, mode: str) -> str:
    """The target file with the shared text in it: Claude keeps its imports, every file keeps agt's rules block."""

    newline = target.newline
    imports = [line for line in target.raw.replace("\r\n", "\n").replace("\r", "\n").split("\n") if IMPORT_RE.match(line)] \
        if target.cli == "claude" else []
    body = "\n".join([*imports, *([""] if imports and text else []), text]) if imports else text
    if mode == "whole":
        span = _rules_span(target.raw)
        kept = target.raw[span[0]:span[1]] if span else ""
        result = body + ("\n\n" + kept if kept else "") + "\n"
        return result.replace("\n", newline) if newline != "\n" else result
    if target.state in ("missing", "unadopted"):
        raise ValueError(f"{target.label} hasn't adopted agt's instructions block")
    start, end = _span(target.raw)
    inner = "\n" + body + "\n" if body else "\n"
    inner = inner.replace("\n", newline) if newline != "\n" else inner
    return target.raw[:start] + inner + target.raw[end:]


@dataclass
class Written:
    label: str
    path: Path
    ok: bool
    message: str = ""
    backup: Path | None = None


def apply(plan: Plan, current: State, run: str) -> list[Written]:
    """Write the plan's instruction copies and record the version. Call it holding the share lock."""

    done: list[Written] = []
    if plan.decision is None:
        return done
    record = current.items.get(KEY) or {}
    copies = dict(record.get("copies") or {})
    if plan.decision.action in (engine.NOTHING, engine.WRITE) and plan.decision.version:
        text = plan.source.text if plan.source else ""
        for target in plan.writes:
            try:
                rendered = render(target, text, plan.mode)
                before = target.path.read_bytes() if target.path.exists() else None
                if before is not None and before.decode("utf-8", "replace") != target.raw:
                    done.append(Written(target.label, target.path, False, "skipped this run: it changed meanwhile"))
                    continue
                backup = None
                if before is not None:
                    folder = share_dir() / "backups" / run / "instructions"
                    fsutil.make_private_dir(folder)
                    backup = folder / f"{target.label}-{target.path.name}"
                    fsutil.write_private(backup, before)
                    fsutil.write_preserving(target.path, rendered.encode("utf-8"))
                else:
                    fsutil.make_private_dir(target.path.parent)
                    fsutil.write_text(target.path, rendered)
                check = read_back(target, plan.mode)
                if check != plan.decision.version:
                    if backup is not None:
                        fsutil.write_preserving(target.path, backup.read_bytes())
                    done.append(Written(target.label, target.path, False, "read back differently; put back", backup))
                    continue
                done.append(Written(target.label, target.path, True, backup=backup))
            except (OSError, ValueError) as exc:
                done.append(Written(target.label, target.path, False, f"couldn't write it ({exc})"))
        if all(written.ok for written in done):
            for copy in plan.copies:
                if copy.state in ("shared", "empty") or (plan.mode == "whole" and copy.state == "missing"):
                    copies[copy.label] = plan.decision.version
            current.items[KEY] = {"version": plan.decision.version, "copies": copies}
    return done


def read_back(target: Copy, mode: str) -> str | None:
    provider = providers.Provider(target.cli, target.path.parent if target.cli != "kiro" else target.path.parent.parent,
                                  target.label, True)
    copy = read(provider, mode)
    return version_of(copy.text) if copy and copy.state in ("shared", "empty") else None


def adopt(provider: providers.Provider, *, wrap: bool = False) -> tuple[Path, str]:
    """Add agt's block to a CLI's instruction file. Returns the file and what was done."""

    path = file_for(provider)
    if path is None:
        raise ValueError(f"{provider.label} has no instruction file agt knows")
    try:
        raw = path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        raw = None
    if raw is not None and _span(raw) is not None:
        return path, "already has agt's block"
    newline = "\n"
    if raw:
        found = re.search(r"\r\n|\n|\r", raw)
        newline = found.group() if found else "\n"
    if raw and wrap:
        span = _rules_span(raw)
        rules_block = raw[span[0]:span[1]] if span else ""
        body = (raw[:span[0]] + raw[span[1]:]) if span else raw
        text = f"{START}\n{body.strip()}\n{END}\n" + (f"\n{rules_block}\n" if rules_block else "")
        result = text.replace("\n", newline) if newline != "\n" else text
        what = "put the file's text inside agt's block"
    else:
        block = f"{START}\n{END}\n"
        if raw:
            separator = "" if raw.endswith(("\n", "\r")) else newline
            result = raw + separator + newline + block.replace("\n", newline)
        else:
            result = block
        what = "added an empty agt block at the end"
    if raw is not None:
        backup = share_dir() / "backups" / "adopt" / time.strftime("%Y%m%d-%H%M%S")
        fsutil.make_private_dir(backup)
        fsutil.write_private(backup / f"{provider.label}-{path.name}", raw.encode("utf-8"))
        fsutil.write_preserving(path, result.encode("utf-8"))
    else:
        fsutil.make_private_dir(path.parent)
        fsutil.write_text(path, result)
    return path, what


def attention(plan: Plan) -> list[dict[str, Any]]:
    """The instructions' entries for the attention list."""

    found = []
    if plan.conflict:
        found.append({"id": f"conflict:{KEY}", "kind": "conflict", "item": KEY, "who": "choice",
                      "message": f"instructions differ between {', '.join(plan.decision.labels)} ({plan.decision.reason})",
                      "fix": ["agt share resolve instructions --prefer <label>"]})
    elif plan.decision is not None and plan.decision.action == engine.WAIT and plan.decision.reason != "saved less than 3 s ago":
        found.append({"id": f"unreadable:{KEY}", "kind": "unreadable", "item": KEY, "who": "action",
                      "message": f"agt can't use the instructions: {plan.decision.reason}", "fix": []})
    if plan.mode == "block":
        for copy in plan.unadopted:
            found.append({"id": f"adopt:{KEY}:{copy.label}", "kind": "adopt", "item": KEY, "who": "choice",
                          "message": f"{copy.label}'s {copy.path.name} has no agt share block, so it doesn't share instructions",
                          "fix": [f"agt share adopt {copy.label}"]})
    return found


def signature_files(included: list[providers.Provider]) -> list[Path]:
    return [path for path in (file_for(provider) for provider in included) if path is not None]


# A repo with only AGENTS.md ------------------------------------------------------

IMPORT_KEY = "instructions/claude-import"
IMPORT_TEXT = "@AGENTS.md\n"


@dataclass
class Import:
    """What a listed repo's `CLAUDE.md` import needs this run."""

    path: Path
    action: str  # "make"; "remove" (agt's, still only the import, not wanted now); "release" (yours now); "forget"
    note: str = ""


def is_import(path: Path) -> bool:
    """Whether the file holds only `@AGENTS.md`, as agt writes it (a link never counts)."""

    if os.path.islink(path) or not path.is_file():
        return False
    try:
        return path.read_text(encoding="utf-8").strip() == IMPORT_TEXT.strip()
    except (OSError, UnicodeDecodeError):
        return False


def claude_import(root: Path, wanted: bool, current: State) -> Import | None:
    """The import's change in this repo, or None. `wanted`: instructions share, and so does Claude Code here.

    agt makes `CLAUDE.md` only when the repo has `AGENTS.md` and no `CLAUDE.md` of its own, at the root or in
    `.claude/`. agt's file goes when `AGENTS.md` does (a branch without it) or it isn't wanted, while it still
    holds only the import; once you edit it, it's yours. One that disappeared is made again, like a cleaned copy.
    """

    path = root / "CLAUDE.md"
    agents = (root / "AGENTS.md").is_file()
    room = not os.path.lexists(path) and not os.path.lexists(root / ".claude" / "CLAUDE.md")
    if current.items.get(IMPORT_KEY):
        if os.path.lexists(path):
            if not is_import(path):
                return Import(path, "release", "CLAUDE.md changed since agt made it, so it's yours now")
            if not wanted or not agents:
                return Import(path, "remove", "AGENTS.md is gone" if wanted else "instructions don't share here now")
            return None
        if wanted and agents and room:
            return Import(path, "make", "holds @AGENTS.md, so Claude Code reads AGENTS.md")
        return Import(path, "forget")
    if wanted and agents and room:
        return Import(path, "make", "holds @AGENTS.md, so Claude Code reads AGENTS.md")
    return None


def turn_off_import(root: Path, current: State) -> Path | None:
    """agt's import in this repo, still only `@AGENTS.md`, for a turn-off to keep or remove."""

    path = root / "CLAUDE.md"
    return path if current.items.get(IMPORT_KEY) and is_import(path) else None


def import_watched(root: Path) -> list[Path]:
    """The files whose change can make or take away the import."""

    return [root / "AGENTS.md", root / "CLAUDE.md", root / ".claude" / "CLAUDE.md"]
