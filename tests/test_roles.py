"""[roles.*] settings: provider strings, level merging and the shape checks at load (roles.py)."""

from pathlib import Path
import unittest

from helpers import TempHome

from agents_router import config, levels, plans, roles


def layer(level, text, path=None):
    return levels.parse(level, Path(path or f"/x/{level}.toml"), text)


class ProviderStrings(unittest.TestCase):
    def test_full_and_short_forms(self):
        p = roles.parse_provider("codex:work/gpt-6.1-sol:high")
        self.assertEqual((p.runtime, p.account, p.model, p.effort), ("codex", "work", "gpt-6.1-sol", "high"))
        self.assertEqual(roles.parse_provider("codex"), roles.Provider("codex"))
        self.assertEqual(roles.parse_provider("codex:work"), roles.Provider("codex", "work"))
        self.assertEqual(str(roles.parse_provider("claude:claude-work/opus")), "claude:claude-work/opus")

    def test_last_colon_part_is_effort_only_when_known(self):
        self.assertEqual(roles.parse_provider("codex:work/foo:bar").model, "foo:bar")
        self.assertIsNone(roles.parse_provider("codex:work/foo:bar").effort)
        self.assertEqual(roles.parse_provider("codex:work:high").effort, "high")

    def test_bad_runtime(self):
        with self.assertRaises(roles.RoleError):
            roles.parse_provider("gemini:default")

    def test_efforts_match_plans(self):
        self.assertEqual(roles.EFFORTS, plans.EFFORTS)

    def test_round_trip(self):
        for text in ("codex", "codex:work", "kiro:primary", "codex:work/gpt-6.1-sol:high", "codex:high"):
            self.assertEqual(roles.parse_provider(str(roles.parse_provider(text))), roles.parse_provider(text))


class Merging(unittest.TestCase):
    USER = """
[roles.reviewer]
providers = ["codex:work", "antigravity:primary"]
[roles.builder]
providers = ["claude:claude-work/opus", "codex:work"]
[roles.security-reviewer]
access = "read"
description = "Checks security"
providers = ["codex:work"]
"""

    def test_base_roles_have_fixed_access(self):
        found = roles.from_layers([layer("user", self.USER)])
        self.assertEqual(found["reviewer"].access, "read")
        self.assertEqual(found["builder"].access, "edit")
        self.assertEqual([str(p) for p in found["builder"].providers], ["claude:claude-work/opus", "codex:work"])

    def test_a_custom_role_can_have_full_access(self):
        found = roles.from_layers([layer("user", '[roles.general-worker]\naccess = "full"\n'
                                                 'providers = ["kiro", "codex:work"]\n')])
        self.assertEqual(found["general-worker"].access, "full")
        self.assertEqual(found["general-worker"].as_table()["access"], "full")

    def test_workspace_merges_per_key_and_providers_replace(self):
        ws = '[roles.reviewer]\nproviders = ["kiro:primary"]\n[roles.builder]\nenabled = false\n'
        found = roles.from_layers([layer("user", self.USER), layer("workspace", ws)])
        self.assertEqual([str(p) for p in found["reviewer"].providers], ["kiro:primary"])
        self.assertFalse(found["builder"].enabled)
        self.assertEqual(len(found["builder"].providers), 2)  # kept from the user file
        self.assertEqual(found["security-reviewer"].description, "Checks security")
        self.assertEqual(found["reviewer"].level(), "workspace")

    def test_ordered_base_first(self):
        found = roles.from_layers([layer("user", self.USER)])
        self.assertEqual([r.name for r in roles.ordered(found)], ["reviewer", "builder", "security-reviewer"])

    def test_disable_any_role_including_base(self):
        found = roles.from_layers([layer("user", '[roles.verifier]\nenabled = false\n')])
        self.assertFalse(found["verifier"].enabled)
        self.assertEqual(found["verifier"].providers, [])


class ShapeChecks(unittest.TestCase):
    def refused(self, text, *needles):
        with self.assertRaises(levels.SettingsError) as caught:
            roles.from_layers([layer("user", text, "/u/settings.toml")])
        for needle in needles:
            self.assertIn(needle, str(caught.exception))
        return str(caught.exception)

    def test_S7c_typo_in_access_names_file_and_line(self):
        self.refused('[roles.helper]\naccess = "wirte"\nproviders = ["codex"]\n', "/u/settings.toml:2", "wirte")

    def test_S7c_error_names_fix_command(self):
        text = self.refused('[roles.builder]\naccess = "wirte"\nproviders = ["codex"]\n')
        self.assertTrue(text.endswith('; fix: agt roles config builder --unset access --user'), text)
        with self.assertRaises(levels.SettingsError) as caught:  # the level flag is the file's own level
            roles.from_layers([layer("workspace", '[roles.reviewer]\nprovider = ["codex"]\n')])
        self.assertTrue(str(caught.exception).endswith("; fix: agt roles config reviewer --unset provider --workspace"))
        self.refused('[roles.reviewer]\nproviders = ["gemini"]\n', "; fix: agt roles config reviewer --only <providers> --user")
        self.refused('[roles.helper]\nproviders = ["codex"]\n', "; fix: agt roles config helper --access read --user")

    def test_a_proposed_table_names_no_fix_command(self):
        with self.assertRaises(levels.SettingsError) as caught:
            roles.from_layers([layer("user", '[roles.builder]\naccess = "read"\nproviders = ["codex"]\n',
                                     "<proposed roles>")])
        self.assertNotIn("fix:", str(caught.exception))

    def test_unknown_key(self):
        self.refused('[roles.reviewer]\nprovider = ["codex"]\n', "no key provider")

    def test_reserved_names(self):
        self.refused('[roles.codex]\naccess = "read"\nproviders = ["codex"]\n', "agt-codex")

    def test_base_role_access_cannot_change(self):
        self.refused('[roles.reviewer]\naccess = "edit"\nproviders = ["codex"]\n', "base role")

    def test_base_role_cannot_have_full_access(self):
        self.refused('[roles.builder]\naccess = "full"\nproviders = ["codex"]\n', "base role with edit access")
        self.refused('[roles.helper]\naccess = "admin"\nproviders = ["codex"]\n', 'access must be "read", "edit" or "full"')

    def test_custom_role_needs_access(self):
        self.refused('[roles.helper]\nproviders = ["codex"]\n', "needs access")

    def test_edit_role_without_providers(self):
        self.refused('[roles.builder]\nproviders = []\n', "edit access, so its providers need an order", "--disable builder")

    def test_full_role_without_providers(self):
        self.refused('[roles.ops]\naccess = "full"\nproviders = []\n', "full access, so its providers need an order")

    def test_bad_provider(self):
        self.refused('[roles.reviewer]\nproviders = ["gemini"]\n', "runtime must be one of")


class ConfigIntegration(TempHome):
    def test_load_config_carries_roles(self):
        (self.home / "settings.toml").write_text('[roles.reviewer]\nproviders = ["codex"]\n', encoding="utf-8")
        loaded = config.load_config(cwd=self.project)
        self.assertIn("reviewer", loaded.roles)

    def test_save_config_keeps_roles_tables(self):
        path = self.home / "settings.toml"
        path.write_text('max_depth = 1\n\n[roles.reviewer]\nproviders = ["codex"]\n', encoding="utf-8")
        loaded = config.load_config(path)
        config.save_config(loaded, path)
        self.assertIn("[roles.reviewer]", path.read_text(encoding="utf-8"))

    def test_bad_roles_stop_load_with_file_and_line(self):
        (self.home / "settings.toml").write_text('[roles.reviewer]\nenabled = "no"\n', encoding="utf-8")
        with self.assertRaises(levels.SettingsError) as caught:
            config.load_config(cwd=self.project)
        self.assertIn("settings.toml:2", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
