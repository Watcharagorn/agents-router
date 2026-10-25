"""agt share's state: what each item looked like at the last sync.

It lives in `<agt home>/state/share/state.json`. The loader is strict: a
missing file is a first run, but a file that can't be read, or one written by
a newer agt (a higher `schema`), stops the sync instead of looking like a
first run, which would refill or re-copy everything. Each save keeps the
previous file as `state.json.prev`.

A record holds the item's version at the last sync, each copy's own version
then (`copies`), and the folders where agt made the copy (`made`). A record
with only `made` belongs to an item whose first sync didn't finish.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

from agents_router import fsutil
from agents_router.share import engine
from agents_router.share.normalize import NORM_VERSION

SCHEMA = 1


class StateError(RuntimeError):
    """The state can't be used; `agt doctor --fix` restores the last good copy."""


@dataclass
class State:
    schema: int = SCHEMA
    norm_version: int = NORM_VERSION
    items: dict[str, dict[str, Any]] = field(default_factory=dict)  # "<kind>/<identity>" -> record
    first_run: bool = True

    def last(self, kind: str, identity: str) -> engine.Last | None:
        record = self.items.get(f"{kind}/{identity}")
        if not record or not record.get("version"):
            return None
        return engine.Last(
            version=record["version"], copies=dict(record.get("copies") or {}), made=set(record.get("made") or []),
            stopped=set(record.get("stopped") or []), released=set(record.get("released") or []),
        )


def share_dir() -> Path:
    """This scope's state folder: `state/share` for your user folders, `state/share/projects/<key>` for a repo."""

    from agents_router.share import scope

    return scope.folder()


def path() -> Path:
    return share_dir() / "state.json"


def save(current: State, location: Path | None = None, *, rotate: bool = False) -> None:
    """Write the state atomically. With `rotate`, the file it replaces is kept as `state.json.prev` first."""

    location = location or path()
    if rotate and location.exists():
        fsutil.write_private(location.with_name(location.name + ".prev"), location.read_bytes())
    current.first_run = False
    fsutil.write_json(location, {"schema": SCHEMA, "norm_version": current.norm_version, "items": current.items})


def load(location: Path | None = None) -> State:
    location = location or path()
    try:
        raw = location.read_text(encoding="utf-8")
    except FileNotFoundError:
        return State()
    except OSError as exc:
        raise StateError(f"agt share's state {location} can't be read ({exc}); `agt doctor --fix` restores it") from None
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise StateError(f"agt share's state {location} is damaged ({exc}); `agt doctor --fix` restores it") from None
    if not isinstance(data, dict) or not isinstance(data.get("items", {}), dict):
        raise StateError(f"agt share's state {location} is damaged; `agt doctor --fix` restores it")
    schema = data.get("schema")
    if not isinstance(schema, int) or schema > SCHEMA:
        raise StateError(f"agt share's state {location} was written by a newer agt (schema {schema}); update agt first")
    return State(schema=schema, norm_version=int(data.get("norm_version") or NORM_VERSION),
                 items=data.get("items") or {}, first_run=False)
