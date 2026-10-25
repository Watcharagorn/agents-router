"""Sharing user MCP servers between CLIs: the one shape, what never goes, and the runs (`kinds add mcp`)."""

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from test_share_items import ItemBase
from test_share_sync import age

from agents_router import cli
from agents_router.share import engine, mcp, providers, settings, watch
from agents_router.share.plan import Options
from agents_router.share.state import State

NOTES = {"command": "npx", "args": ["-y", "notes-mcp"], "env": {"NOTES_TOKEN": "${NOTES_TOKEN}", "MODE": "fast"}}
SERVERS = {
    "plain": {"command": "npx", "args": ["-y", "pkg"]},
    "refs": NOTES,
    "folder": {"command": "./run.sh", "args": [], "cwd": "/srv/tool"},
    "bearer": {"type": "http", "url": "https://mcp.example.com/mcp", "headers": {"Authorization": "Bearer ${TRACKER_TOKEN}"}},
    "region": {"type": "http", "url": "https://mcp.example.com/mcp", "headers": {"X-Region": "eu"}},
    "keyed": {"type": "http", "url": "https://mcp.example.com/mcp", "headers": {"X-Api-Key": "${TRACKER_KEY}"}},
    "events": {"type": "sse", "url": "https://mcp.example.com/sse"},
    "lower": {"type": "http", "url": "https://mcp.example.com/mcp", "headers": {"authorization": "Bearer ${TRACKER_TOKEN}"}},
    "renamed": {"command": "tool", "args": [], "env": {"API_TOKEN": "${OTHER_TOKEN}"}},
}


class ShapeTests(unittest.TestCase):
    """Each CLI's file, written and read back: what goes comes back the same, and what can't is blocked."""

    def test_every_server_round_trips_or_is_blocked(self):
        with tempfile.TemporaryDirectory() as temp:
            for cli_name, folder in (("codex", "codex"), ("kiro", "kiro"), ("antigravity", "gemini")):
                provider = providers.Provider(cli_name, Path(temp) / folder, cli_name, True)
                for name, server in SERVERS.items():
                    expected = mcp.normalize(server)
                    if mcp.blocked(expected, cli_name):
                        continue
                    mcp.write(provider, name, expected)
                    self.assertEqual(mcp.read(provider)[0][name], expected, f"{name} through {cli_name}")
        self.assertIn("Codex has no SSE", mcp.blocked(SERVERS["events"], "codex"))
        self.assertIn("SSE", mcp.blocked(SERVERS["events"], "antigravity"))
        self.assertIn("passes variables only by name", mcp.blocked(SERVERS["renamed"], "codex"))
        self.assertIn("doesn't expand", mcp.blocked(SERVERS["refs"], "antigravity"))
        self.assertIn("can't pass", mcp.blocked(SERVERS["lower"], "codex"))
        self.assertIsNone(mcp.blocked(SERVERS["refs"], "kiro"))

    def test_codex_keeps_its_own_keys_and_every_other_line(self):
        with tempfile.TemporaryDirectory() as temp:
            provider = providers.Provider("codex", Path(temp), "codex", True)
            config = Path(temp) / "config.toml"
            config.write_text('model = "gpt-5"\n\n[mcp_servers.notes]\ncommand = "old"\nstartup_timeout_sec = 20\n\n'
                              '[mcp_servers.notes.env]\nMODE = "slow"\n\n[profiles.fast]\nmodel = "mini"\n', encoding="utf-8")
            mcp.write(provider, "notes", NOTES)
            data = tomllib.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(data["model"], "gpt-5")
            self.assertEqual(data["profiles"], {"fast": {"model": "mini"}})
            self.assertEqual(data["mcp_servers"]["notes"]["startup_timeout_sec"], 20)
            self.assertEqual(data["mcp_servers"]["notes"]["env_vars"], ["NOTES_TOKEN"])
            self.assertEqual(data["mcp_servers"]["notes"]["env"], {"MODE": "fast"})
            mcp.write(provider, "notes", None)
            data = tomllib.loads(config.read_text(encoding="utf-8"))
            self.assertNotIn("notes", data.get("mcp_servers", {}))
            self.assertEqual(data["profiles"], {"fast": {"model": "mini"}})

    def test_literal_secrets_are_found(self):
        self.assertIsNotNone(mcp.secret({"command": "x", "args": [], "env": {"API_TOKEN": "abc123"}}))
        self.assertIsNotNone(mcp.secret({"command": "x", "args": [], "env": {"DATABASE_URL": "postgres://u:pw@db/main"}}))
        self.assertIsNotNone(mcp.secret({"type": "http", "url": "https://x", "headers": {"Authorization": "Bearer abc"}}))
        self.assertIsNotNone(mcp.secret({"command": "x", "args": ["--api-key", "abc"]}))
        self.assertIsNone(mcp.secret(NOTES))

    def test_a_pick_naming_no_included_folder_sends_the_server_nowhere(self):
        with tempfile.TemporaryDirectory() as temp:
            folders = [providers.Provider(name, Path(temp) / name, name, True) for name in ("codex", "kiro")]
            mcp.write(folders[0], "notes", NOTES)
            options = Options(only={"mcp/notes": set()})
            plan = mcp.build(folders, State(), options)
            self.assertEqual(plan.writes, [])
            self.assertFalse((Path(temp) / "kiro" / "settings" / "mcp.json").exists())


class McpTests(ItemBase):
    def setUp(self):
        super().setUp()
        self.claude_json = self.user / ".claude.json"  # the default folder's state file, as Claude Code keeps it
        self.claude_json.write_text(json.dumps({"numStartups": 3, "mcpServers": {
            "notes": {"type": "stdio", **NOTES},
            "agents-router": {"command": "agt", "args": ["mcp"]},
        }}, indent=2), encoding="utf-8")

    def servers(self, label):
        path = {"claude": self.claude_json, "claude-lab": self.lab / ".claude.json",
                "codex": self.user / ".codex" / "config.toml", "kiro": self.user / ".kiro" / "settings" / "mcp.json"}[label]
        if not path.exists():
            return {}
        if label == "codex":
            return {name: mcp._from_codex(table) for name, table in tomllib.loads(path.read_text()).get("mcp_servers", {}).items()}
        return {name: mcp.normalize(entry) for name, entry in json.loads(path.read_text()).get("mcpServers", {}).items()}

    def host_calls(self):
        log = self.root / "host-log.jsonl"
        return [json.loads(line)["argv"] for line in log.read_text().splitlines()] if log.exists() else []

    def test_off_by_default(self):
        self.enable()
        self.assertFalse(settings.load().mcp)
        self.assertEqual(self.servers("kiro"), {})
        self.assertIsNone(json.loads(self.share("status", "--json"))["mcp"])

    def test_a_server_in_one_cli_reaches_the_others(self):
        out = self.enable("--mcp", "on")
        self.assertIn("mcp/notes in", out)
        for label in ("claude-lab", "codex", "kiro"):
            self.assertEqual(self.servers(label).get("notes"), NOTES, label)
            self.assertNotIn("agents-router", self.servers(label))  # agt's own server never goes
        self.assertIn(["mcp", "add-json", "--scope", "user", "notes", json.dumps(NOTES)], self.host_calls())
        self.assertIn("[[skills.config]]", (self.user / ".codex" / "config.toml").read_text())  # Codex's other lines stay
        self.assertEqual(json.loads(self.claude_json.read_text())["numStartups"], 3)
        self.assertTrue(settings.load().mcp)
        self.assertIn("Kiro asks you before it passes ${VAR} values", out)
        self.assertNotIn("mcp/notes", self.share("sync"))  # nothing left to write
        self.assertEqual(self.record("mcp/notes")["made"], ["claude-lab", "codex", "kiro"])

    def test_an_edit_spreads_and_each_cli_keeps_its_own_keys(self):
        self.enable("--mcp", "on")
        kiro = self.user / ".kiro" / "settings" / "mcp.json"
        data = json.loads(kiro.read_text())
        data["mcpServers"]["notes"]["args"] = ["-y", "notes-mcp@2"]
        data["mcpServers"]["notes"]["autoApprove"] = ["search"]
        kiro.write_text(json.dumps(data, indent=2))
        codex = self.user / ".codex" / "config.toml"
        codex.write_text(codex.read_text().replace("[mcp_servers.notes]\n", "[mcp_servers.notes]\nstartup_timeout_sec = 30\n"))
        self.share("sync")
        for label in ("claude", "claude-lab", "codex"):
            self.assertEqual(self.servers(label)["notes"]["args"], ["-y", "notes-mcp@2"], label)
        self.assertEqual(json.loads(kiro.read_text())["mcpServers"]["notes"]["autoApprove"], ["search"])
        self.assertEqual(tomllib.loads(codex.read_text())["mcp_servers"]["notes"]["startup_timeout_sec"], 30)

    def test_a_server_with_a_literal_secret_stays_where_it_is(self):
        data = json.loads(self.claude_json.read_text())
        data["mcpServers"]["vault"] = {"command": "vault-mcp", "args": [], "env": {"VAULT_TOKEN": "hvs.literal"}}
        self.claude_json.write_text(json.dumps(data))
        proposal = json.loads(self.share("enable", "--mcp", "on", "--dry-run", "--json", "--resolve", "gamma-apikey=skip",
                                         *self.flags()))
        self.assertIn("vault", proposal["mcp"]["staying"])
        self.enable("--mcp", "on")
        for label in ("claude-lab", "codex", "kiro"):
            self.assertNotIn("vault", self.servers(label))
        self.assertNotIn("hvs.literal", json.dumps([path.read_text(errors="replace") for path in
                                                     (self.agt_home / "state").rglob("*") if path.is_file()]))

    def flags(self):
        from test_share_plan import FLAGS

        return FLAGS

    def test_different_servers_are_a_conflict_until_you_pick_one(self):
        (self.user / ".codex" / "config.toml").write_text('[mcp_servers.notes]\ncommand = "notes-mcp"\nargs = []\n')
        proposal = json.loads(self.share("enable", "--mcp", "on", "--dry-run", "--json", "--resolve", "gamma-apikey=skip",
                                         *self.flags()))
        self.assertIn("resolve:mcp/notes", [ask["id"] for ask in proposal["needs_answer"]])
        self.enable("--mcp", "on", code=3)
        self.enable("--mcp", "on", "--resolve", "mcp/notes=codex")
        for label in ("claude", "claude-lab", "kiro"):
            self.assertEqual(self.servers(label)["notes"], {"command": "notes-mcp", "args": []}, label)

    def test_resolve_after_the_fact(self):
        self.enable("--mcp", "on")
        kiro = self.user / ".kiro" / "settings" / "mcp.json"
        data = json.loads(kiro.read_text())
        data["mcpServers"]["notes"]["args"] = ["kiro-edit"]
        kiro.write_text(json.dumps(data))
        codex = self.user / ".codex" / "config.toml"
        codex.write_text(codex.read_text().replace('args = ["-y", "notes-mcp"]', 'args = ["codex-edit"]'))
        status = self.share("status", code=1)
        self.assertIn("differ between CLIs (notes)", status)
        out = json.loads(self.share("resolve", "mcp/notes", "--json", code=3))
        self.assertEqual(out["error"], "needs_choice")
        self.share("resolve", "mcp/notes", "--prefer", "kiro")
        for label in ("claude", "claude-lab", "codex"):
            self.assertEqual(self.servers(label)["notes"]["args"], ["kiro-edit"], label)

    def test_a_pick_keeps_a_server_in_one_cli_and_takes_agts_copies_out(self):
        self.enable("--mcp", "on")
        out = self.share("pick", "mcp/notes", "--only", "claude")
        self.assertIn("- mcp/notes in", out)
        for label in ("claude-lab", "codex", "kiro"):
            self.assertNotIn("notes", self.servers(label), label)
        self.assertEqual(self.servers("claude")["notes"], NOTES)
        self.assertEqual(settings.load().only, {"mcp/notes": ["claude"]})
        self.assertNotIn("mcp/notes", self.share("sync"))
        self.share("pick", "mcp/notes", "--in", "kiro")
        self.assertEqual(self.servers("kiro")["notes"], NOTES)

    def test_a_pick_leaves_a_copy_you_changed(self):
        self.enable("--mcp", "on")
        codex = self.user / ".codex" / "config.toml"
        codex.write_text(codex.read_text().replace('args = ["-y", "notes-mcp"]', 'args = ["mine"]'))
        out = self.share("pick", "mcp/notes", "--only", "claude", "--json")
        self.assertEqual(self.servers("codex")["notes"]["args"], ["mine"])
        self.assertNotIn("notes", self.servers("kiro"))
        self.assertIn("notes stays in codex", out)

    def test_disable_with_remove_copies_takes_out_agts_servers(self):
        self.enable("--mcp", "on")
        self.share("disable", "--remove-copies")
        for label in ("claude-lab", "codex", "kiro"):
            self.assertNotIn("notes", self.servers(label), label)
        self.assertEqual(self.servers("claude")["notes"], NOTES)  # where it started stays

    def test_rm_takes_a_server_out_of_every_cli(self):
        self.enable("--mcp", "on")
        data = json.loads(self.claude_json.read_text())
        del data["mcpServers"]["notes"]
        self.claude_json.write_text(json.dumps(data))
        self.assertIn("removed where it started", self.share("status", code=1))
        self.assertEqual(self.servers("kiro")["notes"], NOTES)  # the copies wait for you
        self.share("rm", "mcp/notes")
        for label in ("claude-lab", "codex", "kiro"):
            self.assertNotIn("notes", self.servers(label), label)
        self.assertIsNone(self.record("mcp/notes"))
        self.assertTrue(list((self.agt_home / "state" / "share" / "backups").rglob("kiro-mcp.json")))

    def test_a_session_hook_leaves_claude_to_the_watcher(self):
        self.enable("--mcp", "on")
        codex = self.user / ".codex" / "config.toml"
        codex.write_text(codex.read_text() + '\n[mcp_servers.search]\ncommand = "search-mcp"\nargs = []\n')
        calls = len(self.host_calls())
        watch.background_sync("session", if_stale=False)
        self.assertIn("search", self.servers("kiro"))
        self.assertNotIn("search", self.servers("claude"))
        self.assertEqual(len(self.host_calls()), calls)  # no `claude` start inside the hook's budget
        watch.background_sync("watcher", if_stale=False)
        self.assertIn("search", self.servers("claude"))
        self.assertIn("search", self.servers("claude-lab"))

    def test_a_failed_claude_write_puts_back_what_was_there(self):
        self.enable("--mcp", "on")
        kiro = self.user / ".kiro" / "settings" / "mcp.json"
        data = json.loads(kiro.read_text())
        data["mcpServers"]["notes"]["args"] = ["new"]
        kiro.write_text(json.dumps(data))
        once = self.root / "fail-once"
        once.write_text("")
        with patch.dict(os.environ, {"FAKE_HOST_FAIL_ONCE": str(once)}):
            self.share("sync", code=1)
        changed = [label for label in ("claude", "claude-lab") if self.servers(label)["notes"]["args"] == ["new"]]
        self.assertEqual(len(changed), 1)  # one write failed, and its server came back as it was
        for label in {"claude", "claude-lab"} - set(changed):
            self.assertEqual(self.servers(label)["notes"], NOTES)
        self.share("sync")  # the next run spreads the edit, it doesn't take it back
        for label in ("claude", "claude-lab", "codex"):
            self.assertEqual(self.servers(label)["notes"]["args"], ["new"], label)

    def test_a_write_that_reads_back_wrong_is_put_back(self):
        self.enable("--mcp", "on")
        kiro = self.user / ".kiro" / "settings" / "mcp.json"
        before = kiro.read_bytes()
        data = json.loads(self.claude_json.read_text())
        data["mcpServers"]["notes"]["args"] = ["new"]
        self.claude_json.write_text(json.dumps(data))
        real = mcp._to_json
        with patch.object(mcp, "_to_json", lambda server, cli: {**real(server, cli), "args": ["mangled"]}):
            out = self.share("sync", code=1)
        self.assertIn("read back differently", out)
        self.assertEqual(kiro.read_bytes(), before)

    def test_kinds_and_config_turn_it_on_and_off(self):
        self.enable()
        self.share("kinds", "add", "mcp")
        self.assertTrue(settings.load().mcp)
        self.assertEqual(self.servers("kiro")["notes"], NOTES)
        out = self.share("kinds", "remove", "mcp")
        self.assertIn("every CLI keeps the servers it has", out)
        self.assertFalse(settings.load().mcp)
        self.assertEqual(self.servers("kiro")["notes"], NOTES)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["config", "set", "share.mcp", "true"]), 0)
        self.assertTrue(settings.load().mcp)

    def test_the_watcher_counts_only_claudes_servers_in_its_state_file(self):
        self.enable("--mcp", "on")
        saved = settings.load()
        included = watch.included_folders(saved)
        extra = watch.watched_files(saved, included)
        self.assertIn(self.claude_json, extra)
        before = watch.signature(included, extra)
        data = json.loads(self.claude_json.read_text())
        data["numStartups"] = 4  # Claude rewrites the file all the time
        self.claude_json.write_text(json.dumps(data))
        self.assertEqual(watch.signature(included, extra), before)
        data["mcpServers"]["notes"]["args"] = ["changed"]
        self.claude_json.write_text(json.dumps(data))
        self.assertNotEqual(watch.signature(included, extra), before)

    def test_engine_decisions_hold_for_servers(self):
        self.enable("--mcp", "on")
        (self.user / ".kiro" / "settings" / "mcp.json").write_text(json.dumps({"mcpServers": {}}))
        out = self.share("sync")
        self.assertNotIn("notes", self.servers("kiro"))  # agt made that copy; deleting it stops it going there
        self.assertIn("kiro", self.record("mcp/notes")["stopped"])
        self.assertEqual(engine.STOP, "stop-sharing")
        self.assertNotIn("mcp/notes in kiro", out)


if __name__ == "__main__":
    unittest.main()
