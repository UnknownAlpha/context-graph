# context-graph

Knowledge-graph context for repositories: a Claude Code plugin (`/ctx`) plus standalone tools against any
OpenAI-compatible model. README.md is the user-facing description; this file is for working in the code.

## Layout and conventions

- Flat Python modules at the root, installed editable into `.venv` (`uv pip install -e ".[server]"`). The wheel
  include list is in `pyproject.toml`; add new modules there.
- This directory is also the plugin root: `.claude-plugin/plugin.json`, `skills/ctx/SKILL.md`, `hooks/hooks.json`,
  `scripts/context-graph-mcp.sh`. Keep executables in `scripts/`, never a top-level `bin/` (Anthropic's org sync
  rejects it). Bump `version` in `plugin.json` for every change users should receive.
- Tool names as Claude Code sees them: `mcp__plugin_context-graph_context-graph__<tool>`. The permission rule for the
  whole server is `mcp__plugin_context-graph_context-graph`. The hook matcher in `hooks/hooks.json` must list every
  read-only tool by name; `ingest`, `forget`, `prune` write and stay out of it.
- The graph is deterministic (tree-sitter + string-literal edges + PageRank). Anything an LLM adds must be tagged
  inferred. Do not add an LLM step to ingest without a cache keyed by content hash.
- Per-project state lives in `<project>/.ctx/` (self-ignoring in git, skipped by the walker) and `<project>/.repomap/`
  (the index). Clones live once per user in `~/.local/share/context-graph/workspaces` (or `$CONTEXT_GRAPH_STORE`);
  projects attach via `.ctx/registry.json`. Nothing is sticky: every tool takes `repo`.
- Model settings for standalone use come from `config.py` (`MODEL_*`, with `GLM_*` and `ANTHROPIC_*` fallbacks).
  The plugin itself never calls a model.

## Verify before committing

```bash
.venv/bin/python tests/smoke_mcp.py <repo>        # tools list, index_status, context_pack, path over stdio
.venv/bin/python tests/config_check.py            # config resolution in clean environments
claude plugin validate . --strict
.venv/bin/python compare.py <repo>                # locator hit@3/5 without a model
.venv/bin/python eval.py <repo>                   # end to end with a model; needs .env
```

Run `tests/smoke_mcp.py` from an unrelated directory too: the server must pick the repo up from `CONTEXT_GRAPH_REPO`.

## Rules that came from testing

- Refuse to index a home directory, a drive root, or a folder with more than 3,000 files and no `.git` (`_guard`).
- `context_pack` first, at most a few extra reads; the repo may contain no bug; "cannot confirm" is a valid answer;
  every claim cites `file:line`; namespaces and flags in suggested commands must come from a cited line.
- Reasoning models spend most of the wall time thinking; measure time to a correct answer, worst case included.

## Git identity

Commits in this repo are authored as `UnknownAlpha` (set in the local git config). Do not add co-author trailers here.
