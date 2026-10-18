# Codex on Windows: "sandbox backend: disabled"

Research date: 2026-10-15. Checked on one Windows 11 PC (build 26300) with Codex CLI 0.162.1 installed through npm, plus the openai/codex source at release tags, pull requests, issues and OpenAI's Windows sandbox docs. The proposed fix below hasn't been tested yet.

## What happens

Every sandboxed Codex run refuses every command, even `pwd` or reading a file. That covers `codex exec -s read-only`, workspace-write runs, and OpenAI's own Codex plugin for Claude Code, which always starts Codex in one of those two modes. Codex reports that its "execution policy" rejected the command. Asked to create a file in write mode, it says the session is read-only.

`codex doctor` on that PC:

```text
✓ sandbox      restricted fs + restricted network · approval OnRequest
    filesystem sandbox       restricted
    network sandbox          restricted
    sandbox backend          disabled
```

`codex features list` shows `experimental_windows_sandbox` and `elevated_windows_sandbox` as `removed`, and `windows_sandbox_service` as `under development` and off. `~/.codex/config.toml` had no `[windows]` section. CLI Subagents never writes that file; it only writes config into extra account folders.

## Why

In Codex 0.162.1, `doctor` prints the level from the `[windows] sandbox` setting, and "disabled" means none is set: no `[windows] sandbox` value and no old flag that maps to one (`codex-rs/cli/src/doctor/sandbox.rs` at tag `rust-v0.162.1`). With no sandbox to run commands in, Codex falls back to refusing them.

The old flags weren't dropped without a replacement. They were renamed:

| Date | Version | Change |
|---|---|---|
| 2026-02-11 | PR openai/codex#11341 "Promote Windows Sandbox" | The experimental flags become `[windows] sandbox = "elevated"` or `"unelevated"` |
| 2026-02-12 | 0.100.0 | `experimental_windows_sandbox` and `elevated_windows_sandbox` marked removed. The code still maps the old key to `unelevated` |
| 2026-09-02 | PR openai/codex#42353 | Adds `windows_sandbox_service`, off by default: a service-based way to install the elevated sandbox |
| 2026-09-17 | 0.155.0 | Ships `windows_sandbox_service` as under development |
| 2026-09-22 | PR openai/codex#47375, 0.157.0 | Adds opt-in `prefer_mxc` (Microsoft Execution Containers) |
| 2026-10-08 | 0.162.0 | Adds `codex sandbox uninstall` (removes the old sandbox's machine-wide accounts and firewall rules) |
| 2026-10-09 | 0.162.1 | Latest stable on the research date; the latest alpha was 0.163.0-alpha.11 (2026-10-14) |

No release date or version has been announced for `windows_sandbox_service`.

## Likely fix (not tested yet)

Set the sandbox in `~/.codex/config.toml`:

```toml
[windows]
sandbox = "unelevated"   # no admin needed; weaker network isolation, no denied-read paths
```

or `sandbox = "elevated"`, the stronger option, which needs a one-time setup with admin rights. OpenAI's docs also describe `sandbox = "mxc"` (or `[features] prefer_mxc = true`), which needs Windows build 26100.9278 or 26200.9278 or later and Windows features Microsoft is still rolling out; the Codex desktop app picks MXC on its own for consumer accounts. WSL is OpenAI's other supported route on Windows.

A check that changes no config and calls no model, run in a scratch folder:

```text
codex -c windows.sandbox=unelevated sandbox -- cmd /d /c echo OK
codex -c windows.sandbox=mxc sandbox --permission-profile :workspace -- cmd.exe /d /c echo MXC_OK
```

Don't run `codex sandbox uninstall` unless you want the old sandbox's accounts and firewall rules removed.

Updating Codex alone won't fix this; the setting has to be there.

## What it means for CLI Subagents

- With the sandbox unset, Codex `read` and `edit` jobs can't run commands on Windows. Only `full` (`--dangerously-bypass-approvals-and-sandbox`) works, with the user's normal rights and no checks.
- Verified 2026-10-16 (Windows, Codex 0.162.1): `-c windows.sandbox=unelevated` fixes it with no admin rights. Under `codex sandbox`, commands run, writes inside the workspace work, and writes outside the workspace (and outside Temp) are denied.
- So CLI Subagents adds `-c windows.sandbox="unelevated"` to `read` and `edit` jobs on Windows, also when resuming, unless the Codex config the run reads already sets `[windows] sandbox`. `full` jobs get nothing. The `[codex] windows_sandbox` key (`"unelevated"` default, `"elevated"`, `"off"`) changes this.
- `csa doctor --runtime codex` prints one line on Windows saying which sandbox jobs will use.

## Network and local sockets (tested 2026-10-18, Windows, Codex 0.162.1)

A job in a .NET project reported that `dotnet test` can't run in Codex's sandbox: VSTest's test host couldn't connect to its runner on 127.0.0.1 (`SocketException 10013`), and a retry with `require_escalated` still hung. The owner's Codex config sets `[windows] sandbox = "mxc"`, and csa keeps a sandbox the user set. Each cell below is a `codex exec` run in workspace-write with approvals off, on a throwaway folder: a Python script that opens a TCP server on 127.0.0.1 and connects to it, and `dotnet test --no-restore` on a new one-test xunit project.

| Sandbox | `network_access` | Local socket | `dotnet test` |
|---|---|---|---|
| `mxc` | false (Codex default) | blocked, WinError 10013 | hangs |
| `mxc` | true | works | passes |
| `unelevated` | false | works | passes |
| `unelevated` | true | works | passes |

With `--approve-for-me` (what csa uses for edit jobs) the results matched. An escalated command (approved by Codex's auto-review) opened the local socket under `mxc` too, but escalation is a per-command detour. Since 0.2.7, csa passes `-c sandbox_workspace_write.network_access=true` to edit jobs (`[codex] network_access`, default true).

## Sources

- openai/codex: PRs #11341, #42353, #47375, #50437, #51256; `codex-rs/core/src/features.rs` at `rust-v0.100.0`; `codex-rs/core/src/windows_sandbox.rs` and `codex-rs/cli/src/doctor/sandbox.rs` at `rust-v0.162.1`; releases `rust-v0.162.0` and `rust-v0.162.1`.
- Related issues: openai/codex#9062 (since 2026-01-11, maintainer replies, fix PR #9552), #36475, #45734, #48856 (a 0.162.1 user, 2026-10-11), #50958.
- OpenAI docs: Windows sandbox (developers.openai.com/codex/windows, which redirects to learn.chatgpt.com/docs/windows/windows-sandbox).
- Local checks: `codex doctor`, `codex features list`, `codex sandbox --help` on Codex 0.162.1.

Not verified: whether the helper programs `codex doctor` names (`codex-windows-sandbox-setup.exe`, `codex-command-runner.exe`) ship in the npm `win32-x64` package; they weren't in its `bin` folder on the test PC.
