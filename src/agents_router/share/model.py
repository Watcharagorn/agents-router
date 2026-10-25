"""Each CLI's agent files as the same core fields, and skill folders.

| CLI | Agent file | Body | Tools |
|---|---|---|---|
| claude | `agents/<name>.md`, YAML front matter | text after the front matter | `tools`, a comma list |
| codex | `agents/<name>.toml` | `developer_instructions` | none: Codex can't limit tools |
| antigravity | `config/agents/<name>.md` or `<name>/agent.md` | text after the front matter | `tools`, a list |
| kiro | `agents/<name>.json` | `prompt` | `tools`, a list |

An agent's identity is its file name (the folder name for `<name>/agent.md`);
the `name` inside the file must match it. Only `name`, `description` and the
body are synced. `model` never is.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
from typing import Any

from agents_router.share import codex_toml, frontmatter, kiro_json, normalize, toolmap

CLIS = ("claude", "codex", "antigravity", "kiro")
SUFFIX = {"claude": ".md", "codex": ".toml", "antigravity": ".md", "kiro": ".json"}
AGY_NESTED = "agent.md"


class ItemError(ValueError):
    """A copy agt can't parse, such as a half-saved edit. It is never written over."""

    def __init__(self, path: Path, message: str):
        super().__init__(f"{path}: {message}")
        self.path = path


@dataclass
class Agent:
    cli: str
    name: str
    path: Path
    description: str
    body: str
    tools: list[str] | None
    declared_name: str | None
    data: dict[str, Any]

    @property
    def core(self) -> dict[str, str]:
        return normalize.agent_core(self.name, self.description, self.body)

    @property
    def digest(self) -> str:
        return normalize.digest(self.core)


def agent_files(cli: str, folder: Path) -> list[Path]:
    """Files in an agents folder that are agents: top-level files only (a `templates/` folder isn't one)."""

    if not folder.is_dir():
        return []
    found = [path for path in folder.iterdir() if path.suffix == SUFFIX[cli] and path.is_file()]
    if cli == "antigravity":
        found += [path / AGY_NESTED for path in folder.iterdir() if path.is_dir() and (path / AGY_NESTED).is_file()]
    return sorted(found)


def agent_name(cli: str, path: Path) -> str:
    return path.parent.name if cli == "antigravity" and path.name == AGY_NESTED else path.stem


def new_agent_path(cli: str, folder: Path, name: str) -> Path:
    return folder / f"{name}{SUFFIX[cli]}"


def read_agent(cli: str, path: Path) -> Agent:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ItemError(path, f"can't be read ({exc})") from None
    return parse_agent(cli, path, text)


def parse_agent(cli: str, path: Path, text: str) -> Agent:
    name = agent_name(cli, path)
    try:
        if cli in ("claude", "antigravity"):
            document = frontmatter.parse(text)
            data = document.fields
            body = document.body
            tools = toolmap.parse_tools(data.get("tools")) if "tools" in data else None
        elif cli == "codex":
            data = codex_toml.read(text)
            body = str(data.get("developer_instructions") or "")
            tools = None
        elif cli == "kiro":
            data = kiro_json.read(text)
            body = str(data.get("prompt") or "")
            tools = [str(tool) for tool in data["tools"]] if isinstance(data.get("tools"), list) else None
        else:
            raise ValueError(f"unknown CLI {cli}")
    except (ValueError, KeyError) as exc:  # tomllib, json and front matter errors are ValueErrors
        raise ItemError(path, str(exc)) from None
    declared = data.get("name")
    return Agent(cli=cli, name=name, path=path, description=str(data.get("description") or ""), body=body,
                 tools=tools, declared_name=str(declared) if declared is not None else None, data=data)


def render_agent(cli: str, core: dict[str, str], tools: list[str] | None = None) -> str:
    """A new copy holding only the core fields, plus the tools the target can use."""

    name, description, body = core["name"], core["description"], core["body"]
    if cli == "claude":
        fields: dict[str, Any] = {"name": name, "description": description}
        if tools is not None:
            fields["tools"] = ", ".join(tools)
        return frontmatter.render(fields, f"\n{body}\n")
    if cli == "antigravity":
        fields = {"name": name, "description": description}
        if tools is not None:
            fields["tools"] = tools
        fields["mainAgent"] = False
        return frontmatter.render(fields, f"\n{body}\n")
    if cli == "codex":
        return codex_toml.render({"name": name, "description": description, "developer_instructions": f"{body}\n"})
    if cli == "kiro":
        fields = {"name": name, "description": description, "prompt": body}
        if tools is not None:
            fields["tools"] = tools
        return kiro_json.render(fields)
    raise ValueError(f"unknown CLI {cli}")


def update_agent(cli: str, text: str, current: Agent, core: dict[str, str]) -> str:
    """The copy's text with only the core fields that differ changed; everything else stays."""

    changes: dict[str, Any] = {}
    if current.declared_name != core["name"]:
        changes["name"] = core["name"]
    if normalize.description(current.description) != core["description"]:
        changes["description"] = core["description"]
    body_changed = normalize.text(current.body) != core["body"]
    if cli in ("claude", "antigravity"):
        return frontmatter.update(text, changes, body=f"\n{core['body']}\n" if body_changed else None) if changes or body_changed else text
    if body_changed:
        changes["developer_instructions" if cli == "codex" else "prompt"] = core["body"] + ("\n" if cli == "codex" else "")
    if not changes:
        return text
    return codex_toml.update(text, changes) if cli == "codex" else kiro_json.update(text, changes)


# Skills ------------------------------------------------------------------


@dataclass
class Skill:
    name: str
    path: Path  # the entry in the skills folder, possibly a link
    real: Path
    declared_name: str | None
    description: str
    digest: str


def read_skill(path: Path) -> Skill:
    real = path.resolve()
    manifest = real / "SKILL.md"
    try:
        text = manifest.read_text(encoding="utf-8")
        fields = frontmatter.parse(text).fields
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ItemError(manifest, str(exc)) from None
    declared = fields.get("name")
    return Skill(name=path.name, path=path, real=real, declared_name=str(declared) if declared is not None else None,
                 description=str(fields.get("description") or ""), digest=skill_digest(real, text))


def skill_digest(real: Path, manifest_text: str | None = None) -> str:
    """The normalized SKILL.md plus a sorted list of (path, sha256) for the skill's other files.

    Hidden files (`.DS_Store` and the like) are left out, so Finder can't make a skill look edited.
    """

    if manifest_text is None:
        manifest_text = (real / "SKILL.md").read_text(encoding="utf-8")
    files = []
    for root, folders, names in os.walk(real):
        folders[:] = sorted(folder for folder in folders if not folder.startswith("."))
        for name in sorted(names):
            if name.startswith("."):
                continue
            path = Path(root) / name
            relative = path.relative_to(real).as_posix()
            if relative == "SKILL.md":
                continue
            try:
                files.append((relative, hashlib.sha256(path.read_bytes()).hexdigest()))
            except OSError:
                files.append((relative, "unreadable"))
    return normalize.digest({"skill": normalize.text(manifest_text), "files": files})
