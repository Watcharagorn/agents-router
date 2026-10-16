"""Path rules: where a worker may read and write, and what it may never touch.

Every check looks at two forms of a path: the normalized one as written and the
resolved one (links and junctions followed). A path is denied if either form hits
a deny rule, and allowed only if the resolved form lies inside an allowed root,
so a link inside the workspace can't reach a secret outside it.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Iterable

IS_WINDOWS = os.name == "nt"

# Folder names that hold credentials or identities, wherever they appear.
SECRET_DIRS = {".ssh", ".gnupg", ".aws", ".azure", ".kube", ".password-store", ".secrets"}
# Paths below a home folder (the user's own or the worker's) that hold logins or tokens.
HOME_SECRETS = (
    ".config/gcloud", ".config/gh", "appdata/roaming/github cli", ".git-credentials", ".netrc", "_netrc",
    ".npmrc", ".pypirc", ".docker/config.json", ".claude/.credentials.json", ".claude.json", ".codex/auth.json",
    ".gemini/oauth_creds.json", ".gemini/google_accounts.json", ".gemini/antigravity/mcp_oauth_tokens.json",
    ".local/share/keyrings", "library/keychains", "appdata/roaming/microsoft/credentials",
    "appdata/local/microsoft/credentials", "appdata/roaming/microsoft/protect", "appdata/local/google/chrome/user data",
    "appdata/local/microsoft/edge/user data", "appdata/roaming/mozilla/firefox", "appdata/local/bravesoftware",
    ".mozilla/firefox", ".config/google-chrome", "library/application support/google/chrome",
    "appdata/roaming/1password", "appdata/local/1password", "appdata/roaming/bitwarden", ".config/bitwarden",
)
# Name markers inside an agent folder (.gemini, .claude, .codex) that mean a login or a key.
AGENT_SECRET_MARKERS = ("oauth", "token", "credential", "mcp_config", "auth.json", "installation_id")
AGENT_CONTENT_DIRS = {"brain", "skills", "rules", "plugins", "commands", "agents", "workflows", "projects", "mcp"}
AGENT_DIRS = {".gemini", ".claude", ".codex", ".agents", ".agent", ".cursor", ".windsurf"}
SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".kdbx", ".ppk", ".asc", ".gpg")
SAFE_ENV_SUFFIXES = (".example", ".sample", ".template", ".dist", ".defaults")
SECRET_NAMES = {"credentials.json", ".credentials.json", ".htpasswd", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}
SECRET_NAME_RE = re.compile(
    r"^(id_(rsa|dsa|ecdsa|ed25519)(?!.*\.pub$).*|client_secret.*\.json|secrets?\.(json|ya?ml|toml|ini|env|txt))$"
)
# Files whose edits change how agents or git behave: always denied for writes.
WRITE_PROTECTED_DIRS = {".git"} | AGENT_DIRS
# Edits a reviewer should look at even inside the workspace.
REVIEW_WRITE_NAMES = {"agents.md", "claude.md", "gemini.md", "claude.local.md", "hooks.json", "settings.json"}
REVIEW_WRITE_DIRS = {".vscode", ".husky", ".devcontainer"}


def _fold(text: str) -> str:
    text = text.replace("\\", "/")
    return text.casefold() if IS_WINDOWS else text


def normalize(value: str, base: str | None = None, home: str | None = None) -> str:
    """Absolute, normalized form of `value` (relative paths resolved against `base`)."""

    text = str(value).strip().strip("\"'")
    if text.lower().startswith("file:///"):
        text = text[8:] if IS_WINDOWS else text[7:]
    if text == "~" or text.startswith(("~/", "~\\")):
        text = (home or str(Path.home())) + text[1:]
    if not os.path.isabs(text):
        text = os.path.join(base or os.getcwd(), text)
    return os.path.normpath(text)


def resolve(path: str) -> str:
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return path


def forms(value: str, base: str | None = None, home: str | None = None) -> tuple[str, str]:
    """(normalized, resolved) forms of a path."""

    normal = normalize(value, base, home)
    return normal, resolve(normal)


def inside(path: str, root: str) -> bool:
    """True if `path` is `root` or lies below it (both absolute)."""

    path, root = _fold(os.path.normpath(path)).rstrip("/"), _fold(os.path.normpath(root)).rstrip("/")
    return path == root or path.startswith(root + "/")


def inside_any(path: str, roots: Iterable[str]) -> bool:
    return any(root and inside(path, root) for root in roots)


def _parts(path: str) -> list[str]:
    return [part for part in _fold(path).split("/") if part]


def secret_reason(path: str, homes: Iterable[str] = ()) -> str | None:
    """Why `path` must never be read or written, or None."""

    folded = _fold(path)
    parts = _parts(path)
    names = {part.casefold() for part in parts}
    if names & SECRET_DIRS:
        return "it is a credential folder"
    for home in homes:
        if not home or not inside(path, home):
            continue
        relative = folded[len(_fold(os.path.normpath(home)).rstrip("/")) + 1 :]
        for entry in HOME_SECRETS:
            if relative == entry or relative.startswith(entry + "/"):
                return "it holds a login, token or browser profile"
    for index, part in enumerate(parts[:-1]):
        if part.casefold() in (".gemini", ".claude", ".codex"):
            rest = [item.casefold() for item in parts[index + 1 :]]
            if set(rest[:-1]) & AGENT_CONTENT_DIRS:
                continue  # skills, rules, plans and the like: ordinary content
            if any(marker in rest[-1] for marker in AGENT_SECRET_MARKERS):
                return "it holds an agent login, token or MCP server keys"
    name = parts[-1].casefold() if parts else ""
    if name == ".env" or (name.startswith(".env.") and not name.endswith(SAFE_ENV_SUFFIXES)):
        return "it is an environment file that may hold secrets"
    if name in SECRET_NAMES or SECRET_NAME_RE.match(name) or name.endswith(SECRET_SUFFIXES):
        return "it looks like a key or secrets file"
    return None


def write_protection(path: str) -> str | None:
    """Why a write to `path` is never allowed (git internals, agent and hook config), or None."""

    for part in _parts(path):
        lowered = part.casefold()
        if lowered == ".git":
            return "it is inside .git (git internals and hooks)"
        if lowered in AGENT_DIRS:
            return "it is agent configuration (hooks, settings, rules)"
    return None


def needs_review_for_write(path: str) -> str | None:
    """Why an edit inside the workspace should still go to the reviewer, or None."""

    parts = [part.casefold() for part in _parts(path)]
    if not parts:
        return None
    if parts[-1] in REVIEW_WRITE_NAMES:
        return "it edits agent instructions or settings"
    if set(parts[:-1]) & REVIEW_WRITE_DIRS:
        return "it edits editor, git-hook or container configuration that can run code later"
    if ".github" in parts[:-1] and "workflows" in parts[:-1]:
        return "it edits a CI workflow"
    return None


def check(value: str, *, base: str | None, homes: Iterable[str]) -> tuple[str, str, str | None]:
    """(normalized, resolved, secret reason or None) for one path argument."""

    home_list = [home for home in homes if home]
    normal, resolved = forms(value, base, home_list[0] if home_list else None)
    reason = secret_reason(normal, home_list) or secret_reason(resolved, home_list)
    return normal, resolved, reason
