"""The roles grid as plain state: scripted keys in, the change line and the tables to save out (roles_grid.py)."""

import unittest

from helpers import TempHome  # noqa: F401 - sets up the throwaway process first

from agents_router import roles, roles_grid, ui


def role(name, access, providers, enabled=True):
    return roles.Role(name, access, [roles.parse_provider(text) for text in providers], enabled)


def grid(**kwargs):
    found = [role("reviewer", "read", ["codex:work/gpt-6.1-sol:high", "antigravity:primary"]),
             role("builder", "edit", ["claude:claude-work/opus", "codex:work/gpt-6.1-sol:high"]),
             role("verifier", "read", ["kiro:primary"], enabled=False)]
    return roles_grid.RoleGrid(found, **kwargs)


def press(state, *keys):
    for key in keys:
        state.press(key)
    return state


class GridState(unittest.TestCase):
    def test_rows_are_entries_and_cells_are_ticks_or_places(self):
        state = grid()
        lines = [ui.plain(line) for line in state.table()]
        self.assertRegex(lines[1], r"role on/off\s+on\s+on\s+off")
        self.assertRegex(lines[2], r"codex:work\s+gpt-6.1-sol\s+high\s+✓\s+2\s+·")
        self.assertRegex(lines[4], r"claude:claude-work\s+opus\s+—\s+·\s+1\s+·")

    def test_off_role_stays_visible_dimmed_with_its_providers(self):
        state = grid()
        kiro = state.table()[-1]
        self.assertIn(ui.Span("muted", "✓"), kiro)
        self.assertEqual(state.tables(), {})

    def test_o_and_space_on_the_switch_row_flip_the_role(self):
        state = press(grid(), "right", "right", "o")
        self.assertEqual(state.tables(), {"verifier": {"enabled": True}})
        self.assertEqual(state.changes(), ["verifier: on"])
        press(state, "up", "space")  # the on/off row: space flips it back
        self.assertEqual(state.tables(), {})

    def test_plus_minus_reorder_an_edit_role_only(self):
        state = press(grid(), "right", "-")  # codex:work row in builder: already last
        self.assertEqual(state.tables(), {})
        press(state, "+")
        self.assertEqual(state.tables(), {"builder": {"providers": ["codex:work/gpt-6.1-sol:high", "claude:claude-work/opus"]}})
        self.assertEqual(state.changes(), ["builder: codex:work moves to 1st, claude:claude-work to 2nd"])
        press(state, "left", "+")  # a read role has no order
        self.assertEqual(list(state.tables()), ["builder"])

    def test_m_and_e_change_the_row_in_every_role(self):
        choices = lambda entry: ([None, "gpt-6.1-sol", "gpt-6.1-mini"], [None, "low", "high"])
        state = press(grid(choices=choices), "m", "e")
        self.assertEqual(state.tables(), {
            "reviewer": {"providers": ["codex:work/gpt-6.1-mini", "antigravity:primary"]},
            "builder": {"providers": ["claude:claude-work/opus", "codex:work/gpt-6.1-mini"]}})
        self.assertEqual(state.changes(), ["codex:work model gpt-6.1-sol → gpt-6.1-mini", "codex:work effort high → default"])

    def test_d_duplicates_a_row_for_another_model(self):
        choices = lambda entry: ([None, "gpt-6.1-sol", "gpt-6.1-mini"], [None, "high"])
        state = press(grid(choices=choices), "d", "m", "right", "right", "space")
        self.assertEqual(state.tables()["verifier"], {"providers": ["kiro:primary", "codex:work/gpt-6.1-mini:high"]})
        self.assertEqual(state.changes(), ["verifier: codex:work joins"])

    def test_a_and_n_tick_and_clear_the_whole_column(self):
        state = press(grid(), "a")
        self.assertEqual(len(state.tables()["reviewer"]["providers"]), 4)
        press(state, "n")
        self.assertEqual(state.press("enter"), None)  # an enabled role with no providers can't be saved
        self.assertIn("reviewer would have no providers left", state.problem)
        press(state, "o")
        self.assertEqual(state.press("enter"), roles_grid.DONE)
        self.assertEqual(state.tables(), {"reviewer": {"providers": [], "enabled": False}})

    def test_lines_name_every_change_and_the_keys(self):
        state = press(grid(), "right", "+", "right", "o")
        text = "\n".join(state.lines(style="plain"))
        self.assertIn("2 changes: builder: codex:work moves to 1st, claude:claude-work to 2nd · verifier: on", text)
        self.assertIn("o role on/off", text)
        self.assertEqual(state.press("esc"), roles_grid.QUIT)

    def test_columns_scroll_so_the_cursors_shows(self):
        found = [role(f"role-number-{index}", "read", ["codex:work"]) for index in range(8)]
        state = roles_grid.RoleGrid(found)
        text = "\n".join(state.lines(width=80, style="plain"))
        self.assertIn("role-number-0", text)
        self.assertNotIn("role-number-7", text)
        self.assertIn("more roles › (← → to see them)", text)
        press(state, *["right"] * 7)
        text = "\n".join(state.lines(width=80, style="plain"))
        self.assertIn("role-number-7", text)
        self.assertNotIn("role-number-0", text)
        self.assertRegex(text, r"‹ \d more roles \(← → to see them\)")
        self.assertIsNone(state.cut(400))
        self.assertEqual(len(ui.plain(state.table()[0]).split()), 8)  # read-only: every column

    def test_fallback_prints_the_table_read_only(self):
        written = []
        self.assertEqual(grid().fallback(input, written.append), roles_grid.QUIT)
        self.assertEqual(written[-1], roles_grid.HINT)


if __name__ == "__main__":
    unittest.main()
