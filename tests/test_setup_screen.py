"""`agt setup`'s one screen: the plugin and agt share suggested together, for a person and for an agent."""

import contextlib
import io
import json
from unittest.mock import patch

from test_share_items import ItemBase
from test_share_sync import age

from agents_router import cli
from agents_router.share import settings, triggers


class SetupScreenTests(ItemBase):
    def setup(self, *args, code=0):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            result = cli.main(["setup", *([] if "--host" in args else ["--host", "claude"]), *args])
        self.assertEqual(result, code, out.getvalue())
        return out.getvalue()

    def test_an_agent_gets_the_whole_screen_and_one_command(self):
        before = self.snapshot()
        proposal = json.loads(self.setup("--dry-run", "--json", "--resolve", "gamma-apikey=skip"))
        self.assertEqual(self.snapshot(), before, "a dry run wrote something")
        self.assertEqual(set(proposal["plugin"]), {"planned", "already", "failed", "notes"})
        self.assertIn("dana", [item["name"] for item in proposal["share"]["items"]])
        self.assertEqual(proposal["needs_answer"], [])
        self.assertEqual(set(proposal["switches"]), {"worktrees", "auto_copy", "widen", "rules", "codex_session", "mcp", "plugins"})
        self.assertFalse(proposal["switches"]["rules"]["on"])
        self.assertFalse(proposal["switches"]["mcp"]["on"])  # MCP servers share only when you say
        self.assertFalse(proposal["switches"]["plugins"]["on"])  # and plugins too
        command = proposal["command"]
        self.assertTrue(command.startswith(f"agt setup --yes --if-plan {proposal['plan']} --host claude"), command)
        self.assertIn("--resolve gamma-apikey=skip", command)
        out = self.setup(*command.split()[2:])
        self.assertIn("agt setup  ✓ done\n\n  +  Claude Code  installed the agents-router plugin\n"
                      "  +  Sharing      9 copies in 4 folders\n      + agents/dana ", out)  # one headline, one list
        self.assertIn("\n  agt keeps them the same from now on: every minute, and at each session start in Claude Code.\n", out)
        self.assertNotIn("Saved your answers", out)
        self.assertNotIn("Next:", out)
        self.assertGreater(out.index("Still needed from you"), out.index("  +  Sharing"))  # last, once
        self.assertEqual(out.count("Still needed from you"), 1)
        saved = settings.load()
        self.assertTrue(saved.enabled)
        self.assertEqual(saved.off, {"skills/gamma-apikey"})
        self.assertTrue((self.user / ".kiro" / "agents" / "dana.json").exists())
        self.assertTrue((self.user / "Library" / "LaunchAgents" / f"{triggers.LABEL}.plist").exists())

    def test_a_person_gets_a_short_apply_command_that_applies(self):
        preview = self.setup("--dry-run")
        self.assertIn("! skills/gamma-apikey differs between folders", preview)
        self.assertRegex(preview, r"Pick a copy first +agt setup --dry-run --resolve gamma-apikey=<cli>")
        self.assertNotIn("In one command", preview)  # the plan id can't stand for an answer not given yet
        picked = self.setup("--dry-run", "--resolve", "gamma-apikey=skip")
        command = next(line.split("In one command", 1)[1].strip() for line in picked.splitlines() if "In one command" in line)
        self.assertRegex(command, r"^agt setup --yes --if-plan [0-9a-f]{12} --host claude --resolve gamma-apikey=skip$")
        self.assertNotIn("--rules", command)  # only the flags you gave
        out = self.setup(*command.split()[2:])
        self.assertIn("  +  Sharing      9 copies in 4 folders\n", out)
        self.assertLess(out.index("Still needed from you"), out.index("Then try your first delegated job"))
        self.assertTrue(out.rstrip().endswith("`agt` on its own shows what is set up and what to do next."), out)
        self.assertIn("Nothing was written: the plan changed", self.setup(*command.split()[2:-2], code=1))

    def test_kiro_without_a_login_is_still_needed(self):
        self.assertNotIn("kiro-cli login", self.setup("--dry-run"))
        with patch.dict("os.environ", {"FAKE_HOST_KIRO_LOGGED_OUT": "1"}):
            proposal = json.loads(self.setup("--dry-run", "--json", "--resolve", "gamma-apikey=skip"))
        self.assertTrue(any("kiro-cli login" in line for line in proposal["still_needed"]), proposal["still_needed"])

    def test_a_conflict_is_asked_before_anything_is_written(self):
        proposal = json.loads(self.setup("--dry-run", "--json"))
        self.assertEqual([ask["id"] for ask in proposal["needs_answer"]], ["resolve:gamma-apikey"])
        before = self.snapshot()
        out = self.setup(*proposal["command"].split()[2:], code=3)
        self.assertIn("gamma-apikey", out)
        self.assertEqual(self.snapshot(), before)

    def test_a_changed_plan_is_refused(self):
        proposal = json.loads(self.setup("--dry-run", "--json", "--resolve", "gamma-apikey=skip"))
        age(self.agent(self.claude, "newcomer"))
        out = self.setup(*proposal["command"].split()[2:], code=1)
        self.assertIn("the plan changed since your dry run", out)
        self.assertFalse(settings.load().enabled)

    def test_share_off_installs_only_the_plugin(self):
        self.setup("--yes", "--share", "off")
        self.assertFalse(settings.load().enabled)

    def test_plain_setup_without_a_terminal_works_as_before(self):
        out = self.setup()
        self.assertFalse(settings.load().enabled)
        self.assertIn("agt setup --dry-run --json", out)

    def test_in_a_terminal_enter_does_all_of_it(self):
        from agents_router.share import commands

        answers = iter(["2", ""])  # the conflict: the second copy wins; then Enter
        with patch.object(commands, "_interactive", lambda: True), \
                patch("builtins.input", lambda prompt: next(answers)):
            out = self.setup()
        self.assertIn("agt setup  nothing is written until you press Enter", out)
        self.assertTrue(settings.load().enabled)
