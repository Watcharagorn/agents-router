#!/usr/bin/env python
"""Run Antigravity CLI print mode through a Windows PTY.

The Windows agy.exe currently drops stdout when it is launched from a
non-interactive subprocess. Running it inside ConPTY makes agy behave as if it
has a real console, while this wrapper still returns capturable text.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time


ANSI_RE = re.compile(
    r"\x1b(?:\][^\x07]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[@-Z\\-_])"
)
DEFAULT_PTY_ROWS = 40
DEFAULT_PTY_COLS = 240


def clean_terminal_output(text: str) -> str:
    text = ANSI_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.strip()


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, ""))
    except ValueError:
        return default
    return min(max(value, minimum), maximum)


def _pty_dimensions() -> tuple[int, int]:
    rows = _env_int("AGY_PTY_ROWS", DEFAULT_PTY_ROWS, 20, 200)
    cols = _env_int("AGY_PTY_COLS", DEFAULT_PTY_COLS, 80, 1000)
    return rows, cols


def run_conpty(argv: list[str], cwd: str, timeout_seconds: float) -> tuple[int | None, str]:
    try:
        from winpty import PtyProcess
    except ImportError as exc:  # pragma: no cover - exercised by the CLI user.
        raise RuntimeError(
            "Missing dependency: pywinpty. Install it with "
            "`python -m pip install --user pywinpty`."
        ) from exc

    proc = PtyProcess.spawn(argv, cwd=cwd, dimensions=_pty_dimensions())
    chunks: list[str] = []
    errors: list[str] = []

    def reader() -> None:
        while True:
            try:
                chunk = proc.read(4096)
            except EOFError:
                break
            except Exception as exc:  # pragma: no cover - defensive for PTY backend errors.
                errors.append(f"[pty read error: {exc!r}]")
                break
            if chunk:
                chunks.append(chunk)
            if not proc.isalive():
                # Try one more read on the next loop; ConPTY may still have buffered bytes.
                continue

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()

    deadline = time.monotonic() + timeout_seconds
    while thread.is_alive() and time.monotonic() < deadline:
        thread.join(0.1)

    timed_out = thread.is_alive()
    if timed_out:
        with contextlib.suppress(Exception):
            proc.terminate()
        time.sleep(0.5)
        if proc.isalive():
            with contextlib.suppress(Exception):
                proc.kill()
        thread.join(2)

    if proc.isalive():
        with contextlib.suppress(Exception):
            proc.wait()

    output = "".join(chunks + errors)
    if timed_out:
        suffix = f"[pty timeout after {timeout_seconds}s]"
        output = "\n".join(part for part in (output, suffix) if part)
        return 124, output
    return proc.exitstatus, output


def run_subprocess(argv: list[str], cwd: str, timeout_seconds: float) -> tuple[int | None, str]:
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
        return completed.returncode, (completed.stdout or "") + (completed.stderr or "")
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + (exc.stderr or "")
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        return None, output + f"\n[subprocess timeout after {timeout_seconds}s]"
    except OSError as exc:
        return None, f"{type(exc).__name__}: {exc}"


def run_pty(argv: list[str], cwd: str, timeout_seconds: float) -> tuple[int | None, str]:
    if os.name == "nt":
        try:
            return run_conpty(argv, cwd, timeout_seconds)
        except Exception as exc:  # Fallback keeps MCP diagnostics alive if pywinpty breaks.
            exitstatus, output = run_subprocess(argv, cwd, timeout_seconds)
            diagnostic = f"[pty unavailable: {type(exc).__name__}: {exc}]"
            return exitstatus, "\n".join(part for part in [diagnostic, output] if part)
    return run_subprocess(argv, cwd, timeout_seconds)


def supports_model_flag(agy: str) -> bool:
    try:
        completed = subprocess.run(
            [agy, "--help"],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "--model" in ((completed.stdout or "") + (completed.stderr or ""))


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run `agy --print` through ConPTY and capture its output."
    )
    parser.add_argument("prompt", nargs="?", help="Prompt to send to Antigravity.")
    parser.add_argument("--prompt-file", help="Read the prompt from a UTF-8 text file.")
    parser.add_argument("--cwd", default=os.getcwd(), help="Working directory for agy.")
    parser.add_argument("--agy", default="agy", help="Path to agy executable.")
    parser.add_argument("--model", help="Temporarily set Antigravity model label.")
    parser.add_argument("--timeout", default="5m0s", help="Antigravity print timeout.")
    parser.add_argument(
        "--wall-timeout",
        type=float,
        default=360.0,
        help="Wrapper timeout in seconds.",
    )
    parser.add_argument(
        "--add-dir",
        action="append",
        default=[],
        help="Add a directory to Antigravity workspace; repeatable.",
    )
    parser.add_argument("--raw", action="store_true", help="Print raw PTY output.")
    parser.add_argument("--json", action="store_true", help="Emit JSON result metadata.")
    return parser


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = build_arg_parser().parse_args()
    if args.prompt_file:
        prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    elif args.prompt is not None:
        prompt = args.prompt
    else:
        raise SystemExit("Provide a prompt argument or --prompt-file.")

    use_cli_model_flag = bool(args.model and supports_model_flag(args.agy))
    if args.model and not use_cli_model_flag:
        raise SystemExit("Installed AGY does not support --model; refusing settings-file fallback.")
    agy_args = [args.agy]
    if use_cli_model_flag:
        agy_args.extend(["--model", args.model])
    for directory in args.add_dir:
        agy_args.append(f"--add-dir={directory}")
    agy_args.extend([f"--print={prompt}", f"--print-timeout={args.timeout}"])

    exitstatus, raw_output = run_pty(agy_args, args.cwd, args.wall_timeout)

    cleaned_output = raw_output if args.raw else clean_terminal_output(raw_output)
    if args.json:
        print(
            json.dumps(
                {
                    "exitstatus": exitstatus,
                    "output": cleaned_output,
                    "rawOutput": raw_output,
                    "model": args.model,
                    "usedCliModelFlag": use_cli_model_flag,
                    "usedSettingsModelFallback": False,
                    "cwd": str(Path(args.cwd).resolve()),
                },
                ensure_ascii=False,
            )
        )
    elif cleaned_output:
        print(cleaned_output)

    return 0 if exitstatus in (0, None) else int(exitstatus)


if __name__ == "__main__":
    raise SystemExit(main())
