"""agt share's format adapters, comparison rules, names, tool mapping and exclusions."""

import json
import os
from pathlib import Path
import tempfile
import tomllib
import unicodedata
import unittest

import helpers  # noqa: F401  (isolates the test process from your real folders)

from agents_router.share import codex_toml, exclude, frontmatter, kiro_json, model, names, normalize, toolmap

TRICKY = [
    {"name": "plain", "description": "Reviews code.", "body": "Do the review.\nThen report."},
    {"name": "quotes", "description": 'Says "hi": then # not a comment', "body": "He said \"x\" and 'y'.\n\n- a list"},
    {"name": "slashes", "description": r"Paths like C:\Users\me and \n literally", "body": "Regex: \\d+\\s*\nTOML: '''x''' and \"\"\"y\"\"\""},
    {"name": "unicode", "description": "Café — naïve 日本語", "body": "Tabs\there.\nEmoji 🙂"},
    {"name": "yamlish", "description": "true", "body": "---\nnot front matter\n---"},
]


class FrontmatterTests(unittest.TestCase):
    def test_reads_the_scalar_forms_agent_files_use(self):
        text = (
            "---\n"
            "name: dana\n"
            "# a comment line\n"
            "description: \"Quoted \\\"text\\\" with \\\\ a backslash\"\n"
            "single: 'it''s'\n"
            "tools: Bash, Read, mcp__tracker__search  # trailing comment\n"
            "list: [run_command, \"command, status\"]\n"
            "block:\n"
            "  - one\n"
            "  - two\n"
            "literal: |\n"
            "  line one\n"
            "  line two\n"
            "folded: >-\n"
            "  folded\n"
            "  text\n"
            "wrapped: a long description\n"
            "  that an editor wrapped\n"
            "model: opus\n"
            "---\n"
            "\nBody text\n"
        )
        document = frontmatter.parse(text)
        fields = document.fields
        self.assertEqual(fields["description"], 'Quoted "text" with \\ a backslash')
        self.assertEqual(fields["single"], "it's")
        self.assertEqual(fields["tools"], "Bash, Read, mcp__tracker__search")
        self.assertEqual(fields["list"], ["run_command", "command, status"])
        self.assertEqual(fields["block"], ["one", "two"])
        self.assertEqual(fields["literal"], "line one\nline two\n")
        self.assertEqual(fields["folded"], "folded text")
        self.assertEqual(fields["wrapped"], "a long description that an editor wrapped")
        self.assertEqual(document.body, "\nBody text\n")

    def test_update_changes_only_the_given_keys(self):
        text = "---\nname: dana\n# keep me\ndescription: old\nmodel: opus\ncolor: green\n---\n\nBody\n"
        updated = frontmatter.update(text, {"description": "new: text", "color": None, "extra": ["a", "b"]})
        self.assertEqual(updated, '---\nname: dana\n# keep me\ndescription: "new: text"\nmodel: opus\nextra: [a, b]\n---\n\nBody\n')
        self.assertEqual(frontmatter.update(text, {}), text)
        self.assertEqual(frontmatter.update(text, {}, body="\nOther\n"), text.replace("Body", "Other"))

    def test_update_keeps_crlf_and_a_missing_final_newline(self):
        text = "---\r\nname: x\r\ndescription: d\r\n---\r\nBody"
        updated = frontmatter.update(text, {"description": "e"})
        self.assertEqual(updated, "---\r\nname: x\r\ndescription: e\r\n---\r\nBody")
        self.assertEqual(frontmatter.parse(updated).fields["description"], "e")

    def test_multiline_values_are_replaced_as_one_key(self):
        text = "---\nname: x\ndescription: |\n  one\n  two\n\nmodel: opus\n---\nbody\n"
        updated = frontmatter.update(text, {"description": "flat"})
        self.assertEqual(updated, "---\nname: x\ndescription: flat\n\nmodel: opus\n---\nbody\n")

    def test_tricky_values_round_trip(self):
        for value in ["true", "123", "a: b", "#tag", "[x]", "back\\slash", "line\nbreak", " padded ", "it's", 'say "x"', "-dash", ""]:
            with self.subTest(value=value):
                rendered = frontmatter.render({"v": value, "l": [value, "plain"] if value else ["plain"]}, "")
                fields = frontmatter.parse(rendered).fields
                self.assertEqual(fields["v"], value)
                self.assertEqual(fields["l"], [value, "plain"] if value else ["plain"])
        self.assertEqual(frontmatter.render_entry("mainAgent", False), "mainAgent: false")

    def test_errors_name_the_line(self):
        for text, line in (("---\nname: x\n", 1), ("---\n  stray\nname: x\n---\n", 2), ("---\nnot a key\n---\n", 2),
                           ("---\nname: x\nname: y\n---\n", 3), ('---\nd: "open\n---\n', 2)):
            with self.subTest(text=text):
                with self.assertRaises(frontmatter.FrontmatterError) as raised:
                    frontmatter.parse(text)
                self.assertEqual(raised.exception.line, line)

    def test_no_front_matter_is_only_a_body(self):
        document = frontmatter.parse("Just text\n")
        self.assertFalse(document.has_frontmatter)
        self.assertEqual(document.body, "Just text\n")


class CodexTomlTests(unittest.TestCase):
    def test_render_reads_back_exactly(self):
        for item in TRICKY:
            with self.subTest(name=item["name"]):
                text = codex_toml.render({"name": item["name"], "description": item["description"],
                                          "developer_instructions": item["body"] + "\n"})
                data = tomllib.loads(text)
                self.assertEqual(data["description"], item["description"])
                self.assertEqual(data["developer_instructions"], item["body"] + "\n")

    def test_update_keeps_comments_other_keys_and_tables(self):
        text = (
            "# my agent\n"
            'name = "rev"\n'
            "model = \"gpt-6\"  # keep\n"
            "developer_instructions = '''\n"
            "old text with a \\ backslash, kept literally\n"
            "'''\n"
            "\n"
            "[mcp_servers.x]\n"
            'command = "x"\n'
        )
        self.assertIn("\\ backslash", tomllib.loads(text)["developer_instructions"])
        updated = codex_toml.update(text, {"developer_instructions": "new \\ text\n", "description": "added"})
        data = tomllib.loads(updated)
        self.assertEqual(data["developer_instructions"], "new \\ text\n")
        self.assertEqual(data["description"], "added")
        self.assertEqual(data["model"], "gpt-6")
        self.assertEqual(data["mcp_servers"]["x"]["command"], "x")
        self.assertTrue(updated.startswith('# my agent\nname = "rev"\nmodel = "gpt-6"  # keep\n'))
        self.assertLess(updated.index("description"), updated.index("[mcp_servers.x]"))
        self.assertEqual(codex_toml.update(text, {}), text)

    def test_update_adds_keys_before_the_first_table_when_there_are_none(self):
        updated = codex_toml.update("[profiles.a]\nmodel = \"m\"\n", {"name": "x"})
        self.assertEqual(updated, 'name = "x"\n\n[profiles.a]\nmodel = "m"\n')
        self.assertEqual(codex_toml.update("", {"name": "x"}), 'name = "x"\n')

    def test_an_unclosed_string_is_an_error(self):
        with self.assertRaises(ValueError):
            codex_toml.update('developer_instructions = """\nopen\n', {"name": "x"})


class KiroJsonTests(unittest.TestCase):
    def test_update_keeps_unknown_keys_order_and_indent(self):
        original = {"name": "a", "tools": ["fs_read"], "hooks": {"x": 1}, "prompt": "old", "custom": True}
        for indent in (2, 4, "\t"):
            with self.subTest(indent=indent):
                text = json.dumps(original, indent=indent) + "\n"
                updated = kiro_json.update(text, {"prompt": "new", "description": "d"})
                data = json.loads(updated)
                self.assertEqual(list(data), ["name", "tools", "hooks", "prompt", "custom", "description"])
                self.assertEqual(data["prompt"], "new")
                self.assertEqual(updated, json.dumps({**original, "prompt": "new", "description": "d"}, indent=indent) + "\n")
        self.assertFalse(kiro_json.update('{"a": 1}', {"b": 2}).endswith("\n"))

    def test_file_prompts(self):
        self.assertTrue(kiro_json.is_file_prompt({"prompt": "file://./prompts/x.md"}))
        self.assertFalse(kiro_json.is_file_prompt({"prompt": "You review code."}))
        with self.assertRaises(ValueError):
            kiro_json.read("[1, 2]")


class NormalizeTests(unittest.TestCase):
    def test_line_endings_trailing_spaces_and_end_blank_lines_are_ignored(self):
        self.assertEqual(normalize.text("\n\r\nLine one  \r\nLine two\t\n\n"), "Line one\nLine two")
        self.assertEqual(normalize.description("  Wrapped\n   description "), "Wrapped description")
        a = normalize.agent_core("x", "d  e", "\nBody\n")
        b = normalize.agent_core("x", "d e", "Body   \r\n\r\n")
        self.assertEqual(normalize.digest(a), normalize.digest(b))
        self.assertNotEqual(normalize.digest(a), normalize.digest(normalize.agent_core("x", "d e", "Body!")))


class NameTests(unittest.TestCase):
    def test_identity_and_rules(self):
        self.assertEqual(names.identity("Dana"), names.identity("dana"))
        self.assertIsNone(names.problem("day-plan"))
        self.assertIn("lowercase", names.problem("Day_Plan"))
        self.assertIn("longer", names.problem("a" * 65))
        nfd = unicodedata.normalize("NFD", "café")
        self.assertIn("NFC", names.problem(nfd))
        self.assertEqual(names.collisions(["Dana", "dana", "other"]), [["Dana", "dana"]])
        self.assertEqual(names.collisions([nfd, "café"]), [sorted([nfd, "café"])])


class ToolMapTests(unittest.TestCase):
    def test_claude_tools_and_mcp_references(self):
        self.assertIsNone(toolmap.parse_tools(None))
        self.assertEqual(toolmap.parse_tools("Bash, Read,mcp__tracker__search"), ["Bash", "Read", "mcp__tracker__search"])
        self.assertEqual(toolmap.mcp_reference("mcp__tracker__tracker_search_issues"), ("tracker", "tracker_search_issues", None))
        self.assertEqual(toolmap.mcp_reference("mcp__brave-search"), ("brave-search", None, None))
        self.assertEqual(toolmap.mcp_reference("mcp__plugin_loom_loom__record"), ("loom", "record", "loom"))
        self.assertIsNone(toolmap.mcp_reference("Bash"))
        tools = ["Bash", "mcp__tracker__a", "mcp__brave-search", "mcp__tracker__b", "mcp__plugin_loom_loom__x"]
        self.assertEqual(toolmap.servers_named(tools), ["tracker", "brave-search"])

    def test_mapping_keeps_only_known_names(self):
        tools = ["Bash", "Read", "Grep", "Edit", "Write", "Task", "mcp__tracker__search", "mcp__brave-search"]
        kiro, lost = toolmap.to_kiro(tools)
        self.assertEqual(kiro, ["execute_bash", "fs_read", "grep", "fs_write", "@tracker/search", "@brave-search"])
        self.assertEqual(lost, ["Task"])
        self.assertEqual(toolmap.to_kiro(["Bash"], engine="v3")[0], ["shell"])
        agy, lost = toolmap.to_antigravity(tools)
        self.assertEqual(agy, ["run_command", "command_status", "view_file", "grep_search", "replace_file_content",
                               "write_to_file", "call_mcp_tool"])
        self.assertEqual(lost, ["Task"])

    def test_codex_tool_widening(self):
        self.assertIsNone(toolmap.codex_problem(None, "skip"))
        self.assertIsNone(toolmap.codex_problem(["Bash", "Read"], "allow-if-shell"))
        self.assertIn("no shell", toolmap.codex_problem(["Read", "Grep"], "allow-if-shell"))
        self.assertIn("skip", toolmap.codex_problem(["Bash"], "skip"))


class ExcludeTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name).resolve()

    def tearDown(self):
        self._temp.cleanup()

    def test_agents_that_are_never_shared(self):
        kiro = self.root / "kiro"
        kiro.mkdir()
        (kiro / "kirocrew.lock").write_text("", encoding="utf-8")
        cases = {
            ("claude", ".hidden"): "hidden", ("claude", "agt-codex"): "relay", ("kiro", "kirocrew-conductor"): "KiroCrew",
            ("kiro", "kirocrew"): "KiroCrew", ("kiro", "ops-reporter"): "KiroCrew-only",
        }
        for (cli, name), reason in cases.items():
            with self.subTest(name=name):
                self.assertIn(reason, exclude.agent(cli, name, kiro / f"{name}.json"))
        (kiro / "locked.lock").write_text("", encoding="utf-8")
        self.assertIn("locked.lock", exclude.agent("kiro", "locked", kiro / "locked.json"))
        self.assertIn("file://", exclude.agent("kiro", "linked", kiro / "linked.json", {"prompt": "file://x.md"}))
        self.assertIsNone(exclude.agent("kiro", "reviewer", kiro / "reviewer.json", {"prompt": "Review."}))
        self.assertIsNone(exclude.agent("claude", "dana", self.root / "dana.md"))

    def test_skills_that_are_never_shared(self):
        skills = self.root / "skills"
        for name in ("synced", "agents-router", "no-manifest", "haven-kit", "good"):
            (skills / name).mkdir(parents=True)
            if name != "no-manifest":
                (skills / name / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")
        codex = self.root / "codex"
        codex.mkdir()
        (codex / "config.toml").write_text(
            f'[[skills.config]]\npath = {json.dumps(str(skills / "haven-kit" / "SKILL.md"))}\nenabled = false\n'
            f'[[skills.config]]\npath = {json.dumps(str(skills / "good" / "SKILL.md"))}\nenabled = true\n',
            encoding="utf-8",
        )
        disabled = exclude.codex_disabled_skills(codex)
        self.assertEqual(disabled, {str((skills / "haven-kit").resolve())})
        reasons = {name: exclude.skill(name, skills / name, disabled) for name in ("synced", "agents-router", "no-manifest", "haven-kit", "good")}
        self.assertEqual(reasons, {"synced": "skills/synced is never shared", "agents-router": "agt's own skill",
                                   "no-manifest": "no SKILL.md", "haven-kit": "disabled in Codex", "good": None})
        self.assertEqual(exclude.codex_disabled_skills(self.root / "missing"), set())


class AgentModelTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name).resolve()

    def tearDown(self):
        self._temp.cleanup()

    def write(self, cli, name, text, nested=False):
        folder = self.root / cli
        path = folder / name / "agent.md" if nested else model.new_agent_path(cli, folder, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_every_fixture_reads_back_the_same_in_all_four_formats(self):
        for item in TRICKY:
            core = normalize.agent_core(item["name"], item["description"], item["body"])
            for cli in model.CLIS:
                with self.subTest(name=item["name"], cli=cli):
                    path = self.write(cli, item["name"], model.render_agent(cli, core, tools=["Bash"] if cli != "codex" else None))
                    agent = model.read_agent(cli, path)
                    self.assertEqual(agent.core, core)
                    self.assertEqual(agent.declared_name, item["name"])

    def test_new_copies_hold_only_the_core_fields_and_target_tools(self):
        core = normalize.agent_core("rev", "Reviews", "Body")
        agy = frontmatter.parse(model.render_agent("antigravity", core, ["view_file"])).fields
        self.assertEqual(agy, {"name": "rev", "description": "Reviews", "tools": ["view_file"], "mainAgent": "false"})
        self.assertEqual(set(tomllib.loads(model.render_agent("codex", core))), {"name", "description", "developer_instructions"})
        self.assertEqual(json.loads(model.render_agent("kiro", core, ["fs_read"])),
                         {"name": "rev", "description": "Reviews", "prompt": "Body", "tools": ["fs_read"]})
        self.assertNotIn("tools", frontmatter.parse(model.render_agent("claude", core)).fields)

    def test_update_touches_only_what_changed(self):
        claude = "---\nname: rev\ndescription: Reviews\ntools: Bash, Read\nmodel: opus\n---\n\nOld body\n"
        path = self.write("claude", "rev", claude)
        agent = model.read_agent("claude", path)
        new = normalize.agent_core("rev", "Reviews", "New body")
        self.assertEqual(model.update_agent("claude", claude, agent, new), claude.replace("Old body", "New body"))
        self.assertIs(model.update_agent("claude", claude, agent, agent.core), claude)
        kiro = json.dumps({"name": "rev", "description": "Reviews", "prompt": "Old body", "model": "x", "hooks": {}}, indent=2) + "\n"
        path = self.write("kiro", "rev", kiro)
        updated = json.loads(model.update_agent("kiro", kiro, model.read_agent("kiro", path), new))
        self.assertEqual(updated, {"name": "rev", "description": "Reviews", "prompt": "New body", "model": "x", "hooks": {}})
        codex = '# mine\nname = "rev"\ndescription = "Reviews"\nmodel = "m"\ndeveloper_instructions = """\nOld body\n"""\n'
        path = self.write("codex", "rev", codex)
        text = model.update_agent("codex", codex, model.read_agent("codex", path), new)
        self.assertTrue(text.startswith('# mine\nname = "rev"\ndescription = "Reviews"\nmodel = "m"\n'))
        self.assertEqual(tomllib.loads(text)["developer_instructions"], "New body\n")

    def test_files_names_and_errors(self):
        self.write("claude", "a", "---\nname: a\n---\n")
        (self.root / "claude" / "templates").mkdir()
        (self.root / "claude" / "templates" / "t.md").write_text("x", encoding="utf-8")
        self.assertEqual([path.name for path in model.agent_files("claude", self.root / "claude")], ["a.md"])
        nested = self.write("antigravity", "deep", "---\nname: deep\n---\nBody\n", nested=True)
        self.write("antigravity", "flat", "---\nname: flat\n---\nBody\n")
        found = model.agent_files("antigravity", self.root / "antigravity")
        self.assertEqual([model.agent_name("antigravity", path) for path in found], ["deep", "flat"])
        self.assertEqual(model.read_agent("antigravity", nested).name, "deep")
        mismatched = model.read_agent("claude", self.write("claude", "b", "---\nname: other\n---\n"))
        self.assertEqual((mismatched.name, mismatched.declared_name), ("b", "other"))
        broken = self.write("kiro", "broken", '{"name": "broken", ')
        with self.assertRaises(model.ItemError) as raised:
            model.read_agent("kiro", broken)
        self.assertEqual(raised.exception.path, broken)
        self.assertEqual(model.agent_files("codex", self.root / "missing"), [])


class SkillModelTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name).resolve()
        self.skill = self.root / "real" / "ask"
        (self.skill / "refs").mkdir(parents=True)
        (self.skill / "SKILL.md").write_text("---\nname: ask\ndescription: Asks\n---\nSteps\n", encoding="utf-8")
        (self.skill / "refs" / "a.md").write_text("A", encoding="utf-8")

    def tearDown(self):
        self._temp.cleanup()

    def test_digest_covers_files_but_not_hidden_ones(self):
        first = model.read_skill(self.skill).digest
        (self.skill / ".DS_Store").write_bytes(b"\0")
        self.assertEqual(model.read_skill(self.skill).digest, first)
        (self.skill / "refs" / "a.md").write_text("B", encoding="utf-8")
        self.assertNotEqual(model.read_skill(self.skill).digest, first)

    def test_a_linked_skill_reads_its_real_folder(self):
        link = self.root / "skills" / "ask"
        link.parent.mkdir()
        os.symlink(self.skill, link, target_is_directory=True)
        skill = model.read_skill(link)
        self.assertEqual((skill.path, skill.real, skill.declared_name, skill.description), (link, self.skill, "ask", "Asks"))
        self.assertEqual(skill.digest, model.read_skill(self.skill).digest)


if __name__ == "__main__":
    unittest.main()
