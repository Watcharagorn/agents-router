"""agt share's background side: the launcher, the LaunchAgent, the hook entries, and the runs they start."""

import contextlib
import io
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from test_share_items import ItemBase
from test_share_sync import age

from agents_router import cli, synclog, tools
from agents_router.share import apply, settings, triggers, watch


def lines(path):
    path = Path(path)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


class WatchBase(ItemBase):
    def break_launcher(self):
        launcher = self.agt_home / "bin" / "agt-run"
        launcher.write_text(re.sub(r"^PY=.*$", "PY='/gone/python'", launcher.read_text(), flags=re.M))

    def launchctl(self):
        return lines(self.root / "launchctl.jsonl")

    def spawned(self):
        return lines(self.root / "spawned.jsonl")

    def plist(self):
        with (self.user / "Library" / "LaunchAgents" / f"{triggers.LABEL}.plist").open("rb") as handle:
            return plistlib.load(handle)

    def hooks(self, folder, name="settings.json"):
        return json.loads((folder / name).read_text(encoding="utf-8")).get("hooks", {})

    def commands(self, folder, name="settings.json"):
        return {event: [handler["command"] for group in groups for handler in group["hooks"]]
                for event, groups in self.hooks(folder, name).items()}

    def watch(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["share", "watch", "--once", "--quiet"]), 0)
        return json.loads((self.agt_home / "state" / "share" / "heartbeat.json").read_text())["result"]

    def hook(self, sub, payload=None, *extra):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch("sys.stdin", io.StringIO(json.dumps(payload or {}))):
            self.assertEqual(cli.main(["share", sub, *extra]), 0)
        return out.getvalue()

    def config(self, *args):
        cwd = os.getcwd()
        os.chdir(self.user)
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                code = cli.main(["config", *args])
        finally:
            os.chdir(cwd)
        self.assertEqual(code, 0, out.getvalue())
        return out.getvalue()


class SetUpTests(WatchBase):
    def test_enable_starts_the_watcher_and_adds_the_claude_entries(self):
        out = self.enable()
        self.assertIn("The background watcher runs every minute", out)
        self.assertIn("claude: agt catches up at each session start and saves in Claude Code", out)
        data = self.plist()
        launcher = self.agt_home / "bin" / "agt-run"
        self.assertEqual(data["ProgramArguments"], ["/bin/sh", str(launcher), "share", "watch", "--once", "--quiet"])
        self.assertEqual((data["StartInterval"], data["RunAtLoad"], data["ProcessType"]), (60, True, "Background"))
        self.assertEqual(data["EnvironmentVariables"]["AGT_HOME"], str(self.agt_home))
        self.assertEqual(data["EnvironmentVariables"]["AGT_CONFIG_DIR"], str(self.user / ".agt"))
        self.assertIn(str(self.claude / "agents"), data["WatchPaths"])
        self.assertIn(str(self.user / ".kiro" / "agents"), data["WatchPaths"])
        self.assertTrue(os.access(launcher, os.X_OK))
        with patch.dict(os.environ):  # with AGT_HOME alone, agt would look for settings in the agt home
            os.environ.pop("AGT_CONFIG_DIR")
            self.assertEqual(triggers.install_record()["config_dir"], str(self.agt_home))
        self.assertIn(f"PY={sys.executable}", launcher.read_text().replace("'", ""))
        self.assertEqual([call[0] for call in self.launchctl()], ["print", "bootstrap"])
        for folder, label in ((self.claude, "claude"), (self.lab, "claude-lab")):
            commands = self.commands(folder)
            self.assertEqual(commands["SessionStart"], [triggers.hook_command("session", label)])
            self.assertEqual(commands["PostToolUse"], [triggers.hook_command("saved")])
            self.assertEqual(self.hooks(folder)["PostToolUse"][0]["matcher"], "Write|Edit|MultiEdit")
        self.assertFalse((self.user / ".codex" / "hooks.json").exists())  # Codex's session line is off by default
        # Again: nothing changes, and launchd isn't asked to load it twice.
        before = (self.claude / "settings.json").read_bytes()
        self.enable()
        self.assertEqual([call[0] for call in self.launchctl()], ["print", "bootstrap", "print"])
        self.assertEqual((self.claude / "settings.json").read_bytes(), before)

    def test_your_entries_stay_and_disable_removes_only_agts(self):
        mine = {"theme": "dark", "env": {"KEEP": "1"}, "hooks": {
            "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "guard.sh"}]}],
            "SessionStart": [{"matcher": "startup", "hooks": [{"type": "command", "command": "hello.sh"}]}]}}
        (self.claude / "settings.json").write_text(json.dumps(mine, indent=4) + "\n", encoding="utf-8")
        self.enable()
        data = json.loads((self.claude / "settings.json").read_text())
        self.assertEqual((data["theme"], data["env"]), ("dark", {"KEEP": "1"}))
        self.assertEqual(self.commands(self.claude)["SessionStart"], ["hello.sh", triggers.hook_command("session", "claude")])
        self.assertEqual(self.commands(self.claude)["PreToolUse"], ["guard.sh"])
        self.assertTrue((self.claude / "settings.json").read_text().startswith('{\n    "theme"'))  # your indent
        backups = list((self.agt_home / "state" / "share" / "backups" / "hooks").rglob("claude-settings.json"))
        self.assertEqual([json.loads(path.read_text()) for path in backups], [mine])
        out = self.share("disable")
        self.assertIn("Removed the background watcher.", out)
        self.assertEqual(json.loads((self.claude / "settings.json").read_text()), mine)
        self.assertFalse((self.lab / "settings.json").exists())  # agt made it, and it held only agt's entries
        self.assertFalse((self.user / "Library" / "LaunchAgents" / f"{triggers.LABEL}.plist").exists())
        self.assertEqual(self.launchctl()[-1][0], "bootout")

    def test_switches_change_the_entries_at_once(self):
        self.enable()
        self.config("set", "share.push_on_save", "false")
        self.assertNotIn("PostToolUse", self.commands(self.claude))
        self.assertIn("SessionStart", self.commands(self.claude))
        self.config("set", "share.codex_session", "true")
        self.assertEqual(self.commands(self.user / ".codex", "hooks.json")["SessionStart"],
                         [triggers.hook_command("session", "codex")])
        self.config("set", "share.watcher", "false")
        self.assertFalse((self.user / "Library" / "LaunchAgents" / f"{triggers.LABEL}.plist").exists())
        self.config("unset", "share.codex_session")
        self.assertFalse((self.user / ".codex" / "hooks.json").exists())  # agt made it for its entry alone

    def test_enable_again_keeps_the_switches_you_set(self):
        self.enable()
        self.config("set", "share.push_on_save", "false")
        self.config("set", "share.notify", "false")
        self.enable()
        saved = settings.load()
        self.assertEqual((saved.push_on_save, saved.notify), (False, False))
        self.assertNotIn("PostToolUse", self.commands(self.claude))

    def test_removing_a_claude_folder_takes_its_entries_out(self):
        self.enable()
        self.share("cli", "remove", "claude-lab", "--keep-copies")
        self.assertFalse((self.lab / "settings.json").exists())  # it held only agt's entries
        self.assertIn("SessionStart", self.commands(self.claude))
        self.assertNotIn(str(self.lab / "agents"), self.plist()["WatchPaths"])

    def test_a_hooks_file_agt_cant_read_is_left_alone(self):
        (self.lab / "settings.json").write_text("{ not json", encoding="utf-8")
        out = self.enable()
        self.assertIn("isn't valid JSON", out)
        self.assertEqual((self.lab / "settings.json").read_text(), "{ not json")
        self.assertIn("SessionStart", self.commands(self.claude))

    def test_a_temp_home_never_reaches_the_real_launchd(self):
        with patch.dict(os.environ):
            os.environ.pop("AGT_LAUNCHCTL")
            running, line = triggers.install_watcher([])
            self.assertFalse(running)
            self.assertIn("temp folder" if sys.platform == "darwin" else "needs macOS", line)
            self.assertIsNone(triggers._launchctl("print", "gui/0/x"))


class RunTests(WatchBase):
    def test_the_watcher_skips_when_nothing_moved_and_spreads_an_edit(self):
        self.enable()
        self.assertEqual(self.watch(), "nothing to write")  # the first run plans, then saves the signature
        runs = len(list(synclog.entries()))
        self.assertEqual(self.watch(), "nothing moved")
        self.assertEqual(len(list(synclog.entries())), runs)  # a run that changed nothing isn't a line
        self.agent(self.claude, "dana", body="Dana, edited.")
        age(self.claude / "agents" / "dana.md")
        self.assertEqual(self.watch(), "wrote 3")  # in claude-lab, codex and kiro
        self.assertEqual(self.kiro("dana")["prompt"], "Dana, edited.")
        entry = next(synclog.entries())
        self.assertEqual(entry["trigger"], "watcher")
        self.assertIn("(the background watcher)", synclog.line(entry))
        self.assertEqual(self.watch(), "nothing to write")  # agt's own writes moved the signature once
        self.assertEqual(self.watch(), "nothing moved")

    def test_a_skill_edit_through_a_link_is_seen(self):
        self.enable()
        self.watch()
        self.watch()
        (self.claude / "skills" / "ask-simply" / "notes.md").write_text("more", encoding="utf-8")
        self.assertNotEqual(self.watch(), "nothing moved")

    def test_claudes_synced_skills_never_wake_a_sync(self):
        synced = self.claude / "skills" / "synced" / "from-claude-ai"
        synced.mkdir()
        (synced / "manifest.json").write_text('{"lastUpdated": 1}', encoding="utf-8")
        self.enable()
        self.watch()
        self.assertEqual(self.watch(), "nothing moved")
        (synced / "manifest.json").write_text('{"lastUpdated": 2}', encoding="utf-8")  # Claude Code rewrites it often
        self.skill(self.lab / "skills" / "synced" / "from-claude-ai", "notes")
        self.assertEqual(self.watch(), "nothing moved")
        self.skill(self.lab, "plan-trip")  # a real skill still wakes one
        self.assertNotEqual(self.watch(), "nothing moved")

    def test_a_busy_run_leaves_the_work_to_the_next(self):
        self.enable()
        folders = [self.claude, self.lab, self.user / ".codex", self.user / ".kiro"]
        with apply.lock(folders):
            self.assertEqual(watch.background_sync("watcher")[0], "another run had the folders")
        self.assertEqual(self.watch(), "nothing to write")

    def test_nothing_runs_when_sharing_is_off(self):
        self.assertEqual(self.watch(), "sharing is off")
        self.assertEqual(self.hook("session", {}, "--label", "claude"), "")
        self.assertEqual(self.hook("saved", {"tool_input": {"file_path": str(self.claude / "agents" / "dana.md")}}), "")
        self.assertEqual(self.spawned(), [])

    def test_a_saved_folder_keeps_sharing_without_its_binary(self):
        self.enable()
        with patch.dict(os.environ):
            os.environ.pop("AGT_KIRO_COMMAND")
            out = self.share("status", "--json", code=0)
        self.assertIn("kiro", [folder["label"] for folder in json.loads(out)["folders"]])

    def test_notifications_for_new_urgent_items_at_most_hourly(self):
        self.enable()
        self.agent(self.claude, "dana", body="Edited here.")
        self.write_kiro("dana", prompt="And edited there.")
        age(self.claude / "agents" / "dana.md")
        self.watch()
        told = lines(self.root / "osascript.jsonl")
        self.assertEqual(len(told), 1)
        self.assertIn("1 conflict", told[0][1])
        self.watch()
        self.assertEqual(len(lines(self.root / "osascript.jsonl")), 1)
        self.config("set", "share.notify", "false")
        self.assertFalse(watch.notify(settings.load()))


class HookTests(WatchBase):
    def test_a_session_says_what_arrived_and_what_waits(self):
        self.enable()
        self.watch()
        self.assertEqual(self.hook("session", {"source": "startup"}, "--label", "claude"), "")  # the first one marks the time
        self.agent(self.lab, "rover", body="Roams.")
        age(self.lab / "agents" / "rover.md")
        self.watch()
        out = json.loads(self.hook("session", {"source": "startup"}, "--label", "claude"))
        self.assertIn("brought 1 item from your other CLIs since your last session here: rover", out["systemMessage"])
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertEqual(self.hook("session", {}, "--label", "claude"), "")
        self.agent(self.claude, "dana", body="Edited here.")
        self.write_kiro("dana", prompt="And edited there.")
        age(self.claude / "agents" / "dana.md")
        self.watch()
        out = json.loads(self.hook("session", {}, "--label", "claude"))
        self.assertIn("dana differs between", out["hookSpecificOutput"]["additionalContext"])
        self.assertIn("`agt share resolve dana`", out["systemMessage"])
        self.assertEqual(self.hook("session", {}, "--label", "claude-lab"), "")  # shown once, then once a day
        from agents_router import mcp_server

        said = mcp_server.Server(lambda message: None)._dispatch("initialize", {})["instructions"]
        self.assertIn("dana differs between", said)  # Codex hears it through agt's MCP server

    def test_a_session_catches_up_only_when_something_moved(self):
        self.enable()
        self.watch()
        self.watch()
        self.hook("session", {}, "--label", "claude")
        self.assertEqual(self.spawned(), [])
        self.agent(self.claude, "dana", body="Edited.")
        self.hook("session", {}, "--label", "claude")
        self.assertEqual(self.spawned(), [["share", "sync", "--if-stale", "--quiet", "--trigger", "session"]])

    def test_a_session_restarts_a_stopped_watcher(self):
        self.enable()
        heartbeat = self.agt_home / "state" / "share" / "heartbeat.json"
        heartbeat.write_text(json.dumps({"time": time.time() - 3600, "result": "nothing moved"}))
        self.hook("session", {}, "--label", "claude")
        self.assertEqual([call[0] for call in self.launchctl()[-2:]], ["bootout", "bootstrap"])
        with patch.dict(os.environ, {"FAKE_LAUNCHCTL_FAIL": "bootstrap"}):
            out = json.loads(self.hook("session", {}, "--label", "claude"))
        self.assertIn("watcher stopped and couldn't restart", out["systemMessage"])

    def test_a_save_in_a_shared_folder_starts_a_sync(self):
        self.enable()
        self.hook("saved", {"tool_name": "Edit", "tool_input": {"file_path": str(self.claude / "agents" / "dana.md")}})
        self.hook("saved", {"tool_input": {"file_path": str(self.lab / "skills" / "ask-simply" / "SKILL.md")}})  # through a link
        self.hook("saved", {"tool_input": {"file_path": str(self.root / "elsewhere.txt")}})
        self.hook("saved", {"tool_input": {}})
        self.hook("saved", None)
        self.assertEqual(self.spawned(), [["share", "sync", "--if-stale", "--quiet", "--trigger", "save"]] * 2)
        self.config("set", "share.push_on_save", "false")
        self.hook("saved", {"tool_input": {"file_path": str(self.claude / "agents" / "dana.md")}})
        self.assertEqual(len(self.spawned()), 2)

    def test_hooks_never_run_inside_workers(self):
        self.enable()
        self.agent(self.claude, "dana", body="Edited.")
        with patch.dict(os.environ, {"AGT_DEPTH": "1"}):
            self.assertEqual(self.hook("session", {}, "--label", "claude"), "")
            self.hook("saved", {"tool_input": {"file_path": str(self.claude / "agents" / "dana.md")}})
            self.assertEqual(self.watch(), "inside an agt worker")
        self.assertEqual(self.spawned(), [])

    def test_an_mcp_server_start_catches_up_and_moves_the_launcher(self):
        self.enable()
        self.watch()
        self.watch()
        tools.startup()
        self.assertEqual(self.spawned(), [])
        self.break_launcher()
        self.assertIn("is gone", triggers.launcher_problem())
        self.agent(self.claude, "dana", body="Edited.")
        tools.startup()
        self.assertEqual(self.spawned(), [["share", "sync", "--if-stale", "--quiet", "--trigger", "mcp-start"]])
        self.assertIsNone(triggers.launcher_problem())

    def test_the_badge_and_status_name_a_stopped_watcher(self):
        self.enable()
        self.watch()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["share", "badge"])
        self.assertEqual(out.getvalue(), "")
        heartbeat = self.agt_home / "state" / "share" / "heartbeat.json"
        heartbeat.write_text(json.dumps({"time": time.time() - 3600, "result": "nothing moved"}))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cli.main(["share", "badge"])
        self.assertEqual(out.getvalue(), "share:1!\n")
        status = self.share("status")
        self.assertIn("Watcher stopped: the background watcher stopped. An agent can do it: `agt doctor --fix`.", status)
        lines_out = cli._share_doctor(fix=True)
        self.assertTrue(any(line.startswith("  Watcher") for line in lines_out), lines_out)


@unittest.skipIf(os.name == "nt", "the launcher is a /bin/sh script")
class LauncherTests(WatchBase):
    def run_launcher(self, *args, stdin=""):
        return subprocess.run(["/bin/sh", str(self.agt_home / "bin" / "agt-run"), *args], input=stdin, capture_output=True,
                              text=True, timeout=60, env={**os.environ, "PATH": "/usr/bin:/bin"})

    def test_it_runs_agt_from_the_recorded_install_with_a_bare_path(self):
        self.enable()
        result = self.run_launcher("share", "badge")
        self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)
        result = self.run_launcher("share", "session", "--label", "claude", stdin="{}")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_watchers_bare_environment_sees_the_same_folders(self):
        with patch.dict(os.environ):  # like a Mac with neither set: settings in ~/.agt, agt's home in ~/.agents-router
            os.environ.pop("AGT_HOME")
            os.environ.pop("AGT_CONFIG_DIR")
            self.enable()
            mine = json.loads(self.share("status", "--json"))["folders"]
        bare = {"HOME": str(self.user), "PATH": "/usr/bin:/bin", "TMPDIR": str(self.root / "tmp")}  # what launchd gives
        result = subprocess.run(["/bin/sh", str(self.user / ".agents-router" / "bin" / "agt-run"), "share", "status", "--json"],
                                capture_output=True, text=True, timeout=60, env=bare)
        self.assertEqual(json.loads(result.stdout).get("folders"), mine, result.stdout + result.stderr)

    def test_a_missing_python_says_how_to_reinstall(self):
        self.enable()
        self.break_launcher()
        result = self.run_launcher("share", "session", "--label", "claude", stdin="{}")
        self.assertEqual(result.returncode, 0)
        said = json.loads(result.stdout)
        self.assertIn(f"agt share is stopped: its Python at {sys.executable} is gone", said["systemMessage"])
        self.assertIn("uv tool install --reinstall --managed-python", said["systemMessage"])
        for _ in range(2):
            self.assertEqual(self.run_launcher("share", "watch", "--once").returncode, 0)
        self.assertEqual(len(lines(self.root / "osascript.jsonl")), 1)  # at most once a day
        result = self.run_launcher("share", "saved", stdin="{}")
        self.assertEqual((result.returncode, result.stdout), (0, ""))
        self.assertIn("its Python (/gone/python) is gone", triggers.launcher_problem())

    def test_a_hook_runs_before_agt_made_its_state_folder(self):
        self.enable()
        import shutil

        shutil.rmtree(self.agt_home / "state")
        result = self.run_launcher("share", "session", "--label", "claude", stdin="{}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.agt_home / "state" / "share" / "sessions.json").exists())  # agt ran

    def test_a_hook_entry_does_nothing_once_the_launcher_is_gone(self):
        self.enable()
        (self.agt_home / "bin" / "agt-run").unlink()
        result = subprocess.run(["/bin/sh", "-c", triggers.hook_command("session", "claude")], capture_output=True, text=True)
        self.assertEqual((result.returncode, result.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()
