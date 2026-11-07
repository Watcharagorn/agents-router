"""Lazy, account-scoped model lists for roles config. Only the UI thread consumes results.

Adapters read provider inputs; this module owns request deduplication and the Kiro
cache. Workers are daemons and publish through a queue, so cancelling a picker
never waits for a provider process. No catalog is created by roles show.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from queue import Empty, Queue
import threading
import time
from typing import Callable, Iterable, Literal

from agents_router import config
from agents_router.fsutil import write_json
from agents_router.runtimes import get_runtime
from agents_router.runtimes.base import ModelDiscoveryIO, Runtime

FRESH_SECONDS = 3600


@dataclass(frozen=True)
class Snapshot:
    status: Literal["ready", "loading", "unavailable"]
    models: tuple[str, ...] = ()


def aggregate(snapshots: Iterable[Snapshot]) -> Snapshot:
    """Union account snapshots without reading inputs or changing their state."""

    found = list(snapshots)
    status = next((status for status in ("loading", "ready") if any(item.status == status for item in found)),
                  "unavailable")
    return Snapshot(status, tuple(dict.fromkeys(model for item in found for model in item.models)))


def _binary_identity(runtime: config.RuntimeConfig, adapter: Runtime) -> list:
    """Resolved command and file signatures: replacing a CLI invalidates its cached list."""

    identity = []
    for arg in adapter.find_binary(runtime) or []:
        path = Path(arg).expanduser()
        try:
            resolved = path.resolve()
            stat = resolved.stat()
            identity.append([str(resolved), stat.st_mtime_ns, stat.st_size])
        except OSError:
            identity.append(arg)
    return identity


def _start(work: Callable[[], None]) -> None:
    threading.Thread(target=work, name="agt-models", daemon=True).start()


class ModelCatalog:
    def __init__(self, *, io: ModelDiscoveryIO | None = None, cache: Path | None = None,
                 clock: Callable[[], float] = time.time, runtime_factory: Callable[[str], Runtime] = get_runtime,
                 binary_identity: Callable = _binary_identity, start: Callable = _start,
                 write: Callable = write_json):
        self.io = io or ModelDiscoveryIO()
        self.cache = cache if cache is not None else config.cache_dir() / "models"
        self.clock, self.runtime_factory, self.binary_identity = clock, runtime_factory, binary_identity
        self.start, self.write = start, write
        self._keys: dict[tuple, str] = {}
        self._snapshots: dict[str, Snapshot] = {}
        self._attempts: dict[str, int] = {}
        self._failed: set[str] = set()
        self._results: Queue[tuple[str, tuple[str, ...] | None]] = Queue()

    @staticmethod
    def _account_key(runtime: config.RuntimeConfig, account: config.Account) -> tuple:
        return runtime.runtime, account.name, str(account.home.expanduser().resolve()), runtime.binary

    def snapshot(self, runtime: config.RuntimeConfig, account: config.Account) -> Snapshot:
        """Read state without starting discovery, looking for a binary or reading provider files."""

        self.poll()
        key = self._keys.get(self._account_key(runtime, account))
        return self._snapshots.get(key, Snapshot("unavailable"))

    def poll(self) -> bool:
        """Called by the key loop on the main thread; workers never change UI state."""

        changed = False
        while True:
            try:
                key, models = self._results.get_nowait()
            except Empty:
                return changed
            old = self._snapshots[key]
            if models is None:
                self._failed.add(key)
                self._snapshots[key] = Snapshot("ready" if old.models else "unavailable", old.models)
            else:
                self._failed.discard(key)
                self._snapshots[key] = Snapshot("ready", models)
            changed = True

    def _cached(self, key: str) -> tuple[tuple[str, ...] | None, bool]:
        try:
            data = json.loads(self.io.read_text(self.cache / f"{key}.json"))
            models, saved = data["models"], data["saved"]
            if not isinstance(models, list) or not all(isinstance(model, str) and model.strip() for model in models):
                return None, False
            age = self.clock() - float(saved)
            return tuple(dict.fromkeys(models)), 0 <= age < FRESH_SECONDS
        except (OSError, ValueError, TypeError, KeyError):
            return None, False

    def request(self, runtime: config.RuntimeConfig, account: config.Account) -> Snapshot:
        """Start once per account, reusing a fresh cache or a pending lookup.

        Reopening after a failed lookup may retry once in this invocation. A stale
        successful list stays usable throughout the refresh and after failures.
        """

        self.poll()
        adapter = self.runtime_factory(runtime.runtime)
        account_key = self._account_key(runtime, account)
        identity = [*account_key, self.binary_identity(runtime, adapter)]
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        self._keys[account_key] = key
        old = self._snapshots.get(key)
        if old and (old.status == "loading" or key not in self._failed or self._attempts.get(key, 0) >= 2):
            return old
        if old is None:
            cached, fresh = self._cached(key) if runtime.runtime == "kiro" else (None, False)
            old = Snapshot("ready" if fresh else "loading", cached or ())
            self._snapshots[key] = old
            if fresh:
                return old
        self._snapshots[key] = Snapshot("loading", old.models)
        self._attempts[key] = self._attempts.get(key, 0) + 1

        def work():
            models = None
            try:
                models = adapter.discover_models(runtime, account, io=self.io)
                if models is not None and runtime.runtime == "kiro":
                    try:
                        self.write(self.cache / f"{key}.json", {"saved": self.clock(), "models": list(models)})
                    except OSError:
                        pass  # still usable for this invocation when the cache can't be written
            except Exception:
                # A broken provider input must never leave the menu stuck loading.
                models = None
            self._results.put((key, models))

        self.start(work)
        return self._snapshots[key]
