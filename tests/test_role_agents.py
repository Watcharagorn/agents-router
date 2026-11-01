"""agt-<role> agent files: one per enabled role, only for the orchestrator agt runs in, only agt's own."""

import json
import os
from pathlib import Path
import tomllib
from unittest.mock import patch

import helpers  # noqa: F401  (isolates the test process from your real folders)
from test_setup import SetupBase

from agents_router import role_agents
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
        self.assertIn("agt run --follow --window 60 --role reviewer -", text)
        self.assertNotIn("--runtime ", text)  # no provider list: rebalance never rewrites it
        self.assertNotIn("model=", text)  # agt run --role refuses --model and --account
        self.assertNotIn("account=", text)
        self.assertIn("`effort=...`", text)
        self.assertIn("Use before calling non-trivial work done", text)  # the role's use-me-when line
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
        self.assertIn("agt run --follow --window 60 --role builder -", data["developer_instructions"])
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


class ShareTests(RoleAgentBase):
    def test_share_skips_role_agents(self):
        self.sync()
        path = self.agents / "agt-reviewer.md"
        self.assertEqual(exclude.agent("claude", path.stem, path), "agt's own relay agent")


ROLE_RESULT = ("then the text between each pair of the exact, unprefixed `===== RESULT =====` and "
               "`===== END RESULT =====` delimiters, in order: a read-only role returns one result per provider, and you "
               "keep every one with the line that names its provider.")


class TemplateTests(RoleAgentBase):
    """The role templates keep the relays' framing rules word for word, apart from the role's own lines."""

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
