"""One-use approvals for commands reviewed by the Antigravity hook."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
from typing import Any, BinaryIO

if os.name == "nt":
    import msvcrt
else:  # pragma: no cover - the supported installation target is Windows.
    import fcntl


MAX_REQUEST_BYTES = 64 * 1024
MAX_COMMAND_CHARS = 32_000
MAX_TIMEOUT_SECONDS = 1_800
APPROVAL_TTL_SECONDS = 60
APPROVAL_ID_RE = re.compile(r"^[0-9a-f]{32}$")
RUNTIME_SECRET_RE = re.compile(r"^[0-9a-f]{64}$")
TOKEN_POOL_SIZE_LIMIT = 32
_TOKEN_CONDITION = threading.Condition()
_AVAILABLE_TOKENS: list[str] | None = None


def _token_lock_path(token: str) -> Path:
    lock_dir = state_root() / "token-leases"
    lock_dir.mkdir(parents=True, exist_ok=True)
    return lock_dir / f"{validate_approval_id(token)}.lock"


def _try_lock_token(token: str) -> BinaryIO | None:
    """Acquire a non-blocking OS lock so separate MCP processes cannot collide."""

    handle = _token_lock_path(token).open("a+b")
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
        return handle
    except OSError:
        handle.close()
        return None


def _unlock_token(handle: BinaryIO) -> None:
    try:
        handle.seek(0)
        if os.name == "nt":
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:  # pragma: no cover - the supported installation target is Windows.
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def state_root() -> Path:
    local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return local / "Codex" / "antigravity-bridge"


def default_secret_path() -> Path:
    return Path(
        os.environ.get(
            "ANTIGRAVITY_BRIDGE_BROKER_SECRET",
            state_root() / "broker-secret.bin",
        )
    ).resolve()


def default_approval_dir() -> Path:
    return Path(
        os.environ.get(
            "ANTIGRAVITY_BRIDGE_APPROVALS_DIR",
            state_root() / "approvals",
        )
    ).resolve()


def default_tokens_path() -> Path:
    return Path(
        os.environ.get(
            "ANTIGRAVITY_BRIDGE_BROKER_TOKENS",
            state_root() / "broker-tokens.json",
        )
    ).resolve()


def default_runtime_secret_path() -> Path:
    return Path(
        os.environ.get(
            "ANTIGRAVITY_BRIDGE_RUNTIME_SECRET_FILE",
            state_root() / "runtime-secret.txt",
        )
    ).resolve()


def load_broker_tokens(path: Path | None = None) -> list[str]:
    value = json.loads((path or default_tokens_path()).read_text(encoding="utf-8"))
    if not isinstance(value, list) or not 1 <= len(value) <= TOKEN_POOL_SIZE_LIMIT:
        raise ValueError("broker token pool must contain between 1 and 32 tokens")
    tokens = [validate_approval_id(item) for item in value]
    if len(tokens) != len(set(tokens)):
        raise ValueError("broker token pool contains duplicates")
    return tokens


class BrokerTokenLease:
    def __init__(self, timeout_seconds: float = 30.0):
        self.timeout_seconds = timeout_seconds
        self.token: str | None = None
        self._lock_handle: BinaryIO | None = None

    def __enter__(self) -> str:
        global _AVAILABLE_TOKENS
        deadline = time.monotonic() + self.timeout_seconds
        with _TOKEN_CONDITION:
            if _AVAILABLE_TOKENS is None:
                _AVAILABLE_TOKENS = load_broker_tokens()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("timed out waiting for a reviewed-command broker token")
                for _attempt in range(len(_AVAILABLE_TOKENS)):
                    token = _AVAILABLE_TOKENS.pop()
                    lock_handle = _try_lock_token(token)
                    if lock_handle is not None:
                        self.token = token
                        self._lock_handle = lock_handle
                        return token
                    _AVAILABLE_TOKENS.insert(0, token)
                # A different bridge process may hold every token, so wake
                # periodically instead of relying only on this process's condition.
                _TOKEN_CONDITION.wait(min(remaining, 0.1))

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self.token is None:
            return
        with _TOKEN_CONDITION:
            assert _AVAILABLE_TOKENS is not None
            assert self._lock_handle is not None
            _unlock_token(self._lock_handle)
            self._lock_handle = None
            _AVAILABLE_TOKENS.append(self.token)
            self.token = None
            _TOKEN_CONDITION.notify()


def is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def read_request_bytes(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError("command request must be a regular non-symlink file")
    size = path.stat().st_size
    if size <= 0 or size > MAX_REQUEST_BYTES:
        raise ValueError("command request is empty or oversized")
    return path.read_bytes()


def parse_request(
    raw: bytes, *, project_root: Path, request_dir: Path
) -> dict[str, Any]:
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict) or set(value) != {
        "cwd",
        "command",
        "timeout_seconds",
    }:
        raise ValueError("command request has the wrong fields")
    cwd_value = value["cwd"]
    command = value["command"]
    timeout = value["timeout_seconds"]
    if not isinstance(cwd_value, str) or not cwd_value:
        raise ValueError("command request cwd is invalid")
    cwd = Path(cwd_value).resolve(strict=True)
    if not cwd.is_dir() or not is_inside(cwd, project_root):
        raise ValueError("command request cwd is outside the active project")
    if not isinstance(command, str) or not command.strip():
        raise ValueError("command request command is invalid")
    if len(command) > MAX_COMMAND_CHARS or "\x00" in command:
        raise ValueError("command request command is oversized or contains NUL")
    if not isinstance(timeout, int) or isinstance(timeout, bool):
        raise ValueError("command request timeout is invalid")
    if timeout < 1 or timeout > MAX_TIMEOUT_SECONDS:
        raise ValueError("command request timeout is outside the allowed range")
    return {
        "cwd": str(cwd),
        "command": command,
        "timeout_seconds": timeout,
        "project_root": str(project_root.resolve(strict=True)),
        "request_dir": str(request_dir.resolve(strict=True)),
    }


def request_digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def load_secret(path: Path) -> bytes:
    secret = path.read_bytes()
    if len(secret) < 32:
        raise ValueError("broker secret is missing or too short")
    return secret


def runtime_secret_hash() -> str:
    secret = os.environ.get("ANTIGRAVITY_BRIDGE_RUNTIME_SECRET", "")
    if not RUNTIME_SECRET_RE.fullmatch(secret):
        raise ValueError("bridge runtime secret is missing or invalid")
    return hashlib.sha256(secret.encode("ascii")).hexdigest()


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def validate_approval_id(value: str) -> str:
    if not isinstance(value, str) or not APPROVAL_ID_RE.fullmatch(value):
        raise ValueError("approval id must be exactly 32 lowercase hexadecimal characters")
    return value


def _approval_path(approval_dir: Path, approval_id: str) -> Path:
    return approval_dir / f"{validate_approval_id(approval_id)}.json"


def issue_approval(
    *,
    approval_id: str,
    raw_request: bytes,
    parsed_request: dict[str, Any],
    secret_path: Path,
    approval_dir: Path,
) -> Path:
    secret = load_secret(secret_path)
    digest = request_digest(raw_request)
    payload: dict[str, Any] = {
        "version": 1,
        "approval_id": validate_approval_id(approval_id),
        "request_sha256": digest,
        "project_root": parsed_request["project_root"],
        "request_dir": parsed_request["request_dir"],
        "runtime_sha256": runtime_secret_hash(),
        "expires_at_ns": time.time_ns() + APPROVAL_TTL_SECONDS * 1_000_000_000,
        "nonce": secrets.token_hex(16),
    }
    payload["hmac_sha256"] = hmac.new(
        secret, _canonical(payload), hashlib.sha256
    ).hexdigest()
    approval_dir.mkdir(parents=True, exist_ok=True)
    final = _approval_path(approval_dir, approval_id)
    temporary = approval_dir / f".{approval_id}.{secrets.token_hex(8)}.tmp"
    temporary.write_text(_canonical(payload).decode("utf-8"), encoding="utf-8")
    os.replace(temporary, final)
    return final


def consume_approval(
    *, approval_id: str, secret_path: Path, approval_dir: Path
) -> dict[str, Any]:
    approval_id = validate_approval_id(approval_id)
    path = _approval_path(approval_dir, approval_id)
    raw_record = path.read_bytes()
    record = json.loads(raw_record.decode("utf-8"))
    expected = {
        "version",
        "approval_id",
        "request_sha256",
        "project_root",
        "request_dir",
        "runtime_sha256",
        "expires_at_ns",
        "nonce",
        "hmac_sha256",
    }
    if not isinstance(record, dict) or set(record) != expected:
        raise ValueError("approval record has the wrong fields")
    supplied_mac = record.pop("hmac_sha256")
    if not isinstance(supplied_mac, str):
        raise ValueError("approval signature is invalid")
    expected_mac = hmac.new(
        load_secret(secret_path), _canonical(record), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(supplied_mac, expected_mac):
        raise ValueError("approval signature mismatch")
    if not hmac.compare_digest(
        str(record.get("runtime_sha256", "")), runtime_secret_hash()
    ):
        raise ValueError("approval belongs to a different bridge runtime")
    if record.get("version") != 1 or record.get("approval_id") != approval_id:
        raise ValueError("approval id mismatch")
    digest = record.get("request_sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("approval request digest is invalid")
    expires = record.get("expires_at_ns")
    if not isinstance(expires, int) or time.time_ns() > expires:
        raise ValueError("approval has expired")
    # Consume only after signature/runtime validation. A process outside the
    # Codex MCP runtime cannot steal an approval merely by guessing its token.
    path.unlink()
    return record
