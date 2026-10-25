"""agt share's state: what each item looked like at the last sync.

It lives in `<agt home>/state/share/state.json`. The loader is strict: a
missing file is a first run, but a file that can't be read, or one written by
a newer agt (a higher `schema`), stops the sync instead of looking like a
first run, which would refill or re-copy everything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

from agents_router.config import state_dir
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
        if not record:
            return None
        return engine.Last(
            version=record["version"], copies=dict(record.get("copies") or {}), made=set(record.get("made") or []),
            stopped=set(record.get("stopped") or []), released=set(record.get("released") or []),
        )


def path() -> Path:
    return state_dir() / "share" / "state.json"


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
