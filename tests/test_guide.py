"""`agt guide setup`, the setup skill that carries the same guide, and the maintainer skill.

The guide has one source (src/agents_router/guides/setup.md); the skill's copy and the README's prompt
must say the same thing, or an agent following one would do something the other doesn't.
"""

import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import re
import sys
import tomllib
import unittest

import helpers  # noqa: F401  (isolates the test process from your real folders)

from agents_router import __version__, cli

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "src" / "agents_router" / "guides" / "setup.md"
SKILL = ROOT / "skills" / "agents-router-setup" / "SKILL.md"
MAINTAINER = ROOT / ".claude" / "skills" / "agt-maintainer"
INSTALL = "uv tool install --managed-python git+https://github.com/Watcharagorn/agents-router"
sys.path.insert(0, str(ROOT / "scripts"))
import sync_guide  # noqa: E402


def frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"---\n(.*?)\n---\n", text, re.S)
    if not match:
        raise AssertionError(f"{path}: no frontmatter")
    fields = {}
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(": ")
        if not sep:
            raise AssertionError(f"{path}: frontmatter line without 'key: value': {line!r}")
        fields[key] = value
    return fields


class GuideTests(unittest.TestCase):
    def guide(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["guide", *args])
        return code, out.getvalue()

    def test_agt_guide_setup_prints_the_packaged_guide(self):
        code, out = self.guide("setup")
        self.assertEqual(code, 0)
        self.assertEqual(out, GUIDE.read_text(encoding="utf-8"))
        self.assertIn(f"agt {__version__.rsplit('.', 1)[0]}", out)  # the guide names the version it's for
        code, out = self.guide()
        self.assertEqual(code, 0)
        self.assertIn("agt guide setup", out)

    def test_the_guide_ships_in_the_package(self):
        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertIn("guides/*.md", data["tool"]["setuptools"]["package-data"]["agents_router"])

    def test_the_skill_carries_the_guide_unchanged(self):
        text = SKILL.read_text(encoding="utf-8")
        head, found, copy = text.partition(sync_guide.MARKER)
        self.assertTrue(found, "the marker line is missing from the skill")
        self.assertEqual(copy, "\n" + GUIDE.read_text(encoding="utf-8"),
                         "the skill's guide differs from src/agents_router/guides/setup.md: run python3 scripts/sync_guide.py")
        self.assertIn(INSTALL, head)
        self.assertIn("agt guide setup", head)
        self.assertIn("account=<profile>", head)

    def test_skill_frontmatter(self):
        for path in (SKILL, ROOT / "skills" / "agents-router" / "SKILL.md", MAINTAINER / "SKILL.md"):
            fields = frontmatter(path)
            self.assertEqual(fields.get("name"), path.parent.name, path)
            self.assertGreater(len(fields.get("description", "")), 80, path)

    def test_the_readme_prompt_matches_the_skill(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        install = readme[readme.index("## Install"):readme.index("## Quick start")]
        prompt = install[install.index("```text"):install.index("```", install.index("```text") + 3)]
        self.assertIn(INSTALL, prompt)
        self.assertIn("agt guide setup", prompt)
        self.assertIn("skills/agents-router-setup/SKILL.md", prompt)
        self.assertTrue(SKILL.is_file())
        for path in ("llms-install.md", "AGENTS.md"):
            text = (ROOT / path).read_text(encoding="utf-8")
            self.assertIn("agt guide setup", text, path)
            self.assertIn(INSTALL, text, path)

    def test_the_maintainer_skill_is_one_copy_codex_reads_through_a_link(self):
        link = ROOT / ".codex" / "skills" / "agt-maintainer"
        if link.is_symlink():
            self.assertEqual(link.readlink().as_posix(), "../../.claude/skills/agt-maintainer")
            self.assertEqual(link.resolve(), MAINTAINER.resolve())
        else:  # a checkout without symlinks (Windows, core.symlinks off) holds the link's target as text
            self.assertEqual(link.read_text(encoding="utf-8").strip(), "../../.claude/skills/agt-maintainer")
        for reference in ("live-check.md", "release.md"):
            self.assertTrue((MAINTAINER / "references" / reference).is_file(), reference)
            self.assertIn(f"references/{reference}", (MAINTAINER / "SKILL.md").read_text(encoding="utf-8"))

    def test_the_three_versions_match(self):
        plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
        self.assertEqual({plugin, project, __version__}, {__version__})


if __name__ == "__main__":
    unittest.main()
