"""agt share's saved answers: the `[share]` tables of your user settings file.

The file is `$AGT_CONFIG_DIR/settings.toml`; with only `AGT_HOME` set it's
`<AGT_HOME>/settings.toml`, and otherwise `~/.agt/settings.toml` (see
levels.py). The rest of agt reads the same file. Saving rewrites the `[share]`
tables and keeps every other line of the file. A workspace's own `[share]`
comes with sharing a repo's items.

    [share]
    enabled = true
    kinds = ["agents", "skills"]
    tool_widening = "allow-if-shell"
    auto_copy = true                      # or false, or a list of kinds

    [[share.folders]]                     # the folders sharing, with their labels
    cli = "claude"
    label = "claude-lab"
    path = "/Users/you/.claude-lab"

    [share.targets]                       # your picks per item
    "skills/gamma-apikey" = "off"         # not shared: every copy stays as it is
    "agents/dana" = { not_in = ["codex"] }
    "agents/explorer" = { only = ["kiro"] }
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import tomllib
from typing import Any

from agents_router import config, levels
from agents_router.share import codex_toml, model, toolmap

KINDS = ("agents", "skills")


class SettingsError(levels.SettingsError):
    """The `[share]` settings can't be used; the message names the file and what's wrong."""


@dataclass
class Folder:
    cli: str
    path: Path
    label: str


@dataclass
class Settings:
    enabled: bool = False
    kinds: tuple[str, ...] = KINDS
    tool_widening: str = "allow-if-shell"
    auto_copy: tuple[str, ...] = KINDS  # the kinds whose new items go to every target
    folders: list[Folder] = field(default_factory=list)
    off: set[str] = field(default_factory=set)  # "<kind>/<name>" items that aren't shared
    not_in: dict[str, list[str]] = field(default_factory=dict)  # "<kind>/<name>" -> labels you unticked
    only: dict[str, list[str]] = field(default_factory=dict)  # "<kind>/<name>" -> the only labels it goes to


def path() -> Path:
    return config.user_settings_path()


def parse_auto_copy(value: Any) -> tuple[str, ...]:
    """`on`, `off`, true, false, or a list of kinds (a comma list on the command line)."""

    if value in (True, "on", "true"):
        return KINDS
    if value in (False, "off", "false"):
        return ()
    kinds = [kind.strip() for kind in value.split(",")] if isinstance(value, str) else value
    if not isinstance(kinds, list) or not all(isinstance(kind, str) and kind in KINDS for kind in kinds):
        raise ValueError(f"auto_copy takes on, off or a list of {', '.join(KINDS)}, not {value!r}")
    return tuple(dict.fromkeys(kinds))


def load(location: Path | None = None) -> Settings:
    location = location or path()
    try:
        text = location.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Settings()
    except OSError as exc:
        raise SettingsError(f"{location} can't be read ({exc})") from None
    try:
        data = tomllib.loads(text).get("share", {})
    except tomllib.TOMLDecodeError as exc:
        raise SettingsError(f"{location}: {exc}") from None
    return parse(data, location)


def parse(data: Any, location: Path) -> Settings:
    """A `[share]` table's settings; a bad value names the file."""

    try:
        return _settings(data)
    except (TypeError, ValueError, KeyError) as exc:
        raise SettingsError(f"{location}: [share] {exc}") from None


def _labels(key: str, value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(label, str) for label in value):
        raise ValueError(f"targets.{json.dumps(key)} needs a list of folder labels")
    return list(value)


def _settings(data: Any) -> Settings:
    if not isinstance(data, dict):
        raise ValueError("must be a table")
    known = {"enabled", "kinds", "tool_widening", "auto_copy", "folders", "targets"}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ValueError(f"unknown key {unknown[0]}")
    enabled = data.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be true or false")
    kinds = data.get("kinds", list(KINDS))
    if not isinstance(kinds, list) or not kinds or not all(kind in KINDS for kind in kinds):
        raise ValueError(f"kinds must be a list of {', '.join(KINDS)}")
    widening = data.get("tool_widening", "allow-if-shell")
    if widening not in toolmap.TOOL_WIDENING:
        raise ValueError(f"tool_widening must be {' or '.join(toolmap.TOOL_WIDENING)}")
    folders = []
    for entry in data.get("folders", []):
        if not isinstance(entry, dict) or entry.get("cli") not in model.CLIS or not isinstance(entry.get("path"), str) \
                or not isinstance(entry.get("label"), str):
            raise ValueError("each [[share.folders]] needs cli, path and label")
        folders.append(Folder(entry["cli"], Path(entry["path"]), entry["label"]))
    result = Settings(enabled=enabled, kinds=tuple(kinds), tool_widening=widening,
                      auto_copy=parse_auto_copy(data.get("auto_copy", True)), folders=folders)
    targets = data.get("targets", {})
    if not isinstance(targets, dict):
        raise ValueError("targets must be a table")
    for key, value in targets.items():
        kind, _, name = key.partition("/")
        if kind not in KINDS or not name:
            raise ValueError(f"targets: {json.dumps(key)} isn't <kind>/<name>")
        if value == "off":
            result.off.add(key)
        elif isinstance(value, dict) and value and set(value) <= {"not_in", "only"}:
            if "not_in" in value:
                result.not_in[key] = _labels(key, value["not_in"])
            if "only" in value:
                result.only[key] = _labels(key, value["only"])
        else:
            raise ValueError(f"targets.{json.dumps(key)} must be \"off\" or {{ not_in = [...] }} / {{ only = [...] }}")
    return result


def render(settings: Settings) -> str:
    value = codex_toml.render_value
    auto = True if settings.auto_copy == KINDS else False if not settings.auto_copy else list(settings.auto_copy)
    lines = ["[share]", f"enabled = {value(settings.enabled)}", f"kinds = {value(list(settings.kinds))}",
             f"tool_widening = {value(settings.tool_widening)}", f"auto_copy = {value(auto)}"]
    for folder in settings.folders:
        lines += ["", "[[share.folders]]", f"cli = {value(folder.cli)}", f"label = {value(folder.label)}",
                  f"path = {value(str(folder.path))}"]
    keys = sorted(settings.off | set(settings.not_in) | set(settings.only))
    if keys:
        lines += ["", "[share.targets]"]
        for key in keys:
            if key in settings.off:
                lines.append(f"{json.dumps(key)} = \"off\"")
                continue
            parts = [f"{field_name} = {value(sorted(chosen[key]))}" for field_name, chosen in
                     (("not_in", settings.not_in), ("only", settings.only)) if key in chosen]
            lines.append(f"{json.dumps(key)} = {{ {', '.join(parts)} }}")
    return "\n".join(lines) + "\n"


def save(settings: Settings, location: Path | None = None) -> Path:
    """Replace the `[share]` tables in the settings file, keeping every other line."""

    from agents_router import synclog

    location = location or path()
    synclog.notice_file(location, "user", None)
    written = levels.replace_tables(location, lambda header: header.split(".")[0] == "share", render(settings))
    synclog.saved(written)
    return written
