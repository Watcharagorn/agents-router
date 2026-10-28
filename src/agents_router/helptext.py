"""agt's argument parser: help people can scan, and errors in one shape with a did-you-mean.

`agt --help` and `agt share --help` are pages of their own, with the commands in groups and a few
examples; the commands that hosts and the watcher run are named on one line at the end. Every other
command's help is argparse's, drawn by rich-argparse for a person at a terminal. A mistake on the command
line prints what's wrong, the closest command when there is one, and how to get help, on stderr with
exit 2 (hooks pass 2 through, so it never changes).
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from typing import Any

from agents_router import ui

# Commands people don't type: hosts, the watcher and status lines run them.
INTERNAL = {"_runner", "hook", "mcp", "hook-run", "session", "saved", "watch", "badge"}

ROOT = {
    "name": "agt",
    "about": "Agents Router: run tasks in other agent CLIs under your own accounts",
    "usage": "agt <command> [options]",
    "usage_note": [ui.code("agt"), " on its own shows what's set up and what to do next."],
    "sections": [
        ("Get started", [
            ("setup", "Install the plugin in Claude Code, Codex and Antigravity; register account folders"),
            ("doctor", "Check binaries, logins and shared setup"),
            ("update", "Reinstall agt and update the plugin in every host that has it"),
        ]),
        ("Run tasks in another CLI", [
            ("run", "Run a task and print the result"),
            ("start", "Start a task in the background"),
            ("follow", "Print a job's steps as they happen, then its result"),
            ("jobs", "List recent jobs started in this workspace"),
            ("status", "Show a job's state"),
            ("result", "Show a job's result"),
            ("wait", "Wait until a job finishes or sends a message"),
            ("message", "Send a follow-up or an answer to a job"),
            ("cancel", "Stop a job"),
            ("launch", "Open an interactive session on the best account, with the shared setup"),
        ]),
        ("Share agents and skills between CLIs", [
            ("share", ["Keep them the same across Claude Code, Codex, Antigravity and Kiro ", ui.muted("(agt share --help)")]),
        ]),
        ("Accounts and settings", [
            ("accounts", "List, add, log in, sync, reset or remove accounts"),
            ("config", "See and change agt's settings (show, set, unset)"),
            ("rules", "Show, install or remove the optional delegation rules"),
            ("log", "What agt changed and why, newest first"),
        ]),
    ],
    "options": [
        ("--home PATH", "Use this folder instead of ~/.agents-router (sets AGT_HOME)"),
        ("--color WHEN", "auto, always or never; auto colors a terminal, never an agent's output"),
        ("--version", "Print agt's version"),
        ("-h, --help", "Show this; agt <command> --help shows one command"),
    ],
    "examples": [
        ("agt setup --dry-run", "See what setup would do; writes nothing"),
        ('agt run "fix the failing test"', "Run it in Claude Code and print the result"),
        ('agt start --runtime codex "review the diff"', "Start it in Codex and keep working"),
        ("agt share status", "What's in sync, and what waits for you"),
        ("agt doctor --runtime codex", "Check Codex's binary and logins"),
    ],
    "footer": [
        [ui.bold("Exit codes"), "  0 done   1 waits for you, or failed   2 wrong command or value   3 needs your choice"],
        [ui.muted("Your CLIs run these, not you:"), " mcp, hook"],
        [ui.muted("Docs:"), " https://github.com/Watcharagorn/agents-router"],
    ],
}

SHARE = {
    "name": "agt share",
    "about": "keep your agents and skills the same in every CLI folder you pick",
    "usage": "agt share <command> [options]",
    "usage_note": [ui.code("agt share"), " on its own shows the status."],
    "sections": [
        ("Turn it on and off", [
            ("enable", "Turn sharing on: --dry-run shows the plan, --yes saves your answers and applies it"),
            ("disable", "Stop sharing; every copy stays, and agt share enable turns it on again"),
        ]),
        ("See where things stand", [
            ("status", "What's in sync, what differs and what waits for you; writes nothing"),
            ("plugins", "Plugins another CLI could install; plugins apply installs the ones you pick"),
            ("settings", "Every share setting, where it comes from and the command that changes it"),
        ]),
        ("Change what goes where", [
            ("pick", "Which CLIs get an item; with no item, what goes where"),
            ("resolve", "Pick the version that wins when an item's copies differ"),
            ("sync", "Make every copy match again, from your saved settings"),
            ("mv", "Rename an item in every CLI, keeping each copy's own fields"),
            ("rm", "Remove an item from every CLI, with backups"),
            ("undo", "Put back what the last change to an item replaced, from the backups"),
        ]),
        ("Choose what shares", [
            ("cli", "The CLI folders that share: list, add or remove one"),
            ("kinds", "Share more than agents and skills: instructions, mcp, hooks, plugins"),
            ("repo", "The repos that share their own agents and skills: list, add or remove one"),
            ("adopt", "Add agt's block to CLIs' instruction files, so they share instructions"),
        ]),
    ],
    "examples": [
        ("agt share enable --dry-run", "See the plan; nothing is written"),
        ("agt share pick dana --not-in kiro", "Take one agent out of one CLI"),
        ("agt share undo dana --in kiro", "Bring that copy back"),
        ("agt share kinds add plugins", "Install plugins between Claude Code and Codex"),
    ],
    "footer": [
        [ui.bold("Names"), "  dana, or agents/<name>, skills/<name>, plugins/<name>@<marketplace>"],
        [ui.muted("Your CLIs and the watcher run these, not you:"), " session, saved, watch. ",
         ui.muted("For a status line:"), " badge"],
    ],
}

# `scan` still works (it's an older name for the same preview) but isn't listed: enable --dry-run is the one to learn.
UNLISTED = {"scan"}


def page_lines(page: dict[str, Any]) -> list[ui.Line]:
    lines: list[ui.Line] = [[ui.bold(page["name"]), "  ", page["about"]], "", [ui.bold("Usage"), "  ", page["usage"]]]
    if page.get("usage_note"):
        lines.append(["       ", *page["usage_note"]])
    width = max(len(name) for _, rows in page["sections"] for name, _ in rows)
    for heading, rows in page["sections"]:
        lines += ["", ui.bold(heading)]
        for name, text in rows:
            lines.append(["  ", ui.cmd(name), " " * (width - len(name) + 3), *([text] if isinstance(text, str) else text)])
    if page.get("options"):
        lines += ["", ui.bold("Options"), *ui.columns([[flag, text] for flag, text in page["options"]], gap=3)]
    lines += ["", ui.bold("Examples"), *ui.columns([[ui.cmd(command), ui.muted(text)] for command, text in page["examples"]], gap=3)]
    return lines + [""] + page["footer"]


def _choices(parser: argparse.ArgumentParser) -> list[str]:
    """The subcommands people type, for a did-you-mean."""

    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return [name for name in action.choices if name not in INTERNAL and not name.startswith("_")]
    return []


def corrected(typed: str, suggestion: str) -> str:
    """The command just run, with `typed` swapped for `suggestion`."""

    import shlex

    words = list(ui.ARGV)
    if typed in words:
        words[words.index(typed)] = suggestion
    return " ".join(["agt", *(shlex.quote(word) for word in words)])


def mistake_lines(prog: str, message: str, suggestion: str | None = None, typed: str | None = None,
                  hint: ui.Line | None = None) -> list[ui.Line]:
    lines: list[ui.Line] = [[ui.Span("error-mark", "✗"), f" {prog}: ", *ui.inline(message)]]
    if suggestion:
        lines.append(["  Did you mean ", ui.bold(suggestion), "?"])
        if typed:
            lines.append(["    ", ui.cmd(corrected(typed, suggestion))])
    if hint is not None:
        lines.append(["  ", *([hint] if isinstance(hint, (str, ui.Span)) else hint)])
    return lines


def _command(parser: argparse.ArgumentParser, argv: list[str]) -> argparse.ArgumentParser:
    """The deepest subcommand parser `argv` names, so a mistake points at that command's help."""

    for word in argv:
        if word.startswith("-"):
            break
        action = next((a for a in parser._actions if isinstance(a, argparse._SubParsersAction)), None)
        if action is None or word not in action.choices:
            break
        parser = action.choices[word]
    return parser


class Parser(argparse.ArgumentParser):
    """agt's parsers. Subparsers are made from the parent's class, so every agt command gets this."""

    def __init__(self, *args: Any, page: dict[str, Any] | None = None, **kwargs: Any) -> None:
        self.page = page
        self._drawing = False
        super().__init__(*args, **kwargs)

    def _get_formatter(self) -> argparse.HelpFormatter:
        # argparse also makes a formatter for every add_argument; only drawn help may load rich.
        return (ui.help_formatter(self.prog) if self._drawing else None) or super()._get_formatter()

    def format_help(self) -> str:
        if self.page is not None:
            return "\n".join(ui.plain(line) for line in page_lines(self.page)) + "\n"
        self._drawing = True
        try:
            return super().format_help()
        finally:
            self._drawing = False

    def print_help(self, file: Any = None) -> None:
        if self.page is None:
            super().print_help(file)
        else:
            ui.out(*page_lines(self.page), stream=file or sys.stdout)

    def error(self, message: str) -> None:  # type: ignore[override]
        choice = re.search(r"invalid choice: '?([^'\s,()]+)'?", message)
        if choice and _choices(self):
            typed = choice.group(1)
            close = difflib.get_close_matches(typed, _choices(self), 1, 0.6)
            lines = mistake_lines(self.prog, f"there's no command {typed}", close[0] if close else None, typed,
                                  [ui.code(f"{self.prog} --help"), " lists every command."])
        else:
            prog = _command(self, ui.ARGV).prog if self.prog == "agt" else self.prog
            lines = mistake_lines(prog, message, hint=[ui.code(f"{prog} --help"), " shows how to use it."])
        ui.out(*lines, stream=sys.stderr)
        sys.exit(2)
