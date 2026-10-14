#!/usr/bin/env python
"""Recreate local bridge state after cloning; no downloads or provider login."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import shutil


def inside(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def regular_file(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"Expected a regular file: {path.name}")


def write_new_private(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)


def write_json(path: Path, value: object) -> None:
    regular_file(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        write_new_private(temporary, (json.dumps(value, indent=2) + "\n").encode())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def prepare(
    bridge_dir: Path,
    workspace_root: Path,
    state_dir: Path,
    agy_settings: Path,
    output_dir: Path,
    *,
    write: bool = False,
    configure_agy: bool = False,
    agy_cmd: str | None = None,
) -> dict:
    bridge = bridge_dir.resolve(strict=True)
    workspace = workspace_root.resolve(strict=True)
    state = state_dir.resolve()
    output = output_dir.resolve()
    if not workspace.is_dir():
        raise ValueError("Workspace must be a directory")
    if any(char.isspace() or char in '\"()' for char in str(bridge)):
        raise ValueError("The AGY hook runner requires an adapter path without spaces, quotes, or parentheses")
    if ")" in str(workspace):
        raise ValueError("Workspace cannot contain ')' in an AGY permission rule")
    if inside(state, workspace) or inside(state, bridge) or inside(state, bridge.parents[1]):
        raise ValueError("Private runtime state must remain outside the project and repository")
    for name in ("launch_mcp.py", "reviewed_exec.cmd"):
        if not (bridge / name).is_file():
            raise ValueError(f"Incomplete bridge: missing {name}")

    # Validate every existing input before making any changes. Never rotate a
    # working installation's secrets or silently replace malformed state.
    existing = {}
    for name in ("broker-secret.bin", "runtime-secret.txt", "broker-tokens.json"):
        path = state / name
        regular_file(path)
        if path.exists():
            if path.stat().st_size > 65536:
                raise ValueError(f"Oversized local state: {name}")
            existing[name] = path.read_bytes()
    broker_secret = existing.get("broker-secret.bin", secrets.token_bytes(32))
    runtime_secret = existing.get("runtime-secret.txt", (secrets.token_hex(32) + "\n").encode())
    if len(broker_secret) < 32:
        raise ValueError("Invalid existing broker secret")
    if not re.fullmatch(rb"[0-9a-f]{64}\s*", runtime_secret):
        raise ValueError("Invalid existing runtime secret")
    tokens = json.loads(existing["broker-tokens.json"]) if "broker-tokens.json" in existing else [secrets.token_hex(16) for _ in range(8)]
    if not isinstance(tokens, list) or not 1 <= len(tokens) <= 32:
        raise ValueError("Invalid broker token pool")
    if any(not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token) for token in tokens):
        raise ValueError("Invalid broker token")
    if len(set(tokens)) != len(tokens):
        raise ValueError("Duplicate broker tokens")

    regular_file(agy_settings)
    if agy_settings.exists() and agy_settings.stat().st_size > 1024 * 1024:
        raise ValueError("Oversized AGY settings")
    settings = json.loads(agy_settings.read_text(encoding="utf-8")) if agy_settings.exists() else {}
    if not isinstance(settings, dict):
        raise ValueError("AGY settings must be an object")
    permissions = settings.setdefault("permissions", {})
    trusted = settings.setdefault("trustedWorkspaces", [])
    if not isinstance(permissions, dict) or not isinstance(trusted, list) or any(not isinstance(x, str) for x in trusted):
        raise ValueError("Invalid AGY permissions or trusted workspaces")
    allow = permissions.setdefault("allow", [])
    if not isinstance(allow, list) or any(not isinstance(x, str) for x in allow):
        raise ValueError("AGY permissions.allow must be a list of strings")
    rules = [f"command({(bridge / 'reviewed_exec.cmd').as_posix()} {token})" for token in tokens]
    rules.append(f"write_file({workspace.as_posix()})")
    for rule in rules:
        if rule not in allow:
            allow.append(rule)
    if str(workspace) not in trusted:
        trusted.append(str(workspace))
    settings["useG1Credits"] = False

    python = bridge / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    agy = agy_cmd or shutil.which("agy")
    if not agy:
        candidate = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "agy/bin/agy.exe"
        agy = str(candidate) if candidate.is_file() else "__AGY_EXECUTABLE__"
    env = {
        "AGY_CMD": agy,
        "ANTIGRAVITY_BRIDGE_WORKSPACE_ROOT": str(workspace),
        "ANTIGRAVITY_BRIDGE_TRUSTED_ROOTS": str(workspace),
        "ANTIGRAVITY_BRIDGE_BROKER_SECRET": str(state / "broker-secret.bin"),
        "ANTIGRAVITY_BRIDGE_BROKER_TOKENS": str(state / "broker-tokens.json"),
        "ANTIGRAVITY_BRIDGE_RUNTIME_SECRET_FILE": str(state / "runtime-secret.txt"),
        "ANTIGRAVITY_BRIDGE_APPROVALS_DIR": str(state / "approvals"),
    }
    registration = {"command": str(python), "args": [str(bridge / "launch_mcp.py")], "env": env}
    report = {
        "written": write,
        "new_state_files": sorted(set(("broker-secret.bin", "runtime-secret.txt", "broker-tokens.json")) - existing.keys()),
        "broker_rule_count": len(tokens),
        "agy_settings_changed": bool(write and configure_agy),
        "venv_python_present": python.is_file(),
        "agy_path_detected": agy != "__AGY_EXECUTABLE__",
    }
    if not write:
        return report
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    contents = {"broker-secret.bin": broker_secret, "runtime-secret.txt": runtime_secret, "broker-tokens.json": (json.dumps(tokens) + "\n").encode()}
    for name, content in contents.items():
        if name not in existing:
            write_new_private(state / name, content)
    (state / "approvals").mkdir(exist_ok=True, mode=0o700)
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "mcp.json", {"mcpServers": {"antigravity_agents": registration}})
    write_json(output / "agy-settings.patch.json", {"permissions": {"allow": rules}, "trustedWorkspaces": [str(workspace)], "useG1Credits": False})
    toml = ["[mcp_servers.antigravity_agents]", f"command = {json.dumps(str(python))}", f"args = {json.dumps(registration['args'])}", f"cwd = {json.dumps(str(bridge))}", "enabled = true", "required = false", "startup_timeout_sec = 60", "tool_timeout_sec = 1900", "", "[mcp_servers.antigravity_agents.env]"]
    toml.extend(f"{key} = {json.dumps(value)}" for key, value in env.items())
    config_path = output / "codex-mcp.toml"
    regular_file(config_path)
    config_path.write_text("\n".join(toml) + "\n", encoding="utf-8")
    if configure_agy:
        write_json(agy_settings, settings)
    return report


def main() -> None:
    adapter = Path(__file__).resolve().parents[1]
    local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--bridge-dir", type=Path, default=adapter, help="Antigravity adapter directory (contains launch_mcp.py)")
    parser.add_argument("--state-dir", type=Path, default=local / "Codex/antigravity-bridge")
    parser.add_argument("--agy-settings", type=Path, default=Path.home() / ".gemini/antigravity-cli/settings.json")
    parser.add_argument("--output-dir", type=Path, default=adapter / ".local")
    parser.add_argument("--agy-cmd")
    parser.add_argument("--write", action="store_true", help="Create missing machine-local state and generated config")
    parser.add_argument("--configure-agy", action="store_true", help="Also merge exact broker/workspace grants into AGY settings; requires --write")
    args = parser.parse_args()
    if args.configure_agy and not args.write:
        parser.error("--configure-agy requires --write")
    try:
        report = prepare(args.bridge_dir, args.workspace_root, args.state_dir, args.agy_settings, args.output_dir, write=args.write, configure_agy=args.configure_agy, agy_cmd=args.agy_cmd)
    except (ValueError, OSError, UnicodeError) as error:
        parser.exit(1, f"Setup stopped: {error}\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
