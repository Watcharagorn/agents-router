"""Plugins on in one CLI, offered to the others (`agt share plugins`), installed only after a yes."""

import json
import os
from unittest.mock import patch

from test_share_items import ItemBase

from agents_router.share import plugins, settings, watch


class PluginTests(ItemBase):
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

    def host_calls(self):
        log = self.root / "host-log.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def offers(self):
        saved = settings.load()
        return {offer.key: offer for offer in plugins.offers(watch.included_folders(saved), saved.off)}

    def test_what_is_offered(self):
        self.enable()
        found = self.offers()
        self.assertEqual(sorted(found), ["drafts@elsewhere:claude-lab", "drafts@elsewhere:codex",
                                         "formatter@tools:claude-lab", "formatter@tools:codex"])
        codex = found["formatter@tools:codex"]
        self.assertEqual(codex.steps, [["plugin", "marketplace", "add", "example/tools"], ["plugin", "add", "formatter@tools"]])
        self.assertEqual(found["formatter@tools:claude-lab"].steps[-1], ["plugin", "install", "formatter@tools"])
        self.assertIn("doesn't know where the elsewhere marketplace", found["drafts@elsewhere:codex"].why_not)
        out = json.loads(self.share("plugins", "--json"))
        self.assertEqual(len(out["offers"]), 4)
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
        calls = [call["argv"] for call in self.host_calls() if call["kind"] == "codex"]
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


if __name__ == "__main__":
    import unittest

    unittest.main()
