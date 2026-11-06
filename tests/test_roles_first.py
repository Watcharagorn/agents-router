"""Roles-first candidates and bounded hook text, using only throwaway profiles and settings."""

import contextlib
from functools import partial
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from unittest.mock import patch

from helpers import TempHome

from agents_router import config, hooks, jobs, role_agents, roles, roles_first

SETTINGS = '[roles.builder]\nproviders = ["claude"]\n[roles.reviewer]\nproviders = ["codex"]\n'


class CatalogBase(TempHome):
    def setUp(self):
        super().setUp()
        self.env = dict(os.environ, CLAUDECODE="1")
        self.env.pop("AGT_JOB_ID", None)
        self.env.pop("AGT_DEPTH", None)
        self.agents = self.primary / "agents"
        self.agents.mkdir()
        (self.project / ".git").mkdir()
        self.settings(SETTINGS)
        self.agent("builder", "Use for edits")

    def settings(self, text):
        (self.home / "settings.toml").write_text(text, encoding="utf-8")

    def workspace(self, text="", local=None):
        folder = self.project / ".agt"
        folder.mkdir(exist_ok=True)
        (folder / "settings.toml").write_text(text, encoding="utf-8")
        if local is not None:
            (folder / "settings.local.toml").write_text(local, encoding="utf-8")

    def agent(self, name, sentence="Use for this role", *, managed=True, description=None, header=None):
        description = description if description is not None else role_agents.description(roles.Role(name, "read"), sentence)
        marker = f"# {role_agents.MARKER_LINE}\n" if managed else ""
        header = header if header is not None else f"description: {json.dumps(description)}"
        path = self.agents / f"agt-{name}.md"
        path.write_text(f"---\n{marker}name: agt-{name}\n{header}\n---\nFake relay instructions.\n", encoding="utf-8")
        return path

    def catalog(self, hook_input=None, env=None, cwd=None):
        return roles_first.catalog({} if hook_input is None else hook_input,
                                   self.env if env is None else env, cwd or self.project)

    def long_roles(self):
        names = [f"role-{number:02}" for number in range(40)]
        self.settings("".join(f'[roles.{name}]\naccess = "read"\nproviders = ["codex"]\n' for name in names))
        for name in names:
            self.agent(name, "Use when " + "a long role sentence " * 20)
        return names


class SilenceTests(CatalogBase):
    def test_S8_agt_job_id_silences_the_catalog(self):
        for value in ("job-123", ""):
            with self.subTest(value=value):
                self.assertEqual(self.catalog(env=dict(self.env, AGT_JOB_ID=value)), [])

    def test_S8_positive_agt_depth_silences_the_catalog(self):
        for value in ("1", "2"):
            with self.subTest(value=value):
                self.assertEqual(self.catalog(env=dict(self.env, AGT_DEPTH=value)), [])
        self.assertEqual(self.catalog(env=dict(self.env, AGT_DEPTH="0")), [("agt-builder", "Use for edits")])

    def test_S9_agent_id_presence_silences_both_hook_inputs(self):
        for value in ("agent-123", "", None):
            for event in ("SessionStart", "PreToolUse"):
                with self.subTest(value=value, event=event):
                    self.assertEqual(self.catalog({"agent_id": value, "hook_event_name": event}), [])

    def test_S10_no_installed_role_agents(self):
        (self.agents / "agt-builder.md").unlink()
        self.agents.rmdir()
        self.assertEqual(self.catalog(), [])

    def test_S10_profile_comes_from_the_given_environment(self):
        other = self.root / "other-profile"
        self.assertEqual(self.catalog(env=dict(self.env, CLAUDE_CONFIG_DIR=str(other))), [])
        self.assertEqual(self.catalog(), [("agt-builder", "Use for edits")])

    def test_S10_default_profile_is_home_dot_claude(self):
        user_home = self.root / "user-home"
        default = user_home / ".claude" / "agents"
        default.mkdir(parents=True)
        (self.agents / "agt-builder.md").replace(default / "agt-builder.md")
        env = dict(self.env)
        env.pop("CLAUDE_CONFIG_DIR")
        with patch.dict(os.environ, {"HOME": str(user_home), "USERPROFILE": str(user_home)}):
            self.assertEqual(self.catalog(env=env), [("agt-builder", "Use for edits")])

    def test_S11_user_can_turn_roles_first_off(self):
        self.settings('[subagents]\nprefer_roles = false\n' + SETTINGS)
        self.assertEqual(self.catalog(), [])

    def test_S11_workspace_can_override_user_and_turn_roles_first_off(self):
        self.settings('[subagents]\nprefer_roles = true\n' + SETTINGS)
        self.workspace('[subagents]\nprefer_roles = false\n')
        self.assertEqual(self.catalog(), [])

    def test_S11_local_can_override_workspace_and_user(self):
        self.settings('[subagents]\nprefer_roles = false\n' + SETTINGS)
        self.workspace('[subagents]\nprefer_roles = true\n')
        self.assertEqual(self.catalog(), [("agt-builder", "Use for edits")])
        self.workspace('[subagents]\nprefer_roles = true\n', local='[subagents]\nprefer_roles = false\n')
        self.assertEqual(self.catalog(), [])
        self.workspace('[subagents]\nprefer_roles = false\n', local='[subagents]\nprefer_roles = true\n')
        self.assertEqual(self.catalog(), [("agt-builder", "Use for edits")])

    def test_non_claude_code_environment_is_silent_even_with_a_profile(self):
        for extra in ({}, {"CODEX_THREAD_ID": "codex-test"}, {"CLAUDE_CODE_ENTRYPOINT": "cli"}):
            env = dict(self.env, **extra)
            env.pop("CLAUDECODE", None)
            with self.subTest(extra=extra):
                self.assertEqual(self.catalog(env=env), [])

    def test_plan_mode_is_not_a_catalog_silence_rule(self):
        self.assertEqual(self.catalog({"permission_mode": "plan"}), [("agt-builder", "Use for edits")])


class EligibilityTests(CatalogBase):
    def test_S12_disabled_role_is_excluded(self):
        self.settings(SETTINGS.replace('[roles.builder]\n', '[roles.builder]\nenabled = false\n'))
        self.agent("reviewer", "Use for reviews")
        self.assertEqual(self.catalog(), [("agt-reviewer", "Use for reviews")])

    def test_S12_workspace_role_disable_overrides_user(self):
        self.workspace('[roles.builder]\nenabled = false\n')
        self.assertEqual(self.catalog(), [])

    def test_S12_unmarked_file_is_excluded(self):
        self.agent("builder", managed=False)
        self.agent("reviewer", "Use for reviews")
        self.assertEqual(self.catalog(), [("agt-reviewer", "Use for reviews")])

    def test_S12_management_phrase_inside_a_description_is_not_a_marker(self):
        self.agent("builder", managed=False, description="Managed by agt roles: my own builder")
        self.assertEqual(self.catalog(), [])

    def test_S10_relay_files_are_excluded_even_with_the_marker(self):
        for name in ("claude", "codex", "antigravity", "kiro"):
            self.agent(name)
        self.assertEqual(self.catalog(), [("agt-builder", "Use for edits")])
        (self.agents / "agt-builder.md").unlink()
        self.assertEqual(self.catalog(), [])

    def test_S12_role_gone_from_settings_is_excluded(self):
        self.settings('[roles.reviewer]\nproviders = ["codex"]\n')
        self.agent("reviewer", "Use for reviews")
        self.assertEqual(self.catalog(), [("agt-reviewer", "Use for reviews")])

    def test_S10_no_settings_roles_means_no_candidates(self):
        self.settings("")
        self.assertEqual(self.catalog(), [])

    def test_S14_nested_workspace_agent_with_matching_frontmatter_name_shadows_role(self):
        team = self.project / ".claude" / "agents" / "team"
        team.mkdir(parents=True)
        (team / "x.md").write_text('---\nname: "agt-builder"\ndescription: Project builder\n---\n', encoding="utf-8")
        self.agent("reviewer", "Use for reviews")
        nested = self.project / "src" / "nested"
        nested.mkdir(parents=True)
        self.assertEqual(self.catalog(cwd=nested), [("agt-reviewer", "Use for reviews")])

    def test_S14_matching_filename_with_a_different_frontmatter_name_does_not_shadow(self):
        team = self.project / ".claude" / "agents"
        team.mkdir(parents=True)
        (team / "agt-builder.md").write_text('---\nname: my-builder\ndescription: My agent\n---\n', encoding="utf-8")
        self.assertEqual(self.catalog(), [("agt-builder", "Use for edits")])

    def test_S14_role_file_with_a_different_agent_name_is_not_a_candidate(self):
        path = self.agents / "agt-builder.md"
        path.write_text(path.read_text(encoding="utf-8").replace("name: agt-builder", "name: my-builder"), encoding="utf-8")
        self.assertEqual(self.catalog(), [])


class SentenceTests(CatalogBase):
    def test_sentence_without_a_trailing_period(self):
        self.agent("builder", "Use for edits without a full stop")
        self.assertEqual(self.catalog(), [("agt-builder", "Use for edits without a full stop")])

    def test_sentence_multiline_frontmatter_forms(self):
        tail = role_agents.description(roles.Role("builder", "read"), "PLACEHOLDER").removeprefix("PLACEHOLDER ")
        for header in (f"description: >\n  Use for edits\n  and migrations. {tail}",
                       f"description: |\n  Use for edits\n  and migrations. {tail}",
                       f"description: Use for edits\n  and migrations. {tail}"):
            with self.subTest(header=header.splitlines()[0]):
                self.agent("builder", header=header)
                self.assertEqual(self.catalog(), [("agt-builder", "Use for edits and migrations.")])

    def test_sentence_user_suffix_after_the_managed_anchor_is_not_listed(self):
        about = role_agents.description(roles.Role("builder", "read"), "Use for edits.") + " User suffix."
        self.agent("builder", description=about)
        self.assertEqual(self.catalog(), [("agt-builder", "Use for edits.")])

    def test_sentence_role_name_appearing_twice_is_preserved(self):
        sentence = "The builder builds things; ask the builder for edits"
        self.agent("builder", sentence)
        self.assertEqual(self.catalog(), [("agt-builder", sentence)])

    def test_sentence_uses_the_last_role_specific_anchor(self):
        sentence = 'Runs the builder role through Agents Router (quoted). Use for edits.'
        self.agent("builder", sentence)
        self.assertEqual(self.catalog(), [("agt-builder", sentence)])

    def test_sentence_does_not_cut_at_another_roles_anchor(self):
        sentence = "Use after reviewer. Runs the reviewer role through Agents Router (quoted)."
        self.agent("builder", sentence)
        self.assertEqual(self.catalog(), [("agt-builder", sentence)])

    def test_sentence_is_trimmed_to_160_characters_and_one_line(self):
        self.agent("builder", "Use for edits\n" + "long sentence " * 40)
        sentence = self.catalog()[0][1]
        self.assertLessEqual(len(sentence), 160)
        self.assertGreater(len(sentence), 140)
        self.assertNotIn("\n", sentence)
        self.assertNotIn("Runs the builder role", sentence)


class FailureAndCostTests(CatalogBase):
    def test_settings_read_never_discovers_accounts_or_starts_subprocesses(self):
        self.workspace('[claude]\naccounts = ["stale-profile"]\n')
        with patch("subprocess.run", side_effect=AssertionError("no subprocess from a hook")) as run, \
                patch.object(config, "load_config", side_effect=AssertionError("no full config load")) as load, \
                patch.object(config.RuntimeConfig, "found_accounts", side_effect=AssertionError("no discovery")) as discover:
            self.assertEqual(self.catalog(), [("agt-builder", "Use for edits")])
            run.assert_not_called()
            load.assert_not_called()
            discover.assert_not_called()

    def test_given_environment_selects_the_user_settings_without_changing_process_environment(self):
        alternate = self.root / "alternate-settings"
        alternate.mkdir()
        (alternate / "settings.toml").write_text('[subagents]\nprefer_roles = false\n' + SETTINGS, encoding="utf-8")
        for variable in ("AGT_CONFIG_DIR", "AGT_HOME"):
            with self.subTest(variable=variable):
                env = dict(self.env, **{variable: str(alternate)})
                if variable == "AGT_HOME":
                    env.pop("AGT_CONFIG_DIR", None)
                self.assertEqual(self.catalog(env=env), [])
        self.assertEqual(self.catalog(), [("agt-builder", "Use for edits")])

    def test_S13_bad_settings_fail_open(self):
        for text in ('[broken\n', '[subagents]\nprefer_roles = "no"\n', '[roles.builder]\nenabled = "no"\n'):
            with self.subTest(text=text):
                self.settings(text)
                self.assertEqual(self.catalog(), [])

    def test_S13_unreadable_agent_file_fails_open(self):
        self.agent("reviewer", "Use for reviews")
        read_text = Path.read_text

        def read(path, *args, **kwargs):
            if path == self.agents / "agt-builder.md":
                raise PermissionError("unreadable agent")
            return read_text(path, *args, **kwargs)

        with patch.object(Path, "read_text", autospec=True, side_effect=read):
            self.assertEqual(self.catalog(), [])

    def test_S13_malformed_agent_frontmatter_fails_open(self):
        self.agent("builder", header='description: "unfinished')
        self.assertEqual(self.catalog(), [])

    def test_S13_unexpected_errors_and_invalid_hook_inputs_fail_open(self):
        with patch("agents_router.levels.layers", side_effect=RuntimeError("unexpected failure")):
            self.assertEqual(self.catalog(), [])
        for hook_input in (123, [], "not a hook object"):
            with self.subTest(hook_input=hook_input):
                self.assertEqual(self.catalog(hook_input=hook_input), [])
        self.assertEqual(self.catalog(env=dict(self.env, AGT_DEPTH="invalid")), [])

    def test_import_does_not_load_the_cli_or_runtime_modules(self):
        script = '''import sys
from unittest.mock import patch
with patch("subprocess.run", side_effect=AssertionError("no subprocess at import")):
    from agents_router import roles_first
assert "agents_router.cli" not in sys.modules
assert not any(name.startswith("agents_router.runtimes") for name in sys.modules)
assert "rich" not in sys.modules
'''
        result = subprocess.run([sys.executable, "-c", script], cwd=self.project, env=self.env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


class TextTests(CatalogBase):
    def test_note_and_deny_list_whole_sentences_in_name_order(self):
        self.agent("reviewer", "Use for reviews.")
        candidates = list(reversed(self.catalog()))
        note = roles_first.session_note(candidates)
        reason = roles_first.deny_reason(candidates, "Explore")
        self.assertIn("- agt-builder: Use for edits", note)
        self.assertIn("- agt-reviewer: Use for reviews.", note)
        self.assertIn("agt-builder (Use for edits)", reason)
        self.assertIn("agt-reviewer (Use for reviews.)", reason)
        for text in (note, reason):
            self.assertLess(text.index("agt-builder"), text.index("agt-reviewer"))
            self.assertNotIn("more (agt roles show)", text)
            self.assertIn("agt-fallback: <reason>", text)
        self.assertIn("Your own agents are called as usual when no role above does their job.", note)
        self.assertIn('It does not start a subagent with mode "plan" or a Plan agent to plan for it.', note)
        self.assertIn("a listed role is unavailable", reason)
        self.assertIn("Installed candidates:", reason)

    def test_caps_40_long_roles_keep_fixed_text_whole_rows_and_an_overflow_count(self):
        self.long_roles()
        candidates = self.catalog()
        self.assertEqual(len(candidates), 40)
        for builder, cap, row_format in ((roles_first.session_note, 4_000, "- {}: {}"),
                                         (partial(roles_first.deny_reason, agent_type="general-purpose"), 2_000, "- {} ({})")):
            with self.subTest(cap=cap):
                text = builder(list(reversed(candidates)))
                self.assertLessEqual(len(text), cap)
                self.assertIn("agt-fallback: <reason>", text)
                rows = [line for line in text.splitlines() if line.startswith("- agt-")]
                self.assertGreater(len(rows), 0)
                self.assertEqual(rows, [row_format.format(*candidate) for candidate in candidates[:len(rows)]])
                overflow = re.search(r"\+(\d+) more \(agt roles show\)$", text)
                self.assertIsNotNone(overflow)
                self.assertEqual(int(overflow[1]) + len(rows), 40)
                if cap == 2_000:
                    self.assertIn("asked for general-purpose by name, call general-purpose again", text)
        note = roles_first.session_note(candidates)
        self.assertIn("Planning stays in the main session:", note)
        self.assertIn("the role comes first.", note)

    def test_caps_a_role_too_large_to_fit_is_counted_without_cutting_it(self):
        for builder, cap in ((roles_first.session_note, 4_000),
                             (partial(roles_first.deny_reason, agent_type="general-purpose"), 2_000)):
            text = builder([("agt-builder", "x" * 5_000)])
            self.assertLessEqual(len(text), cap)
            self.assertNotIn("- agt-builder", text)
            self.assertIn("agt-fallback: <reason>", text)
            self.assertTrue(text.endswith("+1 more (agt roles show)"))

    def test_no_candidates_means_no_note_or_deny_reason(self):
        self.assertEqual(roles_first.session_note([]), "")
        self.assertEqual(roles_first.deny_reason([], "general-purpose"), "")


class RolesHookTests(CatalogBase):
    def payload(self, action, **extra):
        event = "SessionStart" if action == "roles-context" else "PreToolUse"
        return {"hook_event_name": event, "cwd": str(self.project), "source": "startup",
                "tool_name": "Agent", "tool_input": {"subagent_type": "Explore", "description": "Find code"},
                **extra}

    def run_hook(self, action, payload=None, *, raw=None, env=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        data = raw if raw is not None else json.dumps(self.payload(action) if payload is None else payload)
        with patch.dict(os.environ, self.env if env is None else env, clear=True), \
                patch("sys.stdin", io.StringIO(data)), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = hooks.main(action)
        self.assertEqual((code, stderr.getvalue()), (0, ""))
        return stdout.getvalue()

    def assert_silent(self, action, payload=None, **kwargs):
        self.assertEqual(self.run_hook(action, payload, **kwargs), "")

    def assert_note(self, output):
        specific = json.loads(output)["hookSpecificOutput"]
        self.assertEqual(specific["hookEventName"], "SessionStart")
        note = specific["additionalContext"]
        self.assertLessEqual(len(note), 4_000)
        self.assertIn("agt-fallback: <reason>", note)
        self.assertIn("Planning stays in the main session:", note)
        self.assertIn('It does not start a subagent with mode "plan" or a Plan agent to plan for it.', note)
        self.assertIn("Your own agents are called as usual when no role above does their job.", note)
        self.assertIn("When one does the same job as a role above,\nthe role comes first.", note)
        return note

    def assert_deny(self, output):
        specific = json.loads(output)["hookSpecificOutput"]
        self.assertEqual((specific["hookEventName"], specific["permissionDecision"]), ("PreToolUse", "deny"))
        reason = specific["permissionDecisionReason"]
        self.assertLessEqual(len(reason), 2_000)
        self.assertIn("agt-fallback: <reason>", reason)
        return reason

    def test_S1_session_note_on_startup_resume_clear_and_compact(self):
        self.agent("reviewer", "Use for reviews")
        for source in ("startup", "resume", "clear", "compact"):
            with self.subTest(source=source):
                note = self.assert_note(self.run_hook("roles-context", self.payload("roles-context", source=source)))
                self.assertIn("- agt-builder: Use for edits", note)
                self.assertIn("- agt-reviewer: Use for reviews", note)

    def test_S1_bounded_note_and_gate_include_each_candidate_or_an_overflow_count(self):
        names = self.long_roles()
        note = self.assert_note(self.run_hook("roles-context"))
        reason = self.assert_deny(self.run_hook("roles-first", self.payload("roles-first", tool_input={
            "subagent_type": "general-purpose", "description": "Make edits"})))
        self.assertIn("asked for general-purpose by name, call general-purpose again", reason)
        for text in (note, reason):
            listed = re.findall(r"^- (agt-role-\d+)[ :()]", text, re.MULTILINE)
            more = re.search(r"\+(\d+) more \(agt roles show\)$", text)
            self.assertIsNotNone(more)
            self.assertGreater(len(listed), 0)
            self.assertEqual(listed, [f"agt-{name}" for name in names[:len(listed)]])
            self.assertEqual(len(listed) + int(more[1]), len(names))

    def test_S1_pending_and_roles_context_leave_each_others_resume_output_unchanged(self):
        payload = self.payload("roles-context", source="resume")
        status = {"job_id": "job-for-resume", "state": "done", "outcome": "success", "finished_at": 123,
                  "cwd": str(self.project), "message": "A finished job"}
        folder = self.home / "jobs" / status["job_id"]
        folder.mkdir(parents=True)
        (folder / "status.json").write_text(json.dumps(status), encoding="utf-8")
        (folder / jobs.NOTIFY_PENDING).touch()
        pending_before = self.run_hook("pending", payload)
        context_before = self.run_hook("roles-context", payload)
        self.assertEqual(self.run_hook("pending", payload), pending_before)
        self.assertEqual(self.run_hook("roles-context", payload), context_before)
        pending = json.loads(pending_before)["hookSpecificOutput"]
        self.assertEqual(pending["hookEventName"], "SessionStart")
        self.assertIn(status["job_id"], pending["additionalContext"])
        self.assertNotIn("agt-builder", pending["additionalContext"])
        self.assertNotIn(status["job_id"], self.assert_note(context_before))

    def test_S1_S2_cli_hooks_emit_the_same_json_without_loading_the_full_cli(self):
        probe = '''import sys
from agents_router.entry import main
code = main()
assert "agents_router.cli" not in sys.modules
assert "rich" not in sys.modules
raise SystemExit(code)
'''
        for action in ("roles-context", "roles-first"):
            with self.subTest(action=action):
                payload = self.payload(action)
                done = subprocess.run([sys.executable, "-c", probe, "hook", action], input=json.dumps(payload),
                                      cwd=self.project, env=self.env, capture_output=True, text=True, timeout=10)
                self.assertEqual((done.returncode, done.stderr), (0, ""))
                self.assertEqual(json.loads(done.stdout), json.loads(self.run_hook(action, payload)))

    def test_S2_S3_gate_denies_only_the_general_purpose_builtins_and_missing_or_empty_type(self):
        self.agent("reviewer", "Use for reviews")
        for tool in ("Agent", "Task"):
            for kind in ("Explore", "Plan", "general-purpose", None, ""):
                tool_input = {"description": "Find code"}
                if kind is not None:
                    tool_input["subagent_type"] = kind
                with self.subTest(tool=tool, kind=kind):
                    payload = self.payload("roles-first", tool_name=tool, tool_input=tool_input)
                    reason = self.assert_deny(self.run_hook("roles-first", payload))
                    self.assertIn("agt-builder (Use for edits)", reason)
                    self.assertIn("agt-reviewer (Use for reviews)", reason)
                    expected = kind or "general-purpose"
                    self.assertIn(f"asked for {expected} by name", reason)
                    self.assertIn(f"call {expected} again", reason)
                    if expected != "Explore":
                        self.assertNotIn("call Explore", reason)

    def test_S4_plan_mode_keeps_the_note_and_allows_every_agent_type_without_reading_catalog(self):
        note = self.assert_note(self.run_hook("roles-context", self.payload("roles-context", permission_mode="plan")))
        self.assertIn("agt-builder", note)
        with patch.object(roles_first, "catalog", side_effect=AssertionError("plan mode must skip catalog")) as catalog:
            for kind in ("Explore", "Plan", "general-purpose", None, "", "agt-builder", "fork", "db-migrator"):
                with self.subTest(kind=kind):
                    self.assert_silent("roles-first", self.payload("roles-first", permission_mode="plan",
                                                                 tool_input={"subagent_type": kind}))
            catalog.assert_not_called()

    def test_S5_S6_fallback_prefix_allows_named_builtin_and_no_fitting_role(self):
        for description in ("agt-fallback: no read-only role installed", "  agt-fallback: user asked for Explore",
                            "\n\tagt-fallback: listed role unavailable"):
            with self.subTest(description=description), \
                    patch.object(roles_first, "catalog", side_effect=AssertionError("fallback must skip catalog")) as catalog:
                self.assert_silent("roles-first", self.payload("roles-first", tool_input={
                    "subagent_type": "Explore", "description": description}))
                catalog.assert_not_called()
        self.assert_deny(self.run_hook("roles-first", self.payload("roles-first", tool_input={
            "subagent_type": "Explore", "description": "Use agt-fallback: only later"})))

    def test_S7_S15_S16_specialist_plugin_role_and_user_agent_types_skip_the_catalog(self):
        with patch.object(roles_first, "catalog", side_effect=AssertionError("specialist must skip catalog")) as catalog:
            for kind in ("claude-code-guide", "web-fetch", "statusline-setup", "fork", "claude", "agt-researcher",
                         "agents-router:agt-codex", "other-plugin:agent", "db-migrator", "my-code-reviewer"):
                with self.subTest(kind=kind):
                    self.assert_silent("roles-first", self.payload("roles-first", tool_input={"subagent_type": kind}))
            catalog.assert_not_called()

    def test_S8_worker_environment_silences_both_hooks(self):
        for extra in ({"AGT_JOB_ID": "job-123"}, {"AGT_JOB_ID": ""}, {"AGT_DEPTH": "1"}):
            for action in ("roles-context", "roles-first"):
                with self.subTest(extra=extra, action=action):
                    self.assert_silent(action, env=dict(self.env, **extra))

    def test_S9_agent_id_presence_silences_both_hooks(self):
        for value in ("agent-123", "", None):
            for action in ("roles-context", "roles-first"):
                with self.subTest(value=value, action=action):
                    self.assert_silent(action, self.payload(action, agent_id=value))

    def test_S10_empty_catalog_and_non_claude_environment_silence_both_hooks(self):
        env = dict(self.env)
        env.pop("CLAUDECODE")
        for action in ("roles-context", "roles-first"):
            self.assert_silent(action, env=env)
        (self.agents / "agt-builder.md").unlink()
        for action in ("roles-context", "roles-first"):
            self.assert_silent(action)

    def test_S11_off_at_user_workspace_or_local_level_silences_both_hooks(self):
        off = '[subagents]\nprefer_roles = false\n'
        for level in ("user", "workspace", "local"):
            self.settings((off if level == "user" else "") + SETTINGS)
            self.workspace(off if level == "workspace" else "", local=off if level == "local" else "")
            for action in ("roles-context", "roles-first"):
                with self.subTest(level=level, action=action):
                    self.assert_silent(action)

    def test_S13_bad_settings_and_malformed_stdin_fail_open_in_both_hooks(self):
        for text in ('[broken\n', '[subagents]\nprefer_roles = "no"\n'):
            self.settings(text)
            for action in ("roles-context", "roles-first"):
                with self.subTest(text=text, action=action):
                    self.assert_silent(action)
        self.settings(SETTINGS)
        for raw in ("not json", "{", "", "[]", '"not an object"', "null"):
            for action in ("roles-context", "roles-first"):
                with self.subTest(raw=raw, action=action):
                    self.assert_silent(action, raw=raw)

    def test_S13_exceptions_from_catalog_text_and_stdin_fail_open(self):
        for action, formatter in (("roles-context", "session_note"), ("roles-first", "deny_reason")):
            for target in ("catalog", formatter):
                with self.subTest(action=action, target=target), \
                        patch.object(roles_first, target, side_effect=RuntimeError("broken hook")):
                    self.assert_silent(action)
            with patch("sys.stdin") as stdin:
                stdin.read.side_effect = OSError("cannot read stdin")
                with patch.dict(os.environ, self.env, clear=True), contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(hooks.main(action), 0)
                self.assertEqual(output.getvalue(), "")

    def test_S7_non_agent_tool_never_reads_the_catalog(self):
        with patch.object(roles_first, "catalog", side_effect=AssertionError("wrong tool must skip catalog")) as catalog:
            for tool in ("Read", "SendMessage", "Bash", "", None):
                with self.subTest(tool=tool):
                    self.assert_silent("roles-first", self.payload("roles-first", tool_name=tool))
            catalog.assert_not_called()
