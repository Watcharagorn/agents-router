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


def reference_roles():
    """The reported table, including accounts used at more than one effort."""

    found = [role("decision-maker", "read", ["codex:xhigh"], False),
             role("reviewer", "read", ["codex:xhigh", "kiro:xhigh"], False),
             role("builder", "edit", ["claude:claude-ntb:xhigh", "codex:xhigh"]),
             role("verifier", "read", ["codex:xhigh", "kiro:xhigh"], False),
             role("code-reviewer", "edit", ["codex:xhigh", "claude:claude-ntb:xhigh", "kiro:xhigh"]),
             role("general-worker", "full", ["claude:claude-ntb:high", "kiro:xhigh", "codex:high"]),
             role("mentor-auditor", "read", ["codex"]),
             role("researcher", "read", ["kiro:xhigh", "claude:claude-ntb:high"])]
    return roles.ordered({item.name: item for item in found})


def table(state):
    return [ui.plain(line).rstrip() for line in state.table()]


class GridState(unittest.TestCase):
    def test_reference_has_three_account_columns_despite_different_efforts(self):
        state = roles_grid.RoleGrid(reference_roles())
        lines = table(state)
        self.assertEqual(lines[0].split(), ["Role", "codex", "kiro", "claude:claude-ntb"])
        self.assertEqual(len(state.rows), 8)
        self.assertEqual(len(lines), 10)  # header, eight roles, shared-effort footnote
        self.assertRegex(lines[6], r"general-worker\s+full·on\s+high:3\s+2\s+high:1")
        self.assertRegex(lines[7], r"mentor-auditor\s+read·on\s+def:✓\s+·\s+·")
        self.assertEqual(lines[-1], "  Effort is xhigh unless a cell shows one (def = the runtime's default). · = not used.")
        self.assertEqual(state.tables(), {})

    def test_shared_effort_counts_role_uses_and_ties_go_to_first_seen(self):
        state = roles_grid.RoleGrid([role("first", "read", ["codex:high", "kiro:xhigh"]),
                                     role("second", "read", ["kiro:xhigh", "claude"])])
        self.assertIn("Effort is xhigh", table(state)[-1])
        self.assertRegex(table(state)[1], r"high:✓\s+✓\s+·")
        state = roles_grid.RoleGrid([role("first", "read", ["codex:high", "kiro:xhigh"])])
        self.assertIn("Effort is high", table(state)[-1])
        self.assertRegex(table(state)[1], r"read·on\s+✓\s+xhigh:✓")

    def test_shared_default_omits_def_and_displays_explicit_efforts(self):
        state = roles_grid.RoleGrid([role("reviewer", "read", ["codex", "kiro", "claude:high"])])
        self.assertRegex(table(state)[1], r"read·on\s+✓\s+✓\s+high:✓")
        self.assertIn("Effort is the runtime's default", table(state)[-1])
        self.assertNotIn("def:", table(state)[1])

    def test_no_providers_means_no_footnote_and_o_still_toggles_role(self):
        empty = roles_grid.RoleGrid([role("off-role-with-no-providers", "read", [], False)])
        self.assertTrue(empty.lines(width=40, height=12, style="plain"))
        state = roles_grid.RoleGrid([role("off", "read", [], False)])
        self.assertEqual(len(table(state)), 2)
        self.assertNotIn("Effort is", "\n".join(table(state)))
        press(state, "right", "a", "m", "e", "s", "+", "-", "space", "o")
        self.assertEqual(state.tables(), {"off": {"enabled": True}})
        self.assertIsNone(state.press("enter"))
        self.assertIn("off would have no providers left", state.problem)

    def test_existing_split_cell_tokens_keep_provider_order_and_model(self):
        state = roles_grid.RoleGrid([role("ops", "edit", ["codex:high", "kiro:xhigh", "codex:/opus:xhigh"]),
                                     role("reviewer", "read", ["kiro:xhigh"])])
        self.assertIn("high:1, opus/3", table(state)[1])
        self.assertEqual(len(state.columns), 2)
        self.assertEqual(state.tables(), {})
        press(state, "d")  # replaced by s
        self.assertEqual(len(state.entries), 4)

    def test_plus_minus_move_split_cell_as_a_block(self):
        state = roles_grid.RoleGrid([role("ops", "edit", ["kiro", "codex:high", "claude", "codex:xhigh"])])
        press(state, "right", "+")
        self.assertEqual(state.providers("ops"), ["codex:high", "codex:xhigh", "kiro", "claude"])
        press(state, "-")
        self.assertEqual(state.providers("ops"), ["kiro", "codex:high", "codex:xhigh", "claude"])
        press(state, "-")
        self.assertEqual(state.providers("ops"), ["kiro", "claude", "codex:high", "codex:xhigh"])
        press(state, "space")
        self.assertEqual(state.providers("ops"), ["kiro", "claude"])

    def test_plus_minus_cross_only_one_entry_of_an_interleaved_account(self):
        for access in ("edit", "full"):
            for key, before, after in (
                ("-", ["codex:high", "kiro:high", "claude:high", "kiro:xhigh"],
                 ["kiro:high", "codex:high", "claude:high", "kiro:xhigh"]),
                ("+", ["kiro:high", "claude:high", "kiro:xhigh", "codex:high"],
                 ["kiro:high", "claude:high", "codex:high", "kiro:xhigh"]),
            ):
                with self.subTest(access=access, key=key):
                    state = roles_grid.RoleGrid([role("ops", access, before)])
                    state.column = next(index for index, column in enumerate(state.columns) if column.runtime == "codex")
                    press(state, key)
                    self.assertEqual(state.tables(), {"ops": {"providers": after}})

    def test_split_block_crosses_only_one_entry_of_an_interleaved_account(self):
        for key, before, after in (
            ("-", ["codex:high", "codex:xhigh", "kiro:high", "claude:high", "kiro:xhigh"],
             ["kiro:high", "codex:high", "codex:xhigh", "claude:high", "kiro:xhigh"]),
            ("+", ["kiro:high", "claude:high", "kiro:xhigh", "codex:high", "codex:xhigh"],
             ["kiro:high", "claude:high", "codex:high", "codex:xhigh", "kiro:xhigh"]),
        ):
            with self.subTest(key=key):
                state = roles_grid.RoleGrid([role("ops", "edit", before)])
                state.column = next(index for index, column in enumerate(state.columns) if column.runtime == "codex")
                press(state, key)
                self.assertEqual(state.tables(), {"ops": {"providers": after}})

    def test_ticking_empty_cell_uses_most_common_model_effort_elsewhere(self):
        found = [role("reviewer", "read", ["kiro"]),
                 role("builder", "edit", ["codex:/gpt-6.1-sol:xhigh"]),
                 role("other", "read", ["codex:high"]),
                 role("more", "read", ["codex:/gpt-6.1-sol:xhigh"])]
        state = press(roles_grid.RoleGrid(found), "right", "space")
        self.assertEqual(state.providers("reviewer"), ["kiro", "codex:/gpt-6.1-sol:xhigh"])
        self.assertEqual(state.changes(), ["reviewer: codex joins"])

    def test_reticking_account_used_nowhere_else_uses_runtime_defaults(self):
        state = roles_grid.RoleGrid([role("builder", "edit", ["codex:/gpt-6.1-sol:xhigh"])])
        press(state, "space", "space")
        self.assertEqual(state.tables(), {"builder": {"providers": ["codex"]}})

    def test_empty_cell_model_effort_split_and_order_keys_do_nothing(self):
        state = press(grid(), "right", "right", "m", "e", "s", "+", "-")
        self.assertEqual(state.tables(), {})

    def test_tables_round_trip_preserves_order_models_efforts_and_switches(self):
        found = reference_roles()
        state = press(roles_grid.RoleGrid(found), "down", "down", "+", "s", "e", "down", "o")
        changes = state.tables()
        self.assertEqual(set(changes), {"builder", "verifier"})
        for item in found:
            change = changes.get(item.name, {})
            item.providers = [roles.parse_provider(value) for value in change.get("providers", map(str, item.providers))]
            item.enabled = change.get("enabled", item.enabled)
        saved = roles_grid.RoleGrid(found)
        self.assertEqual(saved.tables(), {})
        self.assertEqual(table(saved), table(state))
        self.assertEqual(saved.providers("builder"), state.providers("builder"))

    def test_toggle_off_on_restores_changes_when_provider_list_is_identical(self):
        state = roles_grid.RoleGrid([role("reviewer", "read", ["codex"])])
        press(state, "space", "space")
        self.assertEqual(state.tables(), {})
        self.assertEqual(state.changes(), [])

    def test_read_retick_restores_original_order_without_a_save(self):
        for access in ("read", "edit", "full"):
            with self.subTest(access=access):
                state = roles_grid.RoleGrid([role("ops", access, ["codex", "kiro"])])
                press(state, "space", "space")
                self.assertEqual(state.press("enter"), roles_grid.DONE)
                if access == "read":
                    self.assertEqual(state.providers("ops"), ["codex", "kiro"])
                    self.assertEqual(state.tables(), {})
                    self.assertEqual(state.changes(), [])
                else:
                    self.assertEqual(state.tables(), {"ops": {"providers": ["kiro", "codex"]}})
                    self.assertEqual(state.changes(), ["ops: kiro moves to 1st, codex to 2nd"])

    def test_read_retick_keeps_original_accounts_before_new_accounts(self):
        state = roles_grid.RoleGrid([role("ops", "read", ["codex", "kiro"]),
                                     role("other", "read", ["claude"])])
        press(state, "space", "right", "right", "space", "left", "left", "space")
        self.assertEqual(state.press("enter"), roles_grid.DONE)
        self.assertEqual(state.providers("ops"), ["codex", "kiro", "claude"])
        self.assertEqual(state.tables(), {"ops": {"providers": ["codex", "kiro", "claude"]}})
        self.assertEqual(state.changes(), ["ops: claude joins"])

    def test_arrows_and_hjkl_move_over_roles_and_accounts_and_stop_at_edges(self):
        state = press(grid(), "up", "left", "k", "h")
        self.assertEqual((state.row, state.column), (0, 0))
        press(state, "j", "l", "down", "right", "down", "right", "j", "l")
        self.assertEqual((state.row, state.column), (2, 3))
        press(state, "k", "h", "up", "left")
        self.assertEqual((state.row, state.column), (0, 1))

    def test_account_scroll_hint_has_singular_and_plural(self):
        state = roles_grid.RoleGrid([role("reviewer", "read", ["codex:one", "codex:two", "codex:three", "codex:four"])])
        press(state, "right")
        self.assertEqual(state.cut(45), "‹ 1 more account · 2 more accounts › (← → to see them)")

    def test_vertical_scroll_keeps_each_cursor_role_visible(self):
        state = roles_grid.RoleGrid([role(f"role-{index:02}", "read", ["codex:xhigh"]) for index in range(20)])
        for height in (18, 12, 8):
            for index in range(20):
                state.row = index
                lines = state.lines(width=80, height=height, style="plain")
                self.assertLessEqual(len(lines), height - 1)
                self.assertTrue(any(f"▸ role-{index:02}" in line for line in lines), (height, index, lines))
        self.assertIn("↑", "\n".join(state.lines(width=80, height=12, style="plain")))

    def test_short_frames_keep_effort_note_while_scrolling(self):
        state = roles_grid.RoleGrid([role(f"role-{index:02}", "read", ["codex:high"]) for index in range(20)])
        note = "  Effort is high unless a cell shows one (def = the runtime's default). · = not used."
        for height in (18, 12, 8):
            for index in (0, 10, 19):
                with self.subTest(height=height, row=index):
                    state.row = index
                    frame = [ui.plain(line) for line in state.frame(width=80, height=height)]
                    self.assertIn(note, frame)
                    self.assertLessEqual(len(frame), height - 1)
                    self.assertTrue(any(f"▸ role-{index:02}" in line for line in frame))
                    self.assertIn("Effort is high", "\n".join(state.lines(width=80, height=height, style="plain")))

    def test_tiny_frames_fit_budget_and_keep_cursor_role_and_effort_note(self):
        for role_count in (1, 8, 20):
            for account_count in (1, 8):
                providers = [f"codex:account-{index}:high" for index in range(account_count)]
                for keys in ((), ("s", "enter")):
                    state = press(roles_grid.RoleGrid([
                        role(f"role-{index:02}", "edit", providers) for index in range(role_count)]), *keys)
                    for row in sorted({0, role_count // 2, role_count - 1}):
                        state.row = row
                        state.column = account_count - 1
                        for width in (40, 80, 120):
                            for height in range(1, 16):
                                with self.subTest(roles=role_count, accounts=account_count, keys=keys,
                                                  row=row, width=width, height=height):
                                    budget = max(height - 1, 1)
                                    frame = [ui.plain(line) for line in state.frame(width, height)]
                                    self.assertLessEqual(len(frame), budget)
                                    self.assertTrue(any(f"▸ role-{row:02}" in line for line in frame))
                                    if budget >= 2:
                                        self.assertTrue(any("Effort is high" in line for line in frame))
        for height in range(1, 16):
            self.assertLessEqual(len(roles_grid.RoleGrid([]).frame(40, height)), max(height - 1, 1))

    def test_rows_are_roles_and_columns_are_accounts(self):
        state = grid()
        lines = table(state)
        self.assertEqual(lines[0].split(), ["Role", "codex:work", "antigravity:primary", "claude:claude-work", "kiro:primary"])
        self.assertEqual([line.split()[0] for line in lines[1:-1]], ["reviewer", "builder", "verifier"])
        self.assertRegex(lines[1], r"reviewer\s+read·on\s+gpt-6.1-sol/high:✓\s+✓\s+·\s+·")
        self.assertRegex(lines[2], r"builder\s+edit·on\s+gpt-6.1-sol/high:2\s+·\s+opus/1\s+·")

    def test_off_role_stays_visible_dimmed_with_its_providers(self):
        state = grid()
        kiro = state.table()[3]
        self.assertIn(ui.Span("muted", "✓"), kiro)
        self.assertTrue(any(isinstance(piece, ui.Span) and piece.style == "muted" and "read·off" in piece.text
                            for piece in kiro))
        self.assertEqual(state.tables(), {})

    def test_o_toggles_the_cursor_role_without_changing_providers(self):
        state = press(grid(), "down", "down", "o")
        self.assertEqual(state.tables(), {"verifier": {"enabled": True}})
        self.assertEqual(state.changes(), ["verifier: on"])
        press(state, "o")
        self.assertEqual(state.tables(), {})

    def test_plus_minus_reorder_an_edit_role_only(self):
        state = press(grid(), "down", "-")  # codex:work in builder: already last
        self.assertEqual(state.tables(), {})
        press(state, "+")
        self.assertEqual(state.tables(), {"builder": {"providers": ["codex:work/gpt-6.1-sol:high", "claude:claude-work/opus"]}})
        self.assertEqual(state.changes(), ["builder: codex:work moves to 1st, claude:claude-work to 2nd"])
        press(state, "up", "+", "-")  # a read role has no order
        self.assertEqual(list(state.tables()), ["builder"])
        press(state, "down", "-")
        self.assertEqual(state.tables(), {})

    def test_a_full_role_shows_places_and_reorders_like_an_edit_role(self):
        state = roles_grid.RoleGrid([role("ops", "full", ["kiro:primary", "codex:work"])])
        lines = [ui.plain(line) for line in state.table()]
        self.assertRegex(lines[1], r"ops\s+full·on\s+1\s+2")
        press(state, "right", "+")
        self.assertEqual(state.tables(), {"ops": {"providers": ["codex:work", "kiro:primary"]}})
        self.assertEqual(state.changes(), ["ops: codex:work moves to 1st, kiro:primary to 2nd"])

    def test_m_and_e_change_only_the_selected_cell(self):
        choices = lambda entry: ([None, "gpt-6.1-sol", "gpt-6.1-mini"], [None, "low", "high"])
        state = press(grid(choices=choices), "m", "down", "enter", "e")
        self.assertEqual(state.tables(), {
            "reviewer": {"providers": ["codex:work/gpt-6.1-mini", "antigravity:primary"]}})
        self.assertEqual(state.changes(), ["codex:work model gpt-6.1-sol → gpt-6.1-mini", "codex:work effort high → default"])

    def test_s_splits_a_cell_then_m_e_change_only_its_last_entry(self):
        choices = lambda entry: ([None, "gpt-6.1-sol", "gpt-6.1-mini"], [None, "high"])
        state = press(grid(choices=choices), "s", "m", "down", "enter", "e")
        self.assertEqual(state.tables(), {"reviewer": {"providers": [
            "codex:work/gpt-6.1-sol:high", "codex:work/gpt-6.1-mini", "antigravity:primary"]}})
        self.assertEqual(state.changes(), ["reviewer: codex:work joins"])
        self.assertIn("gpt-6.1-sol/high:✓, gpt-6.1-mini/✓", table(state)[1])
        self.assertEqual((state.row, state.column), (0, 0))

    def test_wide_cursor_cell_keeps_last_entry_visible_at_width_80(self):
        state = press(roles_grid.RoleGrid([
            role("decision-maker", "read", ["codex:/gpt-6.1-sol:high"])]), "s", "e", "s", "e")
        full_cell = "gpt-6.1-sol/✓, gpt-6.1-sol/xhigh:✓, gpt-6.1-sol/max:✓"
        self.assertIn(full_cell, table(state)[1])  # piped output keeps the full cell
        for cursor in (False, True):
            with self.subTest(cursor=cursor):
                line = ui.sgr(state.table(cursor=cursor, width=80)[1], "plain", 80).rstrip()
                self.assertTrue(line.endswith("gpt-6.1-sol/max:✓"), line)
                self.assertIn("…", line)
                self.assertLessEqual(ui.cell_len(line), 80)
        line = next(line for line in state.lines(width=80, style="plain") if "▸ decision-maker" in line)
        self.assertTrue(line.rstrip().endswith("gpt-6.1-sol/max:✓"), line)

    def test_changes_match_split_entries_to_originals_only_once(self):
        for access in ("read", "edit", "full"):
            for before, efforts, keys, after, effort_change in (
                (["codex:high", "kiro:high"], ["high", "xhigh"], ("e", "s", "e"),
                 ["codex:xhigh", "codex:high", "kiro:high"], "codex effort high → xhigh"),
                (["codex:high", "codex:xhigh", "kiro:high"], ["high", "xhigh", "max"],
                 ("e", "s", "e", "e"), ["codex:high", "codex:max", "codex:xhigh", "kiro:high"],
                 "codex effort xhigh → max"),
            ):
                with self.subTest(access=access, before=before):
                    state = roles_grid.RoleGrid([role("builder", access, before)],
                                                choices=lambda entry: ([None], efforts))
                    press(state, *keys)
                    self.assertEqual(state.press("enter"), roles_grid.DONE)
                    self.assertEqual(state.tables(), {"builder": {"providers": after}})
                    self.assertEqual(state.changes(), ["builder: codex joins", effort_change])
                    if access in roles.ONE_AT_A_TIME:
                        press(state, "-")
                        self.assertEqual(state.providers("builder"), [after[-1], *after[:-1]])
                        self.assertEqual(state.changes(), [
                            "builder: codex joins, kiro moves to 1st, codex to 2nd" +
                            (", codex to 3rd" if len(before) == 3 else ""), effort_change])

    def test_split_copy_blocks_save_until_changed_or_unticked(self):
        choices = lambda entry: ([None, "gpt-6.1-sol"], ["high", "xhigh"])
        for access in ("read", "edit", "full"):
            for enabled in (True, False):
                for repair, expected in (
                    ("m", ["codex:high", "codex:/gpt-6.1-sol:high", "kiro:high"]),
                    ("e", ["codex:high", "codex:xhigh", "kiro:high"]),
                    ("space", ["kiro:high"]),
                ):
                    with self.subTest(access=access, enabled=enabled, repair=repair):
                        state = roles_grid.RoleGrid([role("ops", access, ["codex:high", "kiro:high"], enabled)],
                                                    choices=choices)
                        press(state, "s")
                        self.assertIsNone(state.press("enter"))
                        self.assertIn("ops", state.problem)
                        self.assertIn("m/e", state.problem)
                        self.assertIn("untick", state.problem)
                        press(state, repair)
                        if repair == "m":
                            press(state, "down", "enter")
                        self.assertEqual(state.press("enter"), roles_grid.DONE)
                        self.assertEqual(state.tables(), {"ops": {"providers": expected}})

    def test_split_copy_follows_last_cell_entry_and_tables_keep_rendered_order(self):
        for before, after, mark in (
            (["codex:high", "kiro:high"], ["codex:high", "codex:xhigh", "kiro:high"], "1, 2"),
            (["codex:high", "kiro:high", "codex:xhigh", "claude:high"],
             ["codex:high", "kiro:high", "codex:xhigh", "codex:max", "claude:high"], "1, xhigh:3, xhigh:4"),
        ):
            with self.subTest(before=before):
                state = press(roles_grid.RoleGrid([role("ops", "edit", before)]), "s")
                self.assertEqual(len(state.tables().get("ops", {}).get("providers", [])), len(before) + 1)
                self.assertIn(mark, table(state)[1])
                press(state, "e")
                self.assertEqual(state.press("enter"), roles_grid.DONE)
                self.assertEqual(state.tables(), {"ops": {"providers": after}})
                saved = roles_grid.RoleGrid([role("ops", "edit", after)])
                self.assertEqual(table(saved), table(state))

    def test_a_and_n_tick_and_clear_the_whole_role_row(self):
        state = press(grid(), "a", "a")
        self.assertEqual(len(state.tables()["reviewer"]["providers"]), 4)
        self.assertEqual(list(state.tables()), ["reviewer"])
        press(state, "n")
        self.assertEqual(state.press("enter"), None)  # an enabled role with no providers can't be saved
        self.assertIn("reviewer would have no providers left", state.problem)
        press(state, "o")
        self.assertEqual(state.press("enter"), roles_grid.DONE)
        self.assertEqual(state.tables(), {"reviewer": {"providers": [], "enabled": False}})

    def test_lines_name_every_change_and_the_keys(self):
        state = press(grid(), "down", "+", "down", "o")
        text = "\n".join(state.lines(width=180, style="plain"))
        self.assertIn("2 changes: builder: codex:work moves to 1st, claude:claude-work to 2nd · verifier: on", text)
        for phrase in ("o role on/off", "a/n whole row", "s split cell", "enter save", "esc quit"):
            self.assertIn(phrase, text)
        self.assertNotIn("duplicate row", text)
        self.assertEqual(state.press("esc"), roles_grid.QUIT)
        self.assertEqual(state.press("q"), roles_grid.QUIT)

    def test_columns_scroll_so_the_cursors_shows(self):
        found = [role("reviewer", "read", [f"codex:account-{index}" for index in range(8)])]
        state = roles_grid.RoleGrid(found)
        text = "\n".join(state.lines(width=80, style="plain"))
        self.assertIn("codex:account-0", text)
        self.assertNotIn("codex:account-7", text)
        self.assertIn("more accounts › (← → to see them)", text)
        press(state, *["right"] * 7)
        text = "\n".join(state.lines(width=80, style="plain"))
        self.assertIn("codex:account-7", text)
        self.assertNotIn("codex:account-0", text)
        self.assertIn("reviewer  read·on", text)
        self.assertRegex(text, r"‹ \d more accounts \(← → to see them\)")
        self.assertIsNone(state.cut(400))
        self.assertEqual(len(table(state)[0].split()), 9)  # read-only: every account

    def test_fallback_prints_the_table_read_only(self):
        state = grid()
        written = []
        self.assertEqual(state.fallback(input, written.append), roles_grid.QUIT)
        self.assertEqual(written[:-1], table(state))
        self.assertEqual(written[-1], roles_grid.HINT)


if __name__ == "__main__":
    unittest.main()
