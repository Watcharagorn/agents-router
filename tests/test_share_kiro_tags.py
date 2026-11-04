"""agt share and Kiro's universal tool tags: share-made copies that still name V2 tools move to tags once."""

import json
import os

from test_share_plan import DANA_TOOLS, MacCopy
from test_share_sync import YES, age

V2_TOOLS = ["execute_bash", "fs_read", "fs_write"]


class KiroTagMigrationTests(MacCopy):
    def enable(self):
        code, out = self.run_share(*YES)
        self.assertEqual(code, 0, out)
        return out

    def sync(self, *extra):
        code, out = self.run_share("sync", *extra)
        self.assertEqual(code, 0, out)
        return out

    def path(self, name):
        return self.user / ".kiro" / "agents" / f"{name}.json"

    def kiro(self, name):
        return json.loads(self.path(name).read_text(encoding="utf-8"))

    def rewrite(self, name, **fields):
        data = {**self.kiro(name), **fields}
        text = json.dumps(data, indent=2) + "\n"
        self.path(name).write_text(text, encoding="utf-8")
        age(self.path(name))
        return text

    def state(self):
        return json.loads((self.agt_home / "state" / "share" / "state.json").read_text())["items"]

    def test_an_old_copy_moves_to_tags_once_with_a_backup_outside_kiro(self):
        self.kiro_agent("kiro-own", tools=["fs_read", "execute_bash"])  # yours, not agt's: it stays as it is
        age(self.path("kiro-own"))
        self.enable()
        # Its execute_bash is Bash, so it may go to Codex (tool_widening = "allow-if-shell").
        self.assertTrue((self.user / ".codex" / "agents" / "kiro-own.toml").is_file())
        self.assertEqual(self.kiro("dana")["tools"], ["shell", "read", "write"])
        old = self.rewrite("dana", tools=V2_TOOLS, model="claude-x")  # a copy an older agt wrote, with a field of yours
        agents = sorted(os.listdir(self.path("dana").parent))
        record = self.state()["agents/dana"]

        plan = json.loads(self.sync("--dry-run", "--json"))
        changes = [change for change in plan["changes"] if change["name"] == "dana"]
        self.assertEqual([(change["action"], change["labels"]) for change in changes], [("migrate", ["kiro"])])
        self.assertEqual([item["name"] for item in plan["items"] if item["decision"] in ("conflict", "wait")], [])
        dana = next(item for item in plan["items"] if item["name"] == "dana")
        self.assertEqual(dana["decision"], "nothing")
        self.assertEqual(dana["cells"]["kiro"]["state"], "has")
        self.assertIn("dana in kiro: its tools moved from Kiro's old tool names to its universal tags, with a backup",
                      dana["notes"])

        out = self.sync()
        self.assertIn("wrote 1 change in 1 folder", out)
        self.assertRegex(out, r"~ agents/dana +kiro \(its tools moved from Kiro's old tool names to its universal tags\)")
        self.assertNotIn("conflict", out.lower())
        data = self.kiro("dana")
        self.assertEqual(data["tools"], ["shell", "read", "write"])
        self.assertEqual(data["model"], "claude-x")
        self.assertEqual(self.path("dana").read_text(encoding="utf-8"),
                         old.replace(json.dumps(V2_TOOLS, indent=2).replace("\n", "\n  "),
                                     json.dumps(["shell", "read", "write"], indent=2).replace("\n", "\n  ")))
        # Kiro loads every .json in its agents folder: nothing new appears there, the backup is in agt's state.
        self.assertEqual(sorted(os.listdir(self.path("dana").parent)), agents)
        backups = list((self.agt_home / "state" / "share" / "backups").glob("*/kiro/dana.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text(encoding="utf-8"))["tools"], V2_TOOLS)
        self.assertEqual(self.state()["agents/dana"], record)  # tools aren't part of a version: the record stays
        self.assertEqual(self.kiro("kiro-own")["tools"], ["fs_read", "execute_bash"])

        before = self.snapshot()
        self.assertIn("✓ everything is in sync, nothing to write", self.sync())
        self.assertEqual(self.snapshot(), before)

    def test_undo_puts_the_old_names_back_until_the_next_sync(self):
        self.enable()
        self.rewrite("dana", tools=V2_TOOLS)
        self.sync()
        out = self.sync()  # nothing left to migrate
        self.assertIn("✓ everything is in sync, nothing to write", out)
        code, out = self.run_share("undo", "dana")
        self.assertEqual(code, 0, out)
        self.assertIn("~ agents/dana   kiro (put back)", out)
        self.assertNotIn("failed", out)
        self.assertEqual(self.kiro("dana")["tools"], V2_TOOLS)
        age(self.path("dana"))
        self.sync()
        self.assertEqual(self.kiro("dana")["tools"], ["shell", "read", "write"])

    def test_a_copy_kiro_upgraded_keeps_its_tools_and_still_gets_edits(self):
        self.enable()
        upgraded = {"tools": ["fs_read", "shell"], "allowedTools": [],
                    "permissions": {"fs_read": {"allow": ["**"]}, "shell": {"deny": ["rm -rf *"]}},
                    "kiroUpgrade": {"from": "v2", "version": "3.0.0", "backup": "dana.json.bak"}}
        old = self.rewrite("dana", **upgraded)
        before = self.snapshot()
        self.assertIn("✓ everything is in sync, nothing to write", self.sync())
        self.assertEqual(self.snapshot(), before)

        self.agent(self.claude, "dana", description="Does more things.", tools=DANA_TOOLS, body="Dana's long instructions.")
        age(self.claude / "agents" / "dana.md")
        self.sync()
        data = self.kiro("dana")
        self.assertEqual(data["description"], "Does more things.")
        # Only the description changed: tools, permissions and kiroUpgrade are byte for byte what Kiro wrote.
        self.assertEqual(self.path("dana").read_text(encoding="utf-8"),
                         old.replace('"description": "Does things."', '"description": "Does more things."'))

    def test_a_copy_without_tools_never_gains_them(self):
        path = self.agent(self.claude, "plain", body="Any tool.")
        age(path)
        self.enable()
        self.assertNotIn("tools", self.kiro("plain"))
        self.assertIn("✓ everything is in sync, nothing to write", self.sync())
        self.assertNotIn("tools", self.kiro("plain"))
