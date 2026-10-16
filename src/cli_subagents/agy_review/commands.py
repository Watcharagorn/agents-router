"""Shell command rules: a conservative read-only parser and deterministic denials.

The parser accepts a command only when it is plainly read-only: a few known
programs, joined at most by pipes, with no expansion, redirection, chaining,
subshells or script blocks anywhere in the text. Everything it can't prove is
read-only goes to the reviewer; the hard-deny list ends some commands at once.
"""

from __future__ import annotations

import os
import re
from typing import Callable

# Characters that expand, redirect, chain or start subshells in sh, cmd or PowerShell.
UNSAFE_CHARS = set("`$%<>;&(){}^!@\r\n")

SIMPLE_READERS = {
    "ls", "dir", "gci", "get-childitem", "cat", "type", "gc", "get-content", "head", "tail", "wc", "pwd",
    "get-location", "echo", "write-output", "tree", "which", "where.exe", "get-command", "gcm", "file", "stat",
    "du", "select-string", "sls", "findstr", "grep", "egrep", "fgrep", "test-path", "resolve-path",
    "get-item", "gi", "measure-object", "measure", "select-object", "select", "sort-object", "format-table",
    "ft", "format-list", "fl", "out-string", "get-filehash", "nl", "basename", "dirname", "realpath", "date",
    "get-date", "whoami", "hostname", "uname", "rg", "find", "fd", "sort", "diff", "cmp", "md5sum",
    "sha256sum", "certutil",
}
VERSION_ONLY = {
    "python", "python3", "py", "node", "npm", "npx", "pnpm", "yarn", "pip", "pip3", "uv", "cargo", "rustc",
    "go", "java", "javac", "dotnet", "gcc", "clang", "make", "cmake", "ruby", "php", "deno", "bun", "gh",
    "docker", "kubectl", "terraform", "pwsh", "powershell", "bash", "sh", "code", "agy", "claude", "codex", "csa",
}
VERSION_FLAGS = {"--version", "-v", "-V", "version", "-version"}
OPTION_DENY = {
    # options that write files or run other programs
    "find": ("-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"),
    "fd": ("-x", "-X", "--exec", "--exec-batch"),
    "rg": ("--pre", "--pre-glob"),
    "sort": ("-o", "--output", "--compress-program"),
    "certutil": ("-urlcache", "-decode", "-encode", "-f", "-split"),
}
CERTUTIL_OK = ("-hashfile",)
# PowerShell drives that aren't files: environment variables, registry, certificates and the like.
PROVIDER_RE = re.compile(r"^(.*\\)?(env|hklm|hkcu|hkcr|hku|hkcc|cert|variable|function|alias|wsman|registry)::?", re.IGNORECASE)

GIT_READ = {
    "status", "log", "diff", "show", "blame", "annotate", "rev-parse", "ls-files", "ls-tree", "describe",
    "shortlog", "cat-file", "grep", "whatchanged", "rev-list", "merge-base", "name-rev", "count-objects",
    "show-ref", "version", "branch", "tag", "stash", "worktree", "reflog",
}
GIT_GLOBAL_OK = {"--no-pager", "-p", "-P", "--paginate", "--no-optional-locks", "--literal-pathspecs"}
GIT_OPTION_DENY = ("--output", "-o", "--open-files-in-pager", "-O", "--ext-diff", "--exec", "--upload-pack")
GIT_BRANCH_FLAGS = {
    "-a", "-r", "-v", "-vv", "--list", "-l", "--all", "--remotes", "--show-current", "--verbose",
    "--no-color", "--color", "--column", "--no-column",
}
GIT_BRANCH_WITH_VALUE = {"--contains", "--no-contains", "--merged", "--no-merged", "--points-at", "--sort"}

# Hard denials: (pattern, reason). Matched on the lowercased command with "\" turned into "/".
_DENY_PATTERNS: list[tuple[str, str]] = [
    (r"\bcmdkey\b|\bvaultcmd\b|\bsecret-tool\b|get-storedcredential|credential-manager|\bmimikatz\b"
     r"|\bsecurity\s+(find|dump|export)-|\bkeyring\s+get\b|convertfrom-securestring|\bcredwiz\b",
     "it reads the system credential store"),
    (r"\bgh\s+auth\s+(token|status\b.*(-t\b|--show-token))|\bgit\s+credential\b|\bgcloud\s+auth\s+print-",
     "it prints a login token"),
    (r"\bgit\b.*\bpush\b.*(\s--force\b|\s-f\b|\s--force-with-lease\b|\s--mirror\b|\s--delete\b|\s-d\b|\s\+\S)",
     "it force-pushes or deletes remote branches"),
    (r"\b(npm|pnpm|yarn|bun)\s+(publish|unpublish)\b|\byarn\s+npm\s+publish\b|\btwine\s+upload\b"
     r"|\b(poetry|uv|flit|hatch)\s+publish\b|\bcargo\s+(publish|yank)\b|\bgem\s+(push|yank)\b"
     r"|\b(docker|podman)\s+push\b|\bgh\s+(release\s+(create|delete|upload)|repo\s+(delete|edit|rename|archive)"
     r"|secret\s+set|pr\s+merge)\b|\b(dotnet\s+)?nuget\s+push\b|\bmvn\b.*\bdeploy\b|\bgradlew?\b.*\bpublish\b"
     r"|\b(vsce|ovsx)\s+publish\b|\bfirebase\s+deploy\b|\bterraform\s+(apply|destroy)\b|\bkubectl\s+(apply|delete|replace)\b",
     "it publishes or deploys"),
    (r"\bgit\s+reset\s+--hard\b|\bgit\s+clean\b.*\s-[a-z]*f|\bgit\s+checkout\s+--\s+\.(\s|$)|\bgit\s+restore\s+\.(\s|$)"
     r"|\bgit\s+stash\s+(drop|clear)\b|\bgit\s+(filter-branch|filter-repo)\b|\bgit\s+update-ref\s+-d\b",
     "it throws away uncommitted or committed work"),
    (r"\bformat(\.com)?\s+[a-z]:|\bdiskpart\b|\bbcdedit\b|\bmkfs\b|\bdd\s+.*of=/dev/|\bshutdown\b|\brestart-computer\b"
     r"|\bstop-computer\b|\bvssadmin\b.*\bdelete\b|\bwbadmin\b.*\bdelete\b|\bcipher\s+/w|\bclear-disk\b|\bformat-volume\b",
     "it can destroy disks or stop the machine"),
    (r"\breg(\.exe)?\s+(add|delete|import|load|save|restore)\b|\bset-mppreference\b|\badd-mppreference\b"
     r"|\bnetsh\s+(advfirewall|firewall)\b|\bset-executionpolicy\b|\bsetx\b|\btakeown\b|\bicacls\b.*\b(grant|setowner)\b"
     r"|\bsystemctl\s+(enable|disable|mask)\b|\bufw\s+disable\b|\bsetenforce\s+0\b|\bsudo\b|\brunas\b",
     "it changes system security settings or raises privileges"),
    (r"\bschtasks\b.*\s/create\b|\b(new|register)-scheduledtask\b|\bsc(\.exe)?\s+(create|config|delete)\b|\bnew-service\b"
     r"|\bcrontab\b|\blaunchctl\s+(load|bootstrap|submit)\b",
     "it creates a scheduled task, service or other persistence"),
    (r"\binvoke-expression\b|\biex\b|-encodedcommand\b|frombase64string"
     r"|\b(powershell|pwsh)(\.exe)?\b[^|]*\s-(e|ec|en|enc|enco|encod|encode|encoded)\s"
     r"|\b(curl|wget|iwr|irm|invoke-webrequest|invoke-restmethod)\b[^|]*\|\s*(sh|bash|zsh|python[0-9.]*|node|iex|pwsh|powershell)\b",
     "it runs hidden or downloaded code"),
]
DENY_RES = [(re.compile(pattern), reason) for pattern, reason in _DENY_PATTERNS]
DELETE_COMMANDS = {"rm", "rmdir", "rd", "del", "erase", "remove-item", "ri", "rimraf", "shred", "unlink"}
# Paths in a command that should make even a sandboxed command go to the reviewer.
PROTECTED_MARKERS = (
    ".git/hooks", ".git/config", ".git/info", "core.hookspath", "core.fsmonitor", "core.sshcommand", "core.pager",
    "core.editor", "git config", "hooks.json", "/.agents", ".agents/", "/.gemini", ".gemini/", "/.claude", ".claude/",
    "/.codex", ".codex/", ".husky", ".vscode", "init --template", "settings.json",
)


def tokenize(text: str) -> list[list[str]] | None:
    """Split a command at top-level pipes into word lists. None if quoting is unbalanced."""

    segments: list[list[str]] = [[]]
    word: list[str] = []
    quote = ""
    started = False

    def end_word() -> None:
        nonlocal word, started
        if started:
            segments[-1].append("".join(word))
        word, started = [], False

    for char in text:
        if quote:
            if char == quote:
                quote = ""
            else:
                word.append(char)
            continue
        if char in "'\"":
            quote, started = char, True
        elif char == "|":
            end_word()
            segments.append([])
        elif char.isspace():
            end_word()
        else:
            word.append(char)
            started = True
    if quote:
        return None
    end_word()
    return segments


def program_name(word: str) -> str:
    name = word.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for suffix in (".exe", ".cmd", ".bat", ".com"):
        if name.endswith(suffix) and name != "where.exe":
            return name[: -len(suffix)]
    return name


def unquoted(command: str) -> str:
    """The command with the contents of quoted strings removed (commit messages and the like)."""

    out: list[str] = []
    quote = ""
    for char in command:
        if quote:
            if char == quote:
                quote = ""
                out.append(char)
            continue
        if char in "'\"":
            quote = char
        out.append(char)
    return "".join(out)


def _match(text: str) -> str | None:
    text = " " + text.lower().replace("\\", "/") + " "
    for pattern, reason in DENY_RES:
        if pattern.search(text):
            return reason
    return None


def denial(command: str) -> str | None:
    """A reason this command must never run, or None. Quoted text doesn't count (see `suspicious`)."""

    return _match(unquoted(command))


INTERPRETER_RE = re.compile(
    r"\b(bash|sh|zsh|dash|cmd|powershell|pwsh|python[0-9.]*|py|node|deno|perl|ruby|php|ssh|wsl|xargs|env)(\.exe)?\b"
    r"|\b(invoke-expression|start-process|docker\s+exec|kubectl\s+exec)\b",
    re.IGNORECASE,
)


def suspicious(command: str) -> str | None:
    """A deny reason that matches only inside quoted text that an interpreter may run (`bash -c "..."`).

    Quoted text given to other programs (a commit message) is data and doesn't count.
    """

    if denial(command) or not INTERPRETER_RE.search(unquoted(command)):
        return None
    return _match(command)


def _flag(word: str) -> bool:
    return word.startswith("-") or (os.name == "nt" and bool(re.fullmatch(r"/[a-zA-Z?]{1,2}", word)))


def destructive_delete(command: str, *, cwd: str, inside_workspace: Callable[[str], bool]) -> str | None:
    """A reason when a delete targets something outside the workspace, or a target that can't be resolved."""

    segments = tokenize(command) or [command.split()]
    for words in segments:
        if not words or program_name(words[0]) not in DELETE_COMMANDS:
            continue
        targets = [word for word in words[1:] if not _flag(word)]
        if not targets:
            return "it deletes without a clear target"
        for target in targets:
            if any(char in target for char in "$%`") or target.startswith("~"):
                return "it deletes with a target that isn't resolved yet (a variable or the home folder)"
            stem = re.split(r"[*?\[]", target, maxsplit=1)[0].strip()
            if stem and re.fullmatch(r"([a-zA-Z]:)?[\\/]*", stem):
                return "it deletes at a drive or file-system root"
            if not inside_workspace(os.path.normpath(os.path.join(cwd, stem or "."))):
                return "it deletes outside the workspace"
    return None


def touches_protected(command: str) -> bool:
    text = command.lower().replace("\\", "/")
    return any(marker in text for marker in PROTECTED_MARKERS)


def _path_like(word: str) -> bool:
    return (
        "/" in word or "\\" in word or word.startswith((".", "~")) or bool(re.match(r"^[a-zA-Z]:", word))
        or word.lower().startswith("env:")
    )


def _option_value(word: str) -> str:
    """The value part of `--opt=value` or PowerShell's `-Param:value`; the word itself otherwise."""

    if word.startswith("-"):
        for separator in ("=", ":"):
            if separator in word:
                return word.split(separator, 1)[1]
        return ""
    return word


def _candidate_paths(words: list[str]) -> list[str]:
    paths = []
    for word in words:
        if _flag(word) and not word.startswith("-"):
            continue
        value = _option_value(word)
        if not value or value.startswith("-"):
            continue
        if _path_like(value) or "." in value:
            paths.append(value)
    return paths


GLOB_CHARS = set("*?[")
MAX_GLOB_MATCHES = 200


def path_words(command: str, cwd: str | None = None) -> list[str]:
    """Every word of a command that may name a file (for the secret check), quoted or not.

    `rev:path` (git show HEAD:.env) adds the path part; with `cwd`, wildcards add what they match there.
    """

    segments = tokenize(command) or [command.split()]
    found = []
    for words in segments:
        for word in words:
            for piece in re.split(r"[;,|&<>()]", word):
                value = _option_value(piece) if piece.startswith("-") else piece
                value = value.strip("'\"")
                if not value:
                    continue
                candidates = [value]
                if ":" in value and not re.match(r"^[a-zA-Z]:[\\/]", value):
                    candidates.append(value.split(":", 1)[1])
                for candidate in candidates:
                    if candidate and (_path_like(candidate) or "." in candidate):
                        found.append(candidate)
                    if cwd and candidate and GLOB_CHARS & set(candidate):
                        found += expand(candidate, cwd)
    return found


def expand(pattern: str, cwd: str) -> list[str]:
    """What a wildcard matches, relative to `cwd` (capped), hidden files included."""

    import glob
    import itertools

    try:
        matches = glob.iglob(pattern, root_dir=cwd, recursive="**" in pattern, include_hidden=True)
        return list(itertools.islice(matches, MAX_GLOB_MATCHES))
    except (OSError, ValueError, TypeError):
        return []


def _git_ok(words: list[str], path_ok: Callable[[str], bool]) -> bool:
    if len(words) == 2 and words[1] in ("--version", "version"):
        return True
    index = 1
    while index < len(words) and words[index].startswith("-"):
        option = words[index]
        if option == "-C" and index + 1 < len(words):
            if not path_ok(words[index + 1]):
                return False
            index += 2
            continue
        if option not in GIT_GLOBAL_OK:
            return False
        index += 1
    if index >= len(words):
        return False
    sub, rest = words[index].lower(), words[index + 1 :]
    if sub not in GIT_READ:
        return False
    for word in rest:
        lowered = word.lower()
        if any(lowered == option or lowered.startswith(option + "=") for option in GIT_OPTION_DENY):
            return False
    if sub == "branch":
        expect_value = False
        for word in rest:
            if expect_value:
                expect_value = False
                continue
            name = word.split("=", 1)[0]
            if name in GIT_BRANCH_WITH_VALUE:
                expect_value = "=" not in word
                continue
            if word not in GIT_BRANCH_FLAGS:
                return False
    elif sub == "tag":
        if rest and rest[0] not in ("-l", "--list"):
            return False
    elif sub == "stash":
        if not rest or rest[0] not in ("list", "show"):
            return False
    elif sub == "worktree":
        if rest[:1] != ["list"]:
            return False
    elif sub == "reflog":
        if rest and rest[0] not in ("show",) and not rest[0].startswith("-"):
            return False
    return all(path_ok(path) for path in _candidate_paths(rest) if _path_like(path))


def read_only(command: str, *, cwd: str, path_ok: Callable[[str], bool]) -> bool:
    """True when the command is plainly read-only and every path it names passes `path_ok`.

    `path_ok` gets each path-like word as written (relative paths are relative to `cwd`).
    """

    if not command.strip() or any(char in UNSAFE_CHARS for char in command):
        return False
    segments = tokenize(command)
    if not segments or any(not words for words in segments):
        return False
    for words in segments:
        name = program_name(words[0])
        if _shadowed(words[0], cwd):
            return False
        if any(PROVIDER_RE.match(_option_value(word) if word.startswith("-") else word) for word in words[1:]):
            return False  # PowerShell drives such as env:, hklm:, cert: (environment variables, registry, keys)
        rest = words[1:]
        if name == "git":
            if not _git_ok(words, path_ok):
                return False
            continue
        if name in VERSION_ONLY:
            if len(rest) != 1 or rest[0] not in VERSION_FLAGS:
                return False
            continue
        if name not in SIMPLE_READERS or ("/" in words[0] or "\\" in words[0]):
            return False
        denied = OPTION_DENY.get(name, ())
        for word in rest:
            option = word.split("=", 1)[0].lower()
            if any(option == item.lower() or (item.startswith("--") and option.startswith(item)) for item in denied):
                return False
            if name in ("sort", "fd") and re.fullmatch(r"-[a-zA-Z]*[oxX][a-zA-Z]*", word):
                return False  # combined short flags such as -no or -Hx
        if name == "certutil" and (not rest or rest[0].lower() not in CERTUTIL_OK):
            return False
        for path in _candidate_paths(rest):
            if (_path_like(path) or os.path.splitext(path)[1]) and not path_ok(path):
                return False
    return True


def _shadowed(word: str, cwd: str) -> bool:
    """True if a file in the working folder could run instead of the program (cmd.exe looks there first)."""

    base = word.replace("\\", "/").rsplit("/", 1)[-1]
    try:
        names = {entry.lower() for entry in os.listdir(cwd)}
    except OSError:
        return False
    stem = base.lower()
    return any(candidate in names for candidate in (stem, *(stem + ext for ext in (".exe", ".cmd", ".bat", ".com", ".ps1"))))
