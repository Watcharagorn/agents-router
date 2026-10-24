"""Per-job decision cache and decision log, safe for hook processes running in parallel."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import secrets
import time
from typing import Any, Iterator

if os.name == "nt":
    import msvcrt
else:  # pragma: no cover - POSIX only
    import fcntl

# Arguments that only describe the call for the UI; they don't change what it does.
VOLATILE_ARGS = {"toolaction", "toolsummary", "waitmsbeforeasync", "isdaemon"}
MAX_CACHE_ENTRIES = 500


def action_key(name: str, args: dict[str, Any]) -> str:
    stable = {key: value for key, value in args.items() if key.lower() not in VOLATILE_ARGS}
    text = json.dumps([name, stable], sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@contextlib.contextmanager
def locked(path: Path, timeout: float = 10.0) -> Iterator[None]:
    """An exclusive lock on `path` (a lock file next to the data)."""

    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        handle = path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            handle.close()
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for {path.name}")
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def cache_get(path: Path, key: str) -> dict[str, Any] | None:
    entry = _read(path).get(key)
    return entry if isinstance(entry, dict) and entry.get("decision") in ("approve", "deny") else None


def cache_put(path: Path, key: str, value: dict[str, Any]) -> None:
    with locked(path.with_name(path.name + ".lock")):
        data = _read(path)
        data[key] = {**value, "time": time.time()}
        if len(data) > MAX_CACHE_ENTRIES:
            for old in sorted(data, key=lambda item: data[item].get("time", 0))[: len(data) - MAX_CACHE_ENTRIES]:
                del data[old]
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
        temporary.write_text(json.dumps(data), encoding="utf-8")
        try:
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()


def log_append(path: Path, record: dict[str, Any]) -> None:
    with locked(path.with_name(path.name + ".lock")):
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_log(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    records = []
    for line in lines:
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records
