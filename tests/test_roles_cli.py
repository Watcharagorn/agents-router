"""agt roles show|propose|config: the flag form, the levels, the user's-words gate, the report and undo, and the grid's save."""

import json
import os
import shlex
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

from helpers import FAKE_HOST, TempHome  # noqa: F401 - sets up the throwaway process first
from test_config_cli import AGENT, ConfigBase

from agents_router import role_agents, roles_cli, roles_grid, synclog

REAL_SYNC = role_agents.sync  # RolesBase patches it; a test that needs the real sync puts this back

ROLES = """# my roles
[roles.reviewer]
providers = ["codex:work/gpt-6.1-sol:high", "antigravity:primary"]

[roles.builder]
providers = ["claude:claude-work/opus", "codex:work/gpt-6.1-sol"]

[roles.verifier]
providers = ["claude:personal/sonnet", "kiro:primary"]
"""


class RolesBase(ConfigBase):
    def setUp(self):
        super().setUp()
        self.accounts = (
            "# accounts\n"
            f"[[codex.accounts]]\nname = \"work\"\nhome = {json.dumps(str(self.root / 'codex-work'))}\n\n"
            f"[[claude.accounts]]\nname = \"claude-work\"\nhome = {json.dumps(str(self.root / 'cw'))}\n\n"
            f"[[claude.accounts]]\nname = \"personal\"\nhome = {json.dumps(str(self.root / 'cp'))}\n\n")
        self.user_file.write_text(self.accounts + ROLES, encoding="utf-8")
        for folder in ("codex-work", "cw", "cp"):  # logged in, so `agt roles show` finds them usable
            (self.root / folder).mkdir(parents=True, exist_ok=True)
            (self.root / folder / "logged_in").touch()
        # `agt roles show` asks `codex login status`: the fake answers, never a real codex on PATH
        codex = patch.dict(os.environ, {"AGT_CODEX_COMMAND": json.dumps([sys.executable, str(FAKE_HOST), "codex"])})
        codex.start()
        self.addCleanup(codex.stop)
        self.synced = []
        sync = patch("agents_router.role_agents.sync", side_effect=self._sync)
        sync.start()
        self.addCleanup(sync.stop)

    def _sync(self, loaded, host=None, dry_run=False, **_):
        self.synced.append(sorted(name for name, role in loaded.roles.items() if role.enabled))
        return []

    def same_bytes(self, path, before):
        self.assertEqual(path.read_bytes() if path.exists() else None, before)

    def backups(self):
        folder = self.home / "state" / "backups" / "settings"
        return sorted(folder.glob("*.toml")) if folder.exists() else []

    def undo(self, out):
        line = next(line for line in out.splitlines() if line.startswith("Undo: "))
        return shlex.split(line[len("Undo: agt "):])


class ShowTests(RolesBase):
    def test_show_is_the_default_and_marks_off_roles_and_unusable_providers(self):
        self.user_file.write_text(self.accounts + ROLES.replace(
            '[roles.verifier]\n', '[roles.verifier]\nenabled = false\n') + '\n[roles.security-reviewer]\naccess = "read"\n'
            'providers = ["codex:gone"]\n', encoding="utf-8")
        code, out, _ = self.agt("roles")
        self.assertEqual(code, 0, out)
        lines = out.splitlines()
        self.assertRegex(lines[0], r"decision-maker|reviewer\s+builder\s+verifier\s+security-reviewer")
        self.assertRegex(lines[1], r"role on/off\s+on\s+on\s+off\s+on")
        self.assertRegex(out, r"codex:work\s+gpt-6.1-sol\s+high\s+✓\s+·")
        self.assertRegex(out, r"claude:claude-work\s+opus\s+—\s+·\s+1")
        self.assertIn("! security-reviewer: codex:gone: codex has no account gone", out)

    def test_show_json(self):
        code, shown = self.json("roles", "show")
        self.assertEqual(code, 0)
        builder = next(row for row in shown["roles"] if row["name"] == "builder")
        self.assertEqual((builder["access"], builder["enabled"], builder["level"]), ("edit", True, "user"))
        self.assertEqual([p["provider"] for p in builder["providers"]], ["claude:claude-work/opus", "codex:work/gpt-6.1-sol"])
        self.assertTrue(all(p["usable"] for p in builder["providers"]))

    def test_S7c_bad_hand_edit_named(self):
        self.user_file.write_text(self.accounts + ROLES.replace(
            '[roles.builder]\n', '[roles.builder]\naccess = "wirte"\n'), encoding="utf-8")
        code, out, err = self.agt("roles", "show", "--json", env=AGENT)
        self.assertEqual(code, 2)
        line = self.text(self.user_file).splitlines().index('access = "wirte"') + 1
        self.assertIn(f"{self.user_file}:{line}", err)
        self.assertIn("access must be", err)
        # the fix agt offers is a roles config command, which repairs the role
        code, out, err = self.agt("roles", "config", "builder", "--only", "codex:work", "--user-said", "fix it", env=AGENT)
        self.assertEqual(code, 0, out + err)
        self.assertIn('builder   dropped access = "wirte"', out)
        self.assertIn('[roles.builder]\nproviders = ["codex:work/gpt-6.1-sol"]\n', self.text(self.user_file))


    def broken(self, table, key_line):
        self.user_file.write_text(self.accounts + ROLES.replace(f"[roles.{table}]\n", f"[roles.{table}]\n{key_line}\n"),
                                  encoding="utf-8")

    def test_S7c_error_names_fix_command(self):
        self.broken("builder", 'access = "wirte"')
        code, _, err = self.agt("roles", "show", "--json", env=AGENT)
        self.assertEqual(code, 2)
        fix = err.strip().rpartition("; fix: ")[2]
        self.assertEqual(fix, "agt roles config builder --unset access --user")
        code, out, err = self.agt(*shlex.split(fix)[1:], env=AGENT)  # the exact command repairs it, no words needed
        self.assertEqual(code, 0, out + err)
        self.assertIn('builder   unset access (was "wirte")', out)
        text = self.text(self.user_file)
        self.assertNotIn("wirte", text)
        self.assertIn('[roles.builder]\nproviders = ["claude:claude-work/opus", "codex:work/gpt-6.1-sol"]\n', text)
        self.assertEqual(self.agt("roles", "show", "--json")[0], 0)

    def test_S7c_bad_providers_fix_is_only(self):
        self.user_file.write_text(self.text(self.user_file).replace(
            'providers = ["codex:work/gpt-6.1-sol:high", "antigravity:primary"]', 'providers = "codex:work"'), encoding="utf-8")
        code, _, err = self.agt("roles", "show", "--json", env=AGENT)
        self.assertEqual(code, 2)
        self.assertTrue(err.strip().endswith("; fix: agt roles config reviewer --only <providers> --user"), err)
        code, out, err = self.agt("roles", "config", "reviewer", "--only", "codex:work", "--user", env=AGENT)
        self.assertEqual(code, 0, out + err)
        self.assertIn('[roles.reviewer]\nproviders = ["codex:work"]\n', self.text(self.user_file))
        self.assertEqual(self.agt("roles", "show", "--json")[0], 0)

    def test_S7c_unset_repairs_bad_key_beside_a_second_broken_table(self):
        self.broken("builder", 'access = "wirte"')
        self.user_file.write_text(self.text(self.user_file).replace(
            "[roles.verifier]\n", '[roles.verifier]\nacess = "read"\n'), encoding="utf-8")
        code, out, err = self.agt("roles", "config", "builder", "--unset", "access", "--user", env=AGENT)
        self.assertEqual(code, 0, out + err)
        self.assertIn('verifier  dropped acess = "read": [roles.verifier] has no key acess', out)
        self.assertEqual(self.text(self.user_file), self.accounts + ROLES)
        self.assertEqual(self.agt("roles", "show", "--json")[0], 0)

    def test_S7c_unset_enabled_false_turns_a_role_on_so_needs_words(self):
        self.broken("verifier", "enabled = false")
        before = self.user_file.read_bytes()
        code, out, err = self.agt("roles", "config", "verifier", "--unset", "enabled", "--json", env=AGENT)
        self.assertEqual(code, 3, out + err)
        self.assertIn("verifier would be turned on", json.loads(out)["ask"])
        self.same_bytes(self.user_file, before)
        code, out, err = self.agt("roles", "config", "verifier", "--unset", "enabled", "--user-said", "turn it on",
                                  env=AGENT)
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("enabled", self.text(self.user_file))

    def test_S7c_unset_removes_an_override_and_refuses_a_key_not_in_the_file(self):
        self.local_file.write_text('[roles.reviewer]\nproviders = ["codex:work/gpt-6.1-sol:high"]\n', encoding="utf-8")
        code, _, err = self.agt("roles", "config", "reviewer", "--unset", "providers", "--workspace", env=AGENT)
        self.assertEqual(code, 2)
        self.assertIn("reviewer has no providers in the workspace file", err)
        code, _, err = self.agt("roles", "config", "reviewer", "--unset", "providers", "--delete", env=AGENT)
        self.assertEqual(code, 2)
        self.assertIn("--unset removes keys", err)
        code, out, err = self.agt("roles", "config", "reviewer", "--unset", "providers", "--local", env=AGENT)
        self.assertEqual(code, 0, out + err)
        self.assertIn("In effect here: codex:work/gpt-6.1-sol:high, antigravity:primary", out)


class ProposeTests(RolesBase):
    def test_propose_prints_and_json_is_the_from_file(self):
        proposal = {"roles": {"reviewer": {"providers": ["codex:work"]}}, "reasons": {"reviewer": "most quota left"}}
        with patch("agents_router.roles_propose.propose", return_value=proposal) as propose:
            code, shown = self.json("roles", "propose", "--rebalance")
            self.assertEqual((code, shown), (0, proposal))
            self.assertEqual(propose.call_args.kwargs["mode"], "rebalance")
            code, out, _ = self.agt("roles", "propose")
        self.assertEqual(code, 0)
        self.assertIn("reviewer  codex:work\n          most quota left\n", out)
        self.assertIn("Nothing written.", out)


    def rebalance_setup(self):
        import time
        from agents_router import accounts

        available = patch("agents_router.roles_propose._available", side_effect=lambda loaded, name: name in ("claude", "codex"))
        available.start()
        self.addCleanup(available.stop)
        resets = time.time() + 3600
        accounts.update_usage("claude", "claude-work", windows={"five_hour": {"utilization": 0.92, "resets_at": resets}})
        return time.strftime("%H:%M", time.localtime(resets))

    def test_S4_rebalance_shows_quota(self):
        at = self.rebalance_setup()
        code, shown = self.json("roles", "propose", "--rebalance")
        self.assertEqual(code, 0)
        work = next(item for item in shown["quota"] if item["account"] == "claude-work")
        self.assertEqual((work["runtime"], work["left_percent"]), ("claude", 8))
        self.assertTrue(work["resets_at"] and work["observed_at"])
        code, out, _ = self.agt("roles", "propose", "--rebalance")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(lines[0], f"claude claude-work 8% left (5h, resets {at})")  # the quota lines come first
        self.assertIn("claude personal unknown (no usage data)", lines[:4])
        self.assertIn(f"claude:claude-work has 8% left (under 20%) until it resets at {at}: moved back in the order", out)
        self.assertLess(lines.index(""), next(i for i, line in enumerate(lines) if line.startswith("builder")))

    def test_S4_skill_example_is_the_real_output(self):
        from pathlib import Path

        at = self.rebalance_setup()
        out = self.agt("roles", "propose", "--rebalance")[1].replace(at, "14:20")
        skill = (Path(__file__).resolve().parents[1] / "skills" / "orchestrator-rebalance" / "SKILL.md").read_text(encoding="utf-8")
        example = skill.split("```\nclaude claude-work", 1)[1].split("```", 1)[0]
        self.assertEqual("claude claude-work" + example, out)

    def test_S4_from_accepts_the_rebalance_file_with_quota(self):
        self.rebalance_setup()
        code, out, _ = self.agt("roles", "propose", "--rebalance", "--json")
        self.assertEqual(code, 0)
        proposal = self.root / "rebalance.json"
        proposal.write_text(out, encoding="utf-8")
        self.assertIn("quota", json.loads(out))
        code, out, err = self.agt("roles", "config", "--from", str(proposal), terminal=True)
        self.assertEqual(code, 0, err)
        self.assertIn('[roles.builder]\nproviders = ["codex:work/gpt-6.1-sol", "claude:claude-work/opus"]',
                      self.text(self.user_file))


class FlagTests(RolesBase):
    def test_S5_from_file_writes_custom_role(self):
        proposal = self.root / "proposal.json"
        proposal.write_text(json.dumps({"roles": {"security-reviewer": {
            "access": "read", "description": "Checks a change for security problems",
            "providers": ["codex:work/gpt-6.1-sol:high", "claude:claude-work/opus"]}}, "reasons": {}}), encoding="utf-8")
        code, out, err = self.agt("roles", "config", "--from", str(proposal), terminal=True)
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines()[0], "Added [roles.security-reviewer] (read, 2 providers, in parallel)")
        text = self.text(self.user_file)
        self.assertTrue(text.startswith(self.accounts + ROLES))  # comments and other tables kept
        self.assertIn('[roles.security-reviewer]\naccess = "read"\nproviders = ["codex:work/gpt-6.1-sol:high", '
                      '"claude:claude-work/opus"]\ndescription = "Checks a change for security problems"\n', text)
        self.assertIn("security-reviewer", self.synced[-1])  # its agent is synced after the write

    def test_write_table_is_quiet_and_leaves_the_sync_to_its_caller(self):
        import contextlib, io

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            report = roles_cli.write_table({"reviewer": {"providers": ["kiro:primary"]}}, level="user")
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(self.synced, [])
        self.assertEqual((report["level"], report["file"]), ("user", str(self.user_file)))
        self.assertTrue(report["backup"] and report["undo"].startswith("agt roles config --from "))
        self.assertIn('[roles.reviewer]\nproviders = ["kiro:primary"]', self.text(self.user_file))
        self.assertEqual(roles_cli.write_table({"reviewer": {"providers": ["kiro:primary"]}}, level="user")["changes"], {})

    def test_from_file_with_a_bad_table_writes_nothing(self):
        before = self.user_file.read_bytes()
        proposal = self.root / "p.json"
        proposal.write_text(json.dumps({"roles": {"reviewer": {"providers": ["codex:work"]},
                                                  "builder": {"access": "read", "providers": ["codex:work"]}}}),
                            encoding="utf-8")
        code, _, err = self.agt("roles", "config", "--from", str(proposal), terminal=True)
        self.assertEqual(code, 2)
        self.assertIn("builder is a base role with edit access", err)
        self.same_bytes(self.user_file, before)
        self.assertEqual(self.backups(), [])

    def test_edit_role_written_without_a_provider_list(self):
        before = self.user_file.read_bytes()
        proposal = self.root / "p.json"
        proposal.write_text(json.dumps({"roles": {"builder": {"providers": []}}}), encoding="utf-8")
        code, _, err = self.agt("roles", "config", "--from", str(proposal), terminal=True)
        self.assertEqual(code, 2)
        self.assertIn("role builder has edit access, so its providers need an order", err)
        self.same_bytes(self.user_file, before)
        self.assertEqual(self.backups(), [])

    def test_S6b_dry_run_writes_nothing(self):
        before = self.user_file.read_bytes()
        code, out, err = self.agt("roles", "config", "builder", "--order", "codex:work/gpt-6.1-sol,claude:claude-work/opus",
                                  "--set-effort", "codex:work=high", "--dry-run", terminal=True)
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines(), [
            "effort    codex:work → high in reviewer, builder",
            "builder   order  1 codex:work/gpt-6.1-sol:high   2 claude:claude-work/opus",
            f"Dry run: nothing written to {roles_cli.ui.tilde(str(self.user_file))} (user). "
            "Run again without --dry-run to save.",
        ])
        self.same_bytes(self.user_file, before)
        self.assertEqual(self.backups(), [])
        self.assertEqual(self.synced, [])

    def test_S6c_order_on_read_role_exit_2(self):
        before = self.user_file.read_bytes()
        code, _, err = self.agt("roles", "config", "reviewer", "--order", "codex:work,antigravity:primary", terminal=True)
        self.assertEqual(code, 2)
        self.assertIn("reviewer has read access, so its providers all run at once and have no order.", err)
        self.assertIn("Use --only, --add or --remove to choose them.", err)
        self.same_bytes(self.user_file, before)

    def test_S6d_last_provider_refused(self):
        self.user_file.write_text(self.accounts + '[roles.reviewer]\nproviders = ["codex:work"]\n', encoding="utf-8")
        before = self.user_file.read_bytes()
        for argv in (("--remove", "codex:work"), ("--only", "")):
            code, _, err = self.agt("roles", "config", "reviewer", *argv, terminal=True)
            self.assertEqual(code, 2, argv)
            self.assertIn("reviewer would have no providers left. To stop using it: agt roles config --disable reviewer", err)
            self.same_bytes(self.user_file, before)

    def test_refused_part_of_a_bulk_command_leaves_the_file_byte_identical(self):
        before = self.user_file.read_bytes()
        cases = [
            ("builder", "--add", "codex:nobody"),  # unknown account
            ("builder", "--remove", "kiro:primary"),  # not one of its providers
            ("--set-model", "kiro:other=x"),  # no role uses it
            ("--enable", "nosuch"),  # unknown role
            ("--disable", "verifier,nosuch"),  # one bad name refuses the whole list
            ("claude", "--access", "read", "--only", "codex:work"),  # reserved name
            ("builder", "--access", "read"),  # base role access
            ("builder", "--prompt", "be careful"),  # custom roles only
            ("builder", "--delete"),
            ("typo", "--add", "codex:work"),  # unknown role without --access and --only
            ("newrole", "--access", "read"),  # a new role needs --only too
            ("builder", "--only", "codex:work", "--add", "kiro:primary"),
            ("--add", "codex:work"),  # no role named
            ("builder",),  # nothing to change
        ]
        for argv in cases:
            code, out, err = self.agt("roles", "config", *argv, terminal=True)
            self.assertEqual(code, 2, (argv, out, err))
            self.same_bytes(self.user_file, before)
        self.assertEqual(self.backups(), [])
        _, _, err = self.agt("roles", "config", "typo", "--add", "codex:work", terminal=True)
        self.assertIn("there's no role typo. Roles: reviewer, builder, verifier", err)

    def test_model_the_account_doesnt_allow_is_refused(self):
        self.user_file.write_text(self.accounts.replace('name = "work"\n', 'name = "work"\nmodels = ["gpt-6.1-sol"]\n')
                                  + ROLES, encoding="utf-8")
        before = self.user_file.read_bytes()
        code, _, err = self.agt("roles", "config", "--set-model", "codex:work=gpt-9", terminal=True)
        self.assertEqual(code, 2)
        self.assertIn("doesn't allow the model gpt-9", err)
        self.same_bytes(self.user_file, before)

    def test_set_model_changes_the_entry_in_every_role_and_lists_them(self):
        code, out, err = self.agt("roles", "config", "--set-model", "codex:work=gpt-6.1-mini", terminal=True)
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines()[0], "model     codex:work → gpt-6.1-mini in reviewer, builder")
        text = self.text(self.user_file)
        self.assertIn('"codex:work/gpt-6.1-mini:high", "antigravity:primary"', text)
        self.assertIn('"claude:claude-work/opus", "codex:work/gpt-6.1-mini"', text)

    def test_custom_role_prompt_description_access_and_delete(self):
        code, out, err = self.agt("roles", "config", "security", "--access", "read", "--only", "codex:work",
                                  "--prompt", "Look for injection.", "--description", "Checks security", terminal=True)
        self.assertEqual(code, 0, err)
        prompt = self.user_file.parent / "roles" / "security.md"
        self.assertEqual(prompt.read_text(encoding="utf-8"), "Look for injection.\n")
        self.assertIn(f'prompt_file = {json.dumps(str(prompt))}', self.text(self.user_file))
        code, out, err = self.agt("roles", "config", "security", "--access", "edit", terminal=True)
        self.assertEqual(code, 0, err)
        self.assertIn("security  access read → edit", out)
        code, out, err = self.agt("roles", "config", "security", "--delete", terminal=True)
        self.assertEqual(code, 0, err)
        self.assertNotIn("roles.security", self.text(self.user_file))

    def test_a_new_custom_role_needs_its_description(self):
        before = self.user_file.read_bytes()
        for extra in ((), ("--description", ""), ("--description", "   ")):
            code, _, err = self.agt("roles", "config", "docs", "--access", "read", "--only", "codex:work", *extra,
                                    terminal=True)
            self.assertEqual(code, 2, (extra, err))
            self.assertIn("docs is a new custom role, so it needs --description", err)
            self.same_bytes(self.user_file, before)
        proposal = self.root / "p.json"
        proposal.write_text(json.dumps({"roles": {"docs": {"access": "read", "providers": ["codex:work"]}}}),
                            encoding="utf-8")
        code, _, err = self.agt("roles", "config", "--from", str(proposal), terminal=True)
        self.assertEqual(code, 2)
        self.assertIn('--from: roles.docs is a new custom role, so it needs "description"', err)
        self.same_bytes(self.user_file, before)
        self.assertEqual(self.backups(), [])
        _, _, err = self.agt("roles", "config", "typo", "--add", "codex:work", terminal=True)
        self.assertIn('--only <provider> --description "<what it does>"', err)  # the hint names a command that works

    def test_a_custom_role_keeps_its_description(self):
        code, _, err = self.agt("roles", "config", "docs", "--access", "read", "--only", "codex:work",
                                "--description", "Writes the docs", terminal=True)
        self.assertEqual(code, 0, err)
        before = self.user_file.read_bytes()
        for argv in (("docs", "--unset", "description"), ("docs", "--description", " ")):
            code, _, err = self.agt("roles", "config", *argv, terminal=True)
            self.assertEqual(code, 2, (argv, err))
            self.assertIn("docs needs its description", err)
            self.same_bytes(self.user_file, before)
        code, _, err = self.agt("roles", "config", "docs", "--description", "Writes and fixes the docs", terminal=True)
        self.assertEqual(code, 0, err)

    def test_a_rebalance_written_back_keeps_a_custom_role_description(self):
        code, _, err = self.agt("roles", "config", "docs", "--access", "read", "--only", "codex:work",
                                "--description", "Writes the docs", terminal=True)
        self.assertEqual(code, 0, err)
        code, out, err = self.agt("roles", "propose", "--rebalance", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["roles"]["docs"]["description"], "Writes the docs")
        proposal = self.root / "rebalance.json"
        proposal.write_text(out, encoding="utf-8")
        code, _, err = self.agt("roles", "config", "--from", str(proposal), terminal=True)
        self.assertEqual(code, 0, err)  # orchestrator-rebalance's write-back isn't refused
        self.assertIn('description = "Writes the docs"', self.text(self.user_file))

    def test_a_custom_role_from_before_descriptions_still_changes(self):
        self.user_file.write_text(self.accounts + ROLES + '\n[roles.old]\naccess = "read"\nproviders = ["codex:work"]\n',
                                  encoding="utf-8")
        code, _, err = self.agt("roles", "config", "old", "--add", "kiro:primary", terminal=True)
        self.assertEqual(code, 0, err)
        self.assertIn('[roles.old]', self.text(self.user_file))

    def test_workspace_prompt_file_is_relative_to_the_settings_file(self):
        code, out, err = self.agt("roles", "config", "sec", "--access", "read", "--only", "codex:work",
                                  "--description", "Checks security", "--prompt", "Look for injection.", "--workspace",
                                  terminal=True)
        self.assertEqual(code, 0, err)
        self.assertEqual((self.project / ".agt" / "roles" / "sec.md").read_text(encoding="utf-8"), "Look for injection.\n")
        self.assertIn('prompt_file = "roles/sec.md"', self.text(self.team_file))
        from agents_router.config import load_config

        self.assertEqual(load_config().roles["sec"].prompt(), "Look for injection.\n")

    def test_S9_local_prompt_text_gets_its_own_file_kept_out_of_git(self):
        if shutil.which("git") is None:
            self.skipTest("needs git")
        subprocess.run(["git", "init", "-q"], cwd=self.project, check=True)
        code, out, err = self.agt("roles", "config", "sec", "--access", "read", "--only", "codex:work",
                                  "--description", "Checks security", "--prompt", "Mine everywhere.", "--user",
                                  terminal=True)
        self.assertEqual(code, 0, err)
        mine = self.home / "roles" / "sec.md"
        self.assertEqual(mine.read_text(encoding="utf-8"), "Mine everywhere.\n")
        self.local_file.write_text("# already here\n", encoding="utf-8")  # the settings file exists already
        code, out, err = self.agt("roles", "config", "sec", "--prompt", "Only in this project.", "--local", terminal=True)
        self.assertEqual(code, 0, err)
        local = self.project / ".agt" / "roles" / "sec.local.md"
        self.assertEqual(local.read_text(encoding="utf-8"), "Only in this project.\n")
        self.assertEqual(mine.read_text(encoding="utf-8"), "Mine everywhere.\n")  # your user prompt is unchanged
        self.assertIn('prompt_file = "roles/sec.local.md"', self.text(self.local_file))
        from agents_router.config import load_config

        self.assertEqual(load_config().roles["sec"].prompt(), "Only in this project.\n")
        code, out, err = self.agt("roles", "config", "sec", "--prompt", "Changed my mind.", "--local", terminal=True)
        self.assertEqual(code, 0, err)  # the same prompt_file: only the text changes, and it does
        self.assertIn("prompt text changed", out)
        self.assertEqual(local.read_text(encoding="utf-8"), "Changed my mind.\n")
        self.team_file.write_text('[roles.qa]\naccess = "read"\nproviders = ["codex:work"]\n', encoding="utf-8")
        code, out, err = self.agt("roles", "config", "qa", "--prompt", "Check the edges.", "--local", terminal=True)
        self.assertEqual(code, 0, err)
        exclude = self.text(self.project / ".git" / "info" / "exclude").splitlines()
        self.assertIn("/.agt/roles/sec.local.md", exclude)
        self.assertIn("/.agt/roles/qa.local.md", exclude)
        status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=self.project,
                                capture_output=True, text=True, check=True).stdout
        self.assertNotIn(".local.md", status)

    def test_primary_is_the_main_account_and_default_is_not_an_account(self):
        code, _, err = self.agt("roles", "config", "reviewer", "--add", "kiro:primary", terminal=True)
        self.assertEqual(code, 0, err)
        before = self.user_file.read_bytes()
        code, _, err = self.agt("roles", "config", "reviewer", "--add", "kiro:default", terminal=True)
        self.assertEqual(code, 2)
        self.assertIn("kiro has no account default", err)
        self.same_bytes(self.user_file, before)

    def test_nothing_to_change(self):
        before = self.user_file.read_bytes()
        code, out, _ = self.agt("roles", "config", "reviewer", "--add", "antigravity:primary", terminal=True)
        self.assertEqual(code, 0)
        self.assertIn("Nothing to change", out)
        self.same_bytes(self.user_file, before)

    def test_change_goes_to_agt_log(self):
        code, _, err = self.agt("roles", "config", "verifier", "--remove", "kiro:primary", terminal=True)
        self.assertEqual(code, 0, err)
        entry = [e for e in synclog.entries() if e.get("key") == "roles.verifier"][-1]
        self.assertEqual(entry["old"]["providers"], ["claude:personal/sonnet", "kiro:primary"])
        self.assertEqual(entry["new"]["providers"], ["claude:personal/sonnet"])
        self.assertEqual(entry["level"], "user")


class AgentTests(RolesBase):
    def test_S7_agent_change_reports_file_and_undo(self):
        original = self.text(self.user_file)
        code, report = self.json("roles", "config", "verifier", "--only", "claude:personal/sonnet,antigravity:primary",
                                 "--dry-run", env=AGENT)
        self.assertEqual(code, 0)
        self.assertEqual((report["dry_run"], report["needs_user_words"], report["level"]), (True, False, "user"))
        self.assertEqual(report["lines"], ["verifier  - kiro:primary  + antigravity:primary"])
        self.assertEqual(self.text(self.user_file), original)
        code, out, err = self.agt("roles", "config", "verifier", "--only", "claude:personal/sonnet,antigravity:primary",
                                  env=AGENT)
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertEqual(lines[0], "verifier  - kiro:primary  + antigravity:primary")
        self.assertTrue(lines[1].startswith(f"Saved to {roles_cli.ui.tilde(str(self.user_file))} (user). Backup: "))
        self.assertEqual(lines[2], "In effect here: claude:personal/sonnet, antigravity:primary")
        self.assertTrue(lines[-1].startswith("Undo: agt roles config --from "))
        self.assertEqual([self.text(path) for path in self.backups()], [original])
        # the undo puts the role back as it was
        code, out, err = self.agt(*self.undo(out), env=AGENT)
        self.assertEqual(code, 0, err)
        self.assertIn('[roles.verifier]\nproviders = ["claude:personal/sonnet", "kiro:primary"]', self.text(self.user_file))

    def test_undo_refused_when_the_file_changed_since(self):
        code, out, err = self.agt("roles", "config", "verifier", "--remove", "kiro:primary", terminal=True)
        self.assertEqual(code, 0, err)
        undo = self.undo(out)
        self.agt("roles", "config", "reviewer", "--remove", "antigravity:primary", terminal=True)
        before = self.user_file.read_bytes()
        code, _, err = self.agt(*undo, terminal=True)
        self.assertEqual(code, 2)
        self.assertIn("changed since that change", err)
        self.same_bytes(self.user_file, before)

    def test_S7b_widen_edit_needs_user_said_exit_3(self):
        before = self.user_file.read_bytes()
        code, refusal = self.json("roles", "config", "builder", "--add", "kiro:primary", env=AGENT)
        self.assertEqual(code, 3)
        self.assertEqual(refusal["error"], "needs_user_words")
        self.assertEqual(refusal["role"], "builder")
        self.assertEqual(refusal["ask"], "kiro:primary would join builder, which edits files. Ask the user, and pass "
                                         "their own words.")
        self.assertEqual(refusal["retry"], "agt roles config builder --add kiro:primary --user-said \"<the user's words>\"")
        self.same_bytes(self.user_file, before)
        self.assertEqual(self.backups(), [])
        code, out, err = self.agt("roles", "config", "builder", "--add", "kiro:primary", "--user-said",
                                  "add kiro to the builder list", env=AGENT)
        self.assertEqual(code, 0, err)
        entry = [e for e in synclog.entries() if e.get("key") == "roles.builder"][-1]
        self.assertEqual((entry["by"], entry["user_said"]), ("agent", "add kiro to the builder list"))

    def test_every_widening_needs_words_and_narrowing_does_not(self):
        widening = [
            ("builder", "--order", "kiro:primary,claude:claude-work/opus"),  # --order brings in a new entry
            ("sec", "--access", "edit", "--only", "codex:work", "--description", "Fixes security bugs"),  # created as edit
            ("--disable", "verifier"),
            ("--enable", "builder"),
        ]
        self.user_file.write_text(self.accounts + ROLES.replace("[roles.builder]\n", "[roles.builder]\n")
                                  + '\n[roles.sec2]\naccess = "read"\nproviders = ["codex:work"]\n', encoding="utf-8")
        before = self.user_file.read_bytes()
        for argv in widening + [("sec2", "--access", "edit")]:
            code, out, err = self.agt("roles", "config", *argv, env=AGENT)
            if argv == ("--enable", "builder"):
                self.assertEqual(code, 0, out)  # already on: nothing to change, nothing to ask
                continue
            self.assertEqual(code, 3, (argv, out, err))
            self.same_bytes(self.user_file, before)
        for argv in (("reviewer", "--add", "claude:claude-work/opus"), ("builder", "--order", "codex:work,claude:claude-work"),
                     ("--set-effort", "codex:work=low"), ("builder", "--remove", "codex:work")):
            code, out, err = self.agt("roles", "config", *argv, env=AGENT)
            self.assertEqual(code, 0, (argv, out, err))

    def test_dry_run_from_an_agent_says_words_are_needed(self):
        code, report = self.json("roles", "config", "--disable", "verifier", "--dry-run", env=AGENT)
        self.assertEqual(code, 0)
        self.assertTrue(report["needs_user_words"])
        self.assertIn("--user-said", report["retry"])
        self.assertNotIn("--dry-run", report["retry"])

    def test_a_person_at_a_terminal_needs_no_words(self):
        code, _, err = self.agt("roles", "config", "builder", "--add", "kiro:primary", terminal=True)
        self.assertEqual(code, 0, err)

    def test_S8_disable_builder(self):
        code, out, err = self.agt("roles", "config", "--disable", "builder", terminal=True)
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines()[0], "builder   off (enabled = false)")
        self.assertIn('[roles.builder]\nproviders = ["claude:claude-work/opus", "codex:work/gpt-6.1-sol"]\nenabled = false\n',
                      self.text(self.user_file))
        self.assertNotIn("builder", self.synced[-1])  # the sync step gets the role as off
        code, out, err = self.agt("roles", "config", "--enable", "builder", terminal=True)
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines()[0], "builder   on")
        self.assertNotIn("enabled", self.text(self.user_file))
        self.assertIn("builder", self.synced[-1])

    def test_S8b_sync_lines_are_printed(self):
        with patch("agents_router.role_agents.sync", return_value=[
                "Kept ~/.claude/agents/agt-builder.md: it isn't agt's (no \"Managed by agt roles\" line)."]):
            code, out, _ = self.agt("roles", "config", "--disable", "builder", terminal=True)
        self.assertEqual(code, 0)
        self.assertIn("Kept ~/.claude/agents/agt-builder.md", out)


class AgentSourceTests(RolesBase):
    """Agent files come from your user settings alone (S7)."""

    def test_S7_an_incomplete_user_role_is_noted_and_the_write_still_saves(self):
        from agents_router import role_agents

        self.team_file.write_text('[roles.sec]\naccess = "read"\nproviders = ["codex:work"]\n', encoding="utf-8")
        with patch("agents_router.role_agents.sync", new=REAL_SYNC), \
                patch.object(role_agents, "_hosts", return_value=["claude"]), \
                patch.object(role_agents, "folders", return_value=[]):
            code, out, err = self.agt("roles", "config", "sec", "--description", "Security checks.", "--user",
                                      terminal=True)
        self.assertEqual(code, 0, err)
        self.assertIn('description = "Security checks."', self.text(self.user_file))
        self.assertIn("no agt-sec agent: your user settings alone don't make a whole sec role", out)

    def test_an_explicit_description_rewrites_the_sentence_in_every_file(self):
        calls = []

        def sync(loaded, host=None, dry_run=False, **kwargs):
            calls.append(kwargs)
            return ["Claude Code (main): installed role agent agt-sec at ~/.claude/agents/agt-sec.md"]

        with patch("agents_router.role_agents.sync", side_effect=sync):
            code, out, err = self.agt("roles", "config", "sec", "--access", "read", "--only", "codex:work",
                                      "--description", "Security checks.", "--user", terminal=True)
        self.assertEqual(code, 0, err)
        self.assertEqual(calls[-1]["fresh"], ["sec"])
        self.assertIn("installed role agent agt-sec", out)
        with patch("agents_router.role_agents.sync", side_effect=sync):
            self.agt("roles", "config", "sec", "--add", "kiro:primary", terminal=True)
        self.assertEqual(calls[-1]["fresh"], ())  # any other change keeps your sentence

    def test_S7_show_names_a_role_with_no_agent(self):
        self.team_file.write_text('[roles.sec]\naccess = "read"\nproviders = ["codex:work"]\n', encoding="utf-8")
        code, out, _ = self.agt("roles", "show")
        self.assertEqual(code, 0, out)
        self.assertIn(f"  ! sec: no agt-sec agent: defined only in {roles_cli.ui.tilde(str(self.team_file))}; "
                      "add --user to define it for your agent", out)
        self.assertNotIn("no agt-builder agent", out)


class PartsShowTests(RolesBase):
    """`agt roles show` says what your agt-<role> files add: the sentence, the Role rules, every file's state (S14)."""

    def test_show_reports_your_sentence_rules_and_files(self):
        from agents_router.config import load_config

        plugins = self.primary / "plugins"
        plugins.mkdir(parents=True)
        (plugins / "installed_plugins.json").write_text(json.dumps(
            {"version": 2, "plugins": {"agents-router@agents-router": [{"scope": "user", "version": "1.0.0"}]}}),
            encoding="utf-8")
        path = self.primary / "agents" / "agt-reviewer.md"
        path.parent.mkdir()
        path.write_bytes(role_agents.render(load_config().roles["reviewer"], "claude", "Use before every commit.",
                                            "Check the tests.\n"))
        code, report = self.json("roles", "show", env={"CLAUDECODE": "1"})
        self.assertEqual(code, 0, report)
        row = next(item for item in report["roles"] if item["name"] == "reviewer")
        self.assertEqual((row["when_to_call"], row["rules_chars"], row["rules_state"]),
                         ("Use before every commit.", 17, "valid"))
        [found] = [item for item in row["agent_files"] if item["file"] == str(path)]
        self.assertEqual((found["cli"], found["state"], found["rules_chars"]), ("claude", "valid", 17))
        builder = next(item for item in report["roles"] if item["name"] == "builder")
        self.assertEqual((builder["rules_state"], builder["rules_chars"]), ("absent", 0))
        code, out, _ = self.agt("roles", "show", env={"CLAUDECODE": "1"})
        self.assertIn(f"  reviewer: {roles_cli.ui.tilde(str(path))} has Role rules (17 characters) and its own "
                      "\"when to call\" sentence", out)
        path.write_text(path.read_text(encoding="utf-8").replace(role_agents.RULES_END + "\n", ""), encoding="utf-8")
        code, out, _ = self.agt("roles", "show", env={"CLAUDECODE": "1"})
        self.assertIn("  ! reviewer: ", out)
        self.assertIn("Role rules end line", out)
        code, report = self.json("roles", "show", env={"CLAUDECODE": "1"})
        row = next(item for item in report["roles"] if item["name"] == "reviewer")
        self.assertEqual(row["rules_state"], "broken")

    def test_show_names_an_archive_while_the_role_is_off(self):
        archive = self.home / "role-agents" / "archive" / "claude" / "claude-1234abcd" / "agt-builder.toml"
        archive.parent.mkdir(parents=True)
        archive.write_text('sentence = "Mine."\nrules = ""\n', encoding="utf-8")
        self.user_file.write_text(self.user_file.read_text(encoding="utf-8").replace(
            "[roles.builder]\n", "[roles.builder]\nenabled = false\n"), encoding="utf-8")
        code, out, _ = self.agt("roles", "show")
        self.assertIn(f"  builder: your sentence and Role rules wait in {roles_cli.ui.tilde(str(archive))}", out)
        code, report = self.json("roles", "show")
        row = next(item for item in report["roles"] if item["name"] == "builder")
        self.assertEqual(row["archived"], [str(archive)])


class LevelTests(RolesBase):
    def test_S7d_workspace_override_reported(self):
        self.local_file.write_text('[roles.reviewer]\nproviders = ["codex:work/gpt-6.1-sol:high"]\n', encoding="utf-8")
        code, out, err = self.agt("roles", "config", "reviewer", "--add", "claude:claude-work/opus", "--user", env=AGENT)
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertEqual(lines[0], "reviewer  + claude:claude-work/opus")
        self.assertIn(f"Overridden here by local ({roles_cli.ui.tilde(str(self.local_file))}); in effect: "
                      "codex:work/gpt-6.1-sol:high", lines)
        self.assertIn("To change it here: agt roles config reviewer --add claude:claude-work/opus --local", lines)
        code, report = self.json("roles", "config", "reviewer", "--remove", "antigravity:primary", "--user", env=AGENT)
        self.assertEqual(report["overridden_by"][0]["level"], "local")

    def test_default_level_is_where_the_role_is_set_never_the_committed_file(self):
        self.team_file.write_text('# the team\'s\n[roles.reviewer]\nproviders = ["codex:work"]\n', encoding="utf-8")
        team = self.team_file.read_bytes()
        code, out, err = self.agt("roles", "config", "reviewer", "--add", "kiro:primary", terminal=True)
        self.assertEqual(code, 0, err)
        self.same_bytes(self.team_file, team)
        self.assertIn('[roles.reviewer]\nproviders = ["codex:work", "kiro:primary"]', self.text(self.local_file))
        # set in the local file now: the next change goes there too
        code, out, err = self.agt("roles", "config", "reviewer", "--remove", "kiro:primary", terminal=True)
        self.assertIn("(local)", out)
        # a role set nowhere but the user file goes to the user file
        code, out, err = self.agt("roles", "config", "verifier", "--remove", "kiro:primary", terminal=True)
        self.assertIn("(user)", out)
        # named: the committed file
        code, out, err = self.agt("roles", "config", "builder", "--remove", "codex:work", "--workspace", terminal=True)
        self.assertEqual(code, 0, err)
        self.assertIn("[roles.builder]", self.text(self.team_file))
        self.assertTrue(self.text(self.team_file).startswith("# the team's\n"))

    def test_from_file_follows_the_level_rule(self):
        self.local_file.write_text('[roles.reviewer]\nproviders = ["codex:work"]\n', encoding="utf-8")
        proposal = self.root / "p.json"
        proposal.write_text(json.dumps({"roles": {"reviewer": {"providers": ["kiro:primary"]}}}), encoding="utf-8")
        code, out, err = self.agt("roles", "config", "--from", str(proposal), terminal=True)
        self.assertEqual(code, 0, err)
        self.assertIn("(local)", out)
        self.assertIn('providers = ["kiro:primary"]', self.text(self.local_file))
        both = self.root / "both.json"
        both.write_text(json.dumps({"roles": {"reviewer": {"providers": ["codex:work"]},
                                              "verifier": {"providers": ["codex:work"]}}}), encoding="utf-8")
        before = (self.user_file.read_bytes(), self.local_file.read_bytes())
        code, _, err = self.agt("roles", "config", "--from", str(both), terminal=True)
        self.assertEqual(code, 2)
        self.assertIn("set in different files", err)
        self.assertEqual((self.user_file.read_bytes(), self.local_file.read_bytes()), before)

    def test_roles_in_different_files_need_a_level(self):
        self.local_file.write_text('[roles.reviewer]\nproviders = ["codex:work"]\n', encoding="utf-8")
        before = self.user_file.read_bytes()
        code, _, err = self.agt("roles", "config", "--set-effort", "codex:work=low", terminal=True)
        self.assertEqual(code, 2)
        self.assertIn("set in different files", err)
        self.same_bytes(self.user_file, before)
        code, _, err = self.agt("roles", "config", "--set-effort", "codex:work=low", "--user", terminal=True)
        self.assertEqual(code, 0, err)


class GridTests(RolesBase):
    def run_grid(self, keys, *argv):
        seen = {}

        def choose(grid, **_):
            for key in keys:
                outcome = grid.press(key)
                if outcome:
                    seen["changes"] = grid.changes()
                    seen["lines"] = grid.lines(style="plain")
                    return outcome
            return roles_grid.QUIT

        with patch.object(roles_cli, "_interactive", return_value=True), \
                patch("agents_router.share.picker.choose", side_effect=choose):
            code, out, err = self.agt("roles", "config", *argv, terminal=True)
        return code, out, err, seen

    def test_S6_grid_three_changes_named_before_save(self):
        # rows: codex:work/gpt-6.1-sol:high, antigravity:primary, claude:claude-work/opus, codex:work/gpt-6.1-sol,
        # claude:personal/sonnet, kiro:primary; columns: reviewer, builder, verifier
        keys = ["down", "down", "right", "-",  # claude:claude-work moves to 2nd in builder
                "down", "left", "space",  # codex:work/gpt-6.1-sol joins reviewer
                "right", "right", "o",  # verifier off
                "enter"]
        code, out, err, seen = self.run_grid(keys)
        self.assertEqual(code, 0, err)
        self.assertEqual(seen["changes"], ["reviewer: codex:work joins",
                                           "builder: codex:work moves to 1st, claude:claude-work to 2nd",
                                           "verifier: off"])
        self.assertTrue(any("3 changes" in line for line in seen["lines"]))
        text = self.text(self.user_file)
        self.assertIn('[roles.builder]\nproviders = ["codex:work/gpt-6.1-sol", "claude:claude-work/opus"]', text)
        self.assertIn('[roles.reviewer]\nproviders = ["codex:work/gpt-6.1-sol:high", "antigravity:primary", '
                      '"codex:work/gpt-6.1-sol"]', text)
        self.assertIn('[roles.verifier]\nproviders = ["claude:personal/sonnet", "kiro:primary"]\nenabled = false', text)
        self.assertTrue(out.splitlines()[-1].startswith("Undo: "))

    def test_S6_grid_o_toggles_enabled(self):
        code, out, err, _ = self.run_grid(["right", "o", "enter"], "--local")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.text(self.local_file).split("\n\n", 1)[1], "[roles.builder]\nenabled = false\n")
        self.assertNotIn("builder", self.synced[-1])
        # off in the user file, turned on at the grid's level: enabled = true there
        self.local_file.unlink()
        self.user_file.write_text(self.text(self.user_file).replace(
            '[roles.builder]\n', '[roles.builder]\nenabled = false\n'), encoding="utf-8")
        code, out, err, _ = self.run_grid(["right", "o", "enter"], "--local")
        self.assertEqual(code, 0, err)
        self.assertIn("[roles.builder]\nenabled = true\n", self.text(self.local_file))
        self.assertIn("builder", self.synced[-1])
        # at the user level, turning it on again removes the switch
        code, out, err, _ = self.run_grid(["right", "o", "o", "enter"], "--user")
        self.assertIn("Nothing was changed.", out)

    def test_grid_refuses_to_save_an_enabled_role_with_no_providers(self):
        before = self.user_file.read_bytes()
        code, out, _, seen = self.run_grid(["n", "enter", "esc"])
        self.assertEqual(code, 0)
        self.assertIn("Nothing was changed.", out)
        self.same_bytes(self.user_file, before)

    def test_grid_esc_changes_nothing(self):
        before = self.user_file.read_bytes()
        code, out, _, _ = self.run_grid(["space", "o", "esc"])
        self.assertEqual((code, out), (0, "Nothing was changed.\n"))
        self.same_bytes(self.user_file, before)

    def test_S6a_no_terminal_read_only(self):
        before = self.user_file.read_bytes()
        with patch("agents_router.share.picker.choose", side_effect=AssertionError("no prompt for an agent")):
            code, out, _ = self.agt("roles", "config", env=AGENT, terminal=True)
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertRegex(lines[1], r"role on/off\s+on\s+on\s+on")
        self.assertEqual(lines[-1], roles_grid.HINT)
        self.same_bytes(self.user_file, before)
        code, shown = self.json("roles", "config", env=AGENT)
        self.assertEqual([row["name"] for row in shown["roles"]], ["reviewer", "builder", "verifier"])


if __name__ == "__main__":
    unittest.main()
