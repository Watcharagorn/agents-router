"""Small filesystem and process helpers shared by every module."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import time
from typing import Any, BinaryIO, Iterator

if os.name == "nt":
    import msvcrt
else:  # pragma: no cover - exercised on POSIX only
    import fcntl


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        return default


def write_private(path: Path, data: bytes) -> None:
    """Write a file atomically with owner-only permissions where the OS supports it."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        for attempt in range(20):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                # Windows refuses to replace a file another process has open.
                if attempt == 19:
                    raise
                time.sleep(0.05)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_json(path: Path, value: Any) -> None:
    write_private(path, (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


def write_text(path: Path, text: str) -> None:
    write_private(path, text.encode("utf-8"))


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def same_path(left: Path | str, right: Path | str) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


@contextlib.contextmanager
def file_lock(path: Path, timeout: float = 10.0) -> Iterator[None]:
    """Hold an exclusive OS lock on `path` for the duration of the block."""

    handle = try_lock(path, timeout=timeout)
    if handle is None:
        raise TimeoutError(f"timed out waiting for lock {path.name}")
    try:
        yield
    finally:
        unlock(handle)


def try_lock(path: Path, timeout: float = 0.0) -> BinaryIO | None:
    """Return an open, locked handle, or None if the lock is held elsewhere."""

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
            return handle
        except OSError:
            handle.close()
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.05)


def unlock(handle: BinaryIO) -> None:
    try:
        handle.seek(0)
        if os.name == "nt":
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:  # pragma: no cover
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        handle.close()


# Links -----------------------------------------------------------------------


def is_link(path: Path) -> bool:
    """True for symlinks and Windows directory junctions."""

    if path.is_symlink():
        return True
    if os.name == "nt":
        try:
            return bool(os.lstat(path).st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT)
        except (OSError, AttributeError):
            return False
    return False


def link_target(path: Path) -> str | None:
    try:
        target = os.readlink(path)
    except OSError:
        return None
    if target.startswith("\\\\?\\"):
        target = target[4:]
    return target


def make_dir_link(source: Path, link: Path) -> str:
    """Link a directory without admin rights. Returns 'junction', 'symlink' or 'copy'."""

    link.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        try:
            import _winapi

            _winapi.CreateJunction(str(source), str(link))
            return "junction"
        except (ImportError, OSError):
            pass
    try:
        os.symlink(source, link, target_is_directory=True)
        return "symlink"
    except OSError:
        shutil.copytree(source, link)
        return "copy"


def remove_link(path: Path) -> None:
    """Remove a symlink or junction without touching what it points to."""

    if not is_link(path):
        raise ValueError(f"refusing to remove non-link {path}")
    if os.name == "nt" and path.is_dir():
        os.rmdir(path)
    else:
        os.unlink(path)


def tree_signature(path: Path) -> str:
    """Cheap change detector for copied directories: file names, sizes and mtimes."""

    digest = hashlib.sha256()
    for item in sorted(path.rglob("*")):
        if item.is_file():
            info = item.stat()
            digest.update(f"{item.relative_to(path)}|{info.st_size}|{int(info.st_mtime)}".encode())
    return digest.hexdigest()


# Processes -------------------------------------------------------------------


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        import ctypes

        process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not process:
            return False
        try:
            code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(process, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(process)
    for process in _SPAWNED:  # pragma: no cover - POSIX zombie reaping
        if process.pid == pid and process.poll() is not None:
            return False
    try:  # pragma: no cover
        finished, _status = os.waitpid(pid, os.WNOHANG)
        if finished == pid:
            return False
    except ChildProcessError:
        pass
    except OSError:
        return False
    try:  # pragma: no cover
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def kill_tree(pid: int) -> None:
    """Terminate a process and all of its children."""

    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            check=False,
            timeout=30,
        )
        return
    try:  # pragma: no cover
        os.killpg(pid, signal.SIGTERM)
        time.sleep(2)
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass


def new_group_kwargs() -> dict[str, Any]:
    """Popen arguments that put the child in its own process group."""

    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}  # pragma: no cover


# Detached children we started. Keeping them referenced lets us reap finished
# ones (no zombies on POSIX) without warnings about still-running processes.
_SPAWNED: list[subprocess.Popen] = []


def spawn_detached(argv: list[str], log_path: Path, env: dict[str, str] | None = None) -> int:
    """Start a background process that outlives its parent. Returns its PID."""

    _SPAWNED[:] = [process for process in _SPAWNED if process.poll() is None]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("ab")
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": log,
        "stderr": log,
        "close_fds": True,
        "env": env,
    }
    if os.name == "nt":
        base = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NO_WINDOW
        )
        try:
            # Leave the host's job object so closing the host doesn't kill running jobs.
            process = subprocess.Popen(argv, creationflags=base | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kwargs)
        except OSError:
            process = subprocess.Popen(argv, creationflags=base, **kwargs)
    else:  # pragma: no cover
        process = subprocess.Popen(argv, start_new_session=True, **kwargs)
    log.close()
    _SPAWNED.append(process)
    return process.pid


def python_executable() -> str:
    """The interpreter to use for background runners (pythonw-safe)."""

    executable = Path(sys.executable)
    if executable.name.lower() == "pythonw.exe":
        candidate = executable.with_name("python.exe")
        if candidate.exists():
            return str(candidate)
    return str(executable)
