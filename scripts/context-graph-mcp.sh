#!/usr/bin/env bash
# Launch the context-graph MCP server from the plugin's own Python environment.
# Prefers uv (creates/syncs .venv on first run); falls back to python3 + venv + pip.
set -euo pipefail
ROOT="${CONTEXT_GRAPH_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$ROOT"
if command -v uv >/dev/null 2>&1; then
  exec uv run --quiet --project "$ROOT" --extra docs context-graph-mcp
fi
if [ ! -x "$ROOT/.venv/bin/context-graph-mcp" ]; then
  python3 -m venv "$ROOT/.venv" >&2
  "$ROOT/.venv/bin/pip" install --quiet -e "$ROOT[docs]" >&2
fi
exec "$ROOT/.venv/bin/context-graph-mcp"
