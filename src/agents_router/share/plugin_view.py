"""Plugins for a person: `agt share plugins`, the plugin part of `kinds add plugins` and its reports, and
`pick plugins/<id>` with no flags.

The JSON (`summary()`, the offers, the attention entries) is a contract for agents, so this builds its own words
from the same plan and never changes them.
"""

from __future__ import annotations

from typing import Any

from agents_router import ui
from agents_router.share import attention, plugins
from agents_router.share import status_view


def _and(names: list[str]) -> str:
    return status_view._and(names)


# Pieces ----------------------------------------------------------------------------------------


def waits(shared: plugins.Plan) -> list[dict[str, Any]]:
    """The plugin waits as status lists them: one per answer, however many CLIs it covers."""

    return attention.grouped(plugins.attention(shared))


def wait_lines(plan: Any, shared: plugins.Plan) -> list[ui.Line]:
    found = waits(shared)
    return status_view.wait_lines([(entry, None) for entry in found], plan) if found else []


def shared_count(shared: plugins.Plan) -> int:
    return sum(1 for entry in shared.entries if not entry.held and entry.decision)


def _held_label(entry: plugins.Entry) -> str:
    """A held plugin's reason as a row label: "Come with Codex", "Not on in claude", "You turned off"."""

    if entry.held == "you turned it off":
        return "You turned off"
    if entry.held.startswith("it comes with Codex"):
        return "Come with Codex"
    if entry.held.startswith("it isn't on in "):
        return "Not on in " + entry.held.removeprefix("it isn't on in ")
    return entry.held[:1].upper() + entry.held[1:]


def _held_short(entry: plugins.Entry) -> str:
    """The same reason inside a sentence: "comes with Codex", "not on in claude", "you turned it off"."""

    if entry.held.startswith("it comes with Codex"):
        return "comes with Codex"
    if entry.held.startswith("it isn't on in "):
        return "not on in " + entry.held.removeprefix("it isn't on in ")
    return entry.held


def _cant(shared: plugins.Plan) -> list[tuple[str, list[str], str]]:
    """(plugin, the CLIs it can't be installed in, why), one per plugin and reason."""

    groups: dict[tuple[str, str], list[str]] = {}
    for entry in shared.entries:
        if not entry.held:
            for label, why in sorted(entry.stays.items()):
                groups.setdefault((entry.name, why), []).append(label)
    return [(name, labels, why) for (name, why), labels in groups.items()]


def cant_lines(shared: plugins.Plan) -> list[ui.Line]:
    """"Can't be installed there": each plugin, where, and why, in full."""

    found = _cant(shared)
    if not found:
        return []
    rows = [[name, ui.muted(", ".join(labels)), ui.inline(status_view._sentence(why))] for name, labels, why in found]
    return [ui.bold("Can't be installed there"), *ui.columns(rows, gap=3)]


def short_lines(shared: plugins.Plan) -> list[ui.Line]:
    """What stays where it is and what can't go somewhere, one muted sentence each, and where to read why."""

    lines: list[ui.Line] = []
    held = [entry for entry in shared.entries if entry.held]
    if held:
        lines.append(ui.muted("Staying where they are: " + ", ".join(f"{entry.name} ({_held_short(entry)})"
                                                                     for entry in held) + "."))
    found = _cant(shared)
    if found:
        lines.append(ui.muted("Can't be installed there: " + ", ".join(f"{name} ({', '.join(labels)})"
                                                                      for name, labels, _ in found) + "."))
    if lines:
        lines.append([ui.code("agt share plugins"), ui.muted(" says why.")])
    return lines


def install_rows(shared: plugins.Plan) -> list[ui.Line]:
    """"Plugins to install": each plugin, the CLIs it goes to, and what to know about each install under it."""

    order: dict[str, list[str]] = {}
    by_id = {entry.id: entry for entry in shared.entries}
    for entry, label in shared.writes:
        order.setdefault(entry.id, []).append(label)
    rows, under = [], []
    for plugin_id, labels in order.items():
        entry = by_id[plugin_id]
        rows.append([[ui.Span("ok-mark", "+"), " ", plugin_id], ui.muted(", ".join(labels))])
        notes = []
        for label in labels:
            notes += [f"In {label}, {note}." for note in entry.notes.get(label, [])]
            if label in entry.approved:
                notes.append(f"In {label}, you said yes to this: {'; '.join(entry.approved[label])}.")
        under.append(notes)
    if not rows:
        return []
    lines: list[ui.Line] = [ui.bold("Plugins to install")]
    column = max(ui.cell_len(ui.plain(row[0])) for row in rows) + 5
    for line, notes in zip(ui.columns(rows, gap=3), under):
        lines += [line, *([" " * column, ui.muted(note)] for note in notes)]
    return lines


def next_line(written: list[Any], by_label: dict[str, Any]) -> list[ui.Piece]:
    """The report's last line when a CLI got an agent, a skill or a plugin: a new session loads it, and Claude
    Code can reload plugins in place."""

    reload = any(outcome.kind == plugins.KIND and outcome.action == "copy"
                 and any(getattr(by_label.get(label), "cli", None) == "claude" for label in outcome.labels)
                 for outcome in written)
    return [ui.bold("Next"), ": start a new session in a CLI"
            + (" (or run /reload-plugins in Claude Code)" if reload else "") + " to use what it got."]


# `agt share plugins` --------------------------------------------------------------------------


def page(plan: Any, shared: plugins.Plan) -> list[ui.Line]:
    """With plugins sharing: where each goes, what waits, what stays and why."""

    head: list[ui.Piece] = [ui.bold("agt share plugins"), "  "]
    if not shared.entries and not shared.problems:
        return [head + ["on: ", ui.muted("none in the folders sharing")]]
    found = waits(shared)
    count = shared_count(shared)
    head += ["on: ", f"{count} shared", ", ", ui.warn(f"{len(found)} thing{'s wait' if len(found) != 1 else ' waits'} for you")] \
        if found else ["on: ", ui.ok(f"{count} shared")]
    lines: list[ui.Line] = [head]
    if found:
        lines += ["", *status_view.wait_lines([(entry, None) for entry in found], plan)]
    held: dict[str, list[str]] = {}
    for entry in shared.entries:
        if entry.held:
            held.setdefault(_held_label(entry), []).append(entry.name)
    if held:
        lines += ["", ui.bold("Staying where they are"),
                  *ui.columns([[ui.muted(label), ", ".join(names)] for label, names in held.items()], gap=3)]
    cant = cant_lines(shared)
    lines += ["", *cant] if cant else []
    narrow = {label: sorted(plugin_id.partition("@")[0] for plugin_id in holding.narrow)
              for label, holding in sorted(shared.holdings.items()) if holding.narrow}
    if narrow:
        lines += ["", ui.bold("Installed for one project only, so not shared"),
                  *ui.columns([[label, ui.muted(", ".join(names))] for label, names in narrow.items()], gap=3)]
    if shared.others:
        many = len(shared.others) > 1
        lines += ["", ui.muted(f"{_and(shared.others)} install{'' if many else 's'} no plugins; plugins go between "
                               "Claude Code and Codex folders only.")]
    return lines


def offers_page(found: list[plugins.Offer]) -> list[ui.Line]:
    """With plugins off: each plugin another CLI could install, what it would miss there, and the ways to start."""

    ready = [offer for offer in found if not offer.why_not]
    head: list[ui.Piece] = [ui.bold("agt share plugins"), "  "]
    head += [f"off; {len(ready)} install{'s' if len(ready) != 1 else ''} could go to another CLI"] if ready \
        else ["off; nothing another CLI could install"]
    lines: list[ui.Line] = [head]
    by_name: dict[str, list[plugins.Offer]] = {}
    for offer in ready:
        by_name.setdefault(offer.plugin.name, []).append(offer)
    if by_name:
        width = max(ui.cell_len(name) for name in by_name) + 2
        lines.append("")
        for name, offers in by_name.items():
            lines.append(["  ", ui.bold(name), " " * (width - ui.cell_len(name)),
                          ui.muted(", ".join(offer.label for offer in offers))])
            for offer in offers:
                if offer.asks:
                    lines.append([" " * (width + 2), ui.muted(f"In {offer.label}, " + "; ".join(offer.asks) + ".")])
                if offer.failed:
                    lines.append([" " * (width + 2), ui.muted(f"In {offer.label}, the last install failed: "),
                                  *ui.inline(offer.failed)])
    cant: dict[tuple[str, str], list[str]] = {}
    for offer in found:
        if offer.why_not:
            cant.setdefault((offer.plugin.name, offer.why_not), []).append(offer.label)
    if cant:
        rows = [[name, ui.muted(", ".join(labels)), ui.inline(status_view._sentence(why))] for (name, why), labels in cant.items()]
        lines += ["", ui.bold("Can't be installed there"), *ui.columns(rows, gap=3)]
    rows = [[ui.muted("Install them with each sync"), ui.cmd("agt share kinds add plugins")],
            [ui.muted("Choose one at a time now"), ui.cmd("agt share plugins apply")]] if ready \
        else [[ui.muted("Share plugins from now on"), ui.cmd("agt share kinds add plugins")]]
    return lines + ["", *ui.columns(rows, gap=3)]


# `agt share pick plugins/<id>` ------------------------------------------------------------------


def pick_lines(entry: plugins.Entry, shared: plugins.Plan) -> list[ui.Line]:
    """Where the plugin is and what each other folder would do with it, then the commands that change it: the
    answer a folder waits for first."""

    rows: list[list[ui.Line]] = []
    first: list[list[ui.Line]] = []
    for label in sorted(shared.by_label):
        copy = entry.copies.get(label)
        if copy:
            state: ui.Line = "has it" + ("" if copy.enabled is not False else ", turned off")
        elif label in entry.asks:
            state = [ui.Span("warn-mark", "!"), " waits for your answer: ", status_view._sentence("; ".join(entry.asks[label]))]
            first += [[ui.muted("Install it as it is"), ui.cmd(f"agt share pick {entry.key} --in {label}")],
                      [ui.muted("Keep it out"), ui.cmd(f"agt share pick {entry.key} --not-in {label}")]]
        elif label in entry.failing:
            state = [ui.Span("error-mark", "✗"), " the last install failed: ", *ui.inline(entry.failing[label])]
            first += [[ui.muted("Try again now"), ui.cmd(f"agt share pick {entry.key} --in {label}")],
                      [ui.muted("Keep it out"), ui.cmd(f"agt share pick {entry.key} --not-in {label}")]]
        elif label in entry.stays:
            state = ["can't: ", *ui.inline(status_view._sentence(entry.stays[label]))]
        elif label in entry.sources:
            state = "goes there" + (f"; you said yes to: {'; '.join(entry.approved[label])}" if label in entry.approved else "")
        else:
            state = ui.muted("doesn't have it")
        rows.append([label, state])
    rows += [[label, ui.muted("installs no plugins")] for label in shared.others]
    lines: list[ui.Line] = [[ui.bold(f"agt share pick {entry.key}"), "  ", ui.muted("where it goes; nothing changed")], "",
                            *ui.columns(rows, gap=3)]
    if entry.held:
        lines += ["", ui.muted(f"It stays where it is: {entry.held}.")]
    generic = [[ui.muted("Add a CLI"), ui.cmd(f"agt share pick {entry.key} --in <cli>")],
               [ui.muted("Take a CLI out"), ui.cmd(f"agt share pick {entry.key} --not-in <cli>")],
               [ui.muted("Only these CLIs"), ui.cmd(f"agt share pick {entry.key} --only <cli>[,<cli>]")]]
    return lines + ["", ui.bold("Change it"), *ui.columns(first + generic, gap=3)]
