"""Role agent files agt-<role> for the orchestrator agt runs in.

Interface (fixed; other modules call it):
    sync(loaded: config.Config, host: str | None = None, dry_run: bool = False) -> list[str]
        Bring this orchestrator's agt-<role> files in line with the roles your user settings file
        defines: write one per enabled role, remove agt's own file for a role that is off, gone or not
        in your user settings, never touch a file without the "Managed by agt roles" line. Returns one
        human line per file changed or kept. `loaded` says where each CLI keeps its agents.

Agent files are user-wide, so they come from your user settings file alone (user_roles): a project's
settings never add, change or remove one. `agt roles config --disable builder --local` turns the
builder off in that project only; its agt-builder file stays for every other project.

Which orchestrator: `host` names it, or a list names several (claude, codex, antigravity; kiro has
no relay agents, so nothing): setup and update pass the CLIs you named with --host. With host None it's the CLI agt runs in (hosts.detect_host()). From a plain terminal
there is no such CLI: then only the CLIs that already hold agt's role agents are kept in line, so
`agt roles config --disable builder` at a terminal still removes agt's own agt-builder file, and
a CLI that never had role agents gets none.

Where the files go, in each CLI's own relay format (the templates in templates/role-agents/):

    Claude Code  <profile>/agents/agt-<role>.md, in every profile that has the plugin
    Codex        <CODEX_HOME>/agents/agt-<role>.toml
    Antigravity  agt's relay plugin, ~/.gemini/config/plugins/agents-router-relays/agents/agt-<role>.md

Each file runs `agt run --follow --caller <cli> --role <role>` and carries no provider list, so a change of
providers never rewrites it. `agt share` never copies them (share/exclude.py skips every agt-* agent).

Two parts of each file are yours, and every rewrite keeps them (read_parts, render):
    the "when to call" sentence  the description before its last anchor(role); agt's part runs from there to a
                                 phrase in CLOSINGS, and text you add after that moves into your sentence
    the Role rules               the lines between RULES_START and RULES_END at the end of the body; `agt run
                                 --role` reads them from the calling CLI's own file (caller_rules) and puts them
                                 before the task
A sentence that is empty, today's default or an earlier one (roles.EARLIER_DESCRIPTIONS) is agt's, not yours: a sync
writes today's default (unedited). A file agt can't read is left as it is and named (broken); turning a role off archives your parts outside every
agents folder (archive_path) and turning it on puts them back.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
import json
import os
from pathlib import Path
import re
import tomllib
from typing import Any, Iterable

from agents_router import hosts, roles
from agents_router.fsutil import is_link, write_private
from agents_router.share import frontmatter

MARKER = "Managed by agt roles"
VERSION = 2  # 1: agt 0.11, which owned the whole file
MARKER_LINE = (f"{MARKER} v{VERSION}: agt setup, agt update and agt roles config rewrite this file but keep the two parts "
               "you own: the start of the description, before \"Runs the <role> role through Agents Router (\", and the "
               "lines between the agt-role-rules markers. Delete this line to make the whole file yours.")
VERSION_RE = re.compile(r"^# " + re.escape(MARKER) + r"(?::|$| v(\d+):)")
RULES_HEADING = "## Role rules"
RULES_INTRO = ("These rules are for the worker, not for you: agt puts them before the task on every call. Pass the task "
               "on unchanged and don't act on them yourself.")
RULES_START = "<!-- agt-role-rules: agt keeps what's between these lines and sends it to the worker before every task -->"
RULES_END = "<!-- end agt-role-rules -->"
# How agt's part of the description ends, every wording ever shipped (append only): text after it is yours.
CLOSINGS = ("then the complete, self-contained task.",)
HOSTS = ("claude", "codex", "antigravity")  # Kiro has no relay agents (Q19)
SUFFIX = {"claude": ".md", "codex": ".toml", "antigravity": ".md"}
LABELS = {"claude": "Claude Code", "codex": "Codex", "antigravity": "Antigravity"}
RELAYS_PLUGIN = "agents-router-relays"  # setup.ANTIGRAVITY_RELAYS


@dataclass(frozen=True)
class Folder:
    host: str
    label: str  # "Claude Code (claude-ntb)", "Codex"
    path: Path  # the agents folder
    problem: str | None = None  # why agt won't write there


@dataclass(frozen=True)
class Change:
    kind: str  # "write", "remove", "kept", "skipped", "note"
    host: str
    line: str


@dataclass(frozen=True)
class Parts:
    """The two parts of a role agent file you own, and whether agt can read it (see read_parts)."""

    state: str  # "valid", "legacy" (an agt 0.11 file), "broken", "absent"
    sentence: str = ""  # your "when to call" sentence; "" for the default
    rules: str = ""  # your Role rules: "\n" line endings, one trailing newline; "" for none
    problem: str | None = None  # broken: the file, the line and what's wrong
    version: int | None = None  # the management line's version; None in a file that isn't agt's
    moved: str = ""  # text you wrote after agt's part of the description, now the end of your sentence


def anchor(name: str) -> str:
    """Where agt's part of the description starts: the text before its last occurrence is your sentence."""

    return f"Runs the {name} role through Agents Router ("


def version(lines: list[str]) -> int | None:
    """The management line's version among a file's first lines (1 for agt 0.11's), None without one.

    The line must start with `# Managed by agt roles`: the phrase inside a description doesn't count."""

    for line in lines[:3]:
        match = VERSION_RE.match(line.rstrip("\r\n").rstrip())  # at column 0: an indented copy in a description doesn't count
        if match:
            return int(match.group(1)) if match.group(1) else 1
    return None


def clean(text: str) -> str:
    """Rules as agt keeps them: "\n" line endings, no blank lines around them, one trailing newline."""

    lines = text.replace("\r\n", "\n").split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines) + "\n" if lines else ""


def _rules(body: str, first: int, path: Path) -> tuple[str, str | None, bool]:
    """(rules, problem, marked) from the body of a file; the body's first line is line `first` of the file."""

    lines = body.replace("\r\n", "\n").split("\n")
    starts = [index for index, line in enumerate(lines) if line.strip().startswith("<!-- agt-role-rules")]
    ends = [index for index, line in enumerate(lines) if line.strip().startswith("<!-- end agt-role-rules")]
    if not starts and not ends:
        return "", None, False
    where = lambda index: f"{path}:{first + index}"
    if len(starts) > 1:
        return "", f"{where(starts[1])}: a second Role rules start line; keep one", True
    if len(ends) > 1:
        return "", f"{where(ends[1])}: a second Role rules end line; keep one", True
    if not ends:
        return "", f"{where(starts[0])}: the Role rules end line `{RULES_END}` is missing after this line", True
    if not starts:
        return "", f"{where(ends[0])}: the Role rules start line is missing before this end line", True
    if ends[0] < starts[0]:
        return "", f"{where(ends[0])}: the Role rules end line comes before its start line (line {first + starts[0]})", True
    return clean("\n".join(lines[starts[0] + 1:ends[0]])), None, True


def _load(path: Path) -> tuple[str, int | None] | Parts:
    """A file's text ("\n" line endings) and management line version, or the Parts saying why not."""

    if is_link(path):
        return Parts("broken", problem=f"{path} is a link; agt reads role agents only from real files")
    try:
        text = path.read_bytes().decode("utf-8").replace("\r\n", "\n")
    except FileNotFoundError:
        return Parts("absent")
    except (OSError, UnicodeDecodeError) as exc:
        return Parts("broken", problem=f"{path} can't be read ({exc})")
    found = version(text.split("\n"))
    if found is not None and found > VERSION:
        return Parts("broken", problem=f"{path}: written by a newer agt (v{found}); run `agt update`", version=found)
    return text, found


def _body(path: Path, text: str) -> tuple[str, int] | str:
    """The agent's instructions and the file line they start on, or the problem that hides them."""

    lines = text.split("\n")
    if path.suffix == ".toml":
        try:
            body = tomllib.loads(text).get("developer_instructions")
        except tomllib.TOMLDecodeError as exc:
            return f"{path}: {exc}"
        if not isinstance(body, str):
            return f"{path}: developer_instructions isn't text"
        at = next((index for index, line in enumerate(lines) if line.startswith("developer_instructions")), 0)
        return body, at + 2  # the text starts on the line after `developer_instructions = '''`
    if not lines or lines[0].rstrip() != frontmatter.FENCE:
        return f"{path}:1: no front matter between --- lines"
    close = next((index for index in range(1, len(lines)) if lines[index].rstrip() == frontmatter.FENCE), None)
    if close is None:
        return f"{path}:1: the front matter has no closing --- line"
    return "\n".join(lines[close + 1:]), close + 2


def read_rules(path: Path) -> Parts:
    """The Role rules alone, as `agt run` reads them: a link or broken markers make the file broken, whoever owns it;
    the description is never read, so a problem there never blocks a run."""

    loaded = _load(path)
    if isinstance(loaded, Parts):
        return loaded
    text, found = loaded
    body = _body(path, text)
    if isinstance(body, str):
        return Parts("broken", problem=body, version=found)
    rules, problem, marked = _rules(*body, path)
    if problem:
        return Parts("broken", problem=problem, version=found)
    if found == VERSION and not marked:  # agt wrote the markers: rules left without them would be dropped unseen
        return Parts("broken", problem=f"{path}: the Role rules lines are missing; put `{RULES_START}` and "
                     f"`{RULES_END}` back around your rules, or delete the file and run `agt update`", version=found)
    return Parts("legacy" if found == 1 else "valid", rules=rules, version=found)


def read_parts(path: Path, name: str) -> Parts:
    """Your two parts of one role agent file, as sync keeps them, never a guess: anything agt can't read makes the
    file broken. Text after agt's part of the description becomes the end of your sentence (`moved`)."""

    found = read_rules(path)
    if found.state in ("broken", "absent"):
        return found
    text = path.read_bytes().decode("utf-8").replace("\r\n", "\n")
    if path.suffix == ".toml":
        description = tomllib.loads(text).get("description")
        line = next((index + 1 for index, item in enumerate(text.split("\n")) if item.startswith("description")), 1)
    else:
        try:
            doc = frontmatter.parse(text)
        except frontmatter.FrontmatterError as exc:
            return Parts("broken", problem=f"{path}, {exc}", version=found.version)
        description = doc.fields.get("description")
        line = doc.spans.get("description", (0, 0))[0] + 2
    if not isinstance(description, str):
        return Parts("broken", problem=f"{path}:{line}: the description isn't text", version=found.version)
    cut = description.rfind(anchor(name))
    if cut < 0:
        return _encodable(Parts(found.state, description.strip(), found.rules, version=found.version), path, line)
    tail = description[cut:]
    ends = [tail.index(phrase) + len(phrase) for phrase in CLOSINGS if phrase in tail]
    if not ends:
        return Parts("broken", problem=f"{path}:{line}: the description has the start of agt's part (\"{anchor(name)}\") "
                     f"but not its end (\"{CLOSINGS[-1]}\"); put the end back, or delete agt's part", version=found.version)
    moved = tail[min(ends):].strip()
    sentence = " ".join(part for part in (description[:cut].strip(), moved) if part)
    return _encodable(Parts(found.state, sentence, found.rules, version=found.version, moved=moved), path, line)


def _encodable(parts: Parts, path: Path, line: int) -> Parts:
    """A sentence or rules agt couldn't write back (a lone surrogate escape) make the file broken, not a crash."""

    try:
        (parts.sentence + parts.rules).encode("utf-8")
    except UnicodeEncodeError:
        return Parts("broken", problem=f"{path}:{line}: text agt can't write back as UTF-8 (a lone \\u escape?)",
                     version=parts.version)
    return parts


def unedited(name: str, sentence: str, default: str) -> bool:
    """A file's sentence is agt's, not yours: empty, today's default, or a default an earlier agt wrote."""

    return sentence in ("", default) or sentence in roles.EARLIER_DESCRIPTIONS.get(name, ())


def _template(name: str) -> str:
    return files("agents_router").joinpath("templates", *name.split("/")).read_text(encoding="utf-8")


def description(role: roles.Role, sentence: str = "") -> str:
    """The agent's description: your "when to call" sentence (else the role's use-me-when line, Q29), then how to
    call it, which starts at anchor(role.name)."""

    how = ("all at once, and returns one result per provider" if role.access == "read"
           else "one at a time, the first in its priority order with room")
    return (f"{sentence or role.use_when} Runs the {role.name} role through Agents Router (`agt run --role {role.name}`): agt picks "
            f"the providers from the user's [roles.{role.name}] settings, {how}. Start it in the background. Begin the "
            "prompt with optional lines effort=LEVEL, cwd=PATH, timeout=SECONDS, window=SECONDS (default 60), override=USER_WORDS "
            "(only with the user's request for more jobs), then the complete, self-contained task.")


def rules_section(rules: str) -> str:
    """The end of the body: agt's heading and markers around your rules."""

    return f"\n{RULES_HEADING}\n\n{RULES_INTRO}\n\n{RULES_START}\n{clean(rules)}{RULES_END}\n"


def render(role: roles.Role, host: str, sentence: str = "", rules: str = "") -> bytes:
    """The agt-<role> file for one CLI, in the format of that CLI's own relay agents, with your two parts in it."""

    body = _template(f"role-agents/{host}.md").replace("{{ROLE}}", role.name).rstrip("\n") + "\n" + rules_section(rules)
    about = description(role, sentence)
    if host == "claude":
        text = "\n".join([
            "---", f"# {MARKER_LINE}", f"name: agt-{role.name}", f"description: {json.dumps(about, ensure_ascii=False)}",
            "model: haiku", "effort: low", "color: cyan", "tools: Bash", "maxTurns: 200", "omitClaudeMd: true", "---",
        ]) + "\n" + body
    elif host == "antigravity":
        # The relay's own frontmatter settings (tools, model, policy), so the two kinds stay alike.
        relay = _template("antigravity-plugin/agents/agt-codex.md").split("\n---\n", 1)[0].splitlines()[1:]
        kept = [line for line in relay if not line.startswith(("#", "name:", "description:"))]
        text = "\n".join(["---", f"# {MARKER_LINE}", f"name: agt-{role.name}",
                          f"description: {json.dumps(about, ensure_ascii=False)}", *kept, "---"]) + "\n" + body
    else:
        from agents_router.share import codex_toml

        relay = _template("codex-agents/agt-codex.toml")
        settings = [line for line in relay.splitlines() if line.startswith(("model =", "model_reasoning_effort ="))]
        tail = relay[relay.index("\n[features]"):]
        text = "\n".join([f"# {MARKER_LINE}", f'name = "agt-{role.name}"',
                          f"description = {codex_toml.render_value(about)}", *settings,
                          "developer_instructions = '''", body.rstrip("\n"), "'''"]) + "\n" + tail
    return text.encode("utf-8")


def ours(path: Path) -> bool:
    """Whether agt wrote this file: one of its first lines is the management line. A link is never agt's."""

    if is_link(path):
        return False
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            head = [handle.readline() for _ in range(3)]
    except OSError:
        return False
    return version(head) is not None


def _claude_folders(loaded: Any, extra: Iterable[Path]) -> list[Folder]:
    from agents_router import setup
    from agents_router.profiles import same_folder

    accounts = [account for account in setup.claude_profiles(loaded, every=True) if setup.has_plugin(account.home)]
    for folder in extra:
        if not any(same_folder(folder, account.home) for account in accounts):
            match = next((account for account in setup.claude_profiles(loaded, every=True)
                          if same_folder(folder, account.home)), None)
            if match is not None:
                accounts.append(match)
    return [Folder("claude", f"{LABELS['claude']} ({setup.profile_name(account)})", Path(account.home) / "agents",
                   "the profile's agents folder is a link" if is_link(Path(account.home) / "agents") else None)
            for account in accounts]


def _antigravity_folder(loaded: Any, dry_run: bool) -> Folder:
    from agents_router.runtimes.antigravity import gemini_root

    root = gemini_root(loaded.runtime("antigravity"))
    plugin = root / "config" / "plugins" / RELAYS_PLUGIN
    problem = None
    if any(is_link(folder) for folder in (root / "config", plugin.parent, plugin, plugin / "agents")):
        problem = f"won't write through a folder link under {plugin}"
    elif not dry_run and not (plugin / "plugin.json").is_file():
        problem = f"agt's relay plugin isn't in {plugin} yet; `agt setup --host antigravity` installs it with the role agents"
    return Folder("antigravity", LABELS["antigravity"], plugin / "agents", problem)


def folders(loaded: Any, host: str, dry_run: bool = False, claude_folders: Iterable[Path] = ()) -> list[Folder]:
    """The agents folders one CLI reads, where its role agents go."""

    if host == "claude":
        return _claude_folders(loaded, claude_folders)
    if host == "codex":
        path = Path(loaded.runtime("codex").primary).expanduser() / "agents"
        return [Folder("codex", LABELS["codex"], path, "the agents folder is a link" if is_link(path) else None)]
    if host == "antigravity":
        return [_antigravity_folder(loaded, dry_run)]
    return []


@dataclass(frozen=True)
class CallerRules:
    """The Role rules `agt run --role` sends: from the calling CLI's own agt-<role> file, or none and why."""

    text: str = ""
    source: Path | None = None  # the file read
    note: str | None = None  # why there are none


def caller_file(name: str, caller: str, loaded: Any, env: dict[str, str] | None = None) -> Path:
    """The agt-<role> file of the CLI that calls. `caller` only picks which of agt's agents folders: never a path.
    Raises RoleError when that folder is reached through a link."""

    from agents_router import config

    env = dict(os.environ if env is None else env)
    if caller == "claude":  # the calling session's own profile
        folder = Path(config.session_folder("claude", env)) / "agents"
    elif caller == "codex":  # the calling Codex's home, which may differ from agt's configured worker home
        folder = Path(env.get("CODEX_HOME") or Path.home() / ".codex").expanduser() / "agents"
    elif caller == "antigravity":
        found = _antigravity_folder(loaded, dry_run=True)
        if found.problem:
            raise roles.RoleError(f"role {name}: no Role rules read: {found.problem}")
        folder = found.path
    else:
        raise roles.RoleError(f"--caller {caller}: claude, codex or antigravity")
    if is_link(folder):
        raise roles.RoleError(f"role {name}: {folder} is a link; agt reads Role rules only from a real agents folder")
    return folder / f"agt-{name}{SUFFIX[caller]}"


def caller_rules(name: str, caller: str | None, loaded: Any, env: dict[str, str] | None = None) -> CallerRules:
    """The Role rules for a call from `caller`'s relay agent. A broken or linked file raises RoleError (exit 2)
    before any provider starts: dropping a rule such as "never edit migrations" silently is worse than stopping."""

    env = dict(os.environ if env is None else env)
    if int(env.get("AGT_DEPTH") or 0) > 0:
        return CallerRules(note="a worker started by agt calls; Role rules come only from your own CLI's agents")
    if caller is None:
        return CallerRules(note="no calling agent (a terminal, Kiro or another tool); agt-<role> agents pass --caller")
    path = caller_file(name, caller, loaded, env)
    parts = read_rules(path)  # with or without the management line: the file is in your own agents folder
    if parts.state == "broken":
        raise roles.RoleError(f"role {name}: Role rules can't be read: {parts.problem}. Nothing started.")
    if parts.state == "absent":
        return CallerRules(note=f"{path} doesn't exist")
    if parts.state == "legacy" and not parts.rules:
        return CallerRules(source=path, note=f"{path} has no Role rules section yet; `agt update` adds one")
    return CallerRules(parts.rules, path)


def archive_path(folder: Folder, name: str) -> Path:
    """Where your parts of one role agent wait while the role is off: outside every agents folder, so no CLI loads
    them; one folder per CLI and agents folder (profile name plus a short hash of its path)."""

    import hashlib

    from agents_router import config

    profile = f"{folder.path.parent.name.lstrip('.') or 'home'}-{hashlib.sha256(os.path.realpath(folder.path).encode()).hexdigest()[:8]}"
    return config.user_settings_path().parent / "role-agents" / "archive" / folder.host / profile / f"agt-{name}.toml"


def _archive(folder: Folder, name: str, parts: Parts, dry_run: bool) -> Path:
    from agents_router.share import codex_toml

    target = archive_path(folder, name)
    if not dry_run:
        write_private(target, codex_toml.render(
            {"file": str(folder.path / f"agt-{name}{SUFFIX[folder.host]}"), "sentence": parts.sentence, "rules": parts.rules},
            header=f"Your parts of agt-{name}, kept by agt while the role is off; turning it back on puts them back.").encode("utf-8"))
    return target


def _archived(folder: Folder, name: str) -> tuple[Parts, Path] | None:
    """Your archived parts of one role agent, or None (no archive, or one agt can't read)."""

    target = archive_path(folder, name)
    try:
        data = tomllib.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    sentence, rules = data.get("sentence", ""), data.get("rules", "")
    if not isinstance(sentence, str) or not isinstance(rules, str):
        return None
    return Parts("valid", sentence, clean(rules)), target


def file_report(path: Path, name: str) -> dict[str, Any]:
    """One agt-<role> file as `agt roles show --json` lists it: its state, your sentence and how long your rules are.
    A file without the management line is "yours": agt never rewrites it, but `agt run` still sends its rules."""

    try:
        agts = ours(path)
        parts = read_parts(path, name) if agts else read_rules(path)
    except OSError as exc:  # an agents folder agt may not search
        agts, parts = True, Parts("broken", problem=f"{path} can't be read ({exc})")
    state = parts.state if agts or parts.state in ("absent", "broken") else "yours"
    sentence = None if unedited(name, parts.sentence, "") else parts.sentence  # an earlier default: agt's, not yours
    return {"file": str(path), "state": state, "when_to_call": sentence, "rules_chars": len(parts.rules),
            **({"problem": parts.problem} if parts.problem else {})}


def here(name: str, loaded: Any) -> dict[str, Any] | None:
    """The report for the agt-<role> file of the CLI agt runs in, or None outside Claude Code, Codex and Antigravity."""

    host = hosts.detect_host()
    if host not in HOSTS:
        return None
    try:
        return {"cli": host, **file_report(caller_file(name, host, loaded), name)}
    except roles.RoleError as exc:
        return {"cli": host, "file": None, "state": "broken", "when_to_call": None, "rules_chars": 0, "problem": str(exc)}


def agent_files(name: str, loaded: Any) -> list[dict[str, Any]]:
    """Every agt-<role> file agt keeps for this role, in every CLI and Claude profile, with its state."""

    found = []
    for host in HOSTS:
        places = [(folder.label, folder.path, folder.problem) for folder in folders(loaded, host, dry_run=True)]
        try:  # the file `agt run` reads for this CLI, when it isn't one of those (CODEX_HOME beside another primary)
            mine = caller_file(name, host, loaded).parent
            if host != "antigravity" and not any(os.path.realpath(path) == os.path.realpath(mine) for _, path, _ in places):
                places.append((f"{LABELS[host]} (this session)", mine, None))
        except roles.RoleError:
            pass
        for label, folder, problem in places:
            path = folder / f"agt-{name}{SUFFIX[host]}"
            if not (path.exists() or is_link(path)):
                continue
            report = file_report(path, name)
            if problem:  # agt won't read through a linked folder, so neither does `agt run`
                report.update(state="broken", problem=f"{path}: {problem}")
            found.append({"cli": host, "where": label, **report})
    return found


def archived(name: str) -> list[Path]:
    """Your archived parts of one role's agents (kept while the role is off or gone)."""

    from agents_router import config

    root = config.user_settings_path().parent / "role-agents" / "archive"
    return sorted(root.glob(f"*/*/agt-{name}.toml")) if root.is_dir() else []


def own_files(folder: Folder) -> list[Path]:
    """agt's role agents in one folder: agt-* files with the marker, never the built-in relays."""

    relays = {f"agt-{runtime}{SUFFIX[folder.host]}" for runtime in roles.RUNTIMES}
    if not folder.path.is_dir():
        return []
    return sorted(path for path in folder.path.glob(f"agt-*{SUFFIX[folder.host]}")
                  if path.name not in relays and ours(path))


def _hosts(loaded: Any, host: str | list[str] | None) -> list[str]:
    """The CLIs to sync: those named (setup's and update's explicit --host), else the one agt runs in."""

    if host is not None:
        named = [host] if isinstance(host, str) else list(host)
        return [name for name in HOSTS if name in named]
    found = hosts.detect_host()
    if found != "unknown":
        return [found] if found in HOSTS else []
    # A plain terminal: keep in line only the CLIs that already have agt's role agents.
    return _holding(loaded)


def _holding(loaded: Any) -> list[str]:
    """The CLIs that already hold agt's role agents."""

    return [name for name in HOSTS if any(own_files(folder) for folder in folders(loaded, name))]


def user_roles() -> tuple[dict[str, roles.Role], list[str]]:
    """The roles your user settings file gives on its own, and a note for each one it leaves incomplete.

    A role whose user table can't stand alone (a custom role with only `description` there and its
    `access` in a workspace) gets no agent and a note, never an error: the write that started the sync
    already saved. Raises SettingsError when the file itself can't be read."""

    from agents_router import config, levels

    layer = levels.read("user", config.user_settings_path())
    if layer is None:
        return {}, []
    tables = layer.data.get("roles", {})
    if not isinstance(tables, dict):
        roles.check_layer(layer)  # raises, naming the line
    found: dict[str, roles.Role] = {}
    notes: list[str] = []
    for name, table in tables.items():
        alone = levels.Layer(layer.level, layer.path, layer.text, {"roles": {name: table}})
        try:
            found.update(roles.from_layers([alone]))
        except (levels.SettingsError, roles.RoleError) as exc:
            notes.append(f"no agt-{name} agent: your user settings alone don't make a whole {name} role ({exc})")
    return found, notes


def changes(loaded: Any, host: str | list[str] | None = None, dry_run: bool = False, claude_folders: Iterable[Path] = (),
            proposed: dict[str, roles.Role] | None = None, fresh: Iterable[str] = ()) -> list[Change]:
    """`sync`, with each line's kind: what setup and update put in their reports.

    The roles come from your user settings file (user_roles), or from `proposed`: the table a setup dry
    run would write there. Roles in `fresh` take their settings description over your sentence."""

    from agents_router import levels

    named = _hosts(loaded, host)
    if fresh and host is None:  # an explicit --description reaches every CLI that holds agt's role agents
        named += [name for name in _holding(loaded) if name not in named]
    if not named:
        return []
    notes: list[str] = []
    if proposed is None:
        try:
            proposed, notes = user_roles()
        except (levels.SettingsError, roles.RoleError) as exc:
            # Never sync from nothing: an unreadable file would remove every role agent.
            return [Change("skipped", named[0], f"Role agents not changed: your user settings can't be read ({exc}).")]
    wanted = {name: role for name, role in proposed.items() if role.enabled}
    found: list[Change] = [Change("note", named[0], note) for note in notes]
    for name in named:
        for folder in folders(loaded, name, dry_run, claude_folders):
            found += _sync_folder(folder, wanted, dry_run, set(fresh), proposed)
    return found


def _sync_folder(folder: Folder, wanted: dict[str, roles.Role], dry_run: bool, fresh: Iterable[str] = (),
                 known: dict[str, roles.Role] | None = None) -> list[Change]:
    """Write, keep or remove one folder's role agents. Your sentence and Role rules survive every rewrite; a role
    in `fresh` gets its settings description in place of your sentence (`agt roles config <role> --description`)."""

    out: list[Change] = []
    known = known or wanted
    suffix = SUFFIX[folder.host]
    if folder.problem:
        if wanted or own_files(folder):
            out.append(Change("skipped", folder.host, f"{folder.label}: role agents not written: {folder.problem}."))
        return out
    for name, role in wanted.items():
        target = folder.path / f"agt-{name}{suffix}"
        exists = target.exists() or is_link(target)
        if exists and not ours(target):
            out.append(Change("kept", folder.host,
                              f"{folder.label}: kept {target}: it isn't agt's (no \"{MARKER}\" line). Rename it, or keep "
                              f"it to go on using your own {name}."))
            continue
        parts = read_parts(target, name) if exists else Parts("absent")
        restored = _archived(folder, name) if not exists else None
        if restored is not None:
            parts = restored[0]
        if parts.state == "broken":
            out.append(Change("kept", folder.host, f"{folder.label}: left {target} as it is: {parts.problem}. "
                                                   "Fix it, then run `agt update`."))
            continue
        sentence = "" if name in fresh or unedited(name, parts.sentence, role.use_when) else parts.sentence
        if folder.host == "codex" and "\'\'\'" in parts.rules:
            out.append(Change("kept", folder.host, f"{folder.label}: left {target} as it is: its Role rules contain "
                                                   "\'\'\', which a Codex agent file can't hold. Remove it, then run `agt update`."))
            continue
        content = render(role, folder.host, sentence, parts.rules)
        try:
            if exists and target.read_bytes() == content:
                continue
            if not dry_run:
                write_private(target, content)
        except OSError as exc:
            out.append(Change("skipped", folder.host, f"{folder.label}: couldn't write role agent agt-{name}: {exc}"))
            continue
        verb = "would install" if dry_run else "installed"
        moved = f" (moved {parts.moved!r}, written after agt's part of the description, into your sentence)" if parts.moved else ""
        if restored is not None:
            moved = f" (your sentence and Role rules back from {restored[1]})"
            if not dry_run:
                restored[1].unlink(missing_ok=True)
        out.append(Change("write", folder.host, f"{folder.label}: {verb} role agent agt-{name} at {target}{moved}"))
    for path in own_files(folder):
        name = path.name[len("agt-"):-len(suffix)]
        if name in wanted:
            continue
        parts = read_parts(path, name)
        if parts.state == "broken":  # removing it would lose rules agt can't read: leave it and say so
            out.append(Change("kept", folder.host, f"{folder.label}: left {path} as it is though role {name} is off "
                                                   f"or gone: {parts.problem}. Fix it, or delete the file yourself."))
            continue
        default = known[name].use_when if name in known else roles.BASE_DESCRIPTIONS.get(name, "")
        kept = None
        try:
            if parts.rules or not unedited(name, parts.sentence, default):
                kept = _archive(folder, name, parts, dry_run)  # your edits outlive the file
            if not dry_run:
                path.unlink()
        except OSError as exc:
            out.append(Change("skipped", folder.host, f"{folder.label}: couldn't remove {path}: {exc}"))
            continue
        verb = "would remove" if dry_run else "removed"
        where = f"; your sentence and Role rules are kept in {kept}" if kept else ""
        out.append(Change("remove", folder.host, f"{folder.label}: {verb} {path} (agt's own file; role {name} is off, "
                                                 f"gone, or not in your user settings{where})"))
    return out


def sync(loaded, host=None, dry_run=False, *, claude_folders=(), fresh=()):
    """Bring this orchestrator's agt-<role> files in line with your user settings' roles (see the module notes)."""

    return [change.line for change in changes(loaded, host, dry_run, claude_folders, fresh=fresh)]

