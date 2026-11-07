"""The roles table: roles as rows, accounts as columns, read-only or as the `agt roles config` grid.

Each role's lead field shows its access and on/off switch. An account's cell
shows its model, any effort differing from the shared effort below the table,
and a tick for read access or its place in an edit/full role's try order.
Several entries for one account share a cell. Off roles keep their providers,
dimmed, so turning one back on restores it as it was.

Keys in the grid: arrows (or h/j/k/l) move, Space ticks the account, `a`/`n`
tick or clear the whole role row, `m` picks the cell's last entry's model,
`e` steps its effort, `+`/`-` move the account's entries as a block in the try order, `s`
splits a cell by appending a copy of its last entry, and `o` toggles the role.
Enter saves; Esc or `q` quits with nothing changed.

`RoleGrid` is plain state: `press` a key, read `lines`, then `tables()` for what
to save, so tests drive it with scripted keys. `share.picker.choose` runs it on
a terminal (it needs only `press`, `lines` and `fallback`).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from typing import Callable

from agents_router import roles, ui
from agents_router.share import picker

DONE, QUIT = picker.DONE, picker.QUIT
TICK, EMPTY = "✓", "·"
HINT = "Read-only here: change roles with flags, e.g. agt roles config builder --order <provider>,<provider>"
ORDINALS = {1: "1st", 2: "2nd", 3: "3rd"}

Choices = Callable[["Entry"], tuple[list[str | None], list[str | None]]]
ModelChoices = Callable[["Entry", bool], tuple[list[picker.Choice], bool]]


@dataclass(frozen=True)
class Entry:
    """One provider entry in a role; several can share an account cell."""

    key: int
    runtime: str
    account: str | None = None
    model: str | None = None
    effort: str | None = None

    @property
    def provider(self) -> roles.Provider:
        return roles.Provider(self.runtime, self.account, self.model, self.effort)

    @property
    def label(self) -> str:
        return self.provider.entry()


@dataclass(frozen=True)
class Row:
    name: str
    access: str
    enabled: bool


@dataclass(frozen=True)
class Column:
    runtime: str
    account: str | None = None

    @property
    def label(self) -> str:
        return roles.Provider(self.runtime, self.account).entry()


def _ordinal(number: int) -> str:
    return ORDINALS.get(number, f"{number}th")


def _cell_tail(text: str, width: int) -> str:
    """Fit a cell to its column, keeping the last (editable) entry visible."""

    if ui.cell_len(text) <= width:
        return text
    tail, used = [], 1  # the leading ellipsis takes one cell
    for char in reversed(text):
        used += ui.cell_len(char)
        if used > width:
            break
        tail.append(char)
    return "…" + "".join(reversed(tail)) if width > 0 else ""


def _default_choices(entry: Entry) -> tuple[list[str | None], list[str | None]]:
    return [None, *([entry.model] if entry.model else [])], [None, *roles.EFFORTS]


class RoleGrid:
    """Role rows × account columns, sharing state and rendering for show and config."""

    def __init__(self, found: list[roles.Role], choices: Choices | None = None, *, title: str = "agt roles config",
                 model_choices: ModelChoices | None = None):
        self.title = title
        self.choices = choices or _default_choices
        self.model_choices = model_choices
        self.model_picker: picker.ListState | None = None
        self.model_entry: Entry | None = None
        self.pending = False
        self.rows = [Row(role.name, role.access, role.enabled) for role in found]
        self.columns: list[Column] = []
        self.entries: list[Entry] = []
        self.ticks: dict[str, list[int]] = {}
        for role in found:
            keys: list[int] = []
            for provider in role.providers:
                column = Column(provider.runtime, provider.account)
                if column not in self.columns:
                    self.columns.append(column)
                keys.append(self._add_entry(provider).key)
            self.ticks[role.name] = keys
        self.first_entries = {entry.key: entry for entry in self.entries}
        self.first_ticks = {name: list(keys) for name, keys in self.ticks.items()}
        self.first_enabled = {row.name: row.enabled for row in self.rows}
        self.row = 0
        self.column = 0
        self.problem: str | None = None

    # State -------------------------------------------------------------------

    def _add_entry(self, provider: roles.Provider) -> Entry:
        entry = Entry(len(self.entries) + 1, provider.runtime, provider.account, provider.model, provider.effort)
        self.entries.append(entry)
        return entry

    def _entry(self) -> Entry | None:
        row, column = self._row(), self._col()
        entries = self._cell_entries(row, column) if row and column else []
        return entries[-1] if entries else None

    def _row(self) -> Row | None:
        return self.rows[self.row] if self.rows else None

    def _col(self) -> Column | None:
        return self.columns[self.column] if self.columns else None

    def _set_entry(self, entry: Entry) -> None:
        self.entries[entry.key - 1] = entry

    def _cell_entries(self, row: Row, column: Column) -> list[Entry]:
        return [self.entries[key - 1] for key in self.ticks[row.name]
                if self._account(self.entries[key - 1]) == column]

    @staticmethod
    def _account(entry: Entry) -> Column:
        return Column(entry.runtime, entry.account)

    def _tick(self, row: Row, column: Column) -> None:
        """Add an unused account with its most common model/effort in the other roles."""

        if self._cell_entries(row, column):
            return
        used = Counter((entry.model, entry.effort) for other in self.rows if other.name != row.name
                       for entry in self._cell_entries(other, column))
        model, effort = used.most_common(1)[0][0] if used else (None, None)
        entry = self._add_entry(roles.Provider(column.runtime, column.account, model, effort))
        ticks = self.ticks[row.name]
        insert = len(ticks)
        if row.access == "read":
            # Parallel providers have no try order: restore an original account
            # ahead of later originals and new accounts, which stay at the end.
            positions = {key: index for index, key in enumerate(self.first_ticks[row.name])}
            accounts: dict[Column, int] = {}
            for key, index in positions.items():
                accounts.setdefault(self._account(self.first_entries[key]), index)
            if column in accounts:
                insert = next((index for index, key in enumerate(ticks) if
                               positions.get(key, accounts.get(self._account(self.entries[key - 1]), len(positions)))
                               > accounts[column]), insert)
        ticks.insert(insert, entry.key)

    def _move(self, row: Row, column: Column, key: str) -> None:
        """Move this account's entries as one block past one neighboring entry."""

        ticks = self.ticks[row.name]
        block = [entry.key for entry in self._cell_entries(row, column)]
        if not block:
            return
        at = ticks.index(block[0])
        rest = [item for item in ticks if item not in block]
        insert = at - 1 if key == "+" else at + 1
        if not 0 <= insert <= len(rest):
            return
        ticks[:] = rest[:insert] + block + rest[insert:]

    def _toggle_role(self) -> None:
        row = self._row()
        if row is not None:
            self.rows[self.row] = replace(row, enabled=not row.enabled)

    def _step(self, values: list[str | None], current: str | None) -> str | None:
        if current not in values:
            values = [*values, current]
        return values[(values.index(current) + 1) % len(values)]

    def _models(self, entry: Entry, request: bool) -> tuple[list[picker.Choice], bool]:
        if self.model_choices:
            return self.model_choices(entry, request)
        models = list(dict.fromkeys([*self.choices(entry)[0], entry.model]))
        return [picker.Choice(model or "", model or f"default ({entry.runtime}'s own)", checked=model == entry.model)
                for model in models], False

    def _pick_model(self, entry: Entry) -> None:
        choices, self.pending = self._models(entry, True)
        self.model_entry = entry
        self.model_picker = picker.ListState(f"model · {self._row().name} · {entry.label}", choices, single=True)

    def poll(self) -> bool:
        if not self.model_picker or not self.pending:
            return False
        choices, self.pending = self._models(self.model_entry, False)
        if choices == self.model_picker.choices:
            return False
        self.model_picker.replace_choices(choices)
        return True

    def press(self, key: str) -> str | None:
        if self.model_picker is not None:
            outcome = self.model_picker.press(key)
            if outcome == DONE:
                model = next(iter(self.model_picker.checked()))
                self._set_entry(replace(self.model_entry, model=model or None))
            if outcome:
                self.model_picker = None
                self.model_entry = None
                self.pending = False
            return None
        if key in ("q", "esc"):
            return QUIT
        if key == "enter":
            self.problem = self._check()
            return None if self.problem else DONE
        if not self.rows:
            return None
        self.problem = None
        row, column, entry = self._row(), self._col(), self._entry()
        ticks = self.ticks[row.name]
        if key in ("up", "k"):
            self.row = max(0, self.row - 1)
        elif key in ("down", "j"):
            self.row = min(len(self.rows) - 1, self.row + 1)
        elif key in ("left", "h"):
            self.column = max(0, self.column - 1)
        elif key in ("right", "l"):
            self.column = min(max(len(self.columns) - 1, 0), self.column + 1)
        elif key == "o":
            self._toggle_role()
        elif key == "space" and column is not None:
            if entry is not None:
                remove = {item.key for item in self._cell_entries(row, column)}
                ticks[:] = [item for item in ticks if item not in remove]
            else:
                self._tick(row, column)
        elif key == "a":
            for account in self.columns:
                self._tick(row, account)
        elif key == "n":
            ticks.clear()
        elif key in ("+", "-") and entry is not None and row.access in roles.ONE_AT_A_TIME:
            self._move(row, column, key)
        elif key == "m" and entry is not None:
            self._pick_model(entry)
        elif key == "e" and entry is not None:
            self._set_entry(replace(entry, effort=self._step(self.choices(entry)[1], entry.effort)))
        elif key == "s" and entry is not None:
            ticks.insert(ticks.index(entry.key) + 1, self._add_entry(entry.provider).key)
        return None

    def _check(self) -> str | None:
        for row in self.rows:
            if row.enabled and not self.ticks.get(row.name):
                return f"{row.name} would have no providers left; o turns it off instead"
            seen: set[roles.Provider] = set()
            for key in self.ticks[row.name]:
                entry = self.entries[key - 1]
                if entry.provider in seen:
                    return f"{row.name}: duplicate {entry.label}; change copy with m/e or untick"
                seen.add(entry.provider)
        return None

    def providers(self, name: str) -> list[str]:
        by_key = {entry.key: entry for entry in self.entries}
        return [str(by_key[key].provider) for key in self.ticks.get(name, [])]

    def _first_providers(self, name: str) -> list[str]:
        return [str(self.first_entries[key].provider) for key in self.first_ticks.get(name, [])]

    def tables(self) -> dict[str, dict]:
        """What to save: for each role that changed, its `providers` and `enabled`."""

        found = {}
        for row in self.rows:
            changed = {}
            if self.providers(row.name) != self._first_providers(row.name):
                changed["providers"] = self.providers(row.name)
            if row.enabled != self.first_enabled[row.name]:
                changed["enabled"] = row.enabled
            if changed:
                found[row.name] = changed
        return found

    def changes(self) -> list[str]:
        """Every change in words: "builder: codex:work moves to 1st · reviewer: codex:work joins · verifier: off"."""

        phrases = []
        changed = self.tables()
        labels = {entry.key: entry.label for entry in self.entries}
        labels.update({key: entry.label for key, entry in self.first_entries.items() if key not in labels})
        for row in self.rows:
            if row.name not in changed:
                continue
            parts = []
            if row.enabled != self.first_enabled[row.name]:
                parts.append("on" if row.enabled else "off")
            before = self.first_ticks[row.name]
            current = self.ticks[row.name]
            # Keep surviving entries' keys, including edited ones. A re-tick may
            # match a removed original, but a split can't reuse a key still in use.
            unmatched = set(before) - set(current)
            after = []
            for key in current:
                match = key if key in before else next((first for first in before if first in unmatched and
                    self.first_entries[first].provider == self.entries[key - 1].provider), key)
                unmatched.discard(match)
                after.append(match)
            if "providers" not in changed[row.name]:
                after = before
            joined = [labels[key] for key in after if key not in before]
            left = [labels[key] for key in before if key not in after]
            parts += [f"{label} joins" for label in joined] + [f"{label} leaves" for label in left]
            if row.access in roles.ONE_AT_A_TIME:
                kept_before = [key for key in before if key in after]
                kept_after = [key for key in after if key in before]
                if kept_before != kept_after:
                    moved = [key for index, key in enumerate(after) if key in before and
                             (key not in kept_before or kept_before.index(key) != kept_after.index(key))]
                    if moved:
                        parts.append(", ".join(f"{labels[key]} {'moves to' if index == 0 else 'to'} "
                                               f"{_ordinal(after.index(key) + 1)}" for index, key in enumerate(moved)))
            if parts:
                phrases.append(f"{row.name}: {', '.join(parts)}")
        for entry in self.entries:
            first = self.first_entries.get(entry.key)
            if first is None or not any("providers" in changed.get(row.name, {}) and entry.key in self.ticks[row.name]
                                        for row in self.rows):
                continue
            if first.model != entry.model:
                phrases.append(f"{entry.label} model {first.model or 'default'} → {entry.model or 'default'}")
            if first.effort != entry.effort:
                phrases.append(f"{entry.label} effort {first.effort or 'default'} → {entry.effort or 'default'}")
        return phrases

    # Drawing -----------------------------------------------------------------

    def _shared_effort(self) -> str | None:
        used = Counter(self.entries[key - 1].effort for row in self.rows for key in self.ticks[row.name])
        return used.most_common(1)[0][0] if used else None

    def _effort_note(self) -> str | None:
        if not any(self.ticks.values()):
            return None
        effort = self._shared_effort() or "the runtime's default"
        return f"  Effort is {effort} unless a cell shows one (def = the runtime's default). · = not used."

    def _cell(self, row: Row, column: Column, shared_effort: str | None) -> str:
        ticks = self.ticks[row.name]
        tokens = []
        for entry in self._cell_entries(row, column):
            mark = str(ticks.index(entry.key) + 1) if row.access in roles.ONE_AT_A_TIME else TICK
            effort = f"{entry.effort or 'def'}:" if entry.effort != shared_effort else ""
            tokens.append((f"{entry.model}/" if entry.model else "") + effort + mark)
        return ", ".join(tokens) or EMPTY

    def _changed(self, row: Row, column: Column) -> bool:
        before = [(entry.provider, index if row.access in roles.ONE_AT_A_TIME else None)
                  for index, key in enumerate(self.first_ticks[row.name])
                  if self._account(entry := self.first_entries[key]) == column]
        after = [(entry.provider, index if row.access in roles.ONE_AT_A_TIME else None)
                 for index, key in enumerate(self.ticks[row.name])
                 if self._account(entry := self.entries[key - 1]) == column]
        return before != after

    def _visible(self, widths: list[int], room: int | None) -> tuple[int, int]:
        """The columns that fit in `room` cells, scrolled so the cursor's column always shows."""

        if not widths or room is None or sum(widths) <= room:
            return 0, len(widths)
        first = next((first for first in range(self.column + 1) if sum(widths[first:self.column + 1]) <= room),
                     self.column)
        last = self.column + 1
        while last < len(widths) and sum(widths[first:last + 1]) <= room:
            last += 1
        return first, last

    def cut(self, width: int) -> str | None:
        """"‹ 1 more account · 2 more accounts ›" when columns don't all fit in `width`."""

        first, last = self._columns_at(width)
        parts = [f"‹ {first} more account{'s' if first != 1 else ''}"] if first else []
        rest = len(self.columns) - last
        parts += [f"{rest} more account{'s' if rest != 1 else ''} ›"] if rest else []
        return " · ".join(parts) + " (← → to see them)" if parts else None

    def _sizes(self) -> tuple[int, int, list[int]]:
        label_w = max([ui.cell_len(row.name) for row in self.rows] + [4])
        lead = label_w + 2 + len("read·off") + 2
        effort = self._shared_effort()
        widths = [max([ui.cell_len(column.label), 3] +
                      [ui.cell_len(self._cell(row, column, effort)) for row in self.rows]) + 2
                  for column in self.columns]
        return label_w, lead, widths

    def _columns_at(self, width: int | None) -> tuple[int, int]:
        lead, widths = self._sizes()[1:]
        return self._visible(widths, None if width is None else width - 4 - lead)

    def table(self, cursor: bool = False, width: int | None = None) -> list[ui.Line]:
        """Header, one row per role, then the shared-effort footnote. With `width`,
        scroll account columns around the cursor while keeping the role lead pinned."""

        label_w, lead, widths = self._sizes()
        effort = self._shared_effort()
        first, last = self._columns_at(width)
        shown = list(range(first, last))
        if width is not None:
            widths = [min(cell_w, max(width - 4 - lead, 0)) for cell_w in widths]
        header: list[ui.Piece] = ["    ", ui.bold("Role"), " " * (lead - 4)]
        for index in shown:
            label = _cell_tail(self.columns[index].label, max(widths[index] - 2, 0))
            header += [ui.bold(label), " " * (widths[index] - ui.cell_len(label))]
        lines: list[ui.Line] = [header]
        for at, row in enumerate(self.rows):
            here = cursor and at == self.row
            status = f"{row.access}·{'on' if row.enabled else 'off'}"
            head = row.name + " " * (label_w - ui.cell_len(row.name) + 2) + status.ljust(8) + "  "
            lead_style = "changed" if row.enabled != self.first_enabled[row.name] else "" if row.enabled else "muted"
            line: list[ui.Piece] = ["  ", ui.bold("▸ ") if here else "  ",
                                   ui.Span(lead_style, head) if lead_style else head]
            for index in shown:
                column, cell_w = self.columns[index], widths[index]
                mark = _cell_tail(self._cell(row, column, effort), max(cell_w - 2, 0))
                style = "changed" if self._changed(row, column) else "" if row.enabled else "muted"
                if here and index == self.column:
                    style = "cursor-changed" if style == "changed" else "cursor"
                line += [ui.Span(style, mark) if style else mark, " " * (cell_w - ui.cell_len(mark))]
            lines.append(line)
        if note := self._effort_note():
            lines.append([ui.muted(note)])
        return lines

    def frame(self, width: int = 100, height: int = 40) -> list[ui.Line]:
        if self.model_picker is not None:
            return self.model_picker.frame(width, height)
        budget = max(height - 1, 1)
        if not self.rows:
            return [ui.bold(self.title), "  No roles yet: agt setup proposes them."][:budget]
        rows = self.table(cursor=True, width=width)
        phrases = self.changes()
        bottom: list[tuple[str, ui.Line]] = []
        if note := self._effort_note():
            # Cells omit the shared effort, so its note must survive scrolling.
            bottom.append(("", [ui.muted(note)]))
        bottom.append(("blank", ""))
        cut = self.cut(width)
        if cut:
            bottom.append(("notes", ["  ", ui.muted(cut)]))
        if self.problem:
            bottom.append(("", ["  ", ui.Span("error", self.problem)]))
        bottom.append(("notes", picker._count(phrases, len(phrases), width, " · ")))
        keys = [("↑↓←→", "move"), ("space", "tick"), ("a/n", "whole row"), ("m", "pick model"), ("e", "effort"),
                ("+/-", "order"), ("o", "role on/off"), ("s", "split cell"), ("enter", "save"), ("esc", "quit")]
        body = [(line, True) for line in rows[1:len(self.rows) + 1]]
        frame = picker._frame([("title", ui.bold(self.title)), ("blank", ""), ("", rows[0])], body, self.row,
                              bottom, keys, width, height)
        if len(frame) > budget:
            # Even the picker's compact keys and header may not fit. Keep the
            # cursor row and effort note before spending lines on anything else.
            frame = [rows[self.row + 1], *(line for tag, line in bottom if not tag)]
            if len(frame) < budget:
                frame.insert(0, rows[0])
        return frame[:budget]

    def lines(self, width: int = 100, height: int = 40, *, style: str = "color") -> list[str]:
        return picker._draw(self.frame(width, height), width, style)

    def fallback(self, read: Callable[[str], str], write: Callable[[str], None]) -> str:  # noqa: ARG002
        """A terminal that can't send single keys gets the table read-only and the flag form."""

        for line in self.table():
            write(ui.plain(line).rstrip())
        write(HINT)
        return QUIT
