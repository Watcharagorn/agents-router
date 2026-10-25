"""Sharing MCP servers between your CLIs' user settings.

Each server is one item, `mcp/<name>`, decided by the same 3-way engine as
agents and skills: a server you add or change in one CLI reaches the others,
different changes are a conflict, and a server removed where agt made it
stops going there. Off unless you turn it on (`agt share kinds add mcp`).

Where each CLI keeps its user servers:

| CLI | Read | Written with |
|---|---|---|
| Claude Code | `mcpServers` in the folder's `.claude.json` | `claude mcp add-json --scope user` (agt never edits `.claude.json`) |
| Codex | `[mcp_servers.<name>]` in `config.toml` | that table, every other line kept |
| Antigravity | `config/mcp_config.json` | a JSON merge |
| Kiro | `settings/mcp.json` | a JSON merge |

Servers are compared in one shape (Claude's): `command`, `args`, `env`, `cwd`
for stdio, `type`, `url`, `headers` for HTTP and SSE. `${VAR}` references are
read raw and never expanded. Codex's `env_vars`, `bearer_token_env_var` and
`env_http_headers` become references in that shape, and back. A CLI's other
keys for a server (Kiro's `disabled`, Codex's timeouts) stay as they are.

Never copied: agt's own server; a server with a literal secret (a credential
in `env`, a header, an argument or the URL), which stays where it is; a
server that renames a variable (`"A": "${B}"`) to Codex, which can only pass
variables through by name; a server that needs `${VAR}` to Antigravity, which
doesn't expand them. Picks in `[share.targets]` (`"mcp/<name>" = { only =
[...] }`) keep a server where you say.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tomllib
from typing import Any

from agents_router import fsutil, levels
from agents_router.mcp_translate import ENV_REF_RE, SECRET_FLAG_RE, URL_CREDENTIALS_RE, _is_self, _secret_in_args, _transport
from agents_router.share import codex_toml, engine, providers
from agents_router.share.state import State, share_dir

KIND = "mcp"
FULL_REF_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
BEARER_RE = re.compile(r"^Bearer \$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
CODEX_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
# The keys of the shared shape, in each CLI's spelling; every other key of a server is that CLI's own.
SHAPE_KEYS = {"type", "command", "args", "env", "cwd", "url", "headers", "serverUrl", "httpUrl"}
CODEX_SHAPE_KEYS = {"command", "args", "env", "env_vars", "cwd", "url", "http_headers", "env_http_headers",
                    "bearer_token_env_var"}


# One shape -------------------------------------------------------------------------------


def _strings(value: Any) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _mapping(value: Any) -> dict[str, str]:
    return {str(key): str(item) for key, item in value.items()} if isinstance(value, dict) else {}


def normalize(server: Any) -> dict[str, Any] | None:
    """Claude's shape, with only the fields every CLI shares; None for a server agt can't read."""

    if not isinstance(server, dict):
        return None
    url = server.get("url") or server.get("serverUrl") or server.get("httpUrl")
    transport = _transport({**server, "url": url})
    if transport == "stdio":
        result: dict[str, Any] = {"command": str(server["command"]), "args": _strings(server.get("args"))}
        if _mapping(server.get("env")):
            result["env"] = _mapping(server.get("env"))
        if server.get("cwd"):
            result["cwd"] = str(server["cwd"])
        return result
    if transport in ("http", "sse") and url:
        result = {"type": transport, "url": str(url)}
        if _mapping(server.get("headers")):
            result["headers"] = _mapping(server.get("headers"))
        return result
    return None


def version_of(server: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(server, sort_keys=True).encode()).hexdigest()


def _from_codex(table: Any) -> dict[str, Any] | None:
    if not isinstance(table, dict):
        return None
    server: dict[str, Any] = {key: table[key] for key in ("command", "args", "cwd", "url") if key in table}
    env = _mapping(table.get("env"))
    for name in _strings(table.get("env_vars")):
        env[name] = "${" + name + "}"
    if env:
        server["env"] = env
    headers = _mapping(table.get("http_headers"))
    for header, variable in _mapping(table.get("env_http_headers")).items():
        headers[header] = "${" + variable + "}"
    if table.get("bearer_token_env_var"):
        headers["Authorization"] = "Bearer ${" + str(table["bearer_token_env_var"]) + "}"
    if headers:
        server["headers"] = headers
    if "url" in server and "command" not in server:
        server["type"] = "http"
    return normalize(server)


def _to_codex(server: dict[str, Any]) -> dict[str, Any]:
    """Codex's `[mcp_servers.<name>]` keys for a server in the one shape (`blocked` has already said it can go)."""

    table: dict[str, Any] = {}
    if "command" in server:
        table.update(command=server["command"], args=server["args"])
        if server.get("cwd"):
            table["cwd"] = server["cwd"]
        env = server.get("env", {})
        literal = {key: value for key, value in env.items() if not FULL_REF_RE.match(value)}
        passed = [key for key, value in env.items() if FULL_REF_RE.match(value)]
        if literal:
            table["env"] = literal
        if passed:
            table["env_vars"] = passed
        return table
    table["url"] = server["url"]
    plain, by_variable = {}, {}
    for header, value in server.get("headers", {}).items():
        bearer = BEARER_RE.match(value) if header == "Authorization" else None
        ref = FULL_REF_RE.match(value)
        if bearer:
            table["bearer_token_env_var"] = bearer.group(1)
        elif ref:
            by_variable[header] = ref.group(1)
        else:
            plain[header] = value
    if plain:
        table["http_headers"] = plain
    if by_variable:
        table["env_http_headers"] = by_variable
    return table


def _to_json(server: dict[str, Any], cli: str) -> dict[str, Any]:
    """Kiro's and Antigravity's shape: Claude's, with Antigravity's `serverUrl` for URL servers."""

    if cli == "antigravity" and "url" in server:
        result = {"serverUrl": server["url"]}
        if server.get("headers"):
            result["headers"] = server["headers"]
        return result
    return dict(server)


# What keeps a server where it is ---------------------------------------------------------


def secret(server: dict[str, Any]) -> str | None:
    """Why this server holds a literal secret (so it's never copied), or None."""

    for key, value in {**server.get("env", {}), **server.get("headers", {})}.items():
        if SECRET_FLAG_RE.search(key) and not ENV_REF_RE.search(value):
            return f"{key} holds a literal value"
        if URL_CREDENTIALS_RE.search(value):
            return f"{key} holds a URL with a password"
    if URL_CREDENTIALS_RE.search(str(server.get("url", ""))):
        return "its URL has a password"
    return _secret_in_args(server.get("args", []))


def blocked(server: dict[str, Any], cli: str) -> str | None:
    """Why this server can't go to this CLI, or None."""

    outside = [server.get("url", ""), server.get("command", ""), server.get("cwd", ""), *server.get("args", [])]
    inside = [*server.get("env", {}).values(), *server.get("headers", {}).values()]
    if cli == "antigravity" and any(ENV_REF_RE.search(str(value)) for value in [*outside, *inside]):
        return "it needs ${VAR}, which Antigravity doesn't expand"
    if cli == "antigravity" and server.get("type") == "sse":
        return "Antigravity's serverUrl doesn't say SSE"
    if cli == "codex":
        for key, value in server.get("env", {}).items():
            ref = FULL_REF_RE.match(value)
            if (ref and ref.group(1) != key) or (not ref and ENV_REF_RE.search(value)):
                return f"its {key} takes another variable's value, and Codex passes variables only by name"
        if server.get("type") == "sse":
            return "Codex has no SSE transport"
        for header, value in server.get("headers", {}).items():
            if ENV_REF_RE.search(value) and not (FULL_REF_RE.match(value) or (header == "Authorization" and BEARER_RE.match(value))):
                return f"its {header} header mixes text and a variable, which Codex can't pass"
        if any(ENV_REF_RE.search(str(value)) for value in outside):
            return "it has ${VAR} outside env and headers, which Codex doesn't expand"
    return None


# Reading ---------------------------------------------------------------------------------


def config_file(provider: providers.Provider) -> Path | None:
    folder = provider.folder
    if provider.cli == "claude":
        from agents_router.runtimes.claude import _default_dir

        return Path.home() / ".claude.json" if fsutil.same_path(folder, _default_dir()) else folder / ".claude.json"
    return {"codex": folder / "config.toml", "antigravity": folder / "config" / "mcp_config.json",
            "kiro": folder / "settings" / "mcp.json"}.get(provider.cli)


def _raw(provider: providers.Provider, path: Path) -> dict[str, Any]:
    """The file's server table as the CLI writes it (raises OSError or ValueError)."""

    text = path.read_text(encoding="utf-8")
    if provider.cli == "codex":
        raw = tomllib.loads(text).get("mcp_servers")
    else:
        data = json.loads(text) if text.strip() else {}
        raw = data.get("mcpServers") if isinstance(data, dict) else None
    return raw if isinstance(raw, dict) else {}


def read(provider: providers.Provider) -> tuple[dict[str, dict[str, Any]], str | None]:
    """The provider's user servers in the one shape, and a problem when its file can't be read."""

    path = config_file(provider)
    if path is None or not path.exists():
        return {}, None
    try:
        raw = _raw(provider, path)
    except (OSError, ValueError) as exc:  # TOML and JSON decode errors are ValueErrors
        return {}, f"{path}: can't be read ({exc})"
    found = {}
    for name, entry in raw.items():
        if not isinstance(name, str) or _is_self(name, entry if isinstance(entry, dict) else {}):
            continue
        server = _from_codex(entry) if provider.cli == "codex" else normalize(entry)
        if server is not None:
            found[name] = server
    return found, None


def fingerprint(path: Path) -> str:
    """The servers in a `.claude.json`, for the watcher: Claude rewrites that file all the time for other reasons."""

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "unreadable"
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    return hashlib.sha256(json.dumps(servers, sort_keys=True, default=str).encode()).hexdigest()


# The plan ----------------------------------------------------------------------------------


@dataclass
class Server:
    name: str
    copies: dict[str, dict[str, Any]] = field(default_factory=dict)  # label -> the server there
    decision: engine.Decision | None = None
    blocked: dict[str, str] = field(default_factory=dict)  # label -> why it can't go there
    held: str | None = None  # why it isn't shared at all (a secret, your pick)

    @property
    def key(self) -> str:
        return f"{KIND}/{self.name}"

    @property
    def source(self) -> dict[str, Any] | None:
        return self.copies.get(self.decision.source) if self.decision and self.decision.source else None

    @property
    def writes(self) -> list[str]:
        if self.held or self.decision is None or self.decision.action != engine.WRITE:
            return []
        return [label for label in self.decision.writes if label not in self.blocked]


@dataclass
class Plan:
    servers: list[Server] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    by_label: dict[str, providers.Provider] = field(default_factory=dict)
    later: set[str] = field(default_factory=set)  # labels whose writes wait for the next run
    removing: dict[str, set[str] | None] = field(default_factory=dict)  # name -> labels to take it out of (None: all)

    @property
    def conflicts(self) -> list[Server]:
        return [server for server in self.servers if server.decision and server.decision.action == engine.CONFLICT]

    @property
    def writes(self) -> list[tuple[Server, str]]:
        return [(server, label) for server in self.servers for label in server.writes if label not in self.later]

    @property
    def pending(self) -> list[Server]:
        """Servers whose original was removed in one CLI; `agt share rm mcp/<name>` removes the rest."""

        return [server for server in self.servers if server.decision and server.decision.action == engine.PENDING_DELETE]

    def notes(self) -> list[str]:
        """What a person should know about the writes: Kiro's approval of variables, and logins."""

        found = []
        for server, label in self.writes:
            source = server.source or {}
            refs = any(ENV_REF_RE.search(value) for value in [*source.get("env", {}).values(), *source.get("headers", {}).values()])
            if self.by_label[label].cli == "kiro" and refs:
                found.append(f"{server.name} in {label}: Kiro asks you before it passes ${{VAR}} values to it")
            if "url" in source and not source.get("headers"):
                found.append(f"{server.name} in {label}: if it uses a login (OAuth), log in from that CLI once")
        return found

    def summary(self) -> str:
        if not self.servers and not self.problems:
            return "MCP servers: none in the folders sharing."
        lines = []
        if self.conflicts:
            lines.append(f"MCP servers: {len(self.conflicts)} differ between CLIs ({', '.join(s.name for s in self.conflicts)}); "
                         "`agt share resolve mcp/<name> --prefer <label>` picks one.")
        if self.writes:
            lines.append(f"MCP servers: {len(self.writes)} to write ("
                         + ", ".join(f"{server.name} to {label}" for server, label in self.writes) + ").")
        if self.pending:
            lines.append(f"MCP servers: {', '.join(server.name for server in self.pending)} removed where it started; "
                         "`agt share rm mcp/<name>` removes the other copies.")
        if not lines:
            shared = [server for server in self.servers if not server.held]
            lines.append(f"MCP servers: {len(shared)} the same everywhere they go.")
        waiting = sorted({label for server in self.servers for label in server.writes if label in self.later})
        if waiting:
            lines.append(f"  Writes to {', '.join(waiting)} wait for the background watcher or `agt share sync`.")
        held = [f"{server.name} ({server.held})" for server in self.servers if server.held]
        if held:
            lines.append("  Staying where they are: " + "; ".join(held) + ".")
        lines += [f"  {server.name} not in {label}: {why}" for server in self.servers if not server.held
                  for label, why in sorted(server.blocked.items()) if label not in server.copies]
        lines += [f"  {line}" for line in self.notes()]
        lines += [f"  {problem}" for problem in self.problems]
        return "\n".join(lines)

    def digest_content(self) -> list[Any]:
        return sorted([server.name, server.decision.action if server.decision else "",
                       sorted(label for label in server.writes if label not in self.later),
                       sorted(version_of(copy) for copy in server.copies.values())] for server in self.servers)


def agt_made(server: Server, current: State) -> set[str]:
    """The CLIs where agt made this server and it's unchanged since: the copies a pick or `disable` may take out."""

    record = current.items.get(server.key) or {}
    return {label for label in record.get("made") or [] if label in server.copies
            and version_of(server.copies[label]) == (record.get("copies") or {}).get(label)}


def removal_only(plan: Plan, removing: dict[str, set[str]]) -> Plan:
    """A plan that only takes these servers out of these CLIs (`disable --remove-copies`)."""

    servers = [dataclasses.replace(server, decision=None) for server in plan.servers if removing.get(server.name)]
    return dataclasses.replace(plan, servers=servers, removing={name: labels for name, labels in removing.items() if labels})


def build(included: list[providers.Provider], current: State, options: Any, *, later: frozenset[str] = frozenset()) -> Plan:
    """The servers in these folders and what a run does with each.

    `options` is the share plan's (picks, answers). Writes to the CLIs in `later` wait for the next run that
    allows them: a session hook leaves Claude to the watcher, since `claude mcp` takes seconds to start.
    """

    plan = Plan(by_label={provider.label: provider for provider in included},
                later={provider.label for provider in included if provider.cli in later})
    servers: dict[str, Server] = {}
    unreadable = set()
    for provider in included:
        found, problem = read(provider)
        if problem:
            plan.problems.append(problem)
            unreadable.add(provider.label)  # nothing is written there, and its servers aren't missing
        for name, server in found.items():
            servers.setdefault(name, Server(name)).copies[provider.label] = server
    for name in sorted(servers):
        item = servers[name]
        plan.servers.append(item)
        key = item.key
        if key in options.off:
            item.held, item.decision = "you turned it off", engine.Decision(engine.NOTHING, reason="not shared")
            continue
        reason = next((secret(server) for server in item.copies.values() if secret(server)), None)
        if reason:
            item.held = f"a literal secret: {reason}"
            item.decision = engine.Decision(engine.NOTHING, reason=item.held)
            continue
        allowed = set(options.only[key]) if key in options.only else set(plan.by_label)
        allowed -= {label for picked, label in options.skip if picked == key}
        for label, provider in plan.by_label.items():
            why = next((blocked(server, provider.cli) for server in item.copies.values() if blocked(server, provider.cli)), None)
            if why:
                item.blocked[label] = why
        left_out = {label for label in item.copies if label not in allowed}  # copies outside your pick stay as they are
        targets = {label for label in allowed if label not in item.copies and label not in item.blocked} - unreadable
        copies = [engine.Copy(label, version_of(server)) for label, server in item.copies.items() if label not in left_out]
        record = current.items.get(key) or {}
        last = engine.Last(version=record["version"], copies=dict(record.get("copies") or {}),
                           made=set(record.get("made") or []), stopped=set(record.get("stopped") or [])) \
            if record.get("version") else None
        decision = engine.decide(copies, targets | (set(item.copies) - left_out), last, left_out | unreadable)
        choice = options.resolve.get(key)
        if decision.action == engine.CONFLICT and choice in item.copies:
            version = version_of(item.copies[choice])
            updates = sorted(label for label, server in item.copies.items()
                             if label not in left_out and version_of(server) != version)
            decision = engine.Decision(engine.WRITE, version=version, source=choice, updates=updates,
                                       fills=sorted(targets), reason=f"resolved: {choice}'s version")
        item.decision = decision
    return plan


# Writing -----------------------------------------------------------------------------------


@dataclass
class Written:
    name: str
    label: str
    path: Path | None
    action: str  # copy, update or remove
    ok: bool
    message: str = ""
    backup: Path | None = None


def _backup(provider: providers.Provider, name: str, run: str) -> tuple[Path | None, bytes | dict[str, Any] | None]:
    """Keep what's there before a write: the server's own entry for Claude (agt never copies `.claude.json`),
    the whole file for the others. Returns the backup file and what to put back."""

    path = config_file(provider)
    if provider.cli == "claude":
        try:
            before = _raw(provider, path).get(name) if path is not None and path.exists() else None
        except (OSError, ValueError):
            before = None
        data = json.dumps({name: before}, indent=2).encode("utf-8") + b"\n" if before is not None else None
    else:
        before = path.read_bytes() if path is not None and path.exists() else None
        data = before
    if data is None:
        return None, None
    folder = share_dir() / "backups" / run / "mcp"
    fsutil.make_private_dir(folder)
    target = folder / (f"{provider.label}-{name}.json" if provider.cli == "claude" else f"{provider.label}-{path.name}")
    if not target.exists():
        fsutil.write_private(target, data)
    return target, before


def _put_back(provider: providers.Provider, name: str, before: bytes | dict[str, Any] | None) -> None:
    """Undo a write that failed or read back wrong."""

    path = config_file(provider)
    if provider.cli == "claude":
        _claude(provider, name, before)
    elif before is None:
        if path is not None:
            path.unlink(missing_ok=True)
    else:
        fsutil.write_preserving(path, before)


def _claude(provider: providers.Provider, name: str, entry: dict[str, Any] | None) -> None:
    """`claude mcp remove`, then `add-json --scope user`, with this folder's settings. agt never edits `.claude.json`."""

    from agents_router.config import load_config
    from agents_router.runtimes import get_runtime
    from agents_router.runtimes.claude import _default_dir

    binary = get_runtime("claude").find_binary(load_config().runtime("claude"))
    if not binary:
        raise OSError("the claude command isn't installed, and agt never edits .claude.json itself")
    env = {key: value for key, value in os.environ.items() if key != "CLAUDE_CONFIG_DIR"}
    if not fsutil.same_path(provider.folder, _default_dir()):
        env["CLAUDE_CONFIG_DIR"] = str(provider.folder)

    def run(*args: str) -> subprocess.CompletedProcess:
        try:
            return subprocess.run([*binary, "mcp", *args], capture_output=True, text=True, timeout=60, env=env,
                                  stdin=subprocess.DEVNULL, **fsutil.no_window_kwargs())
        except subprocess.TimeoutExpired as exc:
            raise OSError(f"claude mcp {args[0]} took over {exc.timeout} s") from None

    path = config_file(provider)
    try:
        there = path is not None and path.exists() and name in _raw(provider, path)
    except (OSError, ValueError):
        there = True  # let `claude mcp remove` say
    if there:
        result = run("remove", "--scope", "user", name)
        if result.returncode != 0:
            raise OSError(f"claude mcp remove failed: {(result.stderr or result.stdout).strip()[:300]}")
    if entry is not None:
        result = run("add-json", "--scope", "user", name, json.dumps(entry))
        if result.returncode != 0:
            raise OSError(f"claude mcp add-json failed: {(result.stderr or result.stdout).strip()[:300]}")


def _toml(value: Any) -> str:
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{json.dumps(str(key))} = {_toml(item)}" for key, item in value.items()) + " }"
    return codex_toml.render_value(value)


def _codex(path: Path, name: str, server: dict[str, Any] | None) -> None:
    """Rewrite `[mcp_servers.<name>]` (and its subtables), keeping every other line and the server's other keys."""

    if not CODEX_NAME_RE.match(name):
        raise ValueError(f"Codex can't take the server name {name}")
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    old = (tomllib.loads(text).get("mcp_servers") or {}).get(name) or {}
    kept = levels.split(text, lambda header: header == f"mcp_servers.{name}" or header.startswith(f"mcp_servers.{name}."))[0]
    while kept and not kept[-1].strip():
        kept.pop()
    lines = list(kept)
    if server is not None:
        table = {**{key: value for key, value in old.items() if key not in CODEX_SHAPE_KEYS}, **_to_codex(server)}
        lines += ([""] if lines else []) + [f"[mcp_servers.{name}]"] + [f"{key} = {_toml(value)}" for key, value in table.items()]
    new = "\n".join(lines) + "\n" if lines else ""
    tomllib.loads(new)  # never leave Codex a file it can't read
    if path.exists():
        fsutil.write_preserving(path, new.encode("utf-8"))
    else:
        fsutil.make_private_dir(path.parent)
        fsutil.write_text(path, new)


def _json(path: Path, cli: str, name: str, server: dict[str, Any] | None) -> None:
    data = json.loads(path.read_text(encoding="utf-8") or "{}") if path.exists() else {}
    if not isinstance(data, dict) or not isinstance(data.setdefault("mcpServers", {}), dict):
        raise ValueError(f"{path} isn't a JSON object with an mcpServers object")
    servers = data["mcpServers"]
    if server is None:
        servers.pop(name, None)
    else:
        old = servers.get(name) if isinstance(servers.get(name), dict) else {}
        servers[name] = {**{key: value for key, value in old.items() if key not in SHAPE_KEYS}, **_to_json(server, cli)}
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if path.exists():
        fsutil.write_preserving(path, text.encode("utf-8"))
    else:
        fsutil.make_private_dir(path.parent)
        fsutil.write_text(path, text)


def write(provider: providers.Provider, name: str, server: dict[str, Any] | None) -> None:
    """Put (or with None, take out) one server in a CLI's user settings."""

    path = config_file(provider)
    if provider.cli == "claude":
        _claude(provider, name, server)
    elif provider.cli == "codex":
        _codex(path, name, server)
    else:
        _json(path, provider.cli, name, server)


def _write_one(provider: providers.Provider, name: str, server: dict[str, Any] | None, run: str) -> Written:
    """One write with its backup, read back; a write that fails or reads back wrong is put back."""

    path = config_file(provider)
    action = "remove" if server is None else "update" if name in read(provider)[0] else "copy"
    backup, before = _backup(provider, name, run)
    try:
        write(provider, name, server)
        after = read(provider)[0].get(name)
        if after == server:
            return Written(name, provider.label, path, action, True, backup=backup)
        message = "it read back differently, so agt put back what was there"
    except (OSError, ValueError) as exc:
        message = f"couldn't write it ({exc})"
    try:
        _put_back(provider, name, before)
    except (OSError, ValueError) as exc:
        message += f"; putting back what was there failed too ({exc}), the backup is {backup}"
    return Written(name, provider.label, path, action, False, message, backup)


def apply(plan: Plan, current: State, run: str) -> list[Written]:
    """Write each server where it goes, read it back, and record it. Call it holding the share lock.

    A server's record moves on only when all of its writes worked, so after a failure the next run
    spreads the change again instead of taking it back.
    """

    done: list[Written] = []
    for item in plan.servers:
        decision = item.decision
        record = dict(current.items.get(item.key) or {})
        copies = {label: version_of(server) for label, server in item.copies.items()}
        if item.name in plan.removing:
            # `agt share rm` (every copy) or a pick that leaves CLIs out (the copies agt made there).
            labels = plan.removing[item.name]
            gone = sorted(set(item.copies) if labels is None else labels & set(item.copies))
            results = [_write_one(plan.by_label[label], item.name, None, run) for label in gone]
            done += results
            if labels is None:
                if all(result.ok for result in results):
                    current.items.pop(item.key, None)
                continue
            removed = {result.label for result in results if result.ok}
            for label in removed:
                copies.pop(label, None)
            record["copies"] = {label: version for label, version in record.get("copies", {}).items() if label not in removed}
            record["made"] = sorted(set(record.get("made") or []) - removed)
        if decision is None or item.held:
            if record.get("version"):
                current.items[item.key] = record
            continue
        if decision.action == engine.NOTHING and decision.version:
            record.update(version=decision.version, copies={**record.get("copies", {}), **copies})
        elif decision.action == engine.STOP:
            record["stopped"] = sorted(set(record.get("stopped") or []) | set(decision.stopped))
            record["copies"] = {label: version for label, version in record.get("copies", {}).items()
                                if label not in decision.stopped}
        elif decision.action == engine.WRITE:
            results = [_write_one(plan.by_label[label], item.name, item.source, run)
                       for label in item.writes if label not in plan.later]
            done += results
            if not all(result.ok for result in results):
                continue
            written = {result.label for result in results}
            record.update(version=decision.version,
                          copies={**record.get("copies", {}), **copies, **{label: decision.version for label in written}},
                          made=sorted(set(record.get("made") or []) | (set(decision.fills) & written)))
        if record.get("version"):
            current.items[item.key] = record
    return done


def attention(plan: Plan) -> list[dict[str, Any]]:
    found = []
    for server in plan.conflicts:
        found.append({"id": f"conflict:{server.key}", "kind": "conflict", "item": server.key, "who": "choice",
                      "message": f"MCP server {server.name} differs between {', '.join(server.decision.labels)}",
                      "fix": [f"agt share resolve {server.key} --prefer <label>"]})
    for server in plan.pending:
        found.append({"id": f"delete:{server.key}", "kind": "delete", "item": server.key, "who": "choice",
                      "message": f"MCP server {server.name} was removed in {', '.join(server.decision.labels)}; "
                                 "its other copies wait", "fix": [f"agt share rm {server.key}"]})
    for problem in plan.problems:
        found.append({"id": f"unreadable:mcp:{problem.partition(':')[0]}", "kind": "unreadable", "item": "mcp",
                      "who": "action", "message": f"agt can't read MCP servers in {problem}", "fix": []})
    return found


def watched_files(included: list[providers.Provider]) -> list[Path]:
    return [path for path in (config_file(provider) for provider in included) if path is not None]
