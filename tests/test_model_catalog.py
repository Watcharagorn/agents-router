"""Read-only model sources, lazy background snapshots and the account-scoped Kiro cache."""

import json
import os
from pathlib import Path
import subprocess
import threading
import unittest
from unittest.mock import Mock, patch

from helpers import FAKE_KIRO, TempHome

from agents_router.config import Account, RuntimeConfig
from agents_router.model_catalog import FRESH_SECONDS, ModelCatalog, Snapshot, aggregate
from agents_router.runtimes import get_runtime
from agents_router.runtimes.base import ModelDiscoveryIO
from agents_router.runtimes.kiro import _model_ids


class Aggregation(unittest.TestCase):
    def test_union_deduplicates_in_account_order_without_changing_inputs(self):
        snapshots = [Snapshot("ready", ("one", "shared")), Snapshot("ready", ("shared", "two"))]
        before = list(snapshots)
        self.assertEqual(aggregate(iter(snapshots)), Snapshot("ready", ("one", "shared", "two")))
        self.assertEqual(snapshots, before)

    def test_status_precedence_preserves_available_models(self):
        for snapshots, expected in (
                ([], Snapshot("unavailable")),
                ([Snapshot("unavailable"), Snapshot("unavailable")], Snapshot("unavailable")),
                ([Snapshot("unavailable"), Snapshot("ready", ("offered",))], Snapshot("ready", ("offered",))),
                ([Snapshot("ready", ("offered",)), Snapshot("loading")], Snapshot("loading", ("offered",))),
                ([Snapshot("loading", ("stale",)), Snapshot("ready", ("offered",))],
                 Snapshot("loading", ("stale", "offered")))):
            with self.subTest(snapshots=snapshots):
                self.assertEqual(aggregate(snapshots), expected)


class CatalogUnion(TempHome):
    def test_missing_codex_cache_does_not_veto_ready_or_loading_worker(self):
        runtime = RuntimeConfig("codex", self.primary)
        missing = Account("missing", self.root / "missing")
        present = Account("present", self.root / "present")
        present.home.mkdir()
        (present.home / "models_cache.json").write_text(json.dumps({"models": [
            {"slug": "work-model", "visibility": "list"}]}))
        work = []
        catalog = ModelCatalog(cache=self.home / "cache" / "models", start=work.append,
                               binary_identity=lambda *_: "fake-codex")
        for account in (missing, present):
            self.assertEqual(catalog.request(runtime, account), Snapshot("loading"))
        work.pop(1)()  # one account returns while the other lookup is still pending
        self.assertEqual(aggregate(catalog.snapshot(runtime, account) for account in (missing, present)),
                         Snapshot("loading", ("work-model",)))
        work.pop()()
        self.assertEqual(aggregate(catalog.snapshot(runtime, account) for account in (missing, present)),
                         Snapshot("ready", ("work-model",)))
        self.assertEqual(catalog.snapshot(runtime, missing), Snapshot("unavailable"))
        self.assertEqual(catalog.snapshot(runtime, present), Snapshot("ready", ("work-model",)))
        self.assertFalse(catalog.cache.exists())  # aggregation never creates a combined cache


class Sources(TempHome):
    def runtime(self, name):
        return RuntimeConfig(name, self.primary), Account("test", self.primary)

    def test_claude_aliases_need_no_binary_file_or_process(self):
        runtime, account = self.runtime("claude")
        io = ModelDiscoveryIO(read_text=Mock(side_effect=AssertionError("read")),
                              run=Mock(side_effect=AssertionError("process")))
        with patch.object(get_runtime("claude").__class__, "find_binary", side_effect=AssertionError("binary")):
            self.assertEqual(get_runtime("claude").discover_models(runtime, account, io=io),
                             ("opus", "sonnet", "haiku", "fable"))

    def test_codex_account_cache_visibility_priority_and_ids(self):
        runtime, account = self.runtime("codex")
        account.home = self.root / "extra-codex"
        account.home.mkdir()
        path = account.home / "models_cache.json"
        path.write_text(json.dumps({"identity": "ignored", "etag": "ignored", "models": [
            {"slug": "later", "visibility": "list", "priority": 20},
            {"slug": "hidden", "visibility": "hide", "priority": 0},
            {"slug": "first", "visibility": "list", "priority": 1},
            {"slug": "last", "visibility": "list"}, {"slug": "unlisted"},
            {"slug": "first", "visibility": "list", "priority": 2}, None]}))
        before = path.read_bytes(), path.stat().st_mtime_ns
        run = Mock(side_effect=AssertionError("Codex must not run"))
        self.assertEqual(get_runtime("codex").discover_models(runtime, account, io=ModelDiscoveryIO(run=run)),
                         ("first", "later", "last"))
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertEqual(list(account.home.iterdir()), [path])

    def test_codex_missing_bad_or_wrong_shape_is_unavailable(self):
        runtime, account = self.runtime("codex")
        for contents in (None, "broken", "[]", "{}", '{"models": {}}'):
            with self.subTest(contents=contents):
                path = account.home / "models_cache.json"
                if contents is not None:
                    path.write_text(contents)
                self.assertIsNone(get_runtime("codex").discover_models(runtime, account))

    def test_antigravity_is_unavailable_without_any_process(self):
        runtime, account = self.runtime("antigravity")
        io = ModelDiscoveryIO(run=Mock(side_effect=AssertionError("must not run agy")))
        self.assertIsNone(get_runtime("antigravity").discover_models(runtime, account, io=io))

    def kiro(self, answers):
        runtime, account = self.runtime("kiro")
        results = [subprocess.CompletedProcess([], code, data if isinstance(data, str) else json.dumps(data),
                                               stderr=stderr[0] if stderr else "")
                   for code, data, *stderr in answers]
        run = Mock(side_effect=results)
        adapter = get_runtime("kiro")
        with patch.object(adapter, "find_binary", return_value=["fake-kiro"]):
            result = adapter.discover_models(runtime, account, io=ModelDiscoveryIO(run=run))
        return result, run

    def test_kiro_logged_out_null_or_missing_account_never_calls_chat(self):
        for code, data in ((1, {"account": "logged-in"}), (1, {"account": None}),
                           (0, {"account": None}), (0, {"account": None, "accountType": "IamIdentityCenter"}),
                           (0, {}), (0, []), (0, "junk"), (0, '{"account":null}\n{"email":"user@example.com"}'),
                           (0, {"region": "ap-southeast-1"}), (0, {"accountType": None, "email": None})):
            with self.subTest(code=code, data=data):
                result, run = self.kiro([(code, data)])
                self.assertIsNone(result)
                self.assertEqual(run.call_count, 1)
                self.assertEqual(run.call_args.args[0], ["fake-kiro", "whoami", "--format", "json"])

    def test_kiro_real_whoami_with_trailing_profile_text_lists_models(self):
        for email in ("user@example.com", "authentication@example.com"):
            with self.subTest(email=email):
                identity = json.dumps({"accountType": "IamIdentityCenter", "email": email,
                                       "region": "ap-southeast-1", "startUrl": "https://d-fake.awsapps.com/start/"})
                output = "Kiro identity:\n" + identity + "\n\nProfile:\nKiroProfile-us-east-1\narn:fake\n"
                result, run = self.kiro([(0, output, "Profile selected\n"), (0, {"models": ["auto"]})])
                self.assertEqual(result, ("auto",))
                self.assertEqual(run.call_count, 2)
                self.assertEqual(run.call_args.args[0][1:], ["chat", "--list-models", "--no-interactive", "--format", "json"])

    def test_kiro_invalid_explicit_account_overrides_other_identity_fields(self):
        for account in (None, "", " \t\n", False, True, 0, 1, {}, [], ["fake-account"]):
            for extra in ({}, {"accountType": "IamIdentityCenter", "email": "user@example.com"}):
                with self.subTest(account=account, extra=extra):
                    result, run = self.kiro([(0, {"account": account, **extra})])
                    self.assertIsNone(result)
                    self.assertEqual(run.call_count, 1)

    def test_kiro_identity_fields_require_nonempty_strings(self):
        for key in ("accountType", "email", "startUrl", "authMethod"):
            for value in (None, "", " \t\n", False, True, 0, 1, {}, {"id": "fake"}, [], ["fake"]):
                with self.subTest(key=key, value=value):
                    result, run = self.kiro([(0, {key: value})])
                    self.assertIsNone(result)
                    self.assertEqual(run.call_count, 1)

    def test_kiro_nonempty_account_object_counts_unless_it_contains_errors(self):
        result, run = self.kiro([(0, {"account": {"id": "fake-account"}}), (0, {"models": ["auto"]})])
        self.assertEqual(result, ("auto",))
        self.assertEqual(run.call_count, 2)
        for account in ({"error": "failure"}, {"details": {"errorMessage": "failure"}}):
            with self.subTest(account=account):
                result, run = self.kiro([(0, {"account": account})])
                self.assertIsNone(result)
                self.assertEqual(run.call_count, 1)

    def test_kiro_auth_words_inside_identity_json_do_not_block_discovery(self):
        for key, value in (("account", 'authentication {"id":"fake"}'), ("accountType", "AuthenticationProvider"),
                           ("email", "authentication@example.com"), ("startUrl", "https://authentication.example.com/start/"),
                           ("authMethod", "authentication"), ("account", {"email": "authentication@example.com"})):
            with self.subTest(key=key, value=value):
                result, run = self.kiro([(0, {key: value}), (0, {"models": ["auto"]})])
                self.assertEqual(result, ("auto",))
                self.assertEqual(run.call_count, 2)

    def test_kiro_whoami_error_fields_override_positive_identity(self):
        for field, value in (("error", "Not logged in"), ("error", None), ("errors", []), ("errorMessage", "failure"),
                              ("error_description", "failure"), ("errorCode", 0), ("authError", False), ("ERROR", "failure")):
            with self.subTest(field=field, value=value):
                result, run = self.kiro([(0, {"accountType": "IamIdentityCenter", field: value})])
                self.assertIsNone(result)
                self.assertEqual(run.call_count, 1)
                self.assertEqual(run.call_args.args[0][1:], ["whoami", "--format", "json"])

    def test_kiro_whoami_auth_failure_on_stderr_never_calls_chat(self):
        for error in ("Not logged in", "Unauthorized", "Authentication failed", "Expired token"):
            with self.subTest(error=error):
                result, run = self.kiro([(0, {"accountType": "IamIdentityCenter"}, error)])
                self.assertIsNone(result)
                self.assertEqual(run.call_count, 1)

    def test_kiro_whoami_auth_failure_in_trailing_stdout_never_calls_chat(self):
        for error in ("Not logged in", "Run kiro-cli login", "Log in again", "KIRO_API_KEY is invalid"):
            with self.subTest(error=error):
                output = '{"accountType":"IamIdentityCenter","email":"user@example.com"}\n\nProfile:\nFake\n' + error
                result, run = self.kiro([(0, output)])
                self.assertIsNone(result)
                self.assertEqual(run.call_count, 1)

    def test_kiro_whoami_auth_failure_before_json_never_calls_chat(self):
        output = 'Not logged in\n{"accountType":"IamIdentityCenter","email":"authentication@example.com"}\nProfile:\nFake'
        result, run = self.kiro([(0, output)])
        self.assertIsNone(result)
        self.assertEqual(run.call_count, 1)

    def test_kiro_recognizes_each_identity_field_without_an_account_key(self):
        for key in ("accountType", "email", "startUrl", "authMethod"):
            with self.subTest(key=key):
                result, run = self.kiro([(0, {key: "fake-identity"}), (0, {"models": ["auto"]})])
                self.assertEqual(result, ("auto",))
                self.assertEqual(run.call_count, 2)

    def test_kiro_list_output_envelope_and_array_ignore_trailing_text(self):
        for data in ({"models": [{"model_id": "auto"}]}, [{"model_id": "auto"}]):
            with self.subTest(data=data):
                result, _ = self.kiro([(0, {"accountType": "IamIdentityCenter"}),
                                       (0, "Available models:\n" + json.dumps(data) + "\n\nProfile:\nFake\n")])
                self.assertEqual(result, ("auto",))

    def kiro_snapshot(self, responses):
        runtime, account = self.runtime("kiro")
        run = Mock(side_effect=responses)
        adapter = get_runtime("kiro")
        work = []
        catalog = ModelCatalog(io=ModelDiscoveryIO(run=run), cache=self.home / "cache" / "models",
                               start=work.append, binary_identity=lambda *_: "fake-kiro",
                               runtime_factory=lambda _: adapter)
        with patch.object(adapter, "find_binary", return_value=["fake-kiro"]):
            self.assertEqual(catalog.request(runtime, account), Snapshot("loading"))
            work.pop()()  # nothing may escape the worker, including unexpected programming errors
        self.assertEqual(catalog.snapshot(runtime, account), Snapshot("unavailable"))
        self.assertFalse(catalog.cache.exists())
        return run

    def test_kiro_unexpected_model_shape_finishes_unavailable(self):
        for data in ({"unknown": []}, {"models": {}}, {"models": [42, {}]}, [42]):
            with self.subTest(data=data):
                self.kiro_snapshot([subprocess.CompletedProcess([], 0, '{"email":"user@example.com"}'),
                                    subprocess.CompletedProcess([], 0, json.dumps(data))])

    def test_kiro_type_error_inside_discovery_finishes_unavailable(self):
        for at_list in (False, True):
            with self.subTest(at_list=at_list):
                responses = [subprocess.CompletedProcess([], 0, '{"accountType":"IamIdentityCenter"}')] if at_list else []
                run = self.kiro_snapshot([*responses, TypeError("unexpected discovery error")])
                self.assertEqual(run.call_count, 2 if at_list else 1)

    def test_kiro_list_only_command_has_closed_stdin_and_timeout(self):
        result, run = self.kiro([(0, {"account": "fake"}), (0, {"models": [
            {"modelId": "auto", "modelName": "Auto"}, {"modelId": "sonnet", "modelName": "Sonnet"}]})])
        self.assertEqual(result, ("auto", "sonnet"))
        self.assertEqual(run.call_args.args[0],
                         ["fake-kiro", "chat", "--list-models", "--no-interactive", "--format", "json"])
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["stdin"], subprocess.DEVNULL)
            self.assertLessEqual(call.kwargs["timeout"], 10)

    def test_kiro_json_shapes_keep_ids_and_deduplicate(self):
        for data, expected in (({"models": [{"id": "sonnet", "name": "Display"}, {"id": "sonnet"}]}, ("sonnet",)),
                               (["auto", "opus"], ("auto", "opus")),
                               ({"models": [{"model_id": "haiku"}]}, ("haiku",)),
                               ({"models": []}, ()), ({"models": {}}, None), ({}, None),
                               ({"models": [42, {}]}, None)):
            with self.subTest(data=data):
                self.assertEqual(_model_ids(data), expected)

    def test_kiro_timeout_bad_json_and_nonzero_are_unavailable(self):
        runtime, account = self.runtime("kiro")
        adapter = get_runtime("kiro")
        for failed in (subprocess.TimeoutExpired("fake", 10), OSError("gone"),
                       subprocess.CompletedProcess([], 0, "not json"), subprocess.CompletedProcess([], 1, "{}")):
            with self.subTest(failed=failed):
                run = Mock(side_effect=[subprocess.CompletedProcess([], 0, '{"account":"fake"}'), failed])
                with patch.object(adapter, "find_binary", return_value=["fake"]):
                    self.assertIsNone(adapter.discover_models(runtime, account, io=ModelDiscoveryIO(run=run)))

    def test_fake_kiro_subprocess_in_isolated_home(self):
        runtime, account = self.runtime("kiro")
        log = self.root / "kiro-argv.jsonl"
        with patch.dict(os.environ, {"AGT_KIRO_COMMAND": FAKE_KIRO, "FAKE_KIRO_ARGV_LOG": str(log)}):
            self.assertEqual(get_runtime("kiro").discover_models(runtime, account), ("auto", "claude-sonnet-4.5"))
            with patch.dict(os.environ, {"FAKE_KIRO_LOGGED_OUT": "1"}):
                self.assertIsNone(get_runtime("kiro").discover_models(runtime, account))
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(calls, [["whoami", "--format", "json"],
                                 ["chat", "--list-models", "--no-interactive", "--format", "json"],
                                 ["whoami", "--format", "json"]])


class Cache(TempHome):
    def setUp(self):
        super().setUp()
        self.runtime = RuntimeConfig("kiro", self.primary)
        self.account = Account("kiro-test", self.primary)
        self.now = 10_000.0
        self.adapter = Mock()
        self.adapter.discover_models.return_value = ("auto", "sonnet")
        self.identity = "fake-binary-v1"
        self.work = []

    def catalog(self, **kwargs):
        return ModelCatalog(cache=self.home / "cache" / "models", clock=lambda: self.now,
                            runtime_factory=lambda _: self.adapter, binary_identity=lambda *_: self.identity,
                            start=self.work.append, **kwargs)

    def finish(self, catalog):
        self.work.pop(0)()
        self.assertTrue(catalog.poll())
        return catalog.snapshot(self.runtime, self.account)

    def seed(self):
        catalog = self.catalog()
        self.assertEqual(catalog.request(self.runtime, self.account), Snapshot("loading"))
        self.assertEqual(self.finish(catalog), Snapshot("ready", ("auto", "sonnet")))
        self.adapter.discover_models.reset_mock()
        return next(catalog.cache.glob("*.json"))

    def test_lazy_and_deduplicated_until_main_thread_polls(self):
        catalog = self.catalog()
        self.assertEqual(catalog.snapshot(self.runtime, self.account), Snapshot("unavailable"))
        self.assertEqual(self.work, [])
        self.assertFalse(catalog.cache.exists())
        for _ in range(5):
            self.assertEqual(catalog.request(self.runtime, self.account), Snapshot("loading"))
        self.assertEqual(len(self.work), 1)
        self.work.pop()()
        self.assertEqual(next(iter(catalog._snapshots.values())), Snapshot("loading"))
        self.assertEqual(catalog.snapshot(self.runtime, self.account), Snapshot("ready", ("auto", "sonnet")))
        catalog.request(self.runtime, self.account)
        self.assertEqual(self.work, [])

    def test_success_is_cached_and_fresh_list_skips_discovery(self):
        path = self.seed()
        self.assertEqual(json.loads(path.read_text()), {"saved": self.now, "models": ["auto", "sonnet"]})
        self.now += FRESH_SECONDS - 1
        catalog = self.catalog()
        self.assertEqual(catalog.request(self.runtime, self.account), Snapshot("ready", ("auto", "sonnet")))
        self.assertEqual(self.work, [])
        self.adapter.discover_models.assert_not_called()

    def test_stale_list_is_usable_while_refresh_runs(self):
        self.seed()
        self.now += FRESH_SECONDS
        catalog = self.catalog()
        self.assertEqual(catalog.request(self.runtime, self.account), Snapshot("loading", ("auto", "sonnet")))
        self.adapter.discover_models.return_value = ("new-model",)
        self.assertEqual(self.finish(catalog), Snapshot("ready", ("new-model",)))

    def test_failure_keeps_good_cache_and_retries_only_once_per_invocation(self):
        path = self.seed()
        before = path.read_bytes(), path.stat().st_mtime_ns
        self.now += FRESH_SECONDS + 1
        catalog = self.catalog()
        self.adapter.discover_models.return_value = None
        for _ in range(2):
            self.assertEqual(catalog.request(self.runtime, self.account), Snapshot("loading", ("auto", "sonnet")))
            self.assertEqual(self.finish(catalog), Snapshot("ready", ("auto", "sonnet")))
        for _ in range(5):
            catalog.request(self.runtime, self.account)
        self.assertEqual(self.work, [])
        self.assertEqual(self.adapter.discover_models.call_count, 2)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_unavailable_without_cache_and_one_retry(self):
        self.adapter.discover_models.return_value = None
        catalog = self.catalog()
        for _ in range(2):
            catalog.request(self.runtime, self.account)
            self.assertEqual(self.finish(catalog), Snapshot("unavailable"))
        catalog.request(self.runtime, self.account)
        self.assertEqual(self.work, [])
        self.assertFalse(catalog.cache.exists())

    def test_key_separates_runtime_account_resolved_home_and_binary(self):
        self.seed()
        for changed in ("account", "home", "binary", "runtime"):
            with self.subTest(changed=changed):
                runtime = RuntimeConfig("kiro", self.primary)
                account = Account("kiro-test", self.primary)
                if changed == "account":
                    account.name = "other"
                elif changed == "home":
                    account.home = self.root / "another"
                elif changed == "runtime":
                    runtime.runtime = "codex"
                else:
                    self.identity = "fake-binary-v2"
                catalog = self.catalog()
                self.assertEqual(catalog.request(runtime, account), Snapshot("loading"))
                self.work.clear()
                self.identity = "fake-binary-v1"

    def test_resolved_home_reuses_cache(self):
        self.seed()
        self.account.home = self.primary / ".." / self.primary.name
        self.assertEqual(self.catalog().request(self.runtime, self.account).status, "ready")
        self.assertEqual(self.work, [])

    def test_bad_cache_and_broken_adapter_do_not_stay_loading(self):
        path = self.seed()
        for contents in ("invalid", "[]", '{"saved": 10000, "models": "wrong"}'):
            with self.subTest(contents=contents):
                path.write_text(contents)
                catalog = self.catalog()
                self.adapter.discover_models.side_effect = RuntimeError("bad adapter")
                self.assertEqual(catalog.request(self.runtime, self.account), Snapshot("loading"))
                self.assertEqual(self.finish(catalog), Snapshot("unavailable"))

    def test_cache_write_failure_does_not_hide_successful_list(self):
        catalog = self.catalog(write=Mock(side_effect=OSError("read-only")))
        catalog.request(self.runtime, self.account)
        self.assertEqual(self.finish(catalog), Snapshot("ready", ("auto", "sonnet")))

    def test_non_kiro_sources_do_not_write_persistent_cache(self):
        for name in ("claude", "codex", "antigravity"):
            with self.subTest(runtime=name):
                self.runtime.runtime = name
                catalog = self.catalog()
                catalog.request(self.runtime, self.account)
                self.finish(catalog)
                self.assertFalse(catalog.cache.exists())

    def test_daemon_worker_never_blocks_request(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()

        def discover(*args, **kwargs):
            entered.set()
            release.wait(2)
            finished.set()
            return ("model",)

        self.adapter.discover_models.side_effect = discover
        catalog = ModelCatalog(cache=self.home / "cache", runtime_factory=lambda _: self.adapter,
                               binary_identity=lambda *_: "fake")
        worker = None
        try:
            self.assertEqual(catalog.request(self.runtime, self.account).status, "loading")
            self.assertTrue(entered.wait(1))
            worker = next(thread for thread in threading.enumerate() if thread.name == "agt-models")
            self.assertTrue(worker.daemon)
            self.assertFalse(finished.is_set())
        finally:
            release.set()
            if worker:
                worker.join(2)


if __name__ == "__main__":
    unittest.main()
