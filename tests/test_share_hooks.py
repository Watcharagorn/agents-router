"""Sharing hooks: verdicts per CLI, approvals pinned to the hook, and copies that translate (`share hook-run`)."""

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from test_share_items import ItemBase

from agents_router.share import hooks, providers, settings, triggers, watch

GUARD = '''import json, sys
payload = json.load(sys.stdin)
command = (payload.get("tool_input") or {}).get("command", "")
if "rm -rf" in command:
    print("no rm -rf here", file=sys.stderr)
    sys.exit(2)
if command.startswith("ls"):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                             "updatedInput": {"command": "ls -la"}}}))
'''
NAME = "pretooluse-guard-shell"


def group(command, matcher="Bash", **extra):
    return {"matcher": matcher, "hooks": [{"type": "command", "command": command, **extra}]}


class VerdictTests(unittest.TestCase):
    def hook(self, event="PreToolUse", matcher="Bash", cli="claude", **handler):
        handlers = [{"type": "command", "command": "/usr/bin/true", **handler}]
        return hooks.Hook("h", event, matcher, handlers, cli, Path("/x"), "0" * 64, ["claude"],
                          secret=hooks.secret(handlers))

    def test_what_can_go_where(self):
        self.assertIsNone(hooks.verdict(self.hook(), "codex"))
        self.assertIsNone(hooks.verdict(self.hook(), "antigravity"))
        self.assertIsNone(hooks.verdict(self.hook(matcher="Edit"), "claude"))  # another Claude folder runs it as it is
        self.assertIn("Edit has no counterpart in codex", hooks.verdict(self.hook(matcher="Edit"), "codex"))
        self.assertIn("regular expression", hooks.verdict(self.hook(matcher="Bash.*"), "codex"))
        self.assertIn("every tool", hooks.verdict(self.hook(matcher="*"), "codex"))
        self.assertIn("no Stop event", hooks.verdict(self.hook(event="Stop", matcher=""), "antigravity"))
        self.assertIsNone(hooks.verdict(self.hook(event="Stop", matcher=""), "codex"))
        self.assertIn("`if` condition", hooks.verdict(self.hook(**{"if": "Bash(git *)"}), "codex"))
        self.assertIn("prompt handler", hooks.verdict(self.hook(type="prompt"), "codex"))
        self.assertIn("MCP tool", hooks.verdict(self.hook(matcher="mcp__notes__search"), "antigravity"))
        self.assertIsNone(hooks.verdict(self.hook(matcher="mcp__notes__search"), "codex"))
        self.assertIn("doesn't write kiro hooks", hooks.verdict(self.hook(), "kiro"))

    def test_secrets_in_commands(self):
        self.assertIsNotNone(hooks.secret([{"command": 'curl -H "Authorization: Bearer abc123" https://hooks.example.com'}]))
        self.assertIsNotNone(hooks.secret([{"command": "API_TOKEN=abc123 ./notify.sh"}]))
        self.assertIsNotNone(hooks.secret([{"command": "notify --token abc123"}]))
        self.assertIsNotNone(hooks.secret([{"command": "curl https://user:pw@hooks.example.com"}]))
        self.assertIsNone(hooks.secret([{"command": 'curl -H "Authorization: Bearer $HOOK_TOKEN" https://hooks.example.com'}]))
        self.assertIsNone(hooks.secret([{"command": "python3 ~/.claude/hooks/guard-shell.py"}]))


class HookTests(ItemBase):
    def setUp(self):
        super().setUp()
        script = self.claude / "hooks" / "guard-shell.py"
        script.parent.mkdir()
        script.write_text(GUARD, encoding="utf-8")
        self.script = script
        self.write_hooks(self.claude / "settings.json", {
            "PreToolUse": [group(f"{sys.executable} {script}")],
            "SessionStart": [group("echo remember-the-tabs", matcher="")],
        })

    def write_hooks(self, path, table, **extra):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**extra, "hooks": table}, indent=2), encoding="utf-8")

    def codex_hooks(self):
        path = self.user / ".codex" / "hooks.json"
        return json.loads(path.read_text()).get("hooks", {}) if path.exists() else {}

    def copy_command(self, table, event="PreToolUse"):
        commands = [handler["command"] for entry in table.get(event, []) for handler in entry["hooks"]
                    if "share hook-run" in handler["command"]]
        self.assertEqual(len(commands), 1, table)
        return commands[0]

    def run_entry(self, command, payload, env=None):
        result = subprocess.run(["/bin/sh", "-c", command], input=json.dumps(payload), capture_output=True, text=True,
                                env={**os.environ, **(env or {})}, timeout=30)
        return result.returncode, result.stdout.strip(), result.stderr.strip()

    def shell(self, command):
        return {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(self.root)}

    def test_off_by_default_then_listed_but_never_copied_unasked(self):
        self.enable()
        self.assertEqual(self.codex_hooks(), {})
        self.assertIsNone(json.loads(self.share("status", "--json"))["hooks"])
        out = self.share("kinds", "add", "hooks")
        self.assertIn("Waiting for your approval", out)
        self.assertIn(NAME, out)
        self.assertNotIn("share hook-run", json.dumps(self.codex_hooks()))

    def test_an_approved_hook_runs_in_codex_through_translation(self):
        self.enable("--hooks", "on")
        out = self.share("pick", f"hooks/{NAME}", "--in", "codex")
        self.assertIn("codex", out)
        self.assertEqual(settings.load().hook_approvals[NAME]["codex"][:6], self.hook().identity[:6])
        command = self.copy_command(self.codex_hooks())
        code, _, err = self.run_entry(command, self.shell("rm -rf build"))
        self.assertEqual((code, err), (2, "no rm -rf here"))
        code, out, _ = self.run_entry(command, self.shell("ls"))
        self.assertEqual(code, 0)
        answer = json.loads(out)["hookSpecificOutput"]
        self.assertEqual((answer["permissionDecision"], answer["updatedInput"]["command"]), ("allow", "ls -la"))
        self.assertEqual(self.run_entry(command, self.shell("echo hi")), (0, "", ""))
        self.assertEqual(self.run_entry(command, self.shell("rm -rf x"), {"AGT_DEPTH": "1"}), (0, "", ""))  # workers: neutral
        self.assertIn("nothing to write", self.share("sync"))

    def hook(self):
        return next(hook for hook in hooks.read(watch_included())[0] if hook.name == NAME)

    def test_a_changed_script_goes_quiet_until_approved_again(self):
        self.enable("--hooks", "on")
        self.share("pick", f"hooks/{NAME}", "--in", "codex")
        command = self.copy_command(self.codex_hooks())
        codex = (self.user / ".codex" / "hooks.json").read_bytes()
        self.script.write_text(GUARD.replace("no rm -rf here", "still no"), encoding="utf-8")
        self.assertEqual(self.run_entry(command, self.shell("rm -rf x")), (0, "", ""))  # it does nothing now
        # A sync (the watcher runs one within a minute) must not approve the change for you.
        self.share("sync")
        watch.background_sync("watcher", if_stale=False)
        self.assertEqual((self.user / ".codex" / "hooks.json").read_bytes(), codex)  # no new entry for Codex to trust
        self.assertEqual(self.run_entry(self.copy_command(self.codex_hooks()), self.shell("rm -rf x")), (0, "", ""))
        status = self.share("status", code=0)
        self.assertIn("changed since you approved it", status)
        self.share("pick", f"hooks/{NAME}", "--in", "codex")
        command = self.copy_command(self.codex_hooks())
        self.assertEqual(self.run_entry(command, self.shell("rm -rf x"))[0], 2)

    def test_a_hook_with_a_secret_cant_be_approved(self):
        self.write_hooks(self.claude / "settings.json", {
            "PostToolUse": [group('curl -s -H "Authorization: Bearer abc123" https://hooks.example.com/done')]})
        self.enable("--hooks", "on")
        listed = hooks.read(watch_included())[0]
        self.assertEqual(len(listed), 1)
        result, out = self.run_share("pick", f"hooks/{listed[0].name}", "--in", "codex")
        self.assertNotEqual(result, 0)
        self.assertNotIn("abc123", json.dumps(self.codex_hooks()))

    def test_entries_keep_their_place_between_agts_own_and_the_copies(self):
        self.enable("--hooks", "on")
        self.share("pick", f"hooks/{NAME}", "--in", "claude-lab")
        path = self.lab / "settings.json"
        first = path.read_bytes()
        self.assertIn(b"share session", first)
        self.assertIn(b"share hook-run", first)
        saved = settings.load()
        triggers.refresh(saved, watch_included())
        self.share("sync")
        triggers.refresh(saved, watch_included())
        self.assertEqual(path.read_bytes(), first)

    def test_claude_folders_with_the_same_hook_count_once(self):
        self.write_hooks(self.lab / "settings.json", {"PreToolUse": [group(f"{sys.executable} {self.script}")]})
        self.enable("--hooks", "on")
        listed = [hook for hook in hooks.read(watch_included())[0] if hook.event == "PreToolUse"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(sorted(listed[0].labels), ["claude", "claude-lab"])

    def test_session_start_context_reaches_codex(self):
        self.enable("--hooks", "on")
        self.share("pick", "hooks/sessionstart-echo", "--in", "codex")
        command = self.copy_command(self.codex_hooks(), "SessionStart")
        code, out, _ = self.run_entry(command, {"hook_event_name": "SessionStart", "source": "startup", "cwd": str(self.root)})
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["hookSpecificOutput"],
                         {"hookEventName": "SessionStart", "additionalContext": "remember-the-tabs"})

    def test_withdrawing_an_approval_or_removing_the_hook_takes_its_copy_out(self):
        self.enable("--hooks", "on")
        self.share("pick", f"hooks/{NAME}", "--in", "codex")
        self.share("pick", "hooks/sessionstart-echo", "--in", "codex")
        self.share("pick", f"hooks/{NAME}", "--not-in", "codex")
        self.assertNotIn(NAME, json.dumps(self.codex_hooks()))
        self.assertIn("sessionstart-echo", json.dumps(self.codex_hooks()))
        self.write_hooks(self.claude / "settings.json", {})  # gone from where it was written
        self.share("sync")
        self.assertNotIn("share hook-run", json.dumps(self.codex_hooks()))
        self.assertFalse(list((self.agt_home / "state" / "share" / "hooks").glob("*.json")))

    def test_turning_hooks_off_takes_the_copies_out(self):
        self.enable("--hooks", "on")
        self.share("pick", f"hooks/{NAME}", "--in", "codex")
        self.share("kinds", "remove", "hooks")
        self.assertNotIn("share hook-run", json.dumps(self.codex_hooks()))
        self.assertFalse(list((self.agt_home / "state" / "share" / "hooks").glob("*.json")))

    def test_antigravity_gets_its_own_set_and_answers(self):
        self.enable("--hooks", "on")  # writes the launcher
        gemini = self.user / ".gemini"
        (gemini / "config").mkdir(parents=True)
        (gemini / "config" / "hooks.json").write_text(json.dumps({"my-set": {"PreToolUse": []}}), encoding="utf-8")
        claude = providers.Provider("claude", self.claude, "claude", True)
        agy = providers.Provider("antigravity", gemini, "antigravity", True)
        plan = hooks.build([claude, agy], {NAME: {"antigravity": self.hook().short}})
        self.assertEqual([copy.label for copy in plan.copies], ["antigravity"])
        hooks.apply(plan)
        data = json.loads((gemini / "config" / "hooks.json").read_text())
        self.assertIn("my-set", data)
        entry = data[hooks.COPY_SET]["PreToolUse"][0]
        self.assertEqual(entry["matcher"], "run_command")
        command = entry["hooks"][0]["command"]
        call = lambda line: {"toolCall": {"name": "run_command", "args": {"CommandLine": line, "Cwd": str(self.root)}}}
        code, out, _ = self.run_entry(command, call("rm -rf build"))
        self.assertEqual((code, json.loads(out)["decision"]), (0, "deny"))
        self.assertEqual(json.loads(self.run_entry(command, call("ls"))[1]), {"decision": "allow"})  # never auto_approve
        self.assertEqual(json.loads(self.run_entry(command, call("echo"), {"AGT_JOB_ID": "j1"})[1]), {"decision": "allow"})
        with patch.object(triggers, "launcher_path", lambda: self.root / "gone" / "agt-run"):
            gone = hooks.command(plan.copies[0].hook, "antigravity", plan.copies[0].approved)
        self.assertEqual(json.loads(self.run_entry(gone, call("rm -rf x"))[1]), {"decision": "allow"})


def watch_included():
    from agents_router.share import watch

    return watch.included_folders(settings.load())


if __name__ == "__main__":
    unittest.main()
