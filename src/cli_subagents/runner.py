"""Background runner: executes one job, including queueing and account failover.

Started as a detached process (`python -m cli_subagents _runner <job_id>`), so a
job keeps running if the MCP server or the host restarts.
"""

from __future__ import annotations

import dataclasses
import json
import os
import queue
import subprocess
import threading
import time
from typing import Any

from cli_subagents import accounts, messages
from cli_subagents.config import SINGLE_ACCOUNT, Account, RuntimeConfig, load_config
from cli_subagents.fsutil import kill_tree, new_group_kwargs, open_private, process_start_time, write_text
from cli_subagents.jobs import job_dir, mark_pending, read_spec, update_status
from cli_subagents.runtimes import get_runtime
from cli_subagents.runtimes.base import (
    CANCELLED,
    ERROR,
    NO_ACCOUNT,
    SUCCESS,
    TIMEOUT,
    USAGE_LIMIT,
    AttemptResult,
    JobSpec,
)

QUEUE_TIMEOUT_SECONDS = 15 * 60
MAX_FOLLOW_UP_TURNS = 5
STATUS_INTERVAL = 2.0
FINAL_STATUS_RETRY_SECONDS = 5.0

CONTINUE_NOTE = (
    "Note from CLI Subagents: the account this conversation ran on reached its usage limit, so the "
    "conversation moved to another account. Continue the task from where you stopped. Check the "
    "current state of the files before redoing anything that may already be done."
)


def handoff_prompt(original: str, previous: AttemptResult, account: str) -> str:
    partial = (previous.text or "").strip()
    note = (
        "Note from CLI Subagents: an earlier attempt at this task ran on another account "
        f"({account}) and stopped because that account reached its usage limit."
    )
    if partial:
        note += f" Its last output was:\n\n<previous_output>\n{partial[-4000:]}\n</previous_output>\n"
    note += (
        "\nCheck the current state of the files before redoing anything that may already be done, "
        "then finish the task."
    )
    return f"{original}\n\n{note}"


class Runner:
    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self.folder = job_dir(job_id)
        self.spec: JobSpec = read_spec(job_id)
        self.config = load_config()
        self.runtime_config: RuntimeConfig = self.config.runtime(self.spec.runtime)
        self.runtime = get_runtime(self.spec.runtime)
        # The whole job, failovers and queueing included, ends by this time.
        self.deadline = self.spec.created + self.spec.timeout_seconds + QUEUE_TIMEOUT_SECONDS
        self.attempts: list[dict[str, Any]] = []
        self.warnings: list[str] = []

    # Helpers ---------------------------------------------------------------

    def cancelled(self) -> bool:
        return (self.folder / "cancel").exists()

    def _write_status(self, **fields: Any) -> None:
        update_status(self.job_id, runner_pid=os.getpid(), attempts=self.attempts, warnings=self.warnings, **fields)

    def status(self, **fields: Any) -> bool:
        """Best-effort heartbeat or progress write: a locked or busy status file must not end the job."""

        try:
            self._write_status(**fields)
            return True
        except OSError:
            return False

    def finish(self, outcome: str, message: str = "", result: AttemptResult | None = None) -> int:
        text = result.text if result else ""
        if text:
            write_text(self.folder / "result.md", text)
        total_cost = sum(float(item.get("cost_usd") or 0) for item in self.attempts) or None
        fields = dict(
            state="done",
            outcome=outcome,
            message=message,
            finished_at=time.time(),
            session_id=result.session_id if result else None,
            cost_usd=total_cost,
            child_pid=None,
            child_started=None,
        )
        try:
            mark_pending(self.job_id)  # before the final status: whoever sees "done" also sees the marker
        except OSError as exc:
            self.warnings.append(f"could not mark the result for delivery: {exc}")
        # The final write is the one that matters, so it keeps trying for a while.
        deadline = time.monotonic() + FINAL_STATUS_RETRY_SECONDS
        while True:
            try:
                self._write_status(**fields)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)
        return 0 if outcome == SUCCESS else 1

    # Main loop -------------------------------------------------------------

    def run(self) -> int:
        self.status(state="starting", started_at=time.time())
        prompt = self.spec.prompt
        excluded: set[str] = set()
        last: AttemptResult | None = None
        failover = self.runtime_config.failover == "on-limit" and not self.spec.account
        # The conversation to continue and the account folder that holds it.
        session = self.spec.resume_session
        owner = self.spec.resume_account
        follow_ups = 0

        while True:
            if self.cancelled():
                return self.finish(CANCELLED, "cancelled", last)
            lease = self._wait_for_account(excluded, prefer=owner)
            if lease is None:
                if self.cancelled():
                    return self.finish(CANCELLED, "cancelled", last)
                if last is not None:
                    return self.finish(last.outcome, last.message, last)
                return self.finish(NO_ACCOUNT, self._no_account_message(excluded))
            try:
                fallback = handoff_prompt(self.spec.prompt, last, owner or "another account") if last else self.spec.prompt
                last = self._attempt(lease, prompt, session, owner, fallback)
            finally:
                lease.release()

            if last.outcome == SUCCESS and last.session_id and follow_ups < MAX_FOLLOW_UP_TURNS:
                pending = messages.take_for_worker(self.job_id)
                if pending:
                    # Follow-ups sent while the worker was busy: continue the same conversation.
                    follow_ups += 1
                    session, owner = last.session_id, lease.account.name
                    prompt = (
                        "Follow-up from the main session (you may already have seen it during your run; "
                        "if you already handled it, just confirm briefly):\n\n"
                        + "\n\n".join(item["text"] for item in pending)
                    )
                    self.status(follow_up_turns=follow_ups)
                    continue
            if last.outcome != USAGE_LIMIT:
                return self.finish(last.outcome, last.message, last)
            if self.spec.runtime not in SINGLE_ACCOUNT:
                # With one account there is nothing to fail over to; a cooldown would only block later jobs.
                until = accounts.cooldown_end(
                    "\n".join(part for part in (last.message, last.error_text) if part),
                    last.resets_at,
                    self.runtime_config.cooldown_minutes,
                )
                accounts.set_cooldown(self.spec.runtime, lease.account.name, until, last.message or USAGE_LIMIT)
            if not failover:
                return self.finish(USAGE_LIMIT, f"{lease.account.name}: {last.message}", last)
            excluded.add(lease.account.name)
            if last.session_id and hasattr(self.runtime, "transfer_session"):
                # Continue the same conversation on the next account.
                session, owner, prompt = last.session_id, lease.account.name, CONTINUE_NOTE
            else:
                session, owner = None, None
                prompt = handoff_prompt(self.spec.prompt, last, lease.account.name)
            self.warnings.append(f"{lease.account.name} hit its usage limit; continuing on another account")

    def _no_account_message(self, excluded: set[str]) -> str:
        usage = accounts.load_usage(self.spec.runtime)
        ends = [
            accounts.cooldown_until(usage.get(account.name))
            for account in self.runtime_config.worker_accounts()
            if account.name not in excluded
        ]
        ends = [end for end in ends if end]
        if ends:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(min(ends)))
            return f"every {self.spec.runtime} account is at its usage limit; the earliest resets around {when}"
        if time.time() >= self.deadline:
            return "the job ran out of time before an account was free"
        return f"no {self.spec.runtime} account was free within {QUEUE_TIMEOUT_SECONDS // 60} minutes"

    def _wait_for_account(self, excluded: set[str], prefer: str | None = None) -> accounts.Lease | None:
        queued_since = time.time()
        while True:
            try:
                lease, _earliest, all_busy = accounts.acquire(
                    self.runtime_config, pinned=self.spec.account, exclude=excluded, prefer=prefer
                )
            except ValueError as exc:
                self.warnings.append(str(exc))
                return None
            if lease is not None:
                if time.time() >= self.deadline:
                    lease.release()
                    return None
                return lease
            if not all_busy:
                return None
            now = time.time()
            if self.cancelled() or now - queued_since > QUEUE_TIMEOUT_SECONDS or now >= self.deadline:
                return None
            self.status(state="queued", message="waiting for a free account slot")
            time.sleep(3)

    def _attempt(
        self,
        lease: accounts.Lease,
        prompt: str,
        session: str | None = None,
        owner: str | None = None,
        fallback_prompt: str | None = None,
    ) -> AttemptResult:
        account = lease.account
        attempt: dict[str, Any] = {"account": account.name, "started_at": time.time()}
        self.attempts.append(attempt)
        if session and owner and owner != account.name:
            source = self.runtime_config.find(owner)
            moved = bool(source) and self.runtime.transfer_session(source, account, session, self.spec.cwd)
            if moved:
                attempt["continued_from"] = owner
            else:
                self.warnings.append(f"could not move the session from {owner}; starting a new one with a summary")
                session, prompt = None, fallback_prompt or prompt
        spec = dataclasses.replace(self.spec, resume_session=session)
        try:
            setup = self.runtime.prepare(self.runtime_config, account, spec)
        except Exception as exc:  # setup problems must not kill the job silently
            setup = {}
            self.warnings.append(f"shared setup for {account.name} failed: {type(exc).__name__}: {exc}")
        for key in ("conflicts", "warnings"):
            for message in setup.get(key) or []:
                if message not in self.warnings:
                    self.warnings.append(message)
        try:
            launch = self.runtime.launch(self.runtime_config, account, spec, prompt, setup)
        except Exception as exc:
            result = AttemptResult(ERROR, message=f"could not start the worker: {exc}", session_id=session)
            attempt.update(ended_at=time.time(), outcome=result.outcome, message=result.message)
            return result
        attempt["session_id"] = launch.session_id
        for message in launch.warnings:
            if message not in self.warnings:
                self.warnings.append(message)
        parser = self.runtime.new_parser()
        time_limit = max(1.0, min(float(self.spec.timeout_seconds), self.deadline - time.time()))

        events_path = self.folder / "events.jsonl"
        stderr_path = self.folder / f"stderr-{len(self.attempts)}.log"
        stop_reason: str | None = None
        exit_code: int | None = None
        with open_private(events_path) as events, open_private(stderr_path, append=False) as stderr:
            process = subprocess.Popen(
                launch.argv,
                cwd=self.spec.cwd,
                env=launch.env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr,
                **new_group_kwargs(),
            )
            try:
                started = process_start_time(process.pid)
                try:
                    lease.set_owner(process.pid, started, self.job_id)
                except OSError as exc:
                    self.warnings.append(f"could not record the worker in its account slot: {exc}")
                self.status(
                    state="running", account=account.name, child_pid=process.pid, child_started=started,
                    session_id=launch.session_id, message=None,
                )
                stop_reason = self._pump(process, launch.stdin, parser, account, events, time_limit)
                try:
                    exit_code = process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    kill_tree(process.pid)
                    exit_code = None
            finally:
                # Whatever happened above, never leave the worker running without its slot.
                if process.poll() is None:
                    kill_tree(process.pid)
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        pass
                if process.stdout:
                    process.stdout.close()
                self.status(child_pid=None, child_started=None)

        stderr_tail = stderr_path.read_text(encoding="utf-8", errors="replace")[-4000:] if stderr_path.exists() else ""
        if stop_reason == CANCELLED:
            result = AttemptResult(CANCELLED, message="cancelled", text=parser.last_text, session_id=parser.session_id)
        elif stop_reason == TIMEOUT:
            result = AttemptResult(
                TIMEOUT,
                message=f"stopped after {int(time_limit)} seconds",
                text=parser.last_text,
                session_id=parser.session_id,
            )
        else:
            try:
                result = parser.finish(exit_code, stderr_tail)
            except Exception as exc:
                result = AttemptResult(ERROR, message=f"could not read the result: {exc}", text=parser.last_text)
        if result.session_id is None:
            result.session_id = launch.session_id
        attempt.update(
            ended_at=time.time(),
            outcome=result.outcome,
            message=result.message,
            exit_code=exit_code,
            session_id=result.session_id,
            cost_usd=result.cost_usd,
            num_turns=result.num_turns,
        )
        self.status(progress=parser.progress(), init=getattr(parser, "init", None))
        return result

    def _pump(self, process: subprocess.Popen, stdin_text: str, parser: Any, account: Account, events, time_limit: float) -> str | None:
        """Feed the prompt, stream events until the worker exits; return a stop reason or None."""

        assert process.stdin is not None and process.stdout is not None
        stdin = process.stdin

        def writer() -> None:
            # A separate thread: a worker that doesn't read its stdin must not block the
            # timeout and cancel checks below.
            try:
                stdin.write(stdin_text.encode("utf-8"))
            except (OSError, ValueError):
                pass
            finally:
                try:
                    stdin.close()
                except (OSError, ValueError):
                    pass

        threading.Thread(target=writer, daemon=True).start()
        lines: queue.Queue[bytes | None] = queue.Queue()

        def reader() -> None:
            try:
                for raw in process.stdout:  # type: ignore[union-attr]
                    lines.put(raw)
            except (OSError, ValueError):
                pass
            lines.put(None)

        threading.Thread(target=reader, daemon=True).start()
        started = time.monotonic()
        last_status = 0.0
        while True:
            try:
                raw = lines.get(timeout=0.5)
            except queue.Empty:
                raw = b""
            if raw is None:
                return None
            if raw:
                events.write(raw if raw.endswith(b"\n") else raw + b"\n")
                self._handle_line(raw, parser, account)
            if time.monotonic() - last_status > STATUS_INTERVAL:
                last_status = time.monotonic()
                self.status(state="running", account=account.name, progress=parser.progress(), init=getattr(parser, "init", None))
            if self.cancelled():
                kill_tree(process.pid)
                return CANCELLED
            if time.monotonic() - started > time_limit:
                kill_tree(process.pid)
                return TIMEOUT

    def _handle_line(self, raw: bytes, parser: Any, account: Account) -> None:
        try:
            event = json.loads(raw.decode("utf-8", errors="replace"))
            if not isinstance(event, dict):
                return
            usage = parser.feed(event)
            if usage:
                accounts.record_rate_limit_event(self.spec.runtime, account.name, usage)
        except Exception:
            # One malformed event must not end the job or orphan the worker.
            return


def main(job_id: str) -> int:
    try:
        runner = Runner(job_id)
    except Exception as exc:
        mark_pending(job_id)
        update_status(job_id, state="done", outcome=ERROR, message=f"runner could not start: {exc}", finished_at=time.time())
        return 1
    try:
        return runner.run()
    except Exception as exc:
        runner.warnings.append(f"{type(exc).__name__}: {exc}")
        return runner.finish(ERROR, f"runner failed: {type(exc).__name__}: {exc}")
