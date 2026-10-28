"""Plugins between Claude Code and Codex: what each CLI can load, the sync that installs them, and `plugins apply`."""

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_share_items import ItemBase

from agents_router import cli
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
        self.assertEqual(out, "agt share plugins apply  ✓ wrote 1 change in 1 folder\n\n  + plugins/formatter@tools   codex\n\n"
                              "Next: start a new session in a CLI to use what it got.\n")
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
        with patch.object(commands, "_interactive", lambda: True), patch("builtins.input", lambda prompt: "n"):
            self.assertTrue(self.share("plugins", "apply").endswith("\n\nagt share plugins apply  nothing installed\n"))
        answers = iter(["y", "x"])  # formatter in claude-lab: yes; in codex: never offer it again
        with patch.object(commands, "_interactive", lambda: True), patch("builtins.input", lambda prompt: next(answers)):
            out = self.share("plugins", "apply")
        self.assertIn("formatter in codex\n  runs   codex plugin marketplace add example/tools\n", out)  # each command it runs
        self.assertIn("\n  + plugins/formatter@tools   claude-lab\n", out)
        self.assertIn("plugins/formatter@tools", settings.load().off)
        self.assertIn("\nformatter isn't offered again in any CLI; `agt share plugins apply formatter@tools:codex --yes` "
                      "installs it and offers it again.\n", out)
        self.assertEqual(sorted(self.offers()), ["drafts@elsewhere:claude-lab", "drafts@elsewhere:codex"])
        self.assertTrue(self.share("plugins").endswith(  # weeks later, the page still says how to bring it back
            "\n\nYou turned off formatter; `agt share plugins apply formatter@tools:codex --yes` installs it and offers "
            "it again.\n"))
        out = self.share("plugins", "apply", "formatter@tools:codex", "--yes")  # the undo for "x"
        self.assertIn("\n  + plugins/formatter@tools   codex\n", out)
        self.assertTrue(out.endswith("\nSaved your answers in ~/.agt/settings.toml.\n"), out)
        self.assertNotIn("plugins/formatter@tools", settings.load().off)
        self.assertNotIn("You turned off", self.share("plugins"))

    def test_try_again_names_every_cli_that_failed(self):
        from agents_router.share import apply, commands
        from agents_router import ui

        outcome = apply.Outcome("plugins", "formatter@tools", ["claude-lab", "claude"], "copy", Path("/x"), False, "boom")
        self.assertEqual(ui.plain(commands._apply_again(outcome, ["claude-lab", "claude"])),
                         "      Try again   agt share plugins apply formatter@tools:claude-lab formatter@tools:claude --yes")

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
        self.assertIn("\nPlugins to install\n  + deck@tools        claude-lab\n  + formatter@tools   claude-lab, codex\n"
                      "                      In codex, Codex turns its 1 command into skills.\n", preview)
        self.assertRegex(preview, r"\n  ! plugin deck, for codex\n    Codex doesn't load its agents \(reviewer\)\.\n"
                                  r"    Install it as it is +agt share pick plugins/deck@tools --in codex\n")
        self.assertIn("\nStaying where they are: browser (comes with Codex), linter (not on in claude).\n", preview)
        self.assertTrue(preview.endswith("\nNo agent or skill changes. To apply it:\n  agt share kinds add plugins\n"), preview)
        self.assertNotIn("Not shared", preview)  # not a change
        self.assertEqual(self.installs(), [])

    def test_a_prompt_nobody_can_answer_turns_nothing_on(self):
        from agents_router.share import commands

        self.enable()
        with patch.object(commands, "_interactive", lambda: True), patch("builtins.input", side_effect=EOFError):
            out = self.share("kinds", "add", "plugins")
        self.assertIn("agt share kinds add plugins  nothing installed; plugins don't share\n", out)
        self.assertFalse(settings.load().plugins)
        self.assertEqual(self.installs(), [])

    def test_each_plugin_goes_stays_or_waits_for_your_answer(self):
        out = self.turn_on()
        self.assertEqual(self.installs(), [(".claude-lab", "deck@tools"), (".claude-lab", "formatter@tools"),
                                           (".codex", "formatter@tools")])
        self.assertIn("  + plugins/formatter@tools   claude-lab, codex\n", out)
        self.assertIn("formatter in codex: Codex turns its 1 command into skills", out)
        self.assertNotIn("waits for you:", out)  # the one wait is listed in full
        self.assertIn("\nWaiting for you\n  ! plugin deck, for codex\n    Codex doesn't load its agents (reviewer).\n", out)
        self.assertRegex(out, r"\n    Install it as it is +agt share pick plugins/deck@tools --in codex\n")
        self.assertIn("\nCan't be installed there\n", out)  # turning plugins on says what it left out, and why
        self.assertRegex(out, r"\n  pdfkit +claude, claude-lab +The studio marketplace has no \.claude-plugin/marketplace\.json")
        self.assertIn("\nNext: start a new session in a CLI (or run /reload-plugins in Claude Code) to use what it got.\n", out)
        self.assertEqual(self.record("plugins/formatter@tools")["made"], ["claude-lab", "codex"])
        status = self.share("status", code=1)
        self.assertIn("  ! plugin deck, for codex\n    Codex doesn't load its agents (reviewer).\n", status)
        self.assertRegex(status, r"\n    Install it as it is +agt share pick plugins/deck@tools --in codex\n")
        self.assertIn("  Plugins   4 shared, 2 stay in their own CLI, 2 can't be installed everywhere\n", status)
        self.assertIn("`agt share plugins` says why the others stay where they are.", status)
        report = json.loads(self.share("status", "--json", code=1))
        summary = report["plugins"]  # the JSON keeps every reason
        self.assertIn("pdfkit not in claude: the studio marketplace has no .claude-plugin/marketplace.json", summary)
        self.assertIn("browser (it comes with Codex", summary)
        self.assertIn("drafts not in codex: agt doesn't know where the elsewhere marketplace comes from", summary)
        self.assertIn("Installed only for a project in claude, so not shared: 1 (local)", summary)
        self.assertIn("kiro: it installs no plugins", summary)
        needs = {entry["id"]: entry for entry in report["attention"]}
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
        self.assertIn("  + plugins/deck@tools   codex\n", out)
        self.assertIn("you said yes to this: Codex doesn't load its agents (planner, reviewer)", out)
        self.assertEqual(settings.load().plugin_approvals["deck@tools"],
                         {"codex": plugins.signature(["Codex doesn't load its agents (planner, reviewer)"])})
        self.assertIn("[share.plugin_approvals]", settings.path().read_text())

    def test_not_in_keeps_it_out_and_uninstalls_agts_copy(self):
        self.turn_on()
        self.share("pick", "plugins/deck@tools", "--not-in", "codex")
        self.assertNotIn("plugins/deck@tools", json.dumps(json.loads(self.share("status", "--json"))["attention"]))
        out = self.share("pick", "plugins/formatter@tools", "--not-in", "codex")
        self.assertIn("  - plugins/formatter@tools   codex\n", out)
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
        self.assertIn("  - plugins/notes@tools   claude-lab\n", out)
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
        self.assertIn("  ! plugin formatter, for claude-lab\n    It's installed there for a project (local scope)", status)

    def test_a_failed_install_is_told_once_and_a_pick_tries_again(self):
        self.enable()
        with patch.dict(os.environ, {"FAKE_HOST_FAIL": "add"}):
            out = self.share("kinds", "add", "plugins", "--yes", code=1)
            self.share("sync")
        self.assertNotIn("formatter in codex: Codex turns", out)  # no note about an install that didn't go in
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

    def test_the_plugins_page_reads_like_status(self):
        self.enable()
        off = self.share("plugins")
        self.assertTrue(off.startswith("agt share plugins  off; 4 installs could go to another CLI\n\n"
                                       "  deck       claude-lab, codex\n"
                                       "             In codex, Codex doesn't load its agents (reviewer).\n"), off)
        self.assertNotIn("marketplace add", off)  # the commands agt runs stay in --json and the apply prompt
        self.assertTrue(off.endswith("\n  Install them with each sync   agt share kinds add plugins\n"
                                     "  Choose one at a time now      agt share plugins apply\n"), off)
        self.share("kinds", "add", "plugins", "--yes")
        on = self.share("plugins")
        self.assertTrue(on.startswith("agt share plugins  on: 4 shared, 1 thing waits for you\n\n"
                                      "Waiting for you\n  ! plugin deck, for codex\n"), on)
        self.assertIn("\nStaying where they are\n  Come with Codex    browser\n  Not on in claude   linter\n", on)
        self.assertIn("\nInstalled for one project only, so not shared\n  claude   local\n", on)
        self.assertTrue(on.endswith("\nkiro installs no plugins; plugins go between Claude Code and Codex folders only.\n"), on)
        pick = self.share("pick", "plugins/deck@tools")
        self.assertTrue(pick.startswith("agt share pick plugins/deck@tools  where it goes; nothing changed\n"), pick)
        self.assertIn("\n  codex        ! waits for your answer: Codex doesn't load its agents (reviewer).\n", pick)
        self.assertIn("\nChange it\n  Install it as it is   agt share pick plugins/deck@tools --in codex\n", pick)

    def test_with_no_one_to_answer_it_gives_the_commands(self):
        self.enable()
        self.assertEqual(self.share("kinds", "add", "plugins", code=3),
                         "agt share kinds add plugins  ! nothing installed: turning plugins on installs 3 now\n\n"
                         "Plugins to install\n  + deck@tools        claude-lab\n  + formatter@tools   claude-lab, codex\n"
                         "                      In codex, Codex turns its 1 command into skills.\n\n"
                         "  Go ahead         agt share kinds add plugins --yes\n"
                         "  See everything   agt share kinds add plugins --dry-run\n")
        out = self.share("plugins", "apply", code=3)
        self.assertIn("\n  All of them               agt share plugins apply deck@tools:claude-lab deck@tools:codex "
                      "formatter@tools:claude-lab formatter@tools:codex --yes\n", out)  # whole, never shortened

    def test_installs_keep_their_order(self):
        self.enable()
        out = json.loads(self.share("kinds", "add", "plugins", "--yes", "--json"))
        self.assertEqual([(entry["name"], entry["labels"]) for entry in out["written"]],
                         [("deck@tools", ["claude-lab"]), ("formatter@tools", ["claude-lab"]), ("formatter@tools", ["codex"])])

    def test_turning_plugins_off_says_what_stays(self):
        self.turn_on()
        self.assertEqual(self.share("kinds", "remove", "plugins", "--dry-run"),
                         "agt share kinds remove plugins  dry run, nothing changed\n\n"
                         "Turning off plugins leaves the 3 installs agt made:\n"
                         "  deck        claude-lab\n  formatter   claude-lab, codex\nEvery CLI keeps them.\n\n"
                         "  Uninstall them too   agt share kinds remove plugins --remove-copies\n")
        self.assertTrue(self.share("kinds", "remove", "plugins", "--dry-run", "--remove-copies").endswith(
            "Turning off plugins removes the 3 installs agt made:\n  deck        claude-lab\n  formatter   claude-lab, codex\n\n"
            "To apply it:\n  agt share kinds remove plugins --remove-copies\n"))
        self.assertEqual(self.share("kinds", "remove", "plugins"),
                         "agt share kinds remove plugins  ✓ plugins don't share any more\n\n"
                         "  Every CLI keeps the plugins it has; agt installs none from now on.\n\n"
                         "  Share them again   agt share kinds add plugins\n")
        self.assertEqual(self.share("kinds", "remove", "plugins"), "agt share kinds remove plugins  already off\n")

    def test_a_failed_uninstall_gives_the_command_to_try_again(self):
        self.turn_on()
        with patch.dict(os.environ, {"FAKE_HOST_FAIL": "remove"}):  # codex's copy stays listed
            out = self.share("rm", "plugins/formatter@tools", code=1)
        self.assertIn("  ✗ plugins/formatter@tools   codex\n", out)
        self.assertIn("\n      Try again   agt share rm plugins/formatter@tools\n", out)

    def test_an_uninstall_the_cli_says_isnt_needed_counts_as_removed(self):
        self.turn_on()
        out = self.share("rm", "plugins/formatter@tools", "-v")  # the fake claude's list doesn't show your own install
        self.assertNotIn("✗", out)
        self.assertNotIn("Try again", out)
        self.assertIn("  - plugins/formatter@tools   claude\n      already gone: claude plugin list doesn't show it\n", out)

    def test_an_uninstall_with_a_list_agt_cant_read_stays_a_failure(self):
        self.turn_on()
        with patch.object(plugins, "_listed", return_value=None):
            out = self.share("rm", "plugins/formatter@tools", code=1)
        self.assertIn("  ✗ plugins/formatter@tools   claude\n", out)
        self.assertIn("\n      Try again   agt share rm plugins/formatter@tools\n", out)

    def test_ctrl_c_says_what_finishes_the_installs(self):
        self.enable()
        err = io.StringIO()
        with patch.object(plugins, "run_steps", side_effect=KeyboardInterrupt), contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["share", "kinds", "add", "plugins", "--yes"]), 130)
        self.assertEqual(err.getvalue(), "Stopped. The next sync finishes what's left.\n")  # plugins share now

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
