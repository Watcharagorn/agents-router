import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import helpers  # noqa: F401  (isolates the test process from your real folders)

from agents_router import cli, setup
from agents_router.config import default_primary, load_config
from agents_router.runtimes.claude import ClaudeRuntime

FAKE_HOST = Path(__file__).with_name("fake_host.py")


class SetupBase(unittest.TestCase):
    """Throwaway Agents Router home and host config folders, with fake claude, codex and agy."""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name).resolve()
        self.claude_dir = self.root / "claude-main"
        self.codex_dir = self.root / "codex-main"
        self.agy_dir = self.root / "agy-main"
        for folder in (self.claude_dir, self.codex_dir, self.agy_dir):
            folder.mkdir()
        self.log = self.root / "host-log.jsonl"
        fake = lambda kind: json.dumps([sys.executable, str(FAKE_HOST), kind])
        self._env = patch.dict(os.environ, {
            "AGT_HOME": str(self.root / "agt"),
            "CLAUDE_CONFIG_DIR": str(self.claude_dir),
            "CODEX_HOME": str(self.codex_dir),
            "FAKE_AGY_HOME": str(self.agy_dir),
            "FAKE_HOST_LOG": str(self.log),
            "AGT_CLAUDE_COMMAND": fake("claude"),
            "AGT_CODEX_COMMAND": fake("codex"),
            "AGT_AGY_COMMAND": fake("agy"),
        })
        self._env.start()
        # Antigravity has no CODEX_HOME-style override; keep setup's file writes temporary too.
        self._primary = patch("agents_router.config.default_primary", side_effect=lambda runtime:
                              self.agy_dir if runtime == "antigravity" else default_primary(runtime))
        self._primary.start()
        os.environ.pop("FAKE_HOST_FAIL", None)

    def tearDown(self):
        self._primary.stop()
        self._env.stop()
        self._temp.cleanup()

    def run_cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["setup", *args])
        return code, out.getvalue()

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def plugins(self, folder):
        path = folder / "fake_plugins.json"
        return json.loads(path.read_text(encoding="utf-8"))["plugins"] if path.exists() else {}


class PluginInstallTests(SetupBase):
    def test_claude_installs_through_its_terminal_commands_into_the_config_dir(self):
        code, out = self.run_cli("--host", "claude")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.plugins(self.claude_dir), {"agents-router@agents-router": True})
        changing = [c["argv"] for c in self.calls() if c["kind"] == "claude" and "list" not in c["argv"] and "auth" not in c["argv"]]
        self.assertEqual(changing, [
            ["plugin", "marketplace", "add", "Watcharagorn/agents-router"],
            ["plugin", "install", "agents-router@agents-router"],
        ])
        self.assertTrue(all(c["folder"] == str(self.claude_dir) for c in self.calls()))
        self.assertIn("Restart Claude Code", out)
        self.assertIn("Claude Code reports no login", out)
        self.assertFalse((self.claude_dir / "settings.json").exists())

    def test_second_run_is_a_no_op(self):
        self.run_cli("--host", "claude", "--host", "codex")
        self.log.unlink()
        (self.claude_dir / "logged_in").touch()
        (self.codex_dir / "logged_in").touch()
        code, out = self.run_cli("--host", "claude", "--host", "codex")
        self.assertEqual(code, 0, out)
        self.assertIn("already installed and enabled", out)
        self.assertNotIn("Restart", out)
        self.assertTrue(all("list" in c["argv"] or "status" in c["argv"] for c in self.calls()))

    def test_disabled_claude_plugin_is_enabled(self):
        state = {"marketplaces": ["agents-router"], "plugins": {"agents-router@agents-router": False}}
        (self.claude_dir / "fake_plugins.json").write_text(json.dumps(state), encoding="utf-8")
        code, out = self.run_cli("--host", "claude")
        self.assertEqual(code, 0, out)
        self.assertIn("claude plugin enable agents-router@agents-router", out)
        self.assertEqual(self.plugins(self.claude_dir), {"agents-router@agents-router": True})

    def test_codex_skips_marketplace_add_when_it_exists(self):
        state = {"marketplaces": ["agents-router"], "plugins": {}}
        (self.codex_dir / "fake_plugins.json").write_text(json.dumps(state), encoding="utf-8")
        code, out = self.run_cli("--host", "codex")
        self.assertEqual(code, 0, out)
        changing = [c["argv"] for c in self.calls() if c["kind"] == "codex" and "list" not in c["argv"] and "status" not in c["argv"]]
        self.assertEqual(changing, [["plugin", "add", "agents-router@agents-router"]])
        self.assertIn("Start a new Codex session", out)

    def test_antigravity_installs_from_the_repository_url(self):
        code, out = self.run_cli("--host", "antigravity")
        self.assertEqual(code, 0, out)
        self.assertIn("agy plugin install https://github.com/Watcharagorn/agents-router", out)
        self.assertEqual(self.plugins(self.agy_dir), {"agents-router": True})
        code, out = self.run_cli("--host", "antigravity")
        self.assertIn("already installed", out)

    def test_dry_run_changes_nothing(self):
        (self.root / "acct").mkdir()
        code, out = self.run_cli("--dry-run", "--account", f"max-2={self.root / 'acct'}")
        self.assertEqual(code, 0, out)
        self.assertIn("dry run", out)
        for text in ("claude plugin marketplace add Watcharagorn/agents-router",
                     "codex plugin add agents-router@agents-router",
                     "agy plugin install https://github.com/Watcharagorn/agents-router",
                     "would register and sync"):
            self.assertIn(text, out)
        for folder in (self.claude_dir, self.codex_dir, self.agy_dir):
            self.assertFalse((folder / "fake_plugins.json").exists())
        self.assertFalse((self.root / "agt" / "settings.toml").exists())
        self.assertEqual(list((self.root / "acct").iterdir()), [])

    def test_failed_codex_install_is_reported_with_the_mcp_fallback(self):
        os.environ["FAKE_HOST_FAIL"] = "add"
        code, out = self.run_cli("--host", "codex")
        self.assertEqual(code, 1)
        self.assertIn("failed on purpose", out)
        self.assertIn("codex mcp add agents-router -- agt mcp", out)

    def test_auto_uses_only_installed_hosts(self):
        with patch("agents_router.runtimes.antigravity.AntigravityRuntime.find_binary", return_value=None), \
             patch("agents_router.runtimes.codex.CodexRuntime.find_binary", return_value=None):
            code, out = self.run_cli()
        self.assertEqual(code, 0, out)
        self.assertEqual({c["kind"] for c in self.calls()}, {"claude"})


class SettingsRouteTests(SetupBase):
    def settings(self):
        return json.loads((self.claude_dir / "settings.json").read_text(encoding="utf-8"))

    def test_missing_claude_binary_declares_the_plugin_in_settings(self):
        original = {"model": "opus", "enabledPlugins": {"other@market": True},
                    "extraKnownMarketplaces": {"market": {"source": {"source": "github", "repo": "o/r"}}}, "hooks": {"x": []}}
        (self.claude_dir / "settings.json").write_text(json.dumps(original), encoding="utf-8")
        with patch.object(ClaudeRuntime, "find_binary", return_value=None):
            code, out = self.run_cli("--host", "claude")
        self.assertEqual(code, 0, out)
        data = self.settings()
        self.assertEqual(data["model"], "opus")
        self.assertEqual(data["hooks"], {"x": []})
        self.assertEqual(data["enabledPlugins"], {"other@market": True, "agents-router@agents-router": True})
        self.assertEqual(data["extraKnownMarketplaces"]["agents-router"],
                         {"source": {"source": "github", "repo": "Watcharagorn/agents-router"}})
        self.assertIn("market", data["extraKnownMarketplaces"])
        backups = list(self.claude_dir.glob("settings.json.agents-router-backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text(encoding="utf-8")), original)
        self.assertIn("Restart Claude Code so it installs", out)
        self.assertEqual(self.calls(), [])
        with patch.object(ClaudeRuntime, "find_binary", return_value=None):
            code, out = self.run_cli("--host", "claude")
        self.assertIn("already declares", out)
        self.assertEqual(len(list(self.claude_dir.glob("settings.json.agents-router-backup-*"))), 1)

    def test_settings_route_creates_the_file_when_missing(self):
        code, out = self.run_cli("--host", "claude", "--claude-route", "settings")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.settings()["enabledPlugins"], {"agents-router@agents-router": True})

    def test_failed_cli_install_falls_back_to_settings(self):
        os.environ["FAKE_HOST_FAIL"] = "install"
        code, out = self.run_cli("--host", "claude")
        self.assertEqual(code, 0, out)
        self.assertIn("failed on purpose", out)
        self.assertTrue(self.settings()["enabledPlugins"]["agents-router@agents-router"])

    def test_cli_route_does_not_fall_back(self):
        os.environ["FAKE_HOST_FAIL"] = "install"
        code, out = self.run_cli("--host", "claude", "--claude-route", "cli")
        self.assertEqual(code, 1)
        self.assertFalse((self.claude_dir / "settings.json").exists())

    def test_invalid_settings_are_left_alone(self):
        (self.claude_dir / "settings.json").write_text("{ not json", encoding="utf-8")
        code, out = self.run_cli("--host", "claude", "--claude-route", "settings")
        self.assertEqual(code, 1)
        self.assertEqual((self.claude_dir / "settings.json").read_text(encoding="utf-8"), "{ not json")
        self.assertEqual(list(self.claude_dir.glob("settings.json.agents-router-backup-*")), [])

    def test_dry_run_leaves_settings_alone(self):
        code, out = self.run_cli("--host", "claude", "--claude-route", "settings", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("would add the agents-router marketplace", out)
        self.assertFalse((self.claude_dir / "settings.json").exists())


class AccountTests(SetupBase):
    def test_existing_folders_are_registered_synced_and_checked(self):
        claude_acct = self.root / ".claude-2"
        codex_acct = self.root / "work"
        claude_acct.mkdir()
        codex_acct.mkdir()
        (claude_acct / "logged_in").touch()
        (codex_acct / "config.toml").write_text("", encoding="utf-8")
        code, out = self.run_cli("--host", "claude", "--account", f"max-2={claude_acct}", "--account", f"plus-2={codex_acct}")
        self.assertEqual(code, 0, out)
        config = load_config()
        self.assertEqual([(a.name, a.home) for a in config.runtime("claude").accounts], [("max-2", claude_acct)])
        self.assertEqual([(a.name, a.home) for a in config.runtime("codex").accounts], [("plus-2", codex_acct)])
        self.assertIn("agt accounts login plus-2 --runtime codex", out)
        self.assertNotIn("agt accounts login max-2", out)
        self.assertIn("shared setup synced", out)
        # Running again keeps one entry per folder.
        code, out = self.run_cli("--host", "claude", "--account", f"max-2={claude_acct}", "--account", f"codex:other={codex_acct}")
        self.assertEqual(code, 0, out)
        self.assertIn("already registered as 'plus-2'", out)
        self.assertEqual(len(load_config().runtime("codex").accounts), 1)

    def test_bad_accounts_are_reported_not_created(self):
        code, out = self.run_cli("--host", "claude", "--account", f"x={self.root / 'missing'}",
                                 "--account", f"main={self.claude_dir}", "--account", "nonsense")
        self.assertEqual(code, 1)
        self.assertIn("doesn't exist", out)
        self.assertIn("primary claude setup", out)
        self.assertIn("NAME=FOLDER", out)
        self.assertFalse((self.root / "missing").exists())
        self.assertEqual(load_config().runtime("claude").accounts, [])

    def test_parse_account_keeps_windows_drive_paths(self):
        runtime, name, folder = setup.parse_account(r"codex:plus-2=C:\Users\me\.codex-2")
        self.assertEqual((runtime, name), ("codex", "plus-2"))
        self.assertTrue(str(folder).lower().endswith(".codex-2"))
        self.assertEqual(setup.parse_account("max-2=~/x")[0], None)
        with self.assertRaises(ValueError):
            setup.parse_account("gemini:a=b")

    def test_guess_runtime(self):
        folder = self.root / "plain"
        folder.mkdir()
        self.assertEqual(setup.guess_runtime(folder), "claude")
        (folder / "sessions").mkdir()
        self.assertEqual(setup.guess_runtime(folder), "codex")
        named = self.root / ".codex-3"
        named.mkdir()
        self.assertEqual(setup.guess_runtime(named), "codex")

    def test_accounts_add_refuses_a_folder_that_another_account_uses(self):
        folder = self.root / "shared"
        folder.mkdir()
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["accounts", "add", "a", "--home", str(folder), "--no-login"]), 0)
            self.assertEqual(cli.main(["accounts", "add", "b", "--home", str(folder), "--no-login"]), 1)
        self.assertIn("already belongs to claude account 'a'", out.getvalue())
        self.assertEqual([a.name for a in load_config().runtime("claude").accounts], ["a"])


if __name__ == "__main__":
    unittest.main()


class UpdateTests(SetupBase):
    def run_update(self, *args, info=("pip-user", None)):
        method, argv = info
        if method.startswith("pip") and argv is None:
            argv = [sys.executable, "-c", "print('reinstalled')"]
        out = io.StringIO()
        with patch.object(setup, "_install_info", return_value=(method, argv)), \
             patch.object(setup, "_replaces_running_program", return_value=False), redirect_stdout(out):
            code = cli.main(["update", *args])
        return code, out.getvalue()

    def test_updates_the_package_and_every_host_that_has_the_plugin(self):
        self.run_cli("--host", "claude", "--host", "codex")
        self.log.unlink()
        code, out = self.run_update()
        self.assertEqual(code, 0, out)
        self.assertIn("agt: reinstalled with `", out)
        changing = [(c["kind"], c["argv"]) for c in self.calls() if "list" not in c["argv"]]
        self.assertEqual(changing, [
            ("claude", ["plugin", "marketplace", "update", "agents-router"]),
            ("claude", ["plugin", "update", "agents-router@agents-router"]),
            ("codex", ["plugin", "marketplace", "upgrade", "agents-router"]),
            ("codex", ["plugin", "add", "agents-router@agents-router"]),
        ])
        self.assertIn("Restart Claude Code", out)
        self.assertIn("Start a new Codex session", out)
        self.assertNotIn("Antigravity", out)

    def test_codex_from_a_local_clone_updates_without_a_fetch(self):
        # Codex refuses `marketplace upgrade` for a marketplace added from a folder; it reads that folder in place.
        with patch.dict(os.environ, {"AGT_PLUGIN_SOURCE": str(REPO_ROOT)}):
            self.run_cli("--host", "codex", "--share", "off", "--yes")
            self.log.unlink()
            code, out = self.run_update("--no-package", "--host", "codex")
        self.assertEqual(code, 0, out)
        changing = [c["argv"] for c in self.calls() if c["kind"] == "codex" and "list" not in c["argv"]]
        self.assertEqual(changing, [["plugin", "add", "agents-router@agents-router"]])
        self.assertNotIn("failed", out)

    def test_dry_run_and_no_package(self):
        self.run_cli("--host", "antigravity")
        self.log.unlink()
        code, out = self.run_update("--dry-run")
        self.assertIn("would run `python -c print('reinstalled')`", out.replace(Path(sys.executable).stem, "python"))
        self.assertRegex(out, r"\+  Antigravity  agy plugin install https://github.com/Watcharagorn/agents-router\n")
        self.assertTrue(all("list" in c["argv"] for c in self.calls()))
        code, out = self.run_update("--no-package", "--host", "antigravity")
        self.assertEqual(code, 0, out)
        self.assertNotIn("agt:", out)
        self.assertRegex(out, r"\+  Antigravity  agy plugin install ")

    def test_nothing_installed_and_unknown_package(self):
        code, out = self.run_update(info=("unknown", None))
        self.assertEqual(code, 1)
        self.assertIn("couldn't tell how the package was installed", out)
        self.assertIn("pipx install --force git+https://github.com/Watcharagorn/agents-router", out)
        self.assertIn("  Plugin   no host has it yet; `agt setup` installs it\n", out)

    def test_settings_route_needs_a_restart(self):
        self.run_cli("--host", "claude", "--claude-route", "settings")
        with patch.object(ClaudeRuntime, "find_binary", return_value=None):
            code, out = self.run_update("--no-package", "--host", "claude")
        self.assertEqual(code, 0, out)
        self.assertIn("a restart refreshes it", out)
        self.assertIn("Restart Claude Code", out)

    def test_detect_install(self):
        url = "git+https://github.com/Watcharagorn/agents-router"
        method, argv = setup.detect_install("/home/u/.local/pipx/venvs/agents-router", "/x/site-packages", None, None, "py")
        self.assertEqual((method, argv[1:]), ("pipx", ["install", "--force", url]))
        method, argv = setup.detect_install("/home/u/.local/share/uv/tools/agents-router", "/x", None, None, "py")
        self.assertEqual((method, argv[1:]), ("uv", ["tool", "install", "--force", "--compile-bytecode", url]))
        user_site = str(self.root / "user-site")
        method, argv = setup.detect_install("/usr", str(self.root / "user-site"), None, user_site, "py")
        self.assertEqual(method, "pip-user")
        self.assertIn("--user", argv)
        self.assertEqual(setup.detect_install("/usr", "/usr/lib/site", None, user_site, "py")[0], "pip")
        editable = json.dumps({"url": "file:///src", "dir_info": {"editable": True}})
        self.assertEqual(setup.detect_install("/usr", "/usr/lib/site", editable, None, "py"), ("editable", None))
        self.assertEqual(setup.detect_install("/usr", None, None, None, "py"), ("unknown", None))

    def test_update_reinstalls_from_where_agt_came_from(self):
        uv = "/home/u/.local/share/uv/tools/agents-router"
        git = json.dumps({"url": "https://example.com/fork/agents-router", "vcs_info": {"vcs": "git", "commit_id": "abc"}})
        self.assertEqual(setup.detect_install(uv, "/x", git, None, "py")[1][-1], "git+https://example.com/fork/agents-router")
        branch = json.dumps({"url": "https://example.com/r", "vcs_info": {"vcs": "git", "commit_id": "abc",
                                                                          "requested_revision": "dev"}})
        self.assertEqual(setup.recorded_source(branch), "git+https://example.com/r@dev")
        folder = json.dumps({"url": Path(self.root / "checkout").as_uri(), "dir_info": {}})
        self.assertEqual(setup.detect_install(uv, "/x", folder, None, "py")[1][-1], str(self.root / "checkout"))
        archive = json.dumps({"url": "https://example.com/main.zip", "archive_info": {"hash": "sha256=00"}})
        self.assertEqual(setup.detect_install(uv, "/x", archive, None, "py")[1][-1], "https://example.com/main.zip")
        self.assertIsNone(setup.recorded_source("not json"))

    def test_a_checkout_is_never_reinstalled(self):
        # The tests run from src/ in this repository, so this must say "checkout" and run nothing.
        self.assertEqual(setup._install_info(), ("checkout", None))


class UpstreamTests(SetupBase):
    """agt doctor warns about upstream CLI Subagents; checked with a temporary PATH and home folder."""

    def test_an_upstream_install_is_found_and_left_alone(self):
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        command = bin_dir / ("csa.exe" if os.name == "nt" else "csa")
        command.write_text("#!/bin/sh\n", encoding="utf-8")
        command.chmod(0o755)
        fake_home = self.root / "home"
        (fake_home / ".cli-subagents").mkdir(parents=True)
        settings = self.claude_dir / "settings.json"
        settings.write_text(json.dumps({"enabledPlugins": {"cli-subagents@cli-subagents": True}}), encoding="utf-8")
        before = settings.read_text(encoding="utf-8")
        with patch.dict(os.environ, {"PATH": str(bin_dir)}), patch("pathlib.Path.home", return_value=fake_home):
            found = setup.upstream_installs(load_config())
        self.assertEqual(len(found), 3, found)
        self.assertIn("`csa` is on PATH", found[0])
        self.assertIn(str(fake_home / ".cli-subagents"), found[1])
        self.assertIn(str(settings), found[2])
        self.assertEqual(settings.read_text(encoding="utf-8"), before)
        self.assertTrue((fake_home / ".cli-subagents").is_dir())

    def test_nothing_is_reported_without_one(self):
        settings = self.claude_dir / "settings.json"
        settings.write_text(json.dumps({"enabledPlugins": {"cli-subagents@cli-subagents": False,
                                                            "agents-router@agents-router": True}}), encoding="utf-8")
        with patch.dict(os.environ, {"PATH": str(self.root)}), patch("pathlib.Path.home", return_value=self.root / "home"):
            self.assertEqual(setup.upstream_installs(load_config()), [])


TEAM = {"accountUuid": "uuid-team", "organizationUuid": "org-team", "organizationType": "claude_team"}
MAX = {"accountUuid": "uuid-max", "organizationUuid": "org-max", "organizationType": "claude_max"}
OTHER = {"accountUuid": "uuid-other", "organizationUuid": "org-other", "organizationType": "claude_pro"}
REPO_ROOT = Path(__file__).resolve().parents[1]


class ProfileBase(SetupBase):
    """A home with Claude profiles; this session runs in ~/.claude-ntb.

    ~/.claude (Max), ~/.claude-ntb (Team, this session), ~/.claude-personal (Max again: one login, its own folder)
    and ~/.claude-old, which [claude] ignore leaves out.
    """

    def setUp(self):
        super().setUp()
        from agents_router import profiles

        self.home = self.root / "home"
        self.home.mkdir()
        os.environ.update(HOME=str(self.home), USERPROFILE=str(self.home), CLAUDECODE="1")
        self.default = self.profile(".claude", MAX)
        self.ntb = self.profile(".claude-ntb", TEAM)
        self.personal = self.profile(".claude-personal", MAX)
        self.old = self.profile(".claude-old", OTHER)
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.ntb)
        (self.root / "agt").mkdir(exist_ok=True)
        (self.root / "agt" / "settings.toml").write_text('[claude]\nignore = ["claude-old"]\n', encoding="utf-8")
        profiles.forget()

    def tearDown(self):
        os.environ.pop("CLAUDECODE", None)
        super().tearDown()

    def profile(self, name, account, logged_in=True):
        folder = self.home / name
        folder.mkdir(exist_ok=True)
        state = self.home / ".claude.json" if name == ".claude" else folder / ".claude.json"
        state.write_text(json.dumps({"oauthAccount": account}), encoding="utf-8")
        if logged_in:
            (folder / "logged_in").touch()
        return folder

    def installs(self):
        """(folder, CLAUDE_CONFIG_DIR set) of each `claude plugin install`."""

        return [(Path(c["folder"]), c["config_dir_set"]) for c in self.calls()
                if c["kind"] == "claude" and c["argv"][:2] == ["plugin", "install"]]

    def proposal(self, *args):
        code, out = self.run_cli("--host", "claude", "--share", "off", "--dry-run", "--json", *args)
        self.assertEqual(code, 0, out)
        return json.loads(out)



class ProfileSetupTests(ProfileBase):
    """Every Claude profile gets the plugin, each with its own environment."""

    def test_every_profile_gets_the_plugin_with_its_own_environment(self):
        proposal = self.proposal()
        self.assertEqual([(p["name"], p["this_session"], p["gets_plugin"]) for p in proposal["claude_profiles"]],
                         [("claude-ntb", True, True), ("claude", False, True), ("claude-personal", False, True)])
        self.assertIn("--claude-profiles all", proposal["command"])
        self.assertIn("--if-plan", proposal["command"])  # a plugin-only run has a plan id too
        self.assertIn("Ask the user once", proposal["next"])
        self.assertEqual(self.installs(), [])
        code, preview = self.run_cli("--host", "claude", "--share", "off", "--dry-run")
        self.assertRegex(preview, r"Claude profiles that get the plugin\n  \[x\]  claude-ntb +~/\S+ +this session\n")
        code, out = self.run_cli(*proposal["command"].split()[2:])
        self.assertEqual(code, 0, out)
        # ~/.claude runs without CLAUDE_CONFIG_DIR (its login is found only that way), even from a ~/.claude-ntb session.
        self.assertEqual(sorted(self.installs()), sorted([(self.ntb, True), (self.default, False), (self.personal, True)]))
        for folder in (self.default, self.ntb, self.personal):
            self.assertEqual(self.plugins(folder), {"agents-router@agents-router": True})
        self.assertFalse((self.old / "fake_plugins.json").exists())
        self.assertIn("Claude Code (claude-ntb)", out)  # one row per profile, under the Claude Code label
        self.assertIn("Claude Code (claude)", out)
        self.assertIn("Restart Claude Code (or type /reload-plugins in an open session)", out)  # this session
        self.assertIn(f"Start a new Claude Code session in {ui_path(self.default)}", out)
        self.assertIn(f"Start a new Claude Code session in {ui_path(self.personal)}", out)

    def test_this_or_named_profiles_only(self):
        proposal = self.proposal("--claude-profiles", "this")
        self.assertEqual([p["name"] for p in proposal["claude_profiles"] if p["gets_plugin"]], ["claude-ntb"])
        self.assertIn("--claude-profiles this", proposal["command"])
        proposal = self.proposal("--claude-profiles", "personal,claude")
        self.assertEqual([p["name"] for p in proposal["claude_profiles"] if p["gets_plugin"]], ["claude", "claude-personal"])
        with redirect_stderr(io.StringIO()) as err:
            code, out = self.run_cli("--host", "claude", "--share", "off", "--dry-run", "--claude-profiles", "nope")
        self.assertEqual(code, 2)
        self.assertIn("no Claude profile named 'nope' here. Known: claude-ntb, claude, claude-personal", err.getvalue())

    def test_a_changed_set_of_profiles_is_refused(self):
        proposal = self.proposal()
        self.profile(".claude-new", OTHER)
        code, out = self.run_cli(*proposal["command"].split()[2:])
        self.assertEqual(code, 1, out)
        self.assertIn("the plan changed since your dry run", out)
        self.assertEqual(self.installs(), [])

    def test_plain_setup_without_a_terminal_installs_only_this_profile(self):
        code, out = self.run_cli("--host", "claude")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.installs(), [(self.ntb, True)])
        self.assertIn("Your other Claude profiles don't have the plugin yet: claude (", out)
        self.assertIn("agt setup --claude-profiles all", out)

    def test_a_failed_install_in_another_profile_leaves_its_settings_alone(self):
        os.environ.update(FAKE_HOST_FAIL="install", FAKE_HOST_FAIL_IN=str(self.default))
        try:
            code, out = self.run_cli("--host", "claude", "--share", "off", "--yes")
        finally:
            os.environ.pop("FAKE_HOST_FAIL_IN", None)
        self.assertEqual(code, 1, out)
        self.assertFalse((self.default / "settings.json").exists())  # no settings.json fallback in another profile
        self.assertEqual(self.plugins(self.ntb), {"agents-router@agents-router": True})
        self.assertIn("/plugin install agents-router@agents-router", out)
        self.assertIn("managed settings", out)

    def test_without_the_claude_command_each_profile_declares_the_plugin(self):
        with patch.object(ClaudeRuntime, "find_binary", return_value=None):
            code, out = self.run_cli("--host", "claude", "--share", "off", "--yes")
        self.assertEqual(code, 0, out)
        for folder in (self.default, self.ntb, self.personal):
            data = json.loads((folder / "settings.json").read_text(encoding="utf-8"))
            self.assertTrue(data["enabledPlugins"]["agents-router@agents-router"])
        self.assertFalse((self.old / "settings.json").exists())
        self.assertIn(f"Restart Claude Code in {ui_path(self.default)}", out)

    def test_update_updates_every_profile_that_has_the_plugin(self):
        self.run_cli("--host", "claude", "--share", "off", "--yes")
        (self.personal / "logged_in").unlink()  # logged out since: still updated
        self.log.unlink()
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["update", "--no-package", "--host", "claude"])
        self.assertEqual(code, 0, out.getvalue())
        updated = sorted((Path(c["folder"]), c["config_dir_set"]) for c in self.calls() if c["argv"][:2] == ["plugin", "update"])
        self.assertEqual(updated, sorted([(self.ntb, True), (self.default, False), (self.personal, True)]))

    def test_a_registered_profile_with_its_own_setup_is_included(self):
        work = self.root / "work-profile"
        work.mkdir()
        (work / ".claude.json").write_text(json.dumps({"oauthAccount": OTHER}), encoding="utf-8")
        (work / "logged_in").touch()
        code, out = self.run_cli("--host", "claude", "--claude-profiles", "this", "--account", f"work={work}")
        self.assertEqual(code, 0, out)
        self.assertEqual(load_config().runtime("claude").find("work").setup, "own")
        proposal = self.proposal()
        self.assertIn(("work", str(work)), [(p["name"], p["folder"]) for p in proposal["claude_profiles"]])

    def test_doctor_finds_a_profile_with_an_older_plugin(self):
        from agents_router import __version__

        with patch.dict(os.environ, {"FAKE_HOST_PLUGIN_VERSION": "0.0.1"}):
            self.run_cli("--host", "claude", "--share", "off", "--yes", "--claude-profiles", "claude")
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["doctor", "--runtime", "claude"])
        self.assertEqual(code, 1, out.getvalue())
        self.assertIn(f"Claude Code profile claude ({ui_short(self.default)}) has agents-router plugin 0.0.1; "
                      f"agt is {__version__}.", out.getvalue())
        self.assertEqual(setup.installed_version(self.default), "0.0.1")
        self.assertIsNone(setup.installed_version(self.ntb))


class FirstJobTests(ProfileBase):
    """The first-job hint names a CLI or profile that can take a job now, never one without a login."""

    def hint(self):
        from agents_router import setup_screen, ui

        out = io.StringIO()
        with redirect_stdout(out):
            ui.out(*setup_screen._first_job())
        return out.getvalue()

    def test_codex_without_a_login_isnt_offered(self):
        out = self.hint()  # Codex and agy are installed, neither logged in; this session runs in claude-ntb
        self.assertNotIn("Codex", out)
        self.assertNotIn("Gemini", out)
        self.assertIn("Use claude to review my last commit.", out)
        self.assertIn("/agents-router:delegate review my last commit on claude", out)
        self.assertIn('agt run --account claude "review my last commit"', out)
        from agents_router.config import load_config

        self.assertIsNotNone(load_config().runtime("claude").find("claude"))  # the name it shows is one agt takes

    def test_a_logged_in_codex_comes_first(self):
        (self.codex_dir / "logged_in").touch()
        out = self.hint()
        self.assertIn("Use Codex to review my last commit.", out)
        self.assertIn('agt run --runtime codex "review my last commit"', out)

    def test_nothing_ready_falls_back_to_a_subagent(self):
        for folder in (self.default, self.personal):
            (folder / "logged_in").unlink()
        from agents_router import profiles

        profiles.forget()
        out = self.hint()
        self.assertIn("Use a subagent to review my last commit.", out)
        self.assertIn('agt run "review my last commit"', out)


class ProfileEdgeTests(ProfileBase):
    """What the review of the first version found: each is a case that went wrong once."""

    def test_an_account_named_in_the_same_run_gets_the_plugin(self):
        work = self.root / "work-profile"
        work.mkdir()
        (work / ".claude.json").write_text(json.dumps({"oauthAccount": OTHER}), encoding="utf-8")
        (work / "logged_in").touch()
        code, out = self.run_cli("--host", "claude", "--share", "off", "--yes", "--claude-profiles", "work",
                                 "--account", f"work={work}")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.installs(), [(work, True)])

    def test_an_ignored_primary_is_left_alone(self):
        (self.root / "agt" / "settings.toml").write_text(
            f'[claude]\nprimary = "{self.default.as_posix()}"\nignore = ["claude-old", "claude"]\n', encoding="utf-8")
        proposal = self.proposal()
        self.assertEqual([p["name"] for p in proposal["claude_profiles"]], ["claude-ntb", "claude-personal"])

    def test_update_and_doctor_reach_a_profile_with_the_plugin_but_no_login(self):
        from agents_router import __version__

        bare = self.home / ".claude-bare"
        (bare / "plugins").mkdir(parents=True)
        (bare / "plugins" / "installed_plugins.json").write_text(json.dumps(
            {"version": 2, "plugins": {"agents-router@agents-router": [{"scope": "user", "version": "0.0.1"}]}}), encoding="utf-8")
        (bare / "fake_plugins.json").write_text(json.dumps(
            {"marketplaces": ["agents-router"], "plugins": {"agents-router@agents-router": True}}), encoding="utf-8")
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["doctor", "--runtime", "claude", "--json"])
        problems = json.loads(out.getvalue())["problems"]
        self.assertEqual(code, 1)
        self.assertIn({"problem": f"Claude Code profile claude-bare ({ui_short(bare)}) has agents-router plugin 0.0.1; "
                                  f"agt is {__version__}.", "fix": "agt update"}, problems)
        with redirect_stdout(io.StringIO()):
            cli.main(["update", "--no-package", "--host", "claude"])
        self.assertIn(bare, [Path(c["folder"]) for c in self.calls() if c["argv"][:2] == ["plugin", "update"]])

    def test_the_settings_route_command_applies_its_own_plan(self):
        proposal = self.proposal("--claude-route", "settings")
        self.assertIn("--claude-route settings", proposal["command"])
        code, out = self.run_cli(*proposal["command"].split()[2:])
        self.assertEqual(code, 0, out)
        self.assertTrue(json.loads((self.ntb / "settings.json").read_text(encoding="utf-8"))["enabledPlugins"])


@unittest.skipIf(os.name == "nt", "launchd and the passwd database are macOS and Unix only")
class SandboxGuardTests(unittest.TestCase):
    def test_a_borrowed_home_never_reaches_the_real_launchd(self):
        import pwd

        from agents_router.share import triggers

        self.assertTrue(triggers._borrowed_home())  # the tests' HOME is a throwaway folder
        with patch.dict(os.environ, {"HOME": pwd.getpwuid(os.getuid()).pw_dir}):
            self.assertFalse(triggers._borrowed_home())
        with patch.dict(os.environ), patch.object(triggers, "_in_temp", return_value=False):
            for key in ("AGT_LAUNCHCTL", "AGT_OSASCRIPT"):
                os.environ.pop(key, None)  # as in a worker, whose environment agt cleans
            self.assertIsNone(triggers._launchctl_command())
            self.assertIsNone(triggers._notifier())
            self.assertIn("sandbox", triggers.watcher_supported() or "")


class PluginSourceTests(SetupBase):
    """AGT_PLUGIN_SOURCE: a local clone instead of GitHub, for live checks in a sandbox."""

    def test_github_by_default_and_a_local_clone_when_set(self):
        self.assertEqual(setup.plugin_source(), "Watcharagorn/agents-router")
        with patch.dict(os.environ, {"AGT_PLUGIN_SOURCE": str(REPO_ROOT)}):
            self.assertEqual(setup.plugin_source(), str(REPO_ROOT))
            code, out = self.run_cli("--host", "claude", "--host", "antigravity", "--share", "off", "--yes")
            self.assertEqual(code, 0, out)
            self.assertIn(["plugin", "marketplace", "add", str(REPO_ROOT)], [c["argv"] for c in self.calls()])
            self.assertIn(["plugin", "install", str(REPO_ROOT)], [c["argv"] for c in self.calls() if c["kind"] == "agy"])
            self.assertEqual(self.plugins(self.claude_dir), {"agents-router@agents-router": True})
            os.environ["CLAUDE_CONFIG_DIR"] = str(self.root / "declared")
            (self.root / "declared").mkdir()
            code, out = self.run_cli("--host", "claude", "--share", "off", "--yes", "--claude-route", "settings")
            self.assertEqual(code, 0, out)
        data = json.loads((self.root / "declared" / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(data["extraKnownMarketplaces"]["agents-router"],
                         {"source": {"source": "directory", "path": str(REPO_ROOT)}})


def ui_path(folder):
    from agents_router import ui

    return ui.tilde(str(folder))


def ui_short(folder):
    from agents_router import ui

    return ui.short_path(folder)
