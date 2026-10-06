# context-graph

Points a model at the few files that matter in a repo, then lets it read them.
Two locators, a GLM runner with three modes, an eval harness, an OpenAI-compatible server, and a Claude Code
plugin. Everything runs in WSL:

```bash
cd ~/tasks/context-graph
```

## Claude Code plugin (fastest way to use it)

This directory is a Claude Code plugin. It needs `uv` on the machine (or `python3`; the launcher falls back to a
venv and pip) and nothing else. First launch installs the Python dependencies into `.venv` here, later launches are
instant.

Try it for one session, from any repo:

```bash
claude --plugin-dir ~/tasks/context-graph
```

Install it for every session:

```bash
claude plugin marketplace add UnknownAlpha/context-graph
claude plugin install context-graph@context-graph
```

Then in any project:

```
/ctx why does the quota dot never turn green after a tenant is committed?
/ctx how does index.html reach the ResourceQuota template?
/ctx what breaks if I rename tenant_status in app.py?
/ctx ingest https://github.com/kubernetes-sigs/kustomize     # clones into .ctx/workspaces and registers it
/ctx @kustomize how is a strategic merge patch applied?      # ask about an ingested repo
/ctx targets                                                 # what is indexed
```

Several repos at once: the project you started Claude Code in is always the default. `ingest` attaches a repo
without changing that. Name a target with `@slug`, or just mention it in the question (its slug or a file path
that exists only there) and the tools route to it. Every tool result starts with `[repo: <slug>]` so a wrong guess
is visible.

Where things live: clones go to a per-user store, `~/.local/share/context-graph/workspaces` (or `$CONTEXT_GRAPH_STORE`,
or the plugin data dir Claude Code provides), once per URL, with the index inside each clone. A project records
which clones it uses in `.ctx/registry.json`, which ignores itself in git. So ingesting a URL a second project
already has takes a second and no disk. `/ctx targets all` shows the store with sizes; `/ctx forget <slug>`
detaches a repo from the current project; `/ctx prune` lists clones no project uses and `/ctx prune apply`
deletes them. `ingest ... local` keeps a clone inside the project's `.ctx/workspaces` instead, for a fully
self-contained folder.

What happens: the `context_pack` MCP tool returns the one-page map, the files the knowledge graph ranks highest and
numbered slices around the matched symbols, about 6k tokens, in one call. Claude answers from that with
`file:line` citations and only reads more if something is missing. `path` and `deps` answer "how does A reach B"
and "what depends on X" from the graph directly. The index lives in `.repomap/` inside the project (add it to
`.gitignore`) and rebuilds itself when files change. Nothing leaves the machine except the prompt to whichever
model Claude Code is configured to use.

Tools exposed: `context_pack`, `locate`, `read_file`, `grep`, `deps`, `path`, `repo_map`, `ingest`, `index_status`.

Auto mode: Claude Code's auto-mode classifier may block MCP tools it cannot evaluate. The plugin ships a PreToolUse hook
that pre-approves its read-only tools. If a tool is still blocked, add this once under `/permissions` (or
`permissions.allow` in `~/.claude/settings.json`), using the plugin-scoped server name:

```
mcp__plugin_context-graph_context-graph
```

## Setup

```bash
cp .env.example .env      # fill in GLM_BASE_URL (ends in /v1), GLM_API_KEY, GLM_MODEL
.venv/bin/python -c "import config; print(config.client().models.list().data[0].id)"   # smoke test
```

Build the locators for a repo (no GLM needed, runs in well under a second; rerun after commits):

```bash
.venv/bin/python graphify_locator.py build ~/tasks/ocp-tenant-provisioning     # needs graphify-out/graph.json
.venv/bin/python treesitter_locator.py build ~/tasks/ocp-tenant-provisioning   # no graphify needed
```

## Chat endpoint (OpenAI-compatible)

```bash
.venv/bin/python server.py --port 8765        # from a terminal logged into the model's cluster if GLM_API_KEY=oc
```

Point any OpenAI client at `http://127.0.0.1:8765/v1` (any API key). First message: paste a git URL, with or
without a question. The server clones it into `workspaces/<owner-repo>` (shallow), builds the graph in seconds,
and answers. Later messages in the same conversation keep working on that repo; the client sends the history.
Or set the model to an indexed workspace name (`GET /v1/models` lists them) and skip the URL.

```bash
curl -s localhost:8765/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model": "context-graph",
  "messages": [{"role": "user", "content": "https://github.com/kubernetes-sigs/kustomize  how does a strategic merge patch get applied to a resource?"}]}'
```

- Modes: `auto` (default: one cheap call on graph-selected slices, escalate to a capped tool loop only if the
  answer hedges), `single`, `agent`, `grep-agent`. Send `/mode agent` as a message to switch, or
  `"metadata": {"mode": "agent"}` in the request.
- Every answer ends with a footer: repo@commit, mode, tokens, tool calls, seconds, and how many `file:line`
  citations were verified against the checkout. The JSON response also carries `context_graph` with the tool
  trace and files read. Transcripts land in `runs/server/`.
- Private hosts: `GIT_TOKEN_GITLAB` / `GIT_TOKEN_GITHUB` in `.env` are injected into the clone URL for that host.
- `POST /ingest {"url": ...}` clones/refreshes and indexes without asking anything.
- VS Code: add the URL as an OpenAI-compatible provider (Manage Models); the model list shows each indexed repo.

## Ask your own questions

One question, one mode (`agent` is the default; also `grep-agent`, `single`):

```bash
.venv/bin/python agent.py ~/tasks/ocp-tenant-provisioning "why does the quota dot never turn green" --mode agent
.venv/bin/python agent.py ~/tasks/ocp-tenant-provisioning "why does the quota dot never turn green" --all-modes
```

Interactive, with follow-ups in the same conversation:

```bash
.venv/bin/python agent.py ~/tasks/ocp-tenant-provisioning --chat --mode agent
#   /mode single | /mode grep-agent | /mode agent    switch mode (clears history)
#   /reset                                            clear history
#   /quit
```

Every run writes `runs/<timestamp>/<question>-<mode>.md` (tool calls, files read, tokens, answer) and a `.json` twin.

Modes:
- `agent`: GLM gets the 1.2k-token repo map plus tools `locate`, `grep`, `read_file` and drives itself.
- `grep-agent`: same without `locate`. The control that shows whether the graph earns its tokens.
- `single`: the pipeline calls `locate`, reads the top three files, one completion, no tools.

## Use it from VS Code agent mode instead

The same tools are exposed as an MCP server (`mcp_server.py`). Already registered for the tenant repo in
`ocp-tenant-provisioning/.vscode/mcp.json`, and a custom agent with the runner's rules lives in
`ocp-tenant-provisioning/.github/agents/context-graph.agent.md`.

1. Open `~/tasks/ocp-tenant-provisioning` in VS Code through the WSL remote (the server command is a WSL path).
   If you open it as a Windows UNC folder instead, change `command` in `.vscode/mcp.json` to `wsl` and prepend
   `-e /home/muhammadtalha/tasks/context-graph/.venv/bin/python` to `args`.
2. Add your GLM in Copilot Chat: model picker -> Manage Models -> OpenAI-compatible provider, URL ending in `/v1`,
   model id `glm-53-fp8-v10`, enter the key when prompted. If the option is missing, bring-your-own-key is disabled on
   your Copilot plan.
3. In the chat, pick agent mode, the `context-graph` agent, and the GLM model. VS Code starts the MCP server on demand;
   check it under the tools icon (`locate`, `grep`, `read_file`, `repo_map`).

Self-test without VS Code: `.venv/bin/python mcp_check.py ~/tasks/ocp-tenant-provisioning`.
For another repo, register the server the same way with that repo's folder as the argument (build its locator first).

## Evaluate

```bash
.venv/bin/python compare.py ~/tasks/ocp-tenant-provisioning --show    # locators only, no GLM: hit@3/5 + tokens
.venv/bin/python tune.py ~/tasks/ocp-tenant-provisioning --write      # grid-search params.json on tuning cases
.venv/bin/python eval.py ~/tasks/ocp-tenant-provisioning              # GLM end to end, all modes, judge, report.md
.venv/bin/python eval.py ~/tasks/ocp-tenant-provisioning --modes agent,grep-agent --cases quota-never-ready,create-415
```

Add a case to `cases.json`: `id`, `question`, `expected_files` (what a person must open), `rubric` (two lines the judge compares against), `holdout` (true keeps it out of tuning).

## Files

- `common.py` tokeniser, BM25 index, string-literal matching, spreading activation, params
- `graphify_locator.py` locator A: cleans graphify's graph.json, adds literal edges, `locate()`, `page()`
- `treesitter_locator.py` locator B: tree-sitter symbols + literal edges, same interface, no LLM
- `tools.py` the three tools, jailed to the repo
- `agent.py` runner and CLI, `eval.py` judge and report, `compare.py` / `tune.py` locator metrics
- `viz.py` renders a locator graph to HTML: `python viz.py <repo>/graphify-out/locator.json out.html`
