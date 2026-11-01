#!/usr/bin/env bash
# A sandbox for live checks of Agents Router: its own HOME, agt, plugin source and launchd stand-in,
# so a check never touches your real ~/.claude*, ~/.codex, ~/.agents-router, installed agt or launchd.
#
#   scripts/sandbox.sh [--dir DIR] create            build it and install agt from this clone (default DIR: ~/agt-sandbox)
#   scripts/sandbox.sh [--dir DIR] reinstall         install agt and the plugin again after a code change
#   scripts/sandbox.sh [--dir DIR] add-profile NAME  one more sandbox Claude profile, .claude-NAME (claude-agt-NAME)
#   scripts/sandbox.sh [--dir DIR] check             show that the sandbox and your real setup are apart
#   scripts/sandbox.sh [--dir DIR] destroy           log the sandbox profiles out, then remove it and its launchers
#
# Use it from any terminal through its launchers, linked into ~/.local/bin for the default sandbox:
#   claude-agt-a, claude-agt-b   Claude Code on the sandbox profiles .claude-a and .claude-b
#   agt-sandbox [COMMAND...]     one command inside the sandbox (agt-sandbox agt doctor), or with none a sandbox shell
# Each one sets the sandbox up for its own process only: your `claude` and `agt` stay your real ones.
# They start in the sandbox's work folder (WORK, below) unless you start them inside the sandbox or WORK.
#
# Logins: Claude Code keeps them in the macOS keychain. With the sandbox's HOME, macOS finds no login
# keychain at all (logins fail with "Couldn't save your login"), so the sandbox gets one of its own:
# DIR/home/Library/Keychains/agt-sandbox.keychain-db (password agt-sandbox), which login.keychain-db links
# to. Sandbox logins live there and never reach your real keychain. Inside the sandbox, DIR/bin/claude is a
# wrapper that runs only with a CLAUDE_CONFIG_DIR inside the sandbox, for every caller (agt's workers
# included), and DIR/bin/codex refuses a CODEX_HOME outside it. agt itself never uses the real launchd
# while HOME isn't your account's home folder.
set -euo pipefail
# shellcheck disable=SC1091  # env.sh is written at run time

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_DIR="$HOME/agt-sandbox"
DIR="${AGT_SANDBOX_DIR:-$DEFAULT_DIR}"
if [ "${1:-}" = "--dir" ]; then
  DIR="$2"
  shift 2
fi
case "$DIR" in /*) ;; *) DIR="$PWD/$DIR" ;; esac  # the scripts it writes hold absolute paths
DIR="${DIR%/}"
ACTION="${1:-}"
NAME="${2:-}"
MARK=".agt-sandbox"
LINKS="$HOME/.local/bin"  # where the default sandbox's launchers are linked; it's on PATH where uv tools are
# The folder sandbox sessions start in. It lies outside your home folder on purpose: Claude Code reads every
# CLAUDE.md from a session's folder up to /, so one under your home would load your own ~/.claude/CLAUDE.md
# into every sandbox session. Claude Code also lists that folder as a project, and agt share offers those.
WORK="$(getconf DARWIN_USER_TEMP_DIR 2>/dev/null || printf '%s' "${TMPDIR:-/tmp}")"
# One per sandbox folder, so two sandboxes never share one; destroy removes it only when it has the marker.
WORK="${WORK%/}/$(basename "$DIR")-work-$(printf '%s' "$DIR" | cksum | cut -d' ' -f1)"
WORK_MARK=".agt-sandbox-work"
KEYCHAIN_PASSWORD="agt-sandbox"  # guards only the sandbox's test logins, in a file under the sandbox

die() { echo "sandbox: $*" >&2; exit 1; }

# These paths are written into shell scripts (env.sh, the wrappers, the launchers): no character the shell
# would read again.
case "$DIR$REPO" in
  *[\$\`\"\'\\]* | *$'\n'*) die "the sandbox folder and this repository's path can't contain \$, \`, quotes, backslashes or line breaks" ;;
esac

real_home() {
  if [ -n "${AGT_SANDBOX:-}" ]; then
    die "run this from a normal terminal, not inside the sandbox (AGT_SANDBOX is set)"
  fi
}

guard_dir() {
  case "$DIR" in
    "" | "/" | "$HOME" | "$HOME/" | "$REPO" | "$REPO/") die "refusing to use $DIR as the sandbox folder" ;;
  esac
}

write_env() {
  local uv_cache uv_python canonical
  uv_cache="$(uv cache dir)"
  uv_python="$(uv python dir)"
  mkdir -p "$WORK"
  touch "$WORK/$WORK_MARK"
  canonical="$(cd "$DIR" && pwd -P)"
  cat > "$DIR/env.sh" <<ENV
# Agents Router sandbox. The launchers (claude-agt-*, agt-sandbox) source it for their own process;
# sourcing it in a terminal tab turns that whole tab into the sandbox (close the tab to leave).
# First drop every Claude Code, Anthropic, Codex and agt variable of whatever started this (a Claude Code
# session passes its session id, messaging socket and token on), so only the sandbox's own apply.
for name in \$(env | sed -nE 's/^((CLAUDE|ANTHROPIC_|CODEX_|AGT_)[A-Za-z0-9_]*)=.*/\\1/p'); do unset "\$name"; done
unset name
export AGT_SANDBOX="$DIR"
export HOME="$DIR/home"
export USERPROFILE="\$HOME"
export PATH="$DIR/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export UV_TOOL_DIR="$DIR/uv-tools" UV_TOOL_BIN_DIR="$DIR/bin"
export UV_CACHE_DIR="$uv_cache" UV_PYTHON_INSTALL_DIR="$uv_python"
export CODEX_HOME="\$HOME/.codex"
export CLAUDE_CONFIG_DIR="\$HOME/.claude-a"
export DISABLE_AUTOUPDATER=1
export AGT_PLUGIN_SOURCE="$REPO"
export AGT_LAUNCHCTL='["$DIR/uv-tools/agents-router/bin/python", "$REPO/tests/fake_launchd.py", "launchctl"]'
export AGT_OSASCRIPT='["$DIR/uv-tools/agents-router/bin/python", "$REPO/tests/fake_launchd.py", "osascript"]'
export FAKE_LAUNCHCTL_STATE="$DIR/launchd/state.json" FAKE_LAUNCHCTL_RECORD="$DIR/launchd/launchctl.jsonl"
export FAKE_OSASCRIPT_RECORD="$DIR/launchd/osascript.jsonl"
[ -e "\$HOME/Library/Keychains/agt-sandbox.keychain-db" ] && security unlock-keychain -p "$KEYCHAIN_PASSWORD" "\$HOME/Library/Keychains/agt-sandbox.keychain-db" 2>/dev/null
unset -f claude codex 2>/dev/null || true  # your shell's own wrappers; bin/claude and bin/codex guard the sandbox
# Start in the work folder unless already inside the sandbox or it. It's outside your home folder, so no real
# CLAUDE.md is above it, and no real repo becomes a sandbox project (agt share offers those).
[ -d "$WORK" ] || { mkdir -p "$WORK" && touch "$WORK/$WORK_MARK"; }
case "\$(pwd -P)/" in "$canonical"/* | "\$(cd "$WORK" && pwd -P)"/*) ;; *) cd "$WORK" ;; esac
PS1="(agt-sandbox) \${PS1:-\$ }"
echo "agt sandbox: HOME=\$HOME, agt \$(command -v agt), CLAUDE_CONFIG_DIR=\$CLAUDE_CONFIG_DIR"
ENV
  # A login shell (Claude Code's Bash tool may start one) runs macOS's path_helper, which moves /etc/paths
  # (Homebrew's real codex and uv) ahead of the sandbox's bin; these run after it and put bin first again.
  local line="export PATH=\"$DIR/bin:\$PATH\"  # agt sandbox: its wrappers first"
  printf '%s\n' "$line" > "$DIR/home/.zprofile"
  printf '%s\n' "$line" > "$DIR/home/.bash_profile"
}

link_clis() {
  # claude and codex are wrappers, not links: every caller (a shell, agt, a worker whose environment agt cleaned)
  # gets the real CLI only on a folder inside the sandbox.
  local name path home
  home="$(cd "$DIR/home" && pwd -P)"
  for name in claude codex agy uv; do
    path="$(type -P "$name" || true)"
    case "$path" in "$DIR"/*) path="$(sed -n 's/^REAL="\(.*\)"$/\1/p' "$path")" ;; esac  # an earlier wrapper
    if [ -z "$path" ]; then
      echo "  no $name on PATH: left out"
      continue
    fi
    rm -f "$DIR/bin/$name"
    case "$name" in
      claude) write_claude "$path" "$home" ;;
      codex) write_codex "$path" "$home" ;;
      *) ln -s "$path" "$DIR/bin/$name" ;;
    esac
    echo "  $name -> $path"
  done
}

write_claude() {
  cat > "$DIR/bin/claude" <<WRAP
#!/bin/bash
# agt sandbox: the real claude, only on a named profile folder inside the sandbox (.claude-a, .claude-b),
# never on a folder of your real setup.
REAL="$1"
SANDBOX_HOME="$2"
case "\${1:-}" in --version|-v|--help|-h) exec "\$REAL" "\$@" ;; esac
refuse() { echo "agt-sandbox: claude refused: \$1. Run claude-agt-a or claude-agt-b (or set CLAUDE_CONFIG_DIR=\$SANDBOX_HOME/.claude-a)." >&2; exit 1; }
real_dir() {  # the real path of folder \$1, or of its existing parent plus its name; fails otherwise
  local raw="\${1%/}" name parent
  [ -n "\$raw" ] || return 1
  case "\$raw" in /*) ;; *) raw="\$PWD/\$raw" ;; esac
  if [ -d "\$raw" ]; then (cd "\$raw" && pwd -P); return; fi
  name="\${raw##*/}"
  parent="\${raw%/*}"
  case "\$name" in "" | . | ..) return 1 ;; esac
  [ -d "\${parent:-/}" ] || return 1
  printf '%s/%s\n' "\$(cd "\${parent:-/}" && pwd -P)" "\$name"
}
[ -n "\${CLAUDE_CONFIG_DIR:-}" ] || refuse "no CLAUDE_CONFIG_DIR (the sandbox uses named profiles only)"
dir="\$(real_dir "\$CLAUDE_CONFIG_DIR")" || refuse "\$CLAUDE_CONFIG_DIR isn't a folder, and neither is the folder it would be made in"
case "\$dir" in
  "\$SANDBOX_HOME/.claude") refuse "the sandbox uses named profiles only, not its default .claude" ;;
  "\$SANDBOX_HOME"/*) exec "\$REAL" "\$@" ;;
  *) refuse "\$CLAUDE_CONFIG_DIR is outside the sandbox" ;;
esac
WRAP
  chmod +x "$DIR/bin/claude"
}

write_codex() {
  cat > "$DIR/bin/codex" <<WRAP
#!/bin/bash
# agt sandbox: the real codex, only with a CODEX_HOME inside the sandbox, and logins kept in files there.
REAL="$1"
SANDBOX_HOME="$2"
case "\${1:-}" in --version|-V|--help|-h) exec "\$REAL" "\$@" ;; esac
real_dir() {  # the real path of folder \$1, or of its existing parent plus its name; fails otherwise
  local raw="\${1%/}" name parent
  [ -n "\$raw" ] || return 1
  case "\$raw" in /*) ;; *) raw="\$PWD/\$raw" ;; esac
  if [ -d "\$raw" ]; then (cd "\$raw" && pwd -P); return; fi
  name="\${raw##*/}"
  parent="\${raw%/*}"
  case "\$name" in "" | . | ..) return 1 ;; esac
  [ -d "\${parent:-/}" ] || return 1
  printf '%s/%s\n' "\$(cd "\${parent:-/}" && pwd -P)" "\$name"
}
home="\${CODEX_HOME:-\$HOME/.codex}"
home="\$(real_dir "\$home")" || { echo "agt-sandbox: codex refused: CODEX_HOME isn't a folder, and neither is the folder it would be made in." >&2; exit 1; }
case "\$home" in
  "\$SANDBOX_HOME"/*) exec "\$REAL" -c 'cli_auth_credentials_store="file"' "\$@" ;;
  *) echo "agt-sandbox: codex refused: CODEX_HOME \$home is outside the sandbox." >&2; exit 1 ;;
esac
WRAP
  chmod +x "$DIR/bin/codex"
}

make_keychain() {
  # The sandbox's own keychain: Claude Code saves its logins in the default keychain, and with this HOME
  # macOS looks for it only at ~/Library/Keychains/login.keychain-db. A keychain file named login.keychain-db
  # can't be unlocked with its own password, so that name is a link to agt-sandbox.keychain-db.
  local folder="$DIR/home/Library/Keychains" file="agt-sandbox.keychain-db"
  mkdir -p "$folder"
  chmod 700 "$folder"
  if [ ! -e "$folder/$file" ]; then
    HOME="$DIR/home" security create-keychain -p "$KEYCHAIN_PASSWORD" "$folder/$file"
    HOME="$DIR/home" security set-keychain-settings "$folder/$file"  # no lock on sleep or timeout
  fi
  ln -sfn "$file" "$folder/login.keychain-db"
  HOME="$DIR/home" security unlock-keychain -p "$KEYCHAIN_PASSWORD" "$folder/$file"
  [ "$(HOME="$DIR/home" security default-keychain 2>/dev/null | tr -d ' "')" = "$folder/$file" ] \
    || die "the sandbox keychain isn't its default keychain; logins would fail"
  echo "  keychain: $folder/$file"
}

write_launchers() {
  # Commands for your own terminal, so you never need to put your shell into the sandbox: claude-agt-NAME per
  # sandbox profile, and agt-sandbox. Each sources env.sh for its own process and runs the sandbox's CLIs by
  # full path, past your shell's own `claude` function and your PATH.
  local folder
  rm -rf "$DIR/launchers"
  mkdir -p "$DIR/launchers"
  for folder in "$DIR"/home/.claude-*; do
    [ -d "$folder" ] || continue
    write_claude_launcher "${folder##*/.claude-}"
  done
  cat > "$DIR/launchers/agt-sandbox" <<LAUNCH
#!/bin/bash
# agt sandbox: run one command inside the sandbox (agt-sandbox agt doctor), or with none open a sandbox shell.
# The sandbox's HOME, PATH and agt apply to that command or shell only; exit the shell to leave.
if [ "\$#" -eq 0 ]; then
  exec /bin/bash --noprofile --rcfile "$DIR/env.sh" -i
fi
. "$DIR/env.sh" >/dev/null
exec "\$@"
LAUNCH
  chmod +x "$DIR/launchers/agt-sandbox"
  link_launchers
}

write_claude_launcher() {
  local file="$DIR/launchers/claude-agt-$1"
  cat > "$file" <<LAUNCH
#!/bin/bash
# agt sandbox: Claude Code on the sandbox profile .claude-$1, with the sandbox's HOME, agt and keychain.
# Your own claude command, its profiles and its logins are untouched.
. "$DIR/env.sh" >/dev/null
export CLAUDE_CONFIG_DIR="\$HOME/.claude-$1"
[ "\$#" -eq 0 ] && [ -t 0 ] && [ -t 2 ] && echo "agt sandbox: Claude Code on claude-$1 (\$CLAUDE_CONFIG_DIR), in \$PWD" >&2
exec "$DIR/bin/claude" "\$@"
LAUNCH
  chmod +x "$file"
}

is_default_dir() {
  [ "$(cd "$DIR" 2>/dev/null && pwd -P)" = "$(cd "$DEFAULT_DIR" 2>/dev/null && pwd -P)" ]
}

link_launchers() {
  # Only the default sandbox gets links, so two sandboxes never fight over the same names. A file of that name
  # that isn't a link into this sandbox is left alone.
  local launcher name link target
  if ! is_default_dir || [ ! -d "$LINKS" ]; then
    echo "  launchers: $DIR/launchers (not linked into $LINKS; run them by their full path)"
    return
  fi
  unlink_launchers stale
  for launcher in "$DIR"/launchers/*; do
    name="${launcher##*/}"
    link="$LINKS/$name"
    if [ -e "$link" ] || [ -L "$link" ]; then
      target="$(readlink "$link" || true)"
      case "$target" in
        "$DIR"/launchers/*) ;;
        *) echo "  $link exists and isn't this sandbox's: left alone (run $launcher)"; continue ;;
      esac
    fi
    ln -sfn "$launcher" "$link"
    echo "  $name: $link"
  done
  case ":$PATH:" in
    *":$LINKS:"*) ;;
    *) echo "  $LINKS isn't on your PATH: add it, or run the launchers from $DIR/launchers" ;;
  esac
}

unlink_launchers() {
  # all: every link into this sandbox's launchers; stale: only those whose launcher is gone.
  local link target
  for link in "$LINKS"/claude-agt-* "$LINKS"/agt-sandbox; do
    [ -L "$link" ] || continue
    target="$(readlink "$link")"
    case "$target" in "$DIR"/launchers/*) ;; *) continue ;; esac
    if [ "$1" = all ] || [ ! -e "$target" ]; then
      rm -f "$link"
      echo "  removed $link"
    fi
  done
}

launcher_prefix() {
  # Empty when the launchers are on PATH through their links, else their folder.
  if is_default_dir && [ "$(readlink "$LINKS/agt-sandbox" 2>/dev/null)" = "$DIR/launchers/agt-sandbox" ]; then
    printf ''
  else
    printf '%s/launchers/' "$DIR"
  fi
}

install_agt() {
  # No background watcher: its launchd label is the same as your real one's. Set again on each install, since an
  # uninstall test removes ~/.agt.
  # shellcheck disable=SC1091
  (
    source "$DIR/env.sh" >/dev/null
    uv tool install --force --reinstall --managed-python "$REPO" >/dev/null && agt --version
    agt config set share.watcher false >/dev/null && agt config set share.notify false >/dev/null
  )
}

create() {
  real_home
  guard_dir
  command -v uv >/dev/null || die "uv isn't on PATH (https://docs.astral.sh/uv/getting-started/installation/)"
  if [ -e "$DIR" ] && [ ! -e "$DIR/$MARK" ]; then
    die "$DIR exists and isn't a sandbox; pick another folder with --dir"
  fi
  mkdir -p "$DIR/home/.claude-a" "$DIR/home/.claude-b" "$DIR/home/.codex" "$DIR/bin" "$DIR/uv-tools" "$DIR/launchd"
  touch "$DIR/$MARK"
  # Codex keeps this login in a file under CODEX_HOME, never in the keychain.
  if [ ! -e "$DIR/home/.codex/config.toml" ]; then
    printf 'cli_auth_credentials_store = "file"\n' > "$DIR/home/.codex/config.toml"
  fi
  echo "Sandbox at $DIR"
  [ "$(uname)" = "Darwin" ] && make_keychain
  link_clis
  write_env
  write_launchers
  install_agt
  local run
  run="$(launcher_prefix)"
  cat <<NEXT

Next, from any terminal (your own claude command stays as it is):
  ${run}claude-agt-a             # Claude Code on sandbox profile a: /login, then /exit
  ${run}claude-agt-b             # profile b: /login with the other account, then /exit
  ${run}agt-sandbox codex login  # optional; stored in the sandbox's .codex/auth.json
  ${run}agt-sandbox              # a shell inside the sandbox; exit to leave
Logins go to the sandbox's own keychain; your real keychain and logins are never touched.
NEXT
}

reinstall() {
  real_home
  [ -e "$DIR/$MARK" ] || die "no sandbox at $DIR; run create first"
  [ "$(uname)" = "Darwin" ] && make_keychain
  link_clis
  write_env
  write_launchers
  install_agt
  # Claude Code and Codex cache a plugin by its version: take it out and put it back so the clone's files load.
  # shellcheck disable=SC1091
  (
    source "$DIR/env.sh" >/dev/null
    for folder in "$HOME"/.claude-*; do
      [ -d "$folder" ] || continue
      if CLAUDE_CONFIG_DIR="$folder" command claude plugin list 2>/dev/null | grep -q "agents-router@agents-router"; then
        CLAUDE_CONFIG_DIR="$folder" command claude plugin marketplace update agents-router >/dev/null 2>&1 || true
        CLAUDE_CONFIG_DIR="$folder" command claude plugin uninstall agents-router@agents-router >/dev/null 2>&1 || true
        CLAUDE_CONFIG_DIR="$folder" command claude plugin install agents-router@agents-router >/dev/null
        echo "  plugin reinstalled in $(basename "$folder")"
      fi
    done
    if command -v codex >/dev/null && codex plugin list 2>/dev/null | grep -q "agents-router"; then
      codex plugin marketplace upgrade agents-router >/dev/null 2>&1 || true
      codex plugin remove agents-router@agents-router >/dev/null 2>&1 || true
      codex plugin add agents-router@agents-router >/dev/null
      echo "  plugin reinstalled in codex"
    fi
  )
  echo "Reloaded agt from $REPO; start new sessions in the sandbox so they load it."
}

add_profile() {
  real_home
  [ -e "$DIR/$MARK" ] || die "no sandbox at $DIR; run create first"
  case "$NAME" in
    "" | -* | *[!a-z0-9-]*) die "a profile name uses a-z, 0-9 and -, as in: add-profile c" ;;
  esac
  mkdir -p "$DIR/home/.claude-$NAME"
  write_launchers
  echo "Next: $(launcher_prefix)claude-agt-$NAME, then /login and /exit."
}

check() {
  real_home
  [ -e "$DIR/$MARK" ] || die "no sandbox at $DIR"
  local link
  echo "Real setup:"
  echo "  claude on PATH: $(command -v claude || echo none)"
  echo "  agt on PATH: $(command -v agt || echo none) ($(agt --version 2>/dev/null || echo -))"
  for link in "$LINKS"/claude-agt-* "$LINKS"/agt-sandbox; do
    if [ -L "$link" ]; then echo "  sandbox launcher: $link"; fi
  done
  echo "  launchd agents-router jobs: $(launchctl list 2>/dev/null | grep -c agents-router || true)"
  # shellcheck disable=SC1091
  (
    source "$DIR/env.sh" >/dev/null
    echo "Sandbox:"
    echo "  HOME: $HOME"
    echo "  agt: $(command -v agt) ($(agt --version))"
    echo "  plugin source: $AGT_PLUGIN_SOURCE"
    echo "  work folder: $WORK"
    [ "$(uname)" = "Darwin" ] && echo "  keychain: $(security default-keychain 2>&1 | tr -d ' "')"
    for folder in "$HOME"/.claude-*; do
      [ -d "$folder" ] || continue
      status="$(CLAUDE_CONFIG_DIR="$folder" command claude auth status --json 2>/dev/null || true)"
      plugin="no plugin"
      grep -q '"agents-router@agents-router"' "$folder/plugins/installed_plugins.json" 2>/dev/null && plugin="plugin installed"
      printf '  %s: %s, %s\n' "$(basename "$folder")" "$(printf '%s' "$status" | tr -d '\n' | grep -o '"loggedIn": *[a-z]*' || echo 'login not checked')" "$plugin"
    done
    [ -e "$HOME/.claude.json" ] && echo "  WARNING: $HOME/.claude.json exists: the default folder was used" || true
  )
}

destroy() {
  real_home
  guard_dir
  [ -e "$DIR/$MARK" ] || die "no sandbox at $DIR (no $MARK marker); nothing removed"
  read -r -p "Remove the sandbox at $DIR? Its logins are logged out first. [y/N] " answer
  [ "$answer" = "y" ] || [ "$answer" = "Y" ] || { echo "Nothing removed."; exit 0; }
  # shellcheck disable=SC1091
  (
    source "$DIR/env.sh" >/dev/null
    # Log out first: it also ends each login's session with Anthropic, not only the local copy.
    for folder in "$HOME"/.claude-* "$HOME"/.agents-router/homes/claude/*; do
      [ -d "$folder" ] || continue
      CLAUDE_CONFIG_DIR="$folder" command claude auth logout >/dev/null 2>&1 && echo "  logged out $(basename "$folder")" || true
    done
    if [ -e "$HOME/.codex/auth.json" ] && command -v codex >/dev/null; then
      codex logout >/dev/null 2>&1 && echo "  logged out codex" || true
    fi
    if [ -e "$HOME/Library/Keychains/agt-sandbox.keychain-db" ]; then
      security delete-keychain "$HOME/Library/Keychains/agt-sandbox.keychain-db" && echo "  removed the sandbox keychain"
    fi
  )
  unlink_launchers all
  rm -rf "$DIR"
  if [ -e "$WORK/$WORK_MARK" ]; then rm -rf "$WORK"; fi
  echo "Removed $DIR."
}

case "$ACTION" in
  create) create ;;
  reinstall) reinstall ;;
  add-profile) add_profile ;;
  check) check ;;
  destroy) destroy ;;
  *) sed -n '2,/^# They run in/p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
