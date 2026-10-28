"""`agt run` and `agt wait` for a person at a terminal: the job working while you wait, and one line at the end.

Agents, pipes and `--json` get the JSON summaries; this module never changes them.
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable

from agents_router import jobs, ui


def _status(job_id: str) -> dict[str, Any]:
    try:
        return jobs.read_status(job_id)
    except ValueError:
        return {"job_id": job_id}


def reader(job_ids: list[str], every: float = 0.5) -> Callable[[], dict[str, dict[str, Any]]]:
    """Each job's status, read from disk at most every `every` seconds (the display redraws more often)."""

    found: dict[str, dict[str, Any]] = {}
    last = [float("-inf")]

    def read() -> dict[str, dict[str, Any]]:
        if time.monotonic() - last[0] >= every:
            found.update({job_id: _status(job_id) for job_id in job_ids})
            last[0] = time.monotonic()
        return found

    return read


def model(status: dict[str, Any]) -> str | None:
    """The model the worker runs, short: "haiku" for claude-haiku-5-5."""

    name = str((status.get("init") or {}).get("model") or status.get("model") or "")
    match = re.match(r"claude-([a-z]+)", name)
    return match.group(1) if match else name or None


def who(status: dict[str, Any], bold: bool = True) -> list[ui.Piece]:
    runtime = str(status.get("runtime") or "job")
    return [ui.bold(runtime) if bold else runtime, *([f" on {status['account']}"] if status.get("account") else [])]


def elapsed(status: dict[str, Any]) -> float:
    started = status.get("started_at") or status.get("created_at") or time.time()
    return float(status.get("finished_at") or time.time()) - float(started)


def tools_used(status: dict[str, Any], last: bool = True) -> str:
    progress = status.get("progress") or {}
    count = int(progress.get("tool_uses") or 0)
    if not count:
        return ""
    text = f"{count} tool use{'s' if count != 1 else ''}"
    return text + (f", last: {progress['last_tool']}" if last and progress.get("last_tool") else "")


# agt run -------------------------------------------------------------------------------------


def run_frame(job_id: str) -> Callable[[str, float], list[ui.Line]]:
    """The two lines `agt run` shows while its job works."""

    read = reader([job_id])

    def draw(spin: str, now: float) -> list[ui.Line]:
        status = read().get(job_id) or {}
        name = model(status)
        line: list[ui.Piece] = [ui.Span("muted", spin), " ", *who(status),
                                ui.muted(f" · {name}" if name else ""), ui.muted(f" · job {job_id}"),
                                "  ", ui.muted(ui.clock(elapsed(status)))]
        line += ["  ", ui.muted(tools_used(status))] if tools_used(status) else []
        return [line, ["  ", ui.muted("Ctrl-C stops watching; the job keeps running. "), ui.code(f"agt status {job_id}"),
                       ui.muted(" looks again.")]]

    return draw


def end_lines(summary: dict[str, Any]) -> list[ui.Line]:
    """How a job ended, in one line, with how to see it again: `summary` is `tools.summarize` of its result."""

    job_id = summary.get("job_id", "")
    spent = ui.took(float(summary.get("elapsed_seconds") or 0))
    facts = ["".join(who(summary, bold=False))]
    facts += [model(summary.get("worker") or {})] if (summary.get("worker") or {}).get("model") else []
    facts += [tools_used(summary, last=False)] if tools_used(summary) else []
    cost = summary.get("cost_usd")
    facts += [f"${cost:.2f}"] if isinstance(cost, (int, float)) and cost >= 0.005 else []
    tail = ui.muted(" · " + " · ".join(fact for fact in facts if fact))
    state, outcome = summary.get("state"), summary.get("outcome")
    if state in jobs.ACTIVE_STATES:
        return [[ui.Span("warn-mark", "…"), f" still running after {spent}", tail],
                *ui.columns([[ui.muted("Look again"), ui.cmd(f"agt status {job_id}")]], gap=3)]
    if outcome == "success":
        lines: list[ui.Line] = [[ui.Span("ok-mark", "✓"), f" done in {spent}", tail]]
    else:
        lines = [[ui.Span("error-mark", "✗"), " ", ui.error(str(outcome or state or "failed")), f" after {spent}", tail]]
        lines += [["  ", *ui.inline(str(summary["message"]))]] if summary.get("message") else []
    said, answers = told(summary)
    lines += said + ui.columns(answers, gap=3)
    lines += [["  ", ui.muted(note)] for note in summary.get("warnings") or []]
    attempts = summary.get("attempts") or []
    if attempts:
        lines.append(["  ", ui.muted("Tried " + ", then ".join(f"{attempt.get('account')} ({attempt.get('outcome')})"
                                                               for attempt in attempts) + ".")])
    lines.append(["  ", ui.code(f"agt result {job_id}"),
                  ui.muted(" shows it again." if outcome == "success" else " shows what it got.")])
    return lines


def told(summary: dict[str, Any], indent: str = "  ") -> tuple[list[ui.Line], list[list[ui.Line]]]:
    """What the job said to you and asks you, in full (reading the summary used them up, so this is the one place a
    person sees them), and an "Answer" row for each question."""

    asked = summary.get("open_questions") or []
    ids = {question.get("id") for question in asked}
    lines: list[ui.Line] = [[indent, ui.hang("It said: "), f'"{item.get("text") or ""}"']
                            for item in summary.get("messages") or [] if item.get("id") not in ids]
    lines += [[indent, ui.hang("It asks: "), f'"{question.get("text") or ""}"'] for question in asked]
    answers = [[ui.muted("Answer"), ui.cmd(f'agt message {summary.get("job_id", "")} "<your answer>" --reply-to {question["id"]}')]
               for question in asked]
    return lines, answers


def stopped_lines(command: str) -> list[ui.Line]:
    return ["Stopped watching; the job keeps running.", *ui.columns([[ui.muted("Look again"), ui.cmd(command)]], gap=3)]


# agt wait ------------------------------------------------------------------------------------


def wait_frame(job_ids: list[str]) -> Callable[[str, float], list[ui.Line]]:
    """What `agt wait` shows while it waits: the time, and each job's work so far."""

    read = reader(job_ids)
    started = time.monotonic()

    def draw(spin: str, now: float) -> list[ui.Line]:
        found = read()
        rows = [[ui.muted(job_id), "".join(who(found.get(job_id) or {}, bold=False)),
                 ui.muted(tools_used(found.get(job_id) or {}))] for job_id in job_ids]
        count = len(job_ids)
        return [[ui.Span("muted", spin), f" waiting for {count} job{'s' if count != 1 else ''}", "  ",
                 ui.muted(ui.clock(now - started))], *ui.columns(rows, indent=4, gap=3)]

    return draw


def _said(update: dict[str, Any]) -> str:
    asked = update.get("open_questions") or []
    sent = update.get("messages") or []
    text = " ".join(str((asked or sent or [{}])[0].get("text") or "").split())
    text = text[:80] + ("…" if len(text) > 80 else "")
    return (f'asks: "{text}"' if asked else f'sent a message: "{text}"') if text else "sent a message"


def wait_lines(report: dict[str, Any]) -> list[ui.Line]:
    """`agt wait` with several jobs, or one that hasn't finished: a row per job, then the command for each."""

    rows: list[list[ui.Line]] = []
    under: list[list[ui.Line]] = []
    actions: list[list[ui.Line]] = []
    for summary in report.get("finished") or []:
        job_id = summary.get("job_id", "")
        spent = ui.took(float(summary.get("elapsed_seconds") or 0))
        if summary.get("outcome") == "success":
            rows.append([[ui.Span("ok-mark", "✓"), " ", ui.muted(job_id)], "".join(who(summary, bold=False)), f"done in {spent}"])
            actions.append([ui.muted("See the result"), ui.cmd(f"agt result {job_id}")])
        else:
            rows.append([[ui.Span("error-mark", "✗"), " ", ui.muted(job_id)], "".join(who(summary, bold=False)),
                         [ui.error(str(summary.get("outcome") or "failed")), f" after {spent}"]])
            actions.append([ui.muted("See why"), ui.cmd(f"agt result {job_id}")])
        said, answers = told(summary)
        under.append(said)
        actions += answers
    waiting = []
    for summary in report.get("updates") or []:
        job_id = summary.get("job_id", "")
        rows.append([[ui.Span("warn-mark", "!"), " ", ui.muted(job_id)], "".join(who(summary, bold=False)), _said(summary)])
        under.append([])
        actions += told(summary)[1]
        waiting.append(job_id)
    for job_id in report.get("still_running") or []:
        status = _status(job_id)
        used = tools_used(status, last=False)
        rows.append([[ui.Span("warn-mark", "…"), " ", ui.muted(job_id)], "".join(who(status, bold=False)),
                     "still running" + (f", {used}" if used else "")])
        under.append([])
        waiting.append(job_id)
    if waiting:
        actions.append([ui.muted("Keep waiting"), ui.cmd("agt wait " + " ".join(waiting))])
    lines: list[ui.Line] = []
    for line, extra in zip(ui.columns(rows, indent=0, gap=3), under):
        lines += [line, *extra]
    return lines + (["", *ui.columns(actions, gap=3)] if actions else [])
