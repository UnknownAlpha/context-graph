# context-graph

A knowledge graph over a repository that gives a model the few files that matter, in about 6k tokens, with
`file:line` citations it can be held to. Works with any model: as a Claude Code plugin it uses whatever model
Claude Code is configured for; the standalone tools talk to any OpenAI-compatible endpoint.

## Claude Code plugin

Requirements on the machine: Claude Code, `git`, and `uv` (or `python3`; the launcher falls back to a venv and
pip). Nothing else. The first launch installs the Python dependencies into the plugin folder, about a minute;
later launches are instant.

Install for every session:

```bash
claude plugin marketplace add UnknownAlpha/context-graph
claude plugin install context-graph@context-graph
```

Or try it once from a checkout: `claude --plugin-dir /path/to/context-graph`.

Then, inside any project:

```
/ctx why does the quota dot never turn green after a tenant is committed?
/ctx how does index.html reach the ResourceQuota template?
/ctx what breaks if I rename tenant_status in app.py?
/ctx what is this project?
/ctx ingest https://github.com/kubernetes-sigs/kustomize     # make another repo available
/ctx @kustomize how is a strategic merge patch applied?      # ask about it
/ctx targets                                                 # what is indexed
```

What happens: the `context_pack` tool returns the one-page map of the repo, the files the graph ranks highest for
the question, and numbered slices around the matched symbols in the top three, in one call. The model answers
from that with `file:line` citations and reads more only if something is missing. `path` and `deps` answer "how
does A reach B" and "what depends on X" from the graph directly. The index lives in `.repomap/` inside the
project (add it to `.gitignore`) and rebuilds itself when files change. Nothing leaves the machine except the
prompt Claude Code sends to its model.

### Several repos at once

The project you started Claude Code in is always the default. `ingest <url or path>` makes another repo
available without changing that: clones go to a per-user store (`~/.local/share/context-graph/workspaces`, or
`$CONTEXT_GRAPH_STORE`), once per URL, so a second project that ingests the same URL attaches in a second with no
new download. Name a target with `@slug`, or mention it in the question (its slug, or a file path that exists only
there) and the tools route to it. Every tool result starts with `[repo: <slug>]`.

- `/ctx targets all` shows the shared store with sizes and which projects use each clone.
- `/ctx forget <slug>` detaches a repo from the current project; the clone stays for other projects.
- `/ctx prune` lists clones no project uses; `/ctx prune apply` deletes them.
- `/ctx ingest <url> local` clones into the project's own `.ctx/workspaces` instead, for a self-contained folder.

A project's attachments are recorded in `.ctx/registry.json`, which ignores itself in git.

### Permissions

Tools: `context_pack`, `locate`, `read_file`, `grep`, `deps`, `path`, `repo_map`, `index_status`, `targets`,
`ingest`, `forget`, `prune`. All but the last three only read. The plugin ships a hook that pre-approves the
read-only tools. If Claude Code's auto mode still blocks one, allow the server once under `/permissions`:

```
mcp__plugin_context-graph_context-graph
```

The plugin runs a Python MCP server with your user's privileges, as every plugin with an MCP server does; the
source is in this repository.

## Standalone use with any model

Everything the plugin does is also available without Claude Code, against any OpenAI-compatible chat endpoint:
vLLM, Ollama, LiteLLM, OpenAI, OpenRouter, Zhipu, llama.cpp server and others.

```bash
git clone https://github.com/UnknownAlpha/context-graph && cd context-graph
uv venv .venv && uv pip install --python .venv/bin/python -e ".[server]"
cp .env.example .env     # MODEL_BASE_URL (ends in /v1), MODEL_API_KEY, MODEL_NAME; see the file for examples
.venv/bin/python -c "import config; print(config.describe()); print([m.id for m in config.client().models.list().data][:5])"
```

`MODEL_API_KEY` may be a key, `none` for servers without auth, or `cmd:<command>` to obtain it from a command at
startup (for example `cmd:oc whoami -t` for an OpenShift AI route). `MODEL_FAST` names a cheaper model for
classification and judging. `MODEL_EXTRA_BODY` is JSON merged into every request for provider-specific knobs, such
as turning a reasoning model's thinking off. Claude Code's `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN` and
`ANTHROPIC_MODEL` are read as fallbacks when they point at a proxy that also serves `/v1/chat/completions`.

### Ask questions from the command line

```bash
.venv/bin/python agent.py <repo> "why does the quota dot never turn green" --mode auto
.venv/bin/python agent.py <repo> "<question>" --all-modes          # compare modes side by side
.venv/bin/python agent.py <repo> --chat --mode auto                # interactive; /mode, /reset, /quit
```

Modes: `auto` (default: one call on graph-selected slices, escalating to a capped tool loop only if the answer
hedges), `single` (one call, no escalation), `agent` (map plus tools, model drives), `grep-agent` (same without
the graph, as a control). Every run writes a transcript under `runs/` with tool calls, files read, tokens, time
and a citation check.

### OpenAI-compatible endpoint

```bash
.venv/bin/python server.py --port 8765
```

Point any OpenAI client at `http://127.0.0.1:8765/v1`. The first message with a git URL clones and indexes it;
later messages ask about it; the `model` field may name an indexed repo. Every answer ends with a footer: repo
and commit, mode, tokens, seconds, and how many citations verified. Private git hosts: `GIT_TOKEN_GITLAB` or
`GIT_TOKEN_GITHUB` in `.env`. This is the same pipeline the plugin uses, with the model call made here instead
of by Claude Code.

### MCP server for other IDEs

`context-graph-mcp [<repo>]` is the stdio MCP server the plugin wraps; register it in any MCP client. For VS Code
agent mode, a `.vscode/mcp.json` entry pointing at `.venv/bin/context-graph-mcp` with the workspace folder as the
argument is enough.

## Evaluation

The claims above are measured, not assumed. `cases.json` holds questions with the files a person would have to
open and a short rubric; the harness runs every case through the chosen modes and has `MODEL_FAST` grade each
answer against the rubric.

```bash
.venv/bin/python compare.py <repo> --show     # locators only, no model: hit@3/5 and tokens per answer
.venv/bin/python tune.py <repo> --write       # grid-search params.json on the tuning cases, report holdout
.venv/bin/python eval.py <repo>               # end to end: accuracy, median and worst time, tokens, per mode
```

Add a case: `id`, `question`, `expected_files`, `rubric`, and `holdout: true` to keep it out of tuning.

## How it works

Four layers, built deterministically on every change, no model involved:

1. **Leaves**: files and their lines. Read only in slices.
2. **Entities and typed edges**: tree-sitter symbols for eleven languages, YAML names, document headings;
   `defines`, `references`, `mentions`, and `shares_literal` edges from string fragments that appear in more than
   one file, which is what links code to config to templates. Names defined in many files get no reference edges;
   import-shaped strings are ignored.
3. **Ranking and communities**: PageRank over files and symbols; test files rank below the code they test.
4. **The map**: one page, about 1.2k tokens, always sent first.

Per question: lexical seeds over bodies and symbols, one hop of spreading activation along the edges, the top
files, slices around the matched symbols, a fixed token budget, then one model call. Citations in the answer are
checked against the checkout by code. The model is told that the graph is a hint, that the repo may contain no
bug, and that "cannot confirm" is an acceptable answer.

## Files

| File | Role |
|---|---|
| `mcp_server.py` | the tools, routing between repos, the shared store |
| `tools.py` | read-only tools jailed to a repo, slices, citation check |
| `treesitter_locator.py`, `common.py` | graph build, ranking, locate, deps, path |
| `ingest.py` | clone or refresh, store layout |
| `agent.py`, `config.py`, `server.py` | standalone runner, model settings, OpenAI-compatible endpoint |
| `graphify_locator.py` | optional: post-processes a graphify graph if one exists |
| `eval.py`, `compare.py`, `tune.py`, `cases.json`, `params.json` | evaluation and tuning |
| `skills/ctx/SKILL.md`, `hooks/hooks.json`, `scripts/`, `.claude-plugin/` | the Claude Code plugin |
