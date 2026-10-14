"""Two-way messages between the main session and a running subagent.

Each job folder holds two append-only logs:

    outbox.jsonl   worker -> main session: progress notes and questions
    inbox.jsonl    main session -> worker: follow-ups and answers

plus small JSON files that remember which entries each side has already seen,
so every message is delivered once.
"""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from typing import Any

from cli_subagents.fsutil import file_lock, read_json, write_json
from cli_subagents.jobs import job_dir

MAX_TEXT = 20_000


def _append(path: Path, record: dict[str, Any]) -> dict[str, Any]:
    with file_lock(path.with_suffix(".lock")):
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def _read(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def _take_unseen(job_id: str, log: str, seen_file: str) -> list[dict[str, Any]]:
    folder = job_dir(job_id)
    seen_path = folder / seen_file
    with file_lock(seen_path.with_suffix(".lock")):
        seen = set(read_json(seen_path, []) or [])
        fresh = [record for record in _read(folder / log) if record.get("id") not in seen]
        if fresh:
            write_json(seen_path, sorted(seen | {record["id"] for record in fresh}))
    return fresh


def _record(kind: str, text: str, **extra: Any) -> dict[str, Any]:
    text = str(text or "").strip()
    if not text:
        raise ValueError("message text is empty")
    return {"id": secrets.token_hex(4), "time": time.time(), "kind": kind, "text": text[:MAX_TEXT], **extra}


# Worker side ------------------------------------------------------------------


def post_from_worker(job_id: str, text: str, kind: str = "message") -> dict[str, Any]:
    return _append(job_dir(job_id) / "outbox.jsonl", _record(kind, text))


def take_for_worker(job_id: str) -> list[dict[str, Any]]:
    """Follow-ups and answers the worker hasn't read yet."""

    return _take_unseen(job_id, "inbox.jsonl", "inbox-read.json")


def wait_for_reply(job_id: str, question_id: str, timeout: float) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for record in _read(job_dir(job_id) / "inbox.jsonl"):
            if record.get("reply_to") == question_id:
                _take_unseen(job_id, "inbox.jsonl", "inbox-read.json")
                return record
        time.sleep(1.0)
    return None


# Main-session side --------------------------------------------------------------


def post_to_worker(job_id: str, text: str, reply_to: str | None = None) -> dict[str, Any]:
    return _append(job_dir(job_id) / "inbox.jsonl", _record("answer" if reply_to else "follow_up", text, reply_to=reply_to))


def take_for_host(job_id: str) -> list[dict[str, Any]]:
    """Worker messages the main session hasn't been shown yet."""

    return _take_unseen(job_id, "outbox.jsonl", "outbox-delivered.json")


def has_new_for_host(job_id: str) -> bool:
    folder = job_dir(job_id)
    seen = set(read_json(folder / "outbox-delivered.json", []) or [])
    return any(record.get("id") not in seen for record in _read(folder / "outbox.jsonl"))


def open_questions(job_id: str) -> list[dict[str, Any]]:
    folder = job_dir(job_id)
    answered = {record.get("reply_to") for record in _read(folder / "inbox.jsonl")}
    return [record for record in _read(folder / "outbox.jsonl") if record.get("kind") == "question" and record["id"] not in answered]
