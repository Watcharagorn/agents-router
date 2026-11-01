"""The roles table: provider entries as rows, roles as columns, read-only or as the `agt roles config` grid.

A row is one provider entry: runtime, account, model and effort. A read role's
cell is a tick; an edit role's cell is the entry's place in its order. The
"role on/off" row above the providers shows each role's switch. A role that is
off keeps its column and its providers, dimmed, so turning it back on restores
it as it was.

Keys in the grid: arrows (or h/j/k/l) move, Space ticks (on the on/off row it
flips the role), `a`/`n` tick or clear the whole column, `m`/`e` step the row's
model or effort through the choices the runtime allows, `+`/`-` move the row
earlier or later in an edit role's order, `d` copies the row (so one account can
serve one role on one model and another role on another), `o` turns the role
under the cursor on or off, Enter saves, Esc or `q` quits with nothing changed.

`RoleGrid` is plain state: `press` a key, read `lines`, then `tables()` for what
to save, so tests drive it with scripted keys. `share.picker.choose` runs it on
a terminal (it needs only `press`, `lines` and `fallback`).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable

from agents_router import roles, ui
from agents_router.share import picker

DONE, QUIT = picker.DONE, picker.QUIT
TICK, EMPTY, NONE = "✓", "·", "—"
HINT = "Read-only here: change roles with flags, e.g. agt roles config builder --order <provider>,<provider>"
ONOFF = "role on/off"
ORDINALS = {1: "1st", 2: "2nd", 3: "3rd"}

Choices = Callable[["Entry"], tuple[list[str | None], list[str | None]]]


@dataclass(frozen=True)
class Entry:
    """A grid row: one provider entry."""

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
class Column:
    name: str
    access: str
    enabled: bool


def _ordinal(number: int) -> str:
    return ORDINALS.get(number, f"{number}th")


def _default_choices(entry: Entry) -> tuple[list[str | None], list[str | None]]:
    return [None, *([entry.model] if entry.model else [])], [None, *roles.EFFORTS]


class RoleGrid:
    def __init__(self, found: list[roles.Role], choices: Choices | None = None, *, title: str = "agt roles config"):
        self.title = title
        self.choices = choices or _default_choices
        self.columns = [Column(role.name, role.access, role.enabled) for role in found]
        self.entries: list[Entry] = []
        self.ticks: dict[str, list[int]] = {}
        for role in found:
            keys: list[int] = []
            for provider in role.providers:
                entry = self._entry_for(provider)
                if entry.key not in keys:
                    keys.append(entry.key)
            self.ticks[role.name] = keys
        self.first_entries = {entry.key: entry for entry in self.entries}
        self.first_ticks = {name: list(keys) for name, keys in self.ticks.items()}
        self.first_enabled = {column.name: column.enabled for column in self.columns}
        self.row = 1 if self.entries else 0  # row 0 is the role on/off row
        self.column = 0
        self.problem: str | None = None

    # State -------------------------------------------------------------------

    def _entry_for(self, provider: roles.Provider) -> Entry:
        for entry in self.entries:
            if entry.provider == provider:
                return entry
        entry = Entry(len(self.entries) + 1, provider.runtime, provider.account, provider.model, provider.effort)
        self.entries.append(entry)
        return entry

    def _entry(self) -> Entry | None:
        return self.entries[self.row - 1] if self.row >= 1 and self.entries else None

    def _col(self) -> Column | None:
        return self.columns[self.column] if self.columns else None

    def _set_entry(self, entry: Entry) -> None:
        self.entries[self.row - 1] = entry

    def _toggle_role(self) -> None:
        column = self._col()
        if column is not None:
            self.columns[self.column] = replace(column, enabled=not column.enabled)

    def _step(self, values: list[str | None], current: str | None) -> str | None:
        if current not in values:
            values = [*values, current]
        return values[(values.index(current) + 1) % len(values)]

    def press(self, key: str) -> str | None:
        if key in ("q", "esc"):
            return QUIT
        if key == "enter":
            self.problem = self._check()
            return None if self.problem else DONE
        if not self.columns:
            return None
        self.problem = None
        column, entry = self._col(), self._entry()
        ticks = self.ticks.setdefault(column.name, [])
        if key in ("up", "k"):
            self.row = max(0, self.row - 1)
        elif key in ("down", "j"):
            self.row = min(len(self.entries), self.row + 1)
        elif key in ("left", "h"):
            self.column = max(0, self.column - 1)
        elif key in ("right", "l"):
            self.column = min(len(self.columns) - 1, self.column + 1)
        elif key == "o" or (key == "space" and entry is None):
            self._toggle_role()
        elif key == "space" and entry is not None:
            if entry.key in ticks:
                ticks.remove(entry.key)
            else:
                ticks.append(entry.key)
        elif key == "a":
            ticks.extend(item.key for item in self.entries if item.key not in ticks)
        elif key == "n":
            ticks.clear()
        elif key in ("+", "-") and entry is not None and column.access == "edit" and entry.key in ticks:
            at = ticks.index(entry.key)
            to = max(0, at - 1) if key == "+" else min(len(ticks) - 1, at + 1)
            ticks.insert(to, ticks.pop(at))
        elif key == "m" and entry is not None:
            self._set_entry(replace(entry, model=self._step(self.choices(entry)[0], entry.model)))
        elif key == "e" and entry is not None:
            self._set_entry(replace(entry, effort=self._step(self.choices(entry)[1], entry.effort)))
        elif key == "d" and entry is not None:
            copy = replace(entry, key=max(item.key for item in self.entries) + 1)
            self.entries.insert(self.row, copy)
            self.row += 1
        return None

    def _check(self) -> str | None:
        for column in self.columns:
            if column.enabled and not self.ticks.get(column.name):
                return f"{column.name} would have no providers left; o turns it off instead"
        return None

    def providers(self, name: str) -> list[str]:
        by_key = {entry.key: entry for entry in self.entries}
        return list(dict.fromkeys(str(by_key[key].provider) for key in self.ticks.get(name, [])))

    def _first_providers(self, name: str) -> list[str]:
        return [str(self.first_entries[key].provider) for key in self.first_ticks.get(name, [])]

    def tables(self) -> dict[str, dict]:
        """What to save: for each role that changed, its `providers` and `enabled`."""

        found = {}
        for column in self.columns:
            changed = {}
            if self.providers(column.name) != self._first_providers(column.name):
                changed["providers"] = self.providers(column.name)
            if column.enabled != self.first_enabled[column.name]:
                changed["enabled"] = column.enabled
            if changed:
                found[column.name] = changed
        return found

    def changes(self) -> list[str]:
        """Every change in words: "builder: codex:work moves to 1st · reviewer: codex:work joins · verifier: off"."""

        phrases = []
        labels = {entry.key: entry.label for entry in self.entries}
        labels.update({key: entry.label for key, entry in self.first_entries.items() if key not in labels})
        for column in self.columns:
            parts = []
            if column.enabled != self.first_enabled[column.name]:
                parts.append("on" if column.enabled else "off")
            before, after = self.first_ticks.get(column.name, []), self.ticks.get(column.name, [])
            joined = [labels[key] for key in after if key not in before]
            left = [labels[key] for key in before if key not in after]
            parts += [f"{label} joins" for label in joined] + [f"{label} leaves" for label in left]
            if column.access == "edit":
                kept_before = [key for key in before if key in after]
                kept_after = [key for key in after if key in before]
                if kept_before != kept_after:
                    moved = [key for index, key in enumerate(after) if key in before and
                             (key not in kept_before or kept_before.index(key) != kept_after.index(key))]
                    if moved:
                        parts.append(", ".join(f"{labels[key]} {'moves to' if index == 0 else 'to'} "
                                               f"{_ordinal(after.index(key) + 1)}" for index, key in enumerate(moved)))
            if parts:
                phrases.append(f"{column.name}: {', '.join(parts)}")
        for entry in self.entries:
            first = self.first_entries.get(entry.key)
            if first is None:
                continue
            if first.model != entry.model:
                phrases.append(f"{entry.label} model {first.model or 'default'} → {entry.model or 'default'}")
            if first.effort != entry.effort:
                phrases.append(f"{entry.label} effort {first.effort or 'default'} → {entry.effort or 'default'}")
        return phrases

    # Drawing -----------------------------------------------------------------

    def _cell(self, column: Column, entry: Entry) -> str:
        ticks = self.ticks.get(column.name, [])
        if entry.key not in ticks:
            return EMPTY
        return str(ticks.index(entry.key) + 1) if column.access == "edit" else TICK

    def _changed(self, column: Column, entry: Entry | None) -> bool:
        if entry is None:
            return column.enabled != self.first_enabled[column.name]
        before = self.first_ticks.get(column.name, [])
        return (entry.key in before) != (entry.key in self.ticks.get(column.name, [])) or (
            column.access == "edit" and self._cell(column, entry) != EMPTY
            and entry.key in before and before.index(entry.key) != self.ticks[column.name].index(entry.key))

    def _visible(self, widths: list[int], room: int | None) -> tuple[int, int]:
        """The columns that fit in `room` cells, scrolled so the cursor's column always shows."""

        if room is None or sum(widths) <= room:
            return 0, len(widths)
        first = next((first for first in range(self.column + 1) if sum(widths[first:self.column + 1]) <= room),
                     self.column)
        last = self.column + 1
        while last < len(widths) and sum(widths[first:last + 1]) <= room:
            last += 1
        return first, last

    def cut(self, width: int) -> str | None:
        """"‹ 1 more role · 2 more roles ›" when the columns don't all fit in `width`."""

        first, last = self._columns_at(width)
        parts = [f"‹ {first} more role{'s' if first != 1 else ''}"] if first else []
        rest = len(self.columns) - last
        parts += [f"{rest} more role{'s' if rest != 1 else ''} ›"] if rest else []
        return " · ".join(parts) + " (← → to see them)" if parts else None

    def _sizes(self) -> tuple[int, int, int, int, list[int]]:
        label_w = max([ui.cell_len(entry.label) for entry in self.entries] + [8])
        model_w = max([ui.cell_len(entry.model or NONE) for entry in self.entries] + [5])
        effort_w = max([ui.cell_len(entry.effort or NONE) for entry in self.entries] + [6])
        lead = label_w + 2 + model_w + 2 + effort_w + 2
        return label_w, model_w, effort_w, lead, [max(ui.cell_len(column.name), 3) + 2 for column in self.columns]

    def _columns_at(self, width: int | None) -> tuple[int, int]:
        lead, widths = self._sizes()[3:]
        return self._visible(widths, None if width is None else width - 4 - lead)

    def table(self, cursor: bool = False, width: int | None = None) -> list[ui.Line]:
        """The table's lines: header, the role on/off row, one row per provider entry. With `width`, only the
        columns that fit, around the cursor's."""

        label_w, model_w, effort_w, lead, widths = self._sizes()
        first, last = self._columns_at(width)
        shown = list(range(first, last))
        header: list[ui.Piece] = ["    " + " " * lead]
        for index in shown:
            column = self.columns[index]
            header += [ui.bold(column.name) if column.enabled else ui.muted(column.name),
                       " " * (widths[index] - ui.cell_len(column.name))]
        lines: list[ui.Line] = [header]
        for row in range(len(self.entries) + 1):
            entry = self.entries[row - 1] if row else None
            here = cursor and row == self.row
            if entry is None:
                head = ONOFF.ljust(lead)
            else:
                head = (entry.label.ljust(label_w) + "  " + (entry.model or NONE).ljust(model_w) + "  "
                        + (entry.effort or NONE).ljust(effort_w) + "  ")
            line: list[ui.Piece] = ["  ", ui.bold("▸ ") if here else "  ", head]
            for index in shown:
                column, cell_w = self.columns[index], widths[index]
                mark = ("on" if column.enabled else "off") if entry is None else self._cell(column, entry)
                style = "changed" if self._changed(column, entry) else "" if column.enabled else "muted"
                if here and index == self.column:
                    style = "cursor-changed" if style == "changed" else "cursor"
                line += [ui.Span(style, mark) if style else mark, " " * (cell_w - ui.cell_len(mark))]
            lines.append(line)
        return lines

    def frame(self, width: int = 100, height: int = 40) -> list[ui.Line]:
        if not self.columns:
            return [ui.bold(self.title), "  No roles yet: agt setup proposes them."]
        rows = self.table(cursor=True, width=width)
        phrases = self.changes()
        bottom: list[tuple[str, ui.Line]] = [("blank", "")]
        cut = self.cut(width)
        if cut:
            bottom.append(("", ["  ", ui.muted(cut)]))
        if self.problem:
            bottom.append(("", ["  ", ui.Span("error", self.problem)]))
        bottom.append(("", picker._count(phrases, len(phrases), width, " · ")))
        keys = [("↑↓←→", "move"), ("space", "tick"), ("a/n", "whole column"), ("m", "model"), ("e", "effort"),
                ("+/-", "order"), ("o", "role on/off"), ("d", "duplicate row"), ("enter", "save"), ("esc", "quit")]
        body = [(line, True) for line in rows[1:]]
        return picker._frame([("title", ui.bold(self.title)), ("blank", ""), ("", rows[0])], body, self.row,
                             bottom, keys, width, height)

    def lines(self, width: int = 100, height: int = 40, *, style: str = "color") -> list[str]:
        return picker._draw(self.frame(width, height), width, style)

    def fallback(self, read: Callable[[str], str], write: Callable[[str], None]) -> str:  # noqa: ARG002
        """A terminal that can't send single keys gets the table read-only and the flag form."""

        for line in self.table():
            write(ui.plain(line).rstrip())
        write(HINT)
        return QUIT
