"""Plugins between Claude Code and Codex: what each CLI can load, the sync that installs them, and `plugins apply`."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_share_items import ItemBase

from agents_router.share import plugins, settings, watch


def plugin_files(root: Path, *, skills=(), commands=(), agents=(), hooks=(), servers=None, manifest=None,
                 codex_manifest=None, apps=False) -> Path:
    """A plugin's installed files, as Claude Code or Codex keeps them."""

    for name in skills:
        (root / "skills" / name).mkdir(parents=True, exist_ok=True)
        (root / "skills" / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {name}.\n---\nDo it.\n")
    for folder, names in (("commands", commands), ("agents", agents)):
        for name in names:
            (root / folder).mkdir(parents=True, exist_ok=True)
            (root / folder / f"{name}.md").write_text(f"---\ndescription: {name}\n---\nDo it.\n")
    if hooks:
        (root / "hooks").mkdir(parents=True, exist_ok=True)
        (root / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {event: [] for event in hooks}}))
    if servers is not None:
        root.mkdir(parents=True, exist_ok=True)
        (root / ".mcp.json").write_text(json.dumps({"mcpServers": servers}))
    for folder, data in ((".claude-plugin", manifest), (".codex-plugin", codex_manifest)):
        if data is not None:
            (root / folder).mkdir(parents=True, exist_ok=True)
            (root / folder / "plugin.json").write_text(json.dumps(data))
    if apps:
        (root / ".app.json").write_text("{}")
    root.mkdir(parents=True, exist_ok=True)
    return root


class CompatibilityTests(unittest.TestCase):
    """What each CLI loads from the other's plugins, as the sandbox showed it."""

    def test_codex_misses_claude_only_parts_and_plugin_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = plugin_files(Path(temp), skills=["fmt"], commands=["fmt"], agents=["reviewer"], hooks=["SessionStart"],
                                servers={"probe": {"command": "node", "args": ["${CLAUDE_PLUGIN_ROOT}/server.js"]}},
                                manifest={"name": "deck", "userConfig": {"token": {"type": "string"}}})
            asks, notes = plugins.check(plugins.parts(root), "claude", "codex")
            self.assertIn("Codex doesn't load its agents (reviewer), install settings (userConfig) (token)", asks)
            self.assertIn("its MCP server probe uses ${CLAUDE_PLUGIN_ROOT}, which Codex doesn't expand, so it won't start there", asks)
            self.assertIn("Codex turns its 1 command into skills", notes)
            self.assertIn("Codex asks you before it runs its hooks (SessionStart)", notes)
            self.assertEqual(plugins.check(plugins.parts(root), "claude", "claude"), ([], []))
            (root / "plugin.json").write_text(json.dumps({"name": "deck", "extensions": {"com.openai": {"hooks": []}}}))
            self.assertNotIn("Codex asks you before it runs its hooks (SessionStart)",
                             plugins.check(plugins.parts(root), "claude", "codex")[1])  # its Codex overlay has none

    def test_claude_misses_codex_apps_relative_servers_and_runs_hooks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = plugin_files(Path(temp), skills=["pdf"], hooks=["Stop"], apps=True,
                                servers={"review": {"command": "/bin/sh", "args": ["-c", "exec \"$CODEX_MCP_NODE_PATH\" ./s.mjs"]},
                                         "local": {"command": "node", "args": ["./server.mjs"], "cwd": "."}},
                                codex_manifest={"name": "pdfkit", "skills": "./skills/", "mcpServers": "./.mcp.json"})
            asks, _ = plugins.check(plugins.parts(root), "codex", "claude")
            self.assertIn("Claude Code doesn't load its apps (apps)", asks)
            self.assertIn("its MCP server review needs $CODEX_MCP_NODE_PATH, which only Codex sets", asks)
            self.assertTrue(any("local starts in a folder relative to the plugin" in ask for ask in asks), asks)
            self.assertTrue(any("runs its hooks (Stop) without asking" in ask for ask in asks), asks)
            self.assertIn("can't tell", plugins.check(plugins.parts(Path(temp) / "gone"), "codex", "claude")[0][0])

    def test_plugin_servers_follow_the_mcp_rules(self):
        with tempfile.TemporaryDirectory() as temp:
            root = plugin_files(Path(temp), skills=["track"], servers={
                "tracker": {"type": "http", "url": "https://mcp.example.com/mcp", "headersHelper": "./token.sh"},
                "events": {"type": "sse", "url": "https://mcp.example.com/sse", "headers": {"X-Team": "core"}},
                "calendar": {"type": "http", "url": "https://calendar.example.com/mcp"}})
            asks, _ = plugins.check(plugins.parts(root), "claude", "codex")
            self.assertIn("its MCP server tracker signs in through a headersHelper command, which only Claude Code runs", asks)
            self.assertIn("its MCP server events won't work in Codex: Codex has no SSE transport", asks)
            self.assertEqual(plugins.logins(plugins.parts(root)),
                             ["its MCP server calendar may need a login (OAuth): log in from that CLI once"])

    def test_a_claude_code_mod_stays_in_claude_code(self):
        with tempfile.TemporaryDirectory() as temp:
            root = plugin_files(Path(temp), manifest={"name": "notes-pane"})
            (root / "hooks").mkdir()
            (root / "hooks" / "hooks.json").write_text(json.dumps({"modules": [{"path": "./register.tsx"}]}))
            found = plugins.parts(root)
            self.assertEqual(found.found, {"modules": ["1 module"]})
            self.assertEqual(plugins.useless(found, "codex"), "Codex would load nothing of it: it has only interface mods (1 module)")
            self.assertIsNone(plugins.useless(found, "claude"))
            plugin_files(root, skills=["notes"])
            self.assertIsNone(plugins.useless(plugins.parts(root), "codex"))
            self.assertIn("Codex doesn't load its interface mods (1 module)", plugins.check(plugins.parts(root), "claude", "codex")[0])

    def test_marketplaces_each_cli_can_add(self):
        with tempfile.TemporaryDirectory() as temp:
            codex_only = Path(temp) / "studio"
            (codex_only / ".agents" / "plugins").mkdir(parents=True)
            (codex_only / ".agents" / "plugins" / "marketplace.json").write_text("{}")
            market = plugins.Market("studio", str(codex_only), "local", codex_only)
            self.assertIn("has no .claude-plugin/marketplace.json, which Claude Code needs",
                          plugins.reach(market, "studio", "codex", "claude"))
            self.assertIsNone(plugins.reach(market, "studio", "codex", "codex"))
            listed = plugins.Market("hosted", "https://example.com/marketplace.json", "url")
            self.assertIn("only from a folder or a Git repo", plugins.reach(listed, "hosted", "claude", "codex"))
            self.assertIsNone(plugins.reach(listed, "hosted", "claude", "claude"))
            self.assertIn("doesn't know where", plugins.reach(None, "elsewhere", "claude", "claude"))


class PluginBase(ItemBase):
    def setUp(self):
        super().setUp()
        claude = self.claude
        (claude / "plugins").mkdir(parents=True, exist_ok=True)
        data = json.loads((claude / "settings.json").read_text()) if (claude / "settings.json").exists() else {}
        data["enabledPlugins"] = {"formatter@tools": True, "linter@tools": False, "agents-router@agents-router": True,
                                  "drafts@elsewhere": True}
        (claude / "settings.json").write_text(json.dumps(data), encoding="utf-8")
        installed = {name: [{"scope": "user", "installPath": str(claude / "plugins" / name), "version": "1.2.0"}]
                     for name in ("formatter@tools", "linter@tools", "agents-router@agents-router", "drafts@elsewhere")}
        installed["local@tools"] = [{"scope": "project", "projectPath": str(self.root), "version": "1.0.0"}]
        (claude / "plugins" / "installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": installed}))
        (claude / "plugins" / "known_marketplaces.json").write_text(json.dumps({
            "tools": {"source": {"source": "github", "repo": "example/tools"}}}))

    def host_calls(self, kind=None):
        log = self.root / "host-log.jsonl"
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return [call for call in calls if kind is None or call["kind"] == kind]

    def installs(self):
        return sorted((call["folder"].rpartition("/")[2], call["argv"][2]) for call in self.host_calls()
                      if call["argv"][:2] in (["plugin", "install"], ["plugin", "add"]))

    def offers(self):
        saved = settings.load()
        return {offer.key: offer for offer in plugins.offers(watch.included_folders(saved), saved.off)}


class PluginTests(PluginBase):
    """`agt share plugins` and `plugins apply`: what another CLI could install, installed after a yes."""

    def test_what_is_offered(self):
        self.enable()
        found = self.offers()
        self.assertEqual(sorted(found), ["drafts@elsewhere:claude-lab", "drafts@elsewhere:codex",
                                         "formatter@tools:claude-lab", "formatter@tools:codex"])
        codex = found["formatter@tools:codex"]
        self.assertEqual(codex.steps, [["plugin", "marketplace", "add", "example/tools"], ["plugin", "add", "formatter@tools"]])
        self.assertEqual(found["formatter@tools:claude-lab"].steps[-1], ["plugin", "install", "formatter@tools", "--json"])
        self.assertIn("can't see its installed files", codex.asks[0])  # its files aren't there, so the yes covers that
        self.assertIn("doesn't know where the elsewhere marketplace", found["drafts@elsewhere:codex"].why_not)
        out = json.loads(self.share("plugins", "--json"))
        self.assertEqual(len(out["offers"]), 4)
        self.assertFalse(out["sharing"])
        self.assertEqual(self.host_calls(), [])  # listing runs no CLI

    def test_a_plugin_turned_off_on_purpose_stays_off(self):
        (self.user / ".codex" / "config.toml").write_text('[plugins."formatter@tools"]\nenabled = false\n', encoding="utf-8")
        self.enable()
        self.assertNotIn("formatter@tools:codex", self.offers())

    def test_nothing_installs_without_a_yes(self):
        self.enable()
        out = json.loads(self.share("plugins", "apply", "--json", code=3))
        self.assertEqual(out["error"], "needs_choice")
        self.assertIn("formatter@tools:codex", out["retry"])
        self.assertNotIn("drafts", out["retry"])  # agt can't install that one
        self.share("plugins", "apply", "--yes", code=2)  # --yes needs the plugins named
        self.assertEqual(self.host_calls(), [])

    def test_apply_installs_what_you_named(self):
        self.enable()
        out = self.share("plugins", "apply", "formatter@tools:codex", "--yes")
        self.assertIn("+ formatter in codex", out)
        calls = [call["argv"] for call in self.host_calls("codex")]
        self.assertEqual(calls[:2], [["plugin", "marketplace", "add", "example/tools"], ["plugin", "add", "formatter@tools"]])
        self.assertNotIn("formatter@tools:codex", self.offers())
        self.assertIn("formatter@tools:claude-lab", self.offers())

    def test_a_failed_install_is_remembered_until_the_version_changes(self):
        self.enable()
        with patch.dict(os.environ, {"FAKE_HOST_FAIL": "add"}):
            self.share("plugins", "apply", "formatter@tools:codex", "--yes", code=1)
        offer = self.offers()["formatter@tools:codex"]
        self.assertIn("failed", offer.failed)
        self.assertIn("the last install failed", self.share("plugins"))
        out = json.loads(self.share("plugins", "apply", "--json", code=3))
        self.assertNotIn("formatter@tools:codex", out["retry"])  # not offered again on its own
        installed = json.loads((self.claude / "plugins" / "installed_plugins.json").read_text())
        installed["plugins"]["formatter@tools"][0]["version"] = "1.3.0"
        (self.claude / "plugins" / "installed_plugins.json").write_text(json.dumps(installed))
        self.assertIsNone(self.offers()["formatter@tools:codex"].failed)

    def test_in_a_terminal_each_one_is_asked(self):
        from agents_router.share import commands

        self.enable()
        answers = iter(["y", "x"])  # formatter in claude-lab: yes; in codex: never offer it again
        with patch.object(commands, "_interactive", lambda: True), patch("builtins.input", lambda prompt: next(answers)):
            out = self.share("plugins", "apply")
        self.assertIn("+ formatter in claude-lab", out)
        self.assertIn("plugins/formatter@tools", settings.load().off)
        self.assertEqual(sorted(self.offers()), ["drafts@elsewhere:claude-lab", "drafts@elsewhere:codex"])

    def test_a_prompt_nobody_can_answer_installs_nothing(self):
        from agents_router.share import commands

        self.enable()
        with patch.object(commands, "_interactive", lambda: True), patch("builtins.input", side_effect=EOFError):
            self.share("plugins", "apply")
        self.assertEqual(self.installs(), [])


class PluginSyncTests(PluginBase):
    """Plugins as a kind that shares: each sync installs them where they fit, and asks where they'd lose parts."""

    def setUp(self):
        super().setUp()
        claude, codex = self.claude, self.user / ".codex"
        plugin_files(claude / "plugins" / "formatter@tools", skills=["fmt"], commands=["fmt"])
        plugin_files(claude / "plugins" / "deck@tools", skills=["slides"], agents=["reviewer"])
        installed = json.loads((claude / "plugins" / "installed_plugins.json").read_text())
        installed["plugins"]["deck@tools"] = [{"scope": "user", "installPath": str(claude / "plugins" / "deck@tools"),
                                              "version": "2.0.0"}]
        (claude / "plugins" / "installed_plugins.json").write_text(json.dumps(installed))
        data = json.loads((claude / "settings.json").read_text())
        data["enabledPlugins"]["deck@tools"] = True
        (claude / "settings.json").write_text(json.dumps(data))
        tools = self.root / "markets" / "tools"
        (tools / ".claude-plugin").mkdir(parents=True)
        (tools / ".claude-plugin" / "marketplace.json").write_text("{}")
        (claude / "plugins" / "known_marketplaces.json").write_text(json.dumps({
            "tools": {"source": {"source": "github", "repo": "example/tools"}, "installLocation": str(tools)}}))
        studio = self.root / "markets" / "studio"
        (studio / ".agents" / "plugins").mkdir(parents=True)
        (studio / ".agents" / "plugins" / "marketplace.json").write_text("{}")
        bundled = codex / ".tmp" / "bundled-marketplaces" / "openai-bundled"
        bundled.mkdir(parents=True)
        plugin_files(codex / "plugins" / "cache" / "studio" / "pdfkit" / "1.0.0", skills=["pdf"])
        with (codex / "config.toml").open("a", encoding="utf-8") as handle:
            handle.write(f'\n[marketplaces.studio]\nsource_type = "local"\nsource = {json.dumps(str(studio))}\n'
                         f'\n[marketplaces.openai-bundled]\nsource_type = "local"\nsource = {json.dumps(str(bundled))}\n'
                         '\n[plugins."pdfkit@studio"]\nenabled = true\n\n[plugins."browser@openai-bundled"]\nenabled = true\n')

    def turn_on(self):
        self.enable()
        return self.share("kinds", "add", "plugins", "--yes")

    def test_off_by_default(self):
        self.enable()
        self.assertFalse(settings.load().plugins)
        self.assertIsNone(json.loads(self.share("status", "--json"))["plugins"])
        self.share("sync")
        self.assertEqual(self.installs(), [])

    def test_turning_plugins_on_shows_what_it_installs_and_waits_for_a_yes(self):
        self.enable()
        out = json.loads(self.share("kinds", "add", "plugins", "--json", code=3))
        self.assertEqual(out["error"], "needs_choice")
        self.assertIn("formatter in claude-lab", out["ask"])
        self.assertEqual(out["retry"], "agt share kinds add plugins --yes")
        self.assertFalse(settings.load().plugins)
        self.assertEqual(self.installs(), [])
        preview = self.share("kinds", "add", "plugins", "--dry-run")
        self.assertIn("Plugins: 3 to install", preview)
        self.assertIn("No agent or skill changes.", preview)
        self.assertIn("To apply it:\n  agt share kinds add plugins\n", preview + "\n")
        self.assertEqual(self.installs(), [])

    def test_a_prompt_nobody_can_answer_turns_nothing_on(self):
        from agents_router.share import commands

        self.enable()
        with patch.object(commands, "_interactive", lambda: True), patch("builtins.input", side_effect=EOFError):
            out = self.share("kinds", "add", "plugins")
        self.assertIn("Nothing was installed; plugins don't share.", out)
        self.assertFalse(settings.load().plugins)
        self.assertEqual(self.installs(), [])

    def test_each_plugin_goes_stays_or_waits_for_your_answer(self):
        out = self.turn_on()
        self.assertEqual(self.installs(), [(".claude-lab", "deck@tools"), (".claude-lab", "formatter@tools"),
                                           (".codex", "formatter@tools")])
        self.assertIn("+ plugins/formatter@tools in codex", out)
        self.assertIn("formatter in codex: Codex turns its 1 command into skills", out)
        self.assertIn("Plugins not installed until you answer:", out)
        self.assertIn("deck in codex: Codex doesn't load its agents (reviewer)", out)
        self.assertIn("`agt share pick plugins/deck@tools --in codex`", out)
        self.assertIn("Plugins that can't go there:", out)  # a sync says what it left out, and why
        self.assertIn("pdfkit not in claude: the studio marketplace has no .claude-plugin/marketplace.json", out)
        self.assertEqual(self.record("plugins/formatter@tools")["made"], ["claude-lab", "codex"])
        status = self.share("status", code=1)
        self.assertIn("Waiting for your answer: plugin deck waits for your answer before it goes to codex", status)
        self.assertIn("pdfkit not in claude: the studio marketplace has no .claude-plugin/marketplace.json", status)
        self.assertIn("browser (it comes with Codex", status)
        self.assertIn("drafts not in codex: agt doesn't know where the elsewhere marketplace comes from", status)
        self.assertIn("Installed only for a project in claude, so not shared: 1 (local)", status)
        self.assertIn("kiro: it installs no plugins", status)
        needs = {entry["id"]: entry for entry in json.loads(self.share("status", "--json", code=1))["attention"]}
        self.assertEqual(needs["plugin:plugins/deck@tools:codex"]["fix"],
                         ["agt share pick plugins/deck@tools --in codex", "agt share pick plugins/deck@tools --not-in codex"])
        before = len(self.installs())
        self.share("sync")
        self.assertEqual(len(self.installs()), before)  # nothing new: deck still waits

    def test_your_yes_installs_it_and_a_change_asks_again(self):
        self.turn_on()
        saved = settings.load()
        saved.plugin_approvals["deck@tools"] = {"codex": plugins.signature(["Codex doesn't load its agents (reviewer)"])}
        settings.save(saved)
        plugin_files(self.claude / "plugins" / "deck@tools", agents=["planner"])  # your yes was for the version before
        self.share("sync")
        self.assertNotIn((".codex", "deck@tools"), self.installs())
        self.assertIn("Codex doesn't load its agents (planner, reviewer)", self.share("status", code=1))
        out = self.share("pick", "plugins/deck@tools", "--in", "codex")
        self.assertIn("+ plugins/deck@tools in codex", out)
        self.assertIn("you said yes to this: Codex doesn't load its agents (planner, reviewer)", out)
        self.assertEqual(settings.load().plugin_approvals["deck@tools"],
                         {"codex": plugins.signature(["Codex doesn't load its agents (planner, reviewer)"])})
        self.assertIn("[share.plugin_approvals]", settings.path().read_text())

    def test_not_in_keeps_it_out_and_uninstalls_agts_copy(self):
        self.turn_on()
        self.share("pick", "plugins/deck@tools", "--not-in", "codex")
        self.assertNotIn("plugins/deck@tools", json.dumps(json.loads(self.share("status", "--json"))["attention"]))
        out = self.share("pick", "plugins/formatter@tools", "--not-in", "codex")
        self.assertIn("- plugins/formatter@tools in codex", out)
        self.assertIn(["plugin", "remove", "formatter@tools"], [call["argv"] for call in self.host_calls("codex")])
        self.assertNotIn('"formatter@tools"', (self.user / ".codex" / "config.toml").read_text())

    def test_a_new_plugin_spreads_and_uninstalls_are_respected(self):
        self.turn_on()
        claude = self.claude
        plugin_files(claude / "plugins" / "notes@tools", skills=["note"])
        installed = json.loads((claude / "plugins" / "installed_plugins.json").read_text())
        installed["plugins"]["notes@tools"] = [{"scope": "user", "installPath": str(claude / "plugins" / "notes@tools"),
                                               "version": "1.0.0"}]
        (claude / "plugins" / "installed_plugins.json").write_text(json.dumps(installed))
        data = json.loads((claude / "settings.json").read_text())
        data["enabledPlugins"]["notes@tools"] = True
        (claude / "settings.json").write_text(json.dumps(data))
        self.share("sync")
        self.assertIn((".codex", "notes@tools"), self.installs())
        self.assertIn((".claude-lab", "notes@tools"), self.installs())
        # You uninstall agt's copy in Codex: it stops going there.
        self.run_host("codex", self.user / ".codex", "plugin", "remove", "notes@tools")
        self.share("sync")
        self.assertEqual(self.installs().count((".codex", "notes@tools")), 1)
        self.assertIn("codex", self.record("plugins/notes@tools")["stopped"])
        # You uninstall your own: agt's other copies wait for `rm`.
        installed["plugins"].pop("notes@tools")
        (claude / "plugins" / "installed_plugins.json").write_text(json.dumps(installed))
        status = json.loads(self.share("status", "--json", code=1))
        self.assertIn("delete:plugins/notes@tools", [entry["id"] for entry in status["attention"]])
        out = self.share("rm", "plugins/notes@tools")
        self.assertIn("- plugins/notes@tools in claude-lab", out)
        self.assertIsNone(self.record("plugins/notes@tools"))

    def test_a_half_written_plugin_file_changes_nothing(self):
        self.turn_on()
        installed = self.lab / "plugins" / "installed_plugins.json"
        installed.write_text(installed.read_text()[:20], encoding="utf-8")  # Claude Code is in the middle of writing it
        self.share("sync")
        self.assertEqual(self.record("plugins/formatter@tools").get("stopped", []), [])
        self.assertEqual(self.record("plugins/formatter@tools")["made"], ["claude-lab", "codex"])
        needs = json.loads(self.share("status", "--json", code=1))["attention"]
        self.assertTrue(any(entry["kind"] == "unreadable" and entry["item"] == "plugins" for entry in needs), needs)

    def test_plugins_apply_counts_as_agts_install(self):
        self.enable()
        self.share("plugins", "apply", "formatter@tools:codex", "--yes")
        self.assertEqual(self.record("plugins/formatter@tools")["made"], ["codex"])

    def test_a_session_or_save_leaves_installs_to_the_watcher(self):
        self.enable()
        saved = settings.load()
        saved.plugins = True
        settings.save(saved)
        self.share("sync", "--trigger", "session")
        self.assertEqual(self.installs(), [])
        self.share("sync")
        self.assertEqual(len(self.installs()), 3)

    def test_a_project_install_there_waits_for_your_answer(self):
        installed = self.lab / "plugins" / "installed_plugins.json"
        installed.parent.mkdir(parents=True, exist_ok=True)
        installed.write_text(json.dumps({"version": 2, "plugins": {
            "formatter@tools": [{"scope": "local", "projectPath": str(self.root), "version": "1.2.0"}]}}))
        self.turn_on()
        self.assertNotIn((".claude-lab", "formatter@tools"), self.installs())
        status = self.share("status", code=1)
        self.assertIn("formatter waits for your answer before it goes to claude-lab: it's installed there for a project "
                      "(local scope)", status)

    def test_a_failed_install_is_told_once_and_a_pick_tries_again(self):
        self.enable()
        with patch.dict(os.environ, {"FAKE_HOST_FAIL": "add"}):
            self.share("kinds", "add", "plugins", "--yes", code=1)
            self.share("sync")
        self.assertEqual([plugin for folder, plugin in self.installs() if folder == ".codex"], ["formatter@tools"])
        needs = json.loads(self.share("status", "--json", code=1))["attention"]
        failing = next(entry for entry in needs if entry["id"] == "failing:plugins/formatter@tools:codex")
        self.assertEqual(failing["fix"][0], "agt share pick plugins/formatter@tools --in codex")
        self.share("pick", "plugins/formatter@tools", "--in", "codex")
        self.assertEqual([plugin for folder, plugin in self.installs() if folder == ".codex"], ["formatter@tools"] * 2)

    def test_kinds_remove_keeps_plugins_unless_you_ask(self):
        self.turn_on()
        self.share("kinds", "remove", "plugins", "--remove-copies")
        self.assertFalse(settings.load().plugins)
        removed = sorted((call["folder"].rpartition("/")[2], call["argv"][2]) for call in self.host_calls()
                         if call["argv"][:2] in (["plugin", "uninstall"], ["plugin", "remove"]))
        self.assertEqual(removed, [(".claude-lab", "deck@tools"), (".claude-lab", "formatter@tools"), (".codex", "formatter@tools")])
        self.assertIn("formatter@tools", json.loads((self.claude / "settings.json").read_text())["enabledPlugins"])

    def test_the_watcher_watches_plugin_files(self):
        self.turn_on()
        saved = settings.load()
        files = watch.watched_files(saved, watch.included_folders(saved))
        self.assertIn(self.claude / "plugins" / "installed_plugins.json", files)
        self.assertIn(self.lab / "settings.json", files)

    def run_host(self, kind, folder, *argv):
        import subprocess
        import sys

        env = {**os.environ, "CLAUDE_CONFIG_DIR" if kind == "claude" else "CODEX_HOME": str(folder)}
        subprocess.run([sys.executable, str(Path(__file__).with_name("fake_host.py")), kind, *argv], env=env, check=True,
                       capture_output=True)


if __name__ == "__main__":
    unittest.main()
