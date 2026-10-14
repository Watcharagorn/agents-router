"""Safely enforce bridge-owned Antigravity CLI settings."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import time
from typing import BinaryIO, Iterator

if os.name == "nt":
    import msvcrt
else:  # pragma: no cover - the supported installation target is Windows.
    import fcntl


MAX_SETTINGS_BYTES = 1024 * 1024


def _try_lock(handle: BinaryIO) -> bool:
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:  # pragma: no cover - the supported installation target is Windows.
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:  # pragma: no cover - the supported installation target is Windows.
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _settings_lock(settings_path: Path, timeout_seconds: float = 10.0) -> Iterator[None]:
    lock_path = settings_path.with_name(settings_path.name + ".codex-bridge.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    deadline = time.monotonic() + timeout_seconds
    try:
        while not _try_lock(handle):
            if time.monotonic() >= deadline:
                raise TimeoutError("timed out waiting for Antigravity settings lock")
            time.sleep(0.05)
        try:
            yield
        finally:
            _unlock(handle)
    finally:
        handle.close()


def _normalized_path(value: str) -> str:
    return os.path.normcase(str(Path(value).expanduser().resolve()))


def enforce_bridge_settings(settings_path: Path, workspace_root: Path) -> dict[str, bool]:
    """Keep credit overages off and trust exactly the selected Codex workspace."""

    settings_path = Path(os.path.abspath(settings_path.expanduser()))
    workspace_root = workspace_root.expanduser().resolve(strict=True)
    if not workspace_root.is_dir():
        raise ValueError("workspace root must be an existing directory")
    with _settings_lock(settings_path):
        if settings_path.is_symlink():
            raise ValueError("Antigravity settings file cannot be a symlink")
        if settings_path.exists():
            if settings_path.stat().st_size > MAX_SETTINGS_BYTES:
                raise ValueError("Antigravity settings file exceeds the size limit")
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        else:
            settings = {}
        if not isinstance(settings, dict):
            raise ValueError("Antigravity settings must be a JSON object")
        trusted = settings.get("trustedWorkspaces", [])
        if not isinstance(trusted, list) or not all(isinstance(item, str) for item in trusted):
            raise ValueError("trustedWorkspaces must be a list of strings")
        permissions = settings.get("permissions", {})
        if not isinstance(permissions, dict):
            raise ValueError("permissions must be a JSON object")
        allow = permissions.get("allow", [])
        if not isinstance(allow, list) or not all(isinstance(item, str) for item in allow):
            raise ValueError("permissions.allow must be a list of strings")

        changed_overages = settings.get("useG1Credits") is not False
        settings["useG1Credits"] = False
        normalized_existing = {_normalized_path(item) for item in trusted if item.strip()}
        changed_trust = _normalized_path(str(workspace_root)) not in normalized_existing
        if changed_trust:
            trusted.append(str(workspace_root))
        settings["trustedWorkspaces"] = trusted
        workspace_text = workspace_root.as_posix()
        if ")" in workspace_text:
            raise ValueError("workspace path cannot contain ')' in an Antigravity permission rule")
        workspace_write_rule = f"write_file({workspace_text})"
        changed_workspace_write = workspace_write_rule not in allow
        if changed_workspace_write:
            allow.append(workspace_write_rule)
        permissions["allow"] = allow
        settings["permissions"] = permissions

        if changed_overages or changed_trust or changed_workspace_write or not settings_path.exists():
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = settings_path.with_name(
                f".{settings_path.name}.{secrets.token_hex(8)}.tmp"
            )
            try:
                temporary.write_text(
                    json.dumps(settings, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                os.replace(temporary, settings_path)
            finally:
                if temporary.exists():
                    temporary.unlink()
        return {
            "overages_changed": changed_overages,
            "workspace_added": changed_trust,
            "workspace_write_added": changed_workspace_write,
        }
