"""The model overlay, its refresh loop and unchanged read-only roles output, in a fake HOME."""

import contextlib
import io
import json
import os
import unittest
from unittest.mock import Mock, patch

from helpers import FAKE_KIRO, TempHome
from test_roles_cli import RolesBase
from test_roles_grid import press, role, table

from agents_router import config, roles, roles_cli, roles_grid, ui
from agents_router.model_catalog import ModelCatalog
from agents_router.share import picker
from agents_router.share.picker import Choice, ListState


class SingleSelect(unittest.TestCase):
    def state(self):
        return ListState("model", [Choice("", "default"), Choice("off", "off-plan", reason="not in this account's plan"),
                                   Choice("current", "current", checked=True), Choice("last", "last")], single=True)

    def test_current_is_marked_and_cursor_starts_on_it(self):
        state = self.state()
        self.assertEqual(state.cursor, 2)
        self.assertEqual(state.checked(), {"current"})
        self.assertIn("[x] current", "\n".join(state.lines(style="plain")))

    def test_off_plan_is_muted_and_skipped(self):
        state = self.state()
        muted = [piece.text for line in state.frame() if isinstance(line, list)
                 for piece in line if isinstance(piece, ui.Span) and piece.style == "muted"]
        self.assertIn("[-] off-plan", " ".join(muted))
        self.assertIn("not in this account's plan", " ".join(muted))
        state.press("up")
        self.assertEqual(state.cursor, 0)
        state.press("down")
        self.assertEqual(state.cursor, 2)

    def test_space_and_enter_pick_exactly_one_and_close(self):
        for key in ("space", "enter"):
            with self.subTest(key=key):
                state = self.state()
                state.press("down")
                self.assertEqual(state.press(key), picker.DONE)
                self.assertEqual(state.checked(), {"last"})

    def test_cancel_preserves_selection_and_bulk_keys_do_nothing(self):
        state = self.state()
        for key in ("a", "n", "down"):
            state.press(key)
        self.assertEqual(state.press("esc"), picker.QUIT)
        self.assertEqual(state.checked(), {"current"})

    def test_refresh_keeps_cursor_even_when_indices_change(self):
        state = self.state()
        state.press("down")
        state.replace_choices([Choice("", "default"), Choice("last", "last"), Choice("current", "current", checked=True)])
        self.assertEqual(state.cursor, 1)
        self.assertEqual(state.checked(), {"current"})

    def test_checked_off_plan_stays_marked_but_cursor_is_allowed(self):
        state = ListState("model", [Choice("", "default"), Choice("old", "old", checked=True, reason="off-plan")], single=True)
        self.assertEqual(state.cursor, 0)
        self.assertEqual(state._mark(state.choices[1]), "[x]")

    def test_all_locked_cannot_pick(self):
        state = ListState("model", [Choice("pending", "loading", reason="")], single=True)
        self.assertIsNone(state.press("space"))
        self.assertIsNone(state.press("enter"))
        self.assertEqual(state.press("esc"), picker.QUIT)

    def test_single_fallback_picks_one_or_cancels(self):
        state = self.state()
        answers = iter(["2", "4"])
        self.assertEqual(state.fallback(lambda _: next(answers), lambda _: None), picker.DONE)
        self.assertEqual(state.checked(), {"last"})
        self.assertEqual(self.state().fallback(lambda _: "q", lambda _: None), picker.QUIT)


class ModelOverlay(TempHome):
    def setup_grid(self, runtime_name="claude", model=None, allowed=None, default=None, extra_roles=()):
        runtime = config.RuntimeConfig(runtime_name, self.primary, default_model=default, primary_models=allowed)
        runtime._found = []
        self.account = config.Account("primary", self.primary, is_primary=True)
        providers = [runtime_name + (f":/{model}" if model else "")]
        found = [role("builder", "edit", providers), *extra_roles]
        self.loaded = config.Config({runtime_name: runtime}, roles={item.name: item for item in found})
        self.runtime = runtime
        self.work = []
        self.catalog = ModelCatalog(cache=self.home / "cache" / "models", start=self.work.append,
                                    binary_identity=lambda *_: "fake-binary")
        choices = roles_cli._choices(self.loaded, self.catalog)
        self.grid = roles_grid.RoleGrid(found, choices, model_choices=roles_cli._model_choices(self.loaded, self.catalog, choices))
        return self.grid

    def ready(self):
        self.work.pop(0)()
        self.grid.poll()

    def text(self):
        return "\n".join(self.grid.lines(width=120, height=40, style="plain"))

    def test_loading_to_ready_updates_same_picker_and_does_not_change_grid(self):
        state = self.setup_grid()
        self.assertEqual(self.work, [])
        self.assertFalse(self.catalog.cache.exists())
        state.press("m")
        menu = state.model_picker
        self.assertTrue(state.pending)
        self.assertIn("model · builder · claude", self.text())
        self.assertIn("loading models…", self.text())
        self.assertTrue(any("loading models…" in ui.plain(line) for line in state.frame()))
        self.assertEqual(state.tables(), {})
        self.ready()
        self.assertIs(state.model_picker, menu)
        self.assertFalse(state.pending)
        self.assertNotIn("loading models…", self.text())
        self.assertEqual([choice.key for choice in menu.choices], ["", "opus", "sonnet", "haiku", "fable"])
        self.assertIn("default (claude's own)", self.text())
        self.assertEqual(state.tables(), {})

    def test_grid_shows_selected_model_and_change_summary(self):
        state = self.setup_grid()
        state.press("m")
        self.ready()
        press(state, "down", "space")
        self.assertIsNone(state.model_picker)
        self.assertEqual(state.tables(), {"builder": {"providers": ["claude:/opus"]}})
        self.assertIn("opus/1", "\n".join(table(state)))
        self.assertEqual(state.changes(), ["claude model default → opus"])
        self.assertIn("m pick model", self.text())
        self.assertEqual(state.press("enter"), picker.DONE)

    def test_picking_default_sets_none(self):
        state = self.setup_grid(model="opus")
        state.press("m")
        self.ready()
        self.assertEqual(state.model_picker.choices[state.model_picker.cursor].key, "opus")
        press(state, "up", "enter")
        self.assertIsNone(state.entries[0].model)
        self.assertEqual(state.tables(), {"builder": {"providers": ["claude"]}})

    def test_escape_only_closes_menu_and_changes_nothing(self):
        for after_ready in (False, True):
            with self.subTest(after_ready=after_ready):
                state = self.setup_grid(model="sonnet")
                state.press("m")
                if after_ready:
                    self.ready()
                press(state, "down", "esc")
                self.assertIsNone(state.model_picker)
                self.assertFalse(state.pending)
                self.assertEqual(state.tables(), {})
                self.assertEqual(state.press("esc"), picker.QUIT)

    def test_configured_unlisted_model_remains_selectable(self):
        state = self.setup_grid(model="custom")
        state.press("m")
        self.ready()
        menu = state.model_picker
        self.assertIn("custom  (set, not offered)", self.text())
        self.assertEqual(menu.choices[menu.cursor].key, "custom")
        self.assertFalse(menu.choices[menu.cursor].locked)
        state.press("space")
        self.assertIsNone(state.model_picker)
        self.assertEqual(state.tables(), {})

    def test_plan_keeps_offered_models_visible_greyed_and_skips_them(self):
        state = self.setup_grid(model="haiku", allowed=["haiku", "custom-allowed"])
        state.press("m")
        self.ready()
        rows = state.model_picker.choices
        self.assertEqual([row.key for row in rows], ["", "opus", "sonnet", "haiku", "fable", "custom-allowed"])
        for row in rows:
            self.assertEqual(row.locked, row.key in ("opus", "sonnet", "fable"))
            if row.locked:
                self.assertEqual(row.reason, "not in this account's plan")
        press(state, "down")
        self.assertEqual(rows[state.model_picker.cursor].key, "custom-allowed")
        state.press("up")
        self.assertEqual(rows[state.model_picker.cursor].key, "haiku")
        state.press("up")
        self.assertEqual(rows[state.model_picker.cursor].key, "")

    def test_disallowed_configured_and_default_models_stay_visible(self):
        state = self.setup_grid(model="custom", allowed=["haiku"], default="opus")
        state.press("m")
        self.ready()
        rows = {row.key: row for row in state.model_picker.choices}
        for key in ("custom", "", "opus"):
            self.assertTrue(rows[key].locked)
        self.assertTrue(rows["custom"].checked)
        self.assertIn("(set, not offered)", rows["custom"].text)
        self.assertEqual(state.model_picker.choices[state.model_picker.cursor].key, "haiku")

    def test_unavailable_retains_allowed_configured_and_default_models(self):
        state = self.setup_grid("antigravity", model="custom", allowed=["custom", "allowed"])
        state.press("m")
        self.ready()
        self.assertIn("no model list for antigravity", self.text())
        self.assertNotIn("loading models", self.text())
        rows = {row.key: row for row in state.model_picker.choices}
        self.assertFalse(rows["custom"].locked)
        self.assertFalse(rows["allowed"].locked)
        self.assertFalse(rows[""].locked)

    def test_allowed_configured_and_runtime_default_merge_after_offered(self):
        state = self.setup_grid(model="custom", allowed=["haiku", "allowed"], default="runtime-model",
                                extra_roles=[role("other", "read", ["claude:/other-model"])])
        state.press("m")
        self.ready()
        self.assertEqual([row.key for row in state.model_picker.choices],
                         ["", "opus", "sonnet", "haiku", "fable", "allowed", "custom", "other-model", "runtime-model"])

    def test_codex_no_limits_or_default_has_visible_cached_models(self):
        state = self.setup_grid("codex")
        (self.primary / "models_cache.json").write_text(json.dumps({"models": [
            {"slug": "fake-codex", "visibility": "list"}, {"slug": "hidden", "visibility": "hide"}]}))
        state.press("m")
        self.ready()
        self.assertEqual([row.key for row in state.model_picker.choices], ["", "fake-codex"])

    def test_fake_kiro_no_limits_or_default_has_models(self):
        state = self.setup_grid("kiro")
        with patch.dict(os.environ, {"AGT_KIRO_COMMAND": FAKE_KIRO}):
            state.press("m")
            self.ready()
        self.assertEqual([row.key for row in state.model_picker.choices], ["", "auto", "claude-sonnet-4.5"])
        self.assertTrue(list(self.catalog.cache.glob("*.json")))

    def test_effort_steps_without_model_discovery(self):
        state = self.setup_grid()
        state.press("e")
        self.assertEqual(state.entries[0].effort, "minimal")
        self.assertEqual(self.work, [])

    def test_missing_account_does_not_discover_on_the_primary_account(self):
        self.setup_grid()
        found = [role("builder", "edit", ["claude:deleted/custom"])]
        self.loaded.roles = {item.name: item for item in found}
        choices = roles_cli._choices(self.loaded, self.catalog)
        self.grid = roles_grid.RoleGrid(found, choices,
                                       model_choices=roles_cli._model_choices(self.loaded, self.catalog, choices))
        self.grid.press("m")
        self.assertEqual(self.work, [])
        self.assertIn("no model list for claude", self.text())
        self.assertIn("custom  (set, not offered)", self.text())

    def test_roles_show_and_json_do_not_create_catalog_or_change_bytes(self):
        self.setup_grid()

        def shown(as_json):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                roles_cli._show(as_json)
            return output.getvalue().encode()

        with patch.object(roles_cli, "_loaded", return_value=self.loaded), \
                patch.object(roles_cli, "show_problem", return_value=None), \
                patch.object(roles_cli, "_agent_notes", return_value=[]), \
                patch.object(roles_cli, "_parts_notes", return_value=[]), \
                patch("agents_router.role_agents.agent_files", return_value=[]):
            before = {mode: shown(mode) for mode in (False, True)}
            self.grid.press("m")
            self.ready()
            with patch("agents_router.model_catalog.ModelCatalog", side_effect=AssertionError("show must not discover")):
                self.assertEqual({mode: shown(mode) for mode in (False, True)}, before)

    def test_choose_redraws_loading_then_ready_without_another_real_key(self):
        state = self.setup_grid()
        frames = []

        def keys(terminal):
            yield "m"
            self.assertTrue(terminal.pending())
            self.work.pop(0)()
            yield ""  # the terminal's 100 ms poll tick
            self.assertFalse(terminal.pending())
            yield "down"
            yield "enter"
            yield "esc"

        with patch.object(picker, "can_draw", return_value=True), \
                patch.object(picker.Terminal, "__enter__", return_value=picker.Terminal()), \
                patch.object(picker.Terminal, "__exit__", return_value=None), \
                patch.object(picker.Terminal, "keys", keys), \
                patch.object(picker.Terminal, "draw", lambda _, lines: frames.append("\n".join(lines))):
            self.assertEqual(picker.choose(state), picker.QUIT)
        self.assertTrue(any("loading models…" in frame for frame in frames))
        self.assertTrue(any("opus" in frame and "loading models…" not in frame for frame in frames))
        self.assertEqual(state.entries[0].model, "opus")

    def test_discovery_exception_becomes_unavailable_without_escaping_key_loop(self):
        state = self.setup_grid("kiro")
        adapter = Mock()
        adapter.discover_models.side_effect = TypeError("unexpected discovery error")
        self.catalog.runtime_factory = lambda _: adapter
        frames = []

        def keys(terminal):
            yield "m"
            self.assertTrue(terminal.pending())
            self.work.pop()()
            yield ""
            self.assertFalse(terminal.pending())
            yield "esc"  # cancel the menu
            yield "esc"  # leave the grid

        with patch.object(picker, "can_draw", return_value=True), \
                patch.object(picker.Terminal, "__enter__", return_value=picker.Terminal()), \
                patch.object(picker.Terminal, "__exit__", return_value=None), \
                patch.object(picker.Terminal, "keys", keys), \
                patch.object(picker.Terminal, "draw", lambda _, lines: frames.append("\n".join(lines))):
            self.assertEqual(picker.choose(state), picker.QUIT)
        self.assertTrue(any("no model list for kiro" in frame for frame in frames))
        self.assertEqual(state.tables(), {})


class WorkerPool(TempHome):
    def setup_pool(self, *, model=None, default=None):
        self.worker = config.Account("work", self.root / "work", models=["work-only"])
        self.worker.home.mkdir()
        self.runtime = config.RuntimeConfig("codex", self.primary, include_primary=False,
                                            primary_models=["primary-only"], default_model=default,
                                            accounts=[self.worker])
        self.runtime._found = []
        found = [role("builder", "edit", ["codex" + (f":/{model}" if model else "")])]
        self.loaded = config.Config({"codex": self.runtime}, roles={item.name: item for item in found})
        self.work = []
        self.catalog = ModelCatalog(cache=self.home / "cache" / "models", start=self.work.append,
                                    binary_identity=lambda *_: "fake-codex")
        self.choices = roles_cli._choices(self.loaded, self.catalog)
        self.menu = roles_cli._model_choices(self.loaded, self.catalog, self.choices)
        self.grid = roles_grid.RoleGrid(found, self.choices, model_choices=self.menu)
        return self.grid

    def cache(self, account, *models):
        (account.home / "models_cache.json").write_text(json.dumps({"models": [
            {"slug": model, "visibility": "list"} for model in models]}))

    def finish(self, index=0):
        self.work.pop(index)()
        self.grid.poll()

    def rows(self):
        return {row.key: row for row in self.grid.model_picker.choices}

    def test_excluded_primary_is_neither_discovered_nor_used_for_plans(self):
        self.setup_pool(model="primary-only")
        self.cache(self.runtime.primary_account(), "primary-cache-only")
        self.cache(self.worker, "work-only")
        self.grid.press("m")
        self.assertEqual(len(self.work), 1)
        self.finish()
        self.assertEqual(list(self.rows()), ["", "work-only", "primary-only"])
        self.assertFalse(self.rows()["work-only"].locked)
        self.assertTrue(self.rows()["primary-only"].locked)
        self.assertEqual(self.catalog.snapshot(self.runtime, self.runtime.primary_account()).status, "unavailable")

    def test_disjoint_worker_plans_union_and_disabled_worker_is_excluded(self):
        self.setup_pool()
        second = config.Account("second", self.root / "second", models=["second-only"])
        second.home.mkdir()
        disabled = config.Account("disabled", self.root / "disabled", enabled=False, models=["disabled-only"])
        self.runtime.accounts.extend([second, disabled])
        self.cache(self.worker, "work-only", "blocked")
        self.cache(second, "second-only", "work-only")
        self.grid.press("m")
        self.assertEqual(len(self.work), 2)
        self.finish()
        self.finish()
        rows = self.rows()
        self.assertEqual(list(rows), ["", "work-only", "blocked", "second-only"])
        self.assertFalse(rows["work-only"].locked)
        self.assertFalse(rows["second-only"].locked)
        self.assertTrue(rows["blocked"].locked)
        self.assertNotIn("disabled-only", rows)
        for model in ("work-only", "second-only"):
            self.assertIsNone(roles_cli.account_problem(self.loaded, roles.parse_provider(f"codex:/{model}")))

    def test_visible_models_remain_while_another_worker_loads_or_has_no_cache(self):
        self.setup_pool()
        self.worker.models = None
        second = config.Account("missing", self.root / "missing")
        self.runtime.accounts.append(second)
        self.cache(self.worker, "offered-only")
        self.grid.press("m")
        self.assertNotIn("offered-only", self.rows())
        self.finish()
        self.assertTrue(self.grid.pending)
        self.assertIn("offered-only", self.rows())
        self.assertFalse(self.rows()["offered-only"].locked)
        self.assertIn("status:loading", self.rows())
        self.finish()
        self.assertFalse(self.grid.pending)
        self.assertIn("offered-only", self.rows())
        self.assertNotIn("status:unavailable", self.rows())

    def test_empty_pool_keeps_configured_and_default_ids_without_primary_fallback(self):
        self.setup_pool(model="configured", default="default-id")
        self.worker.enabled = False
        # A primary account explicitly turned off makes worker_accounts() empty after enabled filtering.
        self.runtime.accounts.append(config.Account("primary", self.primary, enabled=False, models=["primary-only"]))
        self.assertEqual(roles_cli._model_accounts(self.runtime, self.grid.entries[0]), [])
        self.grid.press("m")
        self.assertEqual(self.work, [])
        self.assertEqual(list(self.rows()), ["", "configured", "default-id", "status:unavailable"])
        self.assertTrue(all(row.locked for row in self.rows().values()))
        self.assertIn("no enabled worker accounts",
                      roles_cli.account_problem(self.loaded, roles.parse_provider("codex:/configured")))

    def test_pinned_primary_uses_only_its_own_list_and_plan(self):
        self.setup_pool()
        self.cache(self.runtime.primary_account(), "primary-only")
        self.cache(self.worker, "work-only")
        entry = roles_grid.Entry(1, "codex", account="primary")
        rows, pending = self.menu(entry, True)
        self.assertTrue(pending)
        self.assertEqual(len(self.work), 1)
        self.work.pop()()
        rows, pending = self.menu(entry, False)
        self.assertFalse(pending)
        self.assertEqual([row.key for row in rows], ["", "primary-only"])
        self.assertIsNone(roles_cli.account_problem(self.loaded, roles.parse_provider("codex:primary/primary-only")))
        self.assertIn("doesn't allow", roles_cli.account_problem(self.loaded, roles.parse_provider("codex:primary/work-only")))

    def test_model_pool_does_not_change_effort_choices_or_consult_temporary_usage(self):
        self.setup_pool()
        self.runtime.primary_max_effort = "high"
        self.worker.max_effort = "low"
        with patch("agents_router.accounts.load_usage", side_effect=AssertionError("temporary usage is irrelevant")):
            self.assertEqual(self.choices(self.grid.entries[0])[1], [None, "minimal", "low", "medium", "high"])
            self.assertIsNone(roles_cli.account_problem(self.loaded, roles.parse_provider("codex:/work-only")))
            self.grid.press("m")
        self.assertFalse(self.rows()["work-only"].locked)

    def test_json_keeps_structure_but_corrects_unpinned_model_eligibility(self):
        self.setup_pool()
        self.loaded.roles["builder"].providers = [roles.parse_provider(f"codex:/{model}")
                                                 for model in ("work-only", "primary-only")]
        with patch("agents_router.role_agents.agent_files", return_value=[]), \
                patch("agents_router.role_agents.here", return_value=None), \
                patch("agents_router.role_agents.archived", return_value=[]):
            payload = roles_cli._table_json(self.loaded)
        providers = payload["roles"][0]["providers"]
        self.assertEqual(set(payload), {"roles", "files"})
        self.assertEqual(set(providers[0]), {"provider", "entry", "model", "effort", "usable", "reason"})
        self.assertTrue(providers[0]["usable"])
        self.assertIsNone(providers[0]["reason"])
        self.assertFalse(providers[1]["usable"])
        self.assertIn("no enabled codex worker account allows", providers[1]["reason"])


class WorkerPoolSave(RolesBase):
    def test_saving_unpinned_work_only_model_and_refusing_primary_only_model(self):
        self.user_file.write_text(
            '[codex]\ninclude_primary = false\nprimary_models = ["primary-only"]\n\n'
            f'[[codex.accounts]]\nname = "work"\nhome = {json.dumps(str(self.root / "codex-work"))}\n'
            'models = ["work-only"]\n\n[roles.reviewer]\nproviders = ["codex"]\n', encoding="utf-8")
        code, out, err = self.agt("roles", "config", "--set-model", "codex=work-only", terminal=True)
        self.assertEqual(code, 0, err)
        self.assertIn('providers = ["codex:/work-only"]', self.user_file.read_text())
        self.assertIn("Saved to", out)
        before = self.user_file.read_bytes()
        code, _, err = self.agt("roles", "config", "--set-model", "codex=primary-only", terminal=True)
        self.assertEqual(code, 2)
        self.assertIn("no enabled codex worker account allows the model primary-only", err)
        self.assertEqual(self.user_file.read_bytes(), before)


class TerminalPolling(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX key reader")
    def test_posix_pending_poll_yields_tick_then_reads_keys(self):
        terminal = picker.Terminal()
        terminal.fd = 123
        terminal.pending = lambda: True
        with patch("select.select", side_effect=[([], [], []), ([123], [], [])]) as select, \
                patch("os.read", return_value=b"\r"):
            keys = terminal.keys()
            self.assertEqual(next(keys), "")
            self.assertEqual(next(keys), "enter")
            self.assertEqual(select.call_args_list[0].args[-1], 0.1)

    def test_windows_pending_poll_uses_nonblocking_key_probe(self):
        msvcrt = Mock()
        msvcrt.kbhit.side_effect = [False, True]
        msvcrt.getwch.return_value = "\r"
        terminal = picker.Terminal()
        terminal.pending = lambda: True
        with patch.object(picker.os, "name", "nt"), patch.dict("sys.modules", {"msvcrt": msvcrt}), \
                patch.object(picker.time, "sleep") as sleep:
            keys = terminal.keys()
            self.assertEqual(next(keys), "")
            self.assertEqual(next(keys), "enter")
            sleep.assert_called_once_with(0.1)


if __name__ == "__main__":
    unittest.main()
