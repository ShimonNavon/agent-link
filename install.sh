#!/bin/sh
# Installs the agent-link CLI on this machine, then registers its identity on the hub.
#
#   curl -fsSL http://ubuntu03:30110/install.sh | sh
#   curl -fsSL http://ubuntu03:30110/install.sh | sh -s -- --id fedora-opencode --name "OpenCode (Fedora)"
#
# The CLI is one Python file with no dependencies. Re-running is safe.
set -eu

HUB="${AGENT_LINK_URL:-__HUB_URL__}"
[ "$HUB" = "__HUB_URL__" ] && HUB="http://ubuntu03:30110"
ID=""; NAME=""; KIND=""; MACHINE=""; DESC=""
while [ $# -gt 0 ]; do
  case "$1" in
    --id) ID="${2:?}"; shift 2 ;;
    --name) NAME="${2:?}"; shift 2 ;;
    --kind) KIND="${2:?}"; shift 2 ;;
    --machine) MACHINE="${2:?}"; shift 2 ;;
    --description) DESC="${2:?}"; shift 2 ;;
    --url) HUB="${2:?}"; shift 2 ;;
    -h|--help) sed -n '2,9p' "$0" 2>/dev/null || true; exit 0 ;;
    *) echo "install.sh: unknown argument $1" >&2; exit 2 ;;
  esac
done

command -v python3 >/dev/null 2>&1 || { echo "install.sh: python3 is required" >&2; exit 1; }

BIN="${AGENT_LINK_BIN:-$HOME/.local/bin}"
mkdir -p "$BIN"
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
curl -fsSL "${HUB%/}/link.py" -o "$TMP"
python3 -c 'import ast,sys; ast.parse(open(sys.argv[1]).read())' "$TMP"
install -m 0755 "$TMP" "$BIN/agent-link"
echo "installed $BIN/agent-link"

case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "note: $BIN is not on PATH; add it to your shell profile" ;;
esac

if [ -n "$ID" ]; then
  set -- init --id "$ID" --url "$HUB"
  [ -n "$NAME" ] && set -- "$@" --name "$NAME"
  [ -n "$KIND" ] && set -- "$@" --kind "$KIND"
  [ -n "$MACHINE" ] && set -- "$@" --machine "$MACHINE"
  [ -n "$DESC" ] && set -- "$@" --description "$DESC"
  AGENT_LINK_URL="$HUB" "$BIN/agent-link" "$@"
else
  echo "next: agent-link init --id <machine-agent> --name '<human name>' --url $HUB"
fi

echo
echo "MCP (remote, preferred):  ${HUB%/}/mcp/<your-id>"
echo "MCP (stdio, fallback):    agent-link mcp"
echo "Docs:                     https://projects.navonsimon.com/agent-link.html"
