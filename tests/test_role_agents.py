"""agt-<role> agent files: one per enabled role, only for the orchestrator agt runs in, only agt's own."""

import json
import os
from pathlib import Path
import tomllib
from unittest.mock import patch

import helpers  # noqa: F401  (isolates the test process from your real folders)
from test_setup import SetupBase

from agents_router import role_agents
from agents_router.roles import BASE_DESCRIPTIONS, BASE_ROLES, EARLIER_DESCRIPTIONS, Role
from agents_router.config import load_config
from agents_router.share import exclude

REPO = Path(__file__).resolve().parents[1]
ROLES = """
[roles.reviewer]
providers = ["codex", "antigravity"]

[roles.builder]
providers = ["claude", "codex"]
"""


class RoleAgentBase(SetupBase):
    def setUp(self):
        super().setUp()
        (self.root / "agt").mkdir(exist_ok=True)
        self.settings(ROLES)
        plugins = self.claude_dir / "plugins"
        plugins.mkdir()
        (plugins / "installed_plugins.json").write_text(json.dumps(
            {"version": 2, "plugins": {"agents-router@agents-router": [{"scope": "user", "version": "1.0.0"}]}}),
            encoding="utf-8")
        self.agents = self.claude_dir / "agents"

    def settings(self, text):
        (self.root / "agt" / "settings.toml").write_text(text, encoding="utf-8")

    def sync(self, host="claude", **kwargs):
        return role_agents.sync(load_config(), host, **kwargs)


class RoleAgentTests(RoleAgentBase):
    def test_create_writes_one_agent_per_enabled_role(self):
        lines = self.sync()
        self.assertEqual(sorted(path.name for path in self.agents.iterdir()), ["agt-builder.md", "agt-reviewer.md"])
        self.assertEqual(len(lines), 2)
        self.assertIn("installed role agent agt-reviewer at", lines[0])
        text = (self.agents / "agt-reviewer.md").read_text(encoding="utf-8")
        self.assertIn("Managed by agt roles", text.splitlines()[1])
        self.assertIn("name: agt-reviewer\n", text)
        self.assertIn("agt run --follow --window 60 --caller claude --role reviewer -", text)
        self.assertNotIn("--runtime ", text)  # no provider list: rebalance never rewrites it
        self.assertNotIn("model=", text)  # agt run --role refuses --model and --account
        self.assertNotIn("account=", text)
        self.assertIn("`effort=...`", text)
        self.assertIn(json.dumps(BASE_DESCRIPTIONS["reviewer"])[:-1], text)  # the role's use-me-when line
        self.assertEqual(self.sync(), [], "a second sync changes nothing")

    def test_S8_disable_deletes_own_agent_file(self):
        self.sync()
        self.settings(ROLES + "enabled = false\n")  # the builder is off
        lines = self.sync()
        self.assertFalse((self.agents / "agt-builder.md").exists())
        self.assertTrue((self.agents / "agt-reviewer.md").exists())
        self.assertEqual(len(lines), 1)
        self.assertIn("removed", lines[0])
        self.assertIn("agt's own file", lines[0])

    def test_S8b_hand_made_agent_kept(self):
        self.agents.mkdir()
        mine = self.agents / "agt-builder.md"
        mine.write_text("---\nname: agt-builder\ndescription: my own builder\n---\nBuild it my way.\n", encoding="utf-8")
        lines = self.sync()
        self.assertEqual(mine.read_text(encoding="utf-8").splitlines()[-1], "Build it my way.")
        self.assertTrue(any("kept" in line and "isn't agt's" in line and str(mine) in line for line in lines), lines)
        self.settings(ROLES + "enabled = false\n")
        self.sync()
        self.assertTrue(mine.exists(), "a file without the marker is never deleted")

    def test_the_built_in_relays_and_other_agents_are_left_alone(self):
        self.agents.mkdir()
        relay = self.agents / "agt-codex.md"
        relay.write_text("# Managed by agt roles\n", encoding="utf-8")  # even with the marker: a relay's name
        other = self.agents / "helper.md"
        other.write_text("x", encoding="utf-8")
        self.settings("")
        self.sync()
        self.assertTrue(relay.exists() and other.exists())

    def test_kiro_gets_no_role_agents(self):
        self.assertEqual(self.sync("kiro"), [])
        self.assertFalse(self.agents.exists())

    def test_codex_gets_toml_agents_with_its_relay_settings(self):
        lines = self.sync("codex")
        path = self.codex_dir / "agents" / "agt-builder.toml"
        self.assertTrue(path.exists(), lines)
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        relay = tomllib.loads((REPO / "src/agents_router/templates/codex-agents/agt-codex.toml").read_text(encoding="utf-8"))
        self.assertEqual(data["name"], "agt-builder")
        self.assertEqual((data["model"], data["features"]), (relay["model"], relay["features"]))
        self.assertIn("agt run --follow --window 60 --caller codex --role builder -", data["developer_instructions"])
        self.assertFalse(self.agents.exists(), "only the orchestrator named gets files")

    def test_antigravity_writes_only_into_agts_relay_plugin(self):
        plugin = self.agy_dir / "config" / "plugins" / "agents-router-relays"
        lines = self.sync("antigravity")
        self.assertIn("relay plugin isn't in", lines[0])
        plugin.mkdir(parents=True)
        (plugin / "plugin.json").write_text("{}", encoding="utf-8")
        self.sync("antigravity")
        text = (plugin / "agents" / "agt-reviewer.md").read_text(encoding="utf-8")
        self.assertIn("Managed by agt roles", text.splitlines()[1])
        self.assertIn("subagent: true", text)

    def test_dry_run_writes_nothing(self):
        lines = self.sync(dry_run=True)
        self.assertTrue(all("would install" in line for line in lines) and len(lines) == 2, lines)
        self.assertFalse(self.agents.exists())

    def test_a_custom_description_is_quoted(self):
        self.settings(ROLES + '\n[roles.sec]\naccess = "read"\ndescription = "Checks: \\"security\\" # things"\n'
                      'providers = ["codex"]\n')
        self.sync("codex")
        data = tomllib.loads((self.codex_dir / "agents" / "agt-sec.toml").read_text(encoding="utf-8"))
        self.assertTrue(data["description"].startswith('Checks: "security" # things Runs the sec role'))

    def test_a_terminal_keeps_only_clis_that_already_have_role_agents(self):
        os.environ.pop("CLAUDECODE", None)
        self.assertEqual(role_agents.sync(load_config()), [])  # no CLI had them: none appear
        self.sync()  # written from inside Claude Code earlier
        self.settings(ROLES + "enabled = false\n")
        lines = role_agents.sync(load_config())
        self.assertFalse((self.agents / "agt-builder.md").exists(), lines)
        self.assertFalse((self.codex_dir / "agents").exists())

    def test_inside_a_cli_the_host_is_that_cli(self):
        with patch.dict(os.environ, {"CLAUDECODE": "1"}):
            role_agents.sync(load_config())
        self.assertTrue((self.agents / "agt-reviewer.md").exists())


class ReaderTests(RoleAgentBase):
    """read_parts: your sentence and Role rules from each CLI's file, and the four states, never a guess (S8)."""

    def written(self, host="claude", name="builder"):
        self.sync(host)
        folder = {"claude": self.agents, "codex": self.codex_dir / "agents",
                  "antigravity": self.agy_dir / "config" / "plugins" / "agents-router-relays" / "agents"}[host]
        return folder / f"agt-{name}{role_agents.SUFFIX[host]}"

    def edit(self, path, old, new):
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text)
        path.write_text(text.replace(old, new, 1), encoding="utf-8")

    def description_line(self, path):
        return next(line for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("description"))

    def tail(self, line):
        about = json.loads(line[len("description: "):])
        return about[about.index(role_agents.anchor("builder")):]

    def set_rules(self, path, rules):
        self.edit(path, role_agents.RULES_START + "\n", role_agents.RULES_START + "\n" + rules)

    def test_a_fresh_file_reads_valid_and_empty_in_every_cli(self):
        (self.agy_dir / "config" / "plugins" / "agents-router-relays").mkdir(parents=True)
        (self.agy_dir / "config" / "plugins" / "agents-router-relays" / "plugin.json").write_text("{}", encoding="utf-8")
        for host in ("claude", "codex", "antigravity"):
            parts = role_agents.read_parts(self.written(host), "builder")
            self.assertEqual((parts.state, parts.sentence, parts.rules, parts.version),
                             ("valid", BASE_DESCRIPTIONS["builder"], "", 2), host)

    def test_descriptions_in_every_yaml_form(self):
        path = self.written()
        tail = 'Runs the builder role through Agents Router (`agt run`): then the complete, self-contained task.'
        forms = {
            "json": f"description: {json.dumps('Use for edits. ' + tail)}",
            "single": "description: 'Use for edits, it''s mine. " + tail + "'",
            "plain": "description: Use for edits. " + tail.replace(": ", " - "),
            "wrapped": "description: Use for edits\n  and migrations. " + tail.replace(": ", " - "),
            "literal": "description: |\n  Use for edits.\n  " + tail,
            "folded": "description: >\n  Use for edits.\n  " + tail,
        }
        wanted = {"json": "Use for edits.", "single": "Use for edits, it's mine.", "plain": "Use for edits.",
                  "wrapped": "Use for edits and migrations.", "literal": "Use for edits.", "folded": "Use for edits."}
        original, first = path.read_text(encoding="utf-8"), self.description_line(path)
        for form, line in forms.items():
            path.write_text(original.replace(first, line), encoding="utf-8")
            parts = role_agents.read_parts(path, "builder")
            self.assertEqual((parts.state, parts.sentence), ("valid", wanted[form]), form)

    def test_codex_description_with_quotes_and_hash(self):
        path = self.written("codex")
        self.edit(path, 'description = "' + BASE_DESCRIPTIONS["builder"], 'description = "Use for \\"risky\\" edits # really.')
        parts = role_agents.read_parts(path, "builder")
        self.assertEqual((parts.state, parts.sentence), ("valid", 'Use for "risky" edits # really.'))

    def test_codex_rules_round_trip(self):
        path = self.written("codex")
        rules = 'Say "done" when done.\n# not a comment\nNever edit migrations.\n'
        self.set_rules(path, rules)
        self.assertEqual(role_agents.read_parts(path, "builder").rules, rules)
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        self.assertIn(role_agents.RULES_END, data["developer_instructions"])
        self.assertIn("[features]", path.read_text(encoding="utf-8").split("'''")[-1])

    def test_crlf_reads_the_same(self):
        path = self.written()
        self.set_rules(path, "Run the tests.\n")
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        parts = role_agents.read_parts(path, "builder")
        self.assertEqual((parts.state, parts.rules), ("valid", "Run the tests.\n"))

    def test_a_v1_file_reads_as_legacy(self):
        path = self.written()
        text = path.read_text(encoding="utf-8")
        text = text.replace(role_agents.MARKER_LINE, "Managed by agt roles: agt setup, agt update and agt roles config "
                            "rewrite or remove this file; delete this line to make it yours.")
        path.write_text(text[:text.index("\n" + role_agents.RULES_HEADING)] + "\n", encoding="utf-8")
        parts = role_agents.read_parts(path, "builder")
        self.assertEqual((parts.state, parts.version, parts.rules), ("legacy", 1, ""))

    def test_broken_markers_name_the_line(self):
        path = self.written()
        original = path.read_text(encoding="utf-8")
        start = original.splitlines().index(role_agents.RULES_START) + 1
        cases = {
            "missing end": original.replace(role_agents.RULES_END + "\n", ""),
            "missing start": original.replace(role_agents.RULES_START + "\n", ""),
            "duplicate": original.replace(role_agents.RULES_END, role_agents.RULES_START + "\n" + role_agents.RULES_END),
            "unordered": original.replace(role_agents.RULES_START, "@@").replace(role_agents.RULES_END, role_agents.RULES_START)
                                 .replace("@@", role_agents.RULES_END),
            "both gone": original.replace(role_agents.RULES_START + "\n", "").replace(role_agents.RULES_END + "\n", ""),
        }
        for case, text in cases.items():
            path.write_text(text, encoding="utf-8")
            parts = role_agents.read_parts(path, "builder")
            self.assertEqual(parts.state, "broken", case)
            self.assertIn(str(path), parts.problem, case)
        path.write_text(cases["missing end"], encoding="utf-8")
        self.assertIn(f"{path}:{start}:", role_agents.read_parts(path, "builder").problem)

    def test_the_phrase_in_a_description_doesnt_make_a_file_agts(self):
        self.agents.mkdir()
        mine = self.agents / "agt-builder.md"
        mine.write_text("---\nname: agt-builder\ndescription: Managed by agt roles v2: no, mine\n---\nMine.\n",
                        encoding="utf-8")
        self.assertFalse(role_agents.ours(mine))
        self.sync()
        self.assertEqual(mine.read_text(encoding="utf-8").splitlines()[-1], "Mine.")

    def test_an_indented_management_line_doesnt_make_a_file_agts(self):
        self.agents.mkdir()
        mine = self.agents / "agt-builder.md"
        mine.write_text("---\ndescription: >\n  # Managed by agt roles v2: not really\nname: agt-builder\n---\nMine.\n",
                        encoding="utf-8")
        self.assertFalse(role_agents.ours(mine))

    def test_a_pasted_second_anchor_keeps_the_whole_sentence(self):
        path = self.written()
        line = self.description_line(path)
        about = self.tail(line)
        pasted = "Use after edits; it Runs the builder role through Agents Router (twice)."
        self.edit(path, line, "description: " + json.dumps(pasted + " " + about))
        self.assertEqual(role_agents.read_parts(path, "builder").sentence, pasted)

    def test_text_after_agts_part_moves_into_the_sentence(self):
        path = self.written()
        line = self.description_line(path)
        about = self.tail(line)
        self.edit(path, line, "description: " + json.dumps("Use for edits. " + about + "  Prefer small diffs. "))
        parts = role_agents.read_parts(path, "builder")
        self.assertEqual((parts.sentence, parts.moved), ("Use for edits. Prefer small diffs.", "Prefer small diffs."))
        lines = self.sync()
        self.assertEqual(len(lines), 1)
        self.assertIn("moved 'Prefer small diffs.'", lines[0])
        self.assertEqual(role_agents.read_parts(path, "builder").sentence, "Use for edits. Prefer small diffs.")
        self.assertEqual(self.sync(), [], "a second sync changes nothing")

    def test_agts_part_without_its_end_is_broken_for_sync_only(self):
        path = self.written()
        self.set_rules(path, "Keep it.\n")
        self.edit(path, "then the complete, self-contained task.", "then the task.")
        parts = role_agents.read_parts(path, "builder")
        self.assertEqual(parts.state, "broken")
        self.assertIn(f"{path}:4:", parts.problem)
        self.assertEqual(role_agents.read_rules(path).rules, "Keep it.\n")  # a run still gets the rules

    def test_the_description_ends_with_the_newest_closing_phrase(self):
        from agents_router.roles import Role

        about = role_agents.description(Role("builder", "edit"))
        self.assertTrue(about.endswith(role_agents.CLOSINGS[-1]))
        tail = about[about.rindex(role_agents.anchor("builder")):]
        self.assertEqual(tail.count(role_agents.CLOSINGS[-1]), 1)


class RewriteTests(ReaderTests):
    """Your sentence and Role rules survive every rewrite, in every CLI (S1, S3, S4)."""

    def relay_plugin(self):
        plugin = self.agy_dir / "config" / "plugins" / "agents-router-relays"
        plugin.mkdir(parents=True, exist_ok=True)
        (plugin / "plugin.json").write_text("{}", encoding="utf-8")

    def mine(self, host):
        path = self.written(host)
        self.set_rules(path, "Run the tests before you finish.\nNever edit migrations.\n")
        if host == "codex":
            self.edit(path, 'description = "' + BASE_DESCRIPTIONS["builder"], 'description = "Use for every edit, even tiny.')
        else:
            self.edit(path, "description: " + json.dumps(BASE_DESCRIPTIONS["builder"])[:-1],
                      "description: " + json.dumps("Use for every edit, even tiny.")[:-1])
        return path

    def test_S1_S3_edits_survive_a_rewrite_and_a_changed_template(self):
        self.relay_plugin()
        real = role_agents._template
        for host in ("claude", "codex", "antigravity"):
            path = self.mine(host)
            with patch.object(role_agents, "_template", side_effect=lambda name: real(name).replace(
                    "## 1. Start the job", "## 1. Start the job (new wording)")):
                lines = self.sync(host)
                self.assertEqual(len(lines), 2, (host, lines))  # both roles' relay steps changed
                self.assertIn("(new wording)", path.read_text(encoding="utf-8"), host)
                self.assertEqual(self.sync(host), [], f"{host}: a second sync changes nothing")
            parts = role_agents.read_parts(path, "builder")
            self.assertEqual((parts.state, parts.sentence, parts.rules),
                             ("valid", "Use for every edit, even tiny.", "Run the tests before you finish.\nNever edit migrations.\n"), host)
            if host == "codex":
                tomllib.loads(path.read_text(encoding="utf-8"))  # still valid TOML

    HOSTS = ("claude", "codex", "antigravity")

    def without_section(self, text):
        start = text.index("\n" + role_agents.RULES_HEADING)
        end = text.index(role_agents.RULES_END + "\n") + len(role_agents.RULES_END) + 1
        return text[:start] + "\n" + text[end:]

    def test_S4_an_empty_sentence_gets_the_default(self):
        self.relay_plugin()
        for host in self.HOSTS:
            with self.subTest(host=host):
                path = self.mine(host)
                old = "Use for every edit, even tiny."
                self.edit(path, json.dumps(old)[1:-1] + " Runs the", "Runs the")
                self.sync(host)
                self.assertEqual(role_agents.read_parts(path, "builder").sentence, BASE_DESCRIPTIONS["builder"])
                self.assertEqual(self.sync(host), [])

    def test_a_legacy_file_gains_an_empty_section_and_keeps_its_sentence(self):
        self.relay_plugin()
        for host in self.HOSTS:
            with self.subTest(host=host):
                path = self.written(host)
                text = path.read_text(encoding="utf-8").replace(BASE_DESCRIPTIONS["builder"], "My old sentence.")
                text = text.replace(role_agents.MARKER_LINE, "Managed by agt roles: agt setup, agt update and agt roles "
                                    "config rewrite or remove this file; delete this line to make it yours.")
                path.write_text(self.without_section(text), encoding="utf-8")
                self.assertEqual(role_agents.read_parts(path, "builder").state, "legacy")
                self.assertTrue(any("agt-builder" in line for line in self.sync(host)))
                parts = role_agents.read_parts(path, "builder")
                self.assertEqual((parts.state, parts.version, parts.sentence, parts.rules), ("valid", 2, "My old sentence.", ""))
                self.assertEqual(self.sync(host), [])

    def test_S8_a_broken_file_is_left_byte_identical_and_reported(self):
        self.relay_plugin()
        for host in self.HOSTS:
            with self.subTest(host=host):
                path = self.written(host)
                self.edit(path, role_agents.RULES_END + "\n", "")
                path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
                before = path.read_bytes()
                lines = self.sync(host)
                self.assertEqual(path.read_bytes(), before)
                self.assertTrue(any(f"left {path} as it is" in line and "Role rules end line" in line for line in lines), lines)

    def test_the_relay_is_told_the_rules_are_for_the_worker(self):
        self.relay_plugin()
        for host in self.HOSTS:
            with self.subTest(host=host):
                text = self.written(host).read_text(encoding="utf-8")
                self.assertIn(role_agents.RULES_HEADING + "\n\n" + role_agents.RULES_INTRO, text)

    def test_a_valid_crlf_file_is_rewritten_once(self):
        path = self.mine("claude")
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        self.assertEqual(len(self.sync()), 1)
        self.assertNotIn(b"\r\n", path.read_bytes())
        self.assertEqual(self.sync(), [])
        self.assertEqual(role_agents.read_parts(path, "builder").sentence, "Use for every edit, even tiny.")

    def test_codex_rules_with_triple_quotes_are_refused(self):
        path = self.written("codex")
        text = path.read_text(encoding="utf-8")
        start = text.index("developer_instructions = \'\'\'")
        end = text.index("\n\'\'\'\n", start)
        inner = text[start + len("developer_instructions = \'\'\'"):end].replace(
            role_agents.RULES_START + "\n", role_agents.RULES_START + "\nSay \'\'\' never.\n")
        path.write_text(text[:start] + "developer_instructions = " + json.dumps(inner + "\n") + text[end + 5:],
                        encoding="utf-8")
        self.assertIn("\'\'\'", role_agents.read_parts(path, "builder").rules)
        before = path.read_bytes()
        lines = self.sync("codex")
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(any("can't hold" in line for line in lines), lines)

    def reviewer_with(self, host, sentence, legacy=False):
        """agt's reviewer file in one CLI, with `sentence` in place of today's default (a 0.11 file when legacy)."""

        self.written(host, "reviewer").unlink()  # start from a fresh file, not the last subtest's
        path = self.written(host, "reviewer")
        text = path.read_text(encoding="utf-8")
        self.assertIn(BASE_DESCRIPTIONS["reviewer"], text)
        text = text.replace(BASE_DESCRIPTIONS["reviewer"], sentence, 1)
        if legacy:
            text = self.without_section(text.replace(role_agents.MARKER_LINE, "Managed by agt roles: agt setup, agt "
                                                     "update and agt roles config rewrite or remove this file; delete "
                                                     "this line to make it yours."))
        path.write_text(text, encoding="utf-8")
        return path

    def test_an_earlier_default_sentence_moves_to_todays_default(self):
        self.relay_plugin()
        [earlier] = EARLIER_DESCRIPTIONS["reviewer"]  # agt 0.11's reviewer sentence
        self.assertNotEqual(earlier, BASE_DESCRIPTIONS["reviewer"])
        for host in self.HOSTS:
            for legacy in (False, True):
                with self.subTest(host=host, legacy=legacy):
                    path = self.reviewer_with(host, earlier, legacy)
                    if not legacy:
                        self.set_rules(path, "Check the tests.\n")
                    self.assertEqual(role_agents.read_parts(path, "reviewer").sentence, earlier)
                    self.assertEqual(role_agents.file_report(path, "reviewer")["when_to_call"], None,
                                     "an earlier default isn't shown as your sentence")
                    lines = self.sync(host)
                    self.assertTrue(any("agt-reviewer" in line for line in lines), lines)
                    parts = role_agents.read_parts(path, "reviewer")
                    self.assertEqual((parts.state, parts.version, parts.sentence, parts.rules),
                                     ("valid", 2, BASE_DESCRIPTIONS["reviewer"], "" if legacy else "Check the tests.\n"))
                    self.assertEqual(path.read_bytes(), role_agents.render(
                        load_config().roles["reviewer"], host, "", parts.rules))
                    self.assertEqual(self.sync(host), [], f"{host}: a second sync changes nothing")

    def test_an_edited_reviewer_sentence_is_kept(self):
        self.relay_plugin()
        for host in self.HOSTS:
            for legacy in (False, True):
                with self.subTest(host=host, legacy=legacy):
                    path = self.reviewer_with(host, "Use before every commit.", legacy)
                    self.assertEqual(role_agents.file_report(path, "reviewer")["when_to_call"], "Use before every commit.")
                    self.sync(host)
                    self.assertEqual(role_agents.read_parts(path, "reviewer").sentence, "Use before every commit.")
                    self.assertEqual(self.sync(host), [])

    def test_an_explicit_description_replaces_the_sentence_everywhere(self):
        self.relay_plugin()
        self.settings(ROLES + '\n[roles.sec]\naccess = "read"\nproviders = ["codex"]\ndescription = "Old."\n')
        paths = {host: self.written(host, "sec") for host in self.HOSTS}
        self.edit(paths["claude"], json.dumps("Old.")[:-1], json.dumps("Hand-edited.")[:-1])
        self.settings(ROLES + '\n[roles.sec]\naccess = "read"\nproviders = ["codex"]\ndescription = "New."\n')
        self.assertEqual(self.sync(), [], "a background sync keeps your sentence")
        self.assertEqual(role_agents.read_parts(paths["claude"], "sec").sentence, "Hand-edited.")
        with patch.dict(os.environ, {"CLAUDECODE": "1"}):  # run inside Claude Code: the other CLIs' copies change too
            lines = role_agents.sync(load_config(), fresh=["sec"])
        self.assertEqual(len([line for line in lines if "agt-sec" in line]), 3, lines)
        for host, path in paths.items():
            self.assertEqual(role_agents.read_parts(path, "sec").sentence, "New.", host)


class ArchiveTests(RewriteTests):
    """Turning a role off keeps your parts; turning it on puts them back (S10)."""

    def archives(self):
        folder = self.root / "agt" / "role-agents" / "archive"
        return sorted(folder.rglob("*.toml")) if folder.is_dir() else []

    def test_S10_off_archives_your_parts_and_on_restores_them(self):
        path = self.mine("claude")
        self.settings(ROLES + "enabled = false\n")  # the builder is off
        lines = self.sync()
        self.assertFalse(path.exists())
        [archive] = self.archives()
        self.assertIn(f"kept in {archive}", lines[0])
        self.assertIn("claude", archive.parts)
        data = tomllib.loads(archive.read_text(encoding="utf-8"))
        self.assertEqual(data["sentence"], "Use for every edit, even tiny.")
        self.settings(ROLES)  # on again
        lines = self.sync()
        self.assertIn(f"back from {archive}", lines[0])
        parts = role_agents.read_parts(path, "builder")
        self.assertEqual((parts.sentence, parts.rules),
                         ("Use for every edit, even tiny.", "Run the tests before you finish.\nNever edit migrations.\n"))
        self.assertEqual(self.archives(), [])
        self.assertEqual(self.sync(), [])

    def test_an_unedited_file_is_removed_with_no_archive(self):
        path = self.written()
        self.settings(ROLES + "enabled = false\n")
        lines = self.sync()
        self.assertFalse(path.exists())
        self.assertEqual(self.archives(), [])
        self.assertNotIn("kept in", lines[0])

    def test_a_file_with_an_earlier_default_is_removed_with_no_archive(self):
        self.relay_plugin()
        for host in self.HOSTS:
            with self.subTest(host=host):
                self.settings(ROLES)
                path = self.reviewer_with(host, EARLIER_DESCRIPTIONS["reviewer"][0])
                self.settings(ROLES.replace("[roles.reviewer]\n", "[roles.reviewer]\nenabled = false\n"))
                lines = self.sync(host)
                self.assertFalse(path.exists())
                self.assertEqual(self.archives(), [])
                self.assertFalse(any("kept in" in line for line in lines), lines)

    def test_a_broken_file_of_a_role_turned_off_is_left_and_reported(self):
        path = self.mine("claude")
        self.edit(path, role_agents.RULES_END + "\n", "")
        before = path.read_bytes()
        self.settings(ROLES + "enabled = false\n")
        lines = self.sync()
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(any("left" in line and "is off" in line for line in lines), lines)

    def test_a_deleted_custom_role_keeps_its_archive(self):
        self.settings(ROLES + '\n[roles.sec]\naccess = "read"\nproviders = ["codex"]\n')
        path = self.written("claude", "sec")
        self.set_rules(path, "Look for injection.\n")
        self.settings(ROLES)  # sec is gone
        lines = self.sync()
        [archive] = self.archives()
        self.assertTrue(any(f"kept in {archive}" in line and "agt-sec" in line for line in lines), lines)
        self.sync()
        self.assertEqual(self.archives(), [archive])


class UserSettingsOnlyTests(RoleAgentBase):
    """Agent files are user-wide: a project's settings never add, change or remove one (S7)."""

    def workspace(self, text):
        project = self.root / "project"
        (project / ".agt").mkdir(parents=True)
        (project / ".agt" / "settings.toml").write_text(text, encoding="utf-8")
        cwd = os.getcwd()
        os.chdir(project)
        self.addCleanup(os.chdir, cwd)

    def files(self):
        return {path.name: path.read_bytes() for path in self.agents.iterdir()}

    def test_S7_a_workspace_turning_a_role_off_leaves_the_files(self):
        self.sync()
        before = self.files()
        self.workspace("[roles.builder]\nenabled = false\n")
        self.assertFalse(load_config().roles["builder"].enabled)  # off in this project
        self.assertEqual(self.sync(), [])
        self.assertEqual(self.files(), before)

    def test_S7_a_workspace_only_role_gets_no_file(self):
        self.workspace('[roles.sec]\naccess = "read"\nproviders = ["codex"]\n')
        self.assertIn("sec", load_config().roles)
        self.sync()
        self.assertEqual(sorted(self.files()), ["agt-builder.md", "agt-reviewer.md"])

    def test_S7_a_role_incomplete_in_the_user_file_gets_a_note_not_an_error(self):
        self.settings(ROLES + '\n[roles.sec]\ndescription = "Security checks."\n')
        self.workspace('[roles.sec]\naccess = "read"\nproviders = ["codex"]\n')
        lines = self.sync()
        self.assertEqual(sorted(self.files()), ["agt-builder.md", "agt-reviewer.md"])
        note = next(line for line in lines if line.startswith("no agt-sec agent:"))
        self.assertIn("access", note)

    def test_unreadable_user_settings_remove_nothing(self):
        self.sync()
        loaded = load_config()
        before = self.files()
        self.settings("[roles\n")
        lines = role_agents.sync(loaded, "claude")
        self.assertEqual(self.files(), before)
        self.assertEqual(len(lines), 1)
        self.assertIn("can't be read", lines[0])


class ShareTests(RoleAgentBase):
    def test_share_skips_role_agents(self):
        self.sync()
        path = self.agents / "agt-reviewer.md"
        self.assertEqual(exclude.agent("claude", path.stem, path), "agt's own relay agent")


ROLE_RESULT = ("then the text between each pair of the exact, unprefixed `===== RESULT =====` and "
               "`===== END RESULT =====` delimiters, in order: a read-only role returns one result per provider, and you "
               "keep every one with the line that names its provider.")


class EvalStandInTests(RoleAgentBase):
    """The role agents the graded skill cases install (tests/skill-evals/**/profile-agents/) say what agt writes."""

    EDITED = {"edited-sentence-calls-reviewer"}  # its "when to call" sentence is edited on purpose

    def test_every_stand_in_has_the_description_agt_writes(self):
        from agents_router.share import frontmatter

        found = sorted((REPO / "tests" / "skill-evals").glob("**/profile-agents/agt-*.md"))
        self.assertTrue(found, "no stand-ins found")
        for path in found:
            name = path.stem[len("agt-"):]
            case = path.parent.parent.name
            if name not in BASE_ROLES:
                continue
            with self.subTest(case=case, role=name):
                about = frontmatter.parse(path.read_text(encoding="utf-8")).fields["description"]
                expected = role_agents.description(Role(name, BASE_ROLES[name]))
                if case in self.EDITED:
                    agts = expected[expected.index(role_agents.anchor(name)):]
                    self.assertTrue(about.endswith(agts), "agt's part of the description is unchanged")
                    self.assertNotEqual(about, expected, "the sentence this case edits is still edited")
                else:
                    self.assertEqual(about, expected)


class TemplateTests(RoleAgentBase):
    """The role templates keep the relays' framing rules word for word, apart from the role's own lines."""

    def test_every_run_command_passes_its_caller(self):
        for host in ("claude", "codex", "antigravity"):
            template = (REPO / f"src/agents_router/templates/role-agents/{host}.md").read_text(encoding="utf-8")
            runs = [line for line in template.splitlines() if "agt run " in line and "--role" in line]
            self.assertTrue(runs, host)
            for line in runs:
                self.assertIn(f"--caller {host} --role", line, (host, line))

    def test_status_rules_match_the_relays(self):
        relays = {"claude": REPO / "agents/agt-codex.md",
                  "antigravity": REPO / "src/agents_router/templates/antigravity-plugin/agents/agt-codex.md",
                  "codex": REPO / "src/agents_router/templates/codex-agents/agt-codex.toml"}
        result = ("then the text between the exact, unprefixed `===== RESULT =====` and `===== END RESULT =====` "
                  "delimiters.")
        for host, path in relays.items():
            relay = path.read_text(encoding="utf-8")
            relay = relay[relay.index("## 2. Read the authenticated status"):]
            if host == "codex":
                relay = relay[:relay.index("\n'''\n")] + "\n"
            template = (REPO / f"src/agents_router/templates/role-agents/{host}.md").read_text(encoding="utf-8")
            template = template[template.index("## 2. Read the authenticated status"):]
            self.assertIn(result, relay, host)
            self.assertEqual(template.replace(ROLE_RESULT, result), relay, host)


if __name__ == "__main__":
    import unittest

    unittest.main()
