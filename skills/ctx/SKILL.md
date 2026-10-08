---
name: ctx
description: Answer questions about the current repository, or any repo ingested in this project, using its knowledge graph. Use for "why does X fail", "where is Y", "how does A reach B", "what breaks if I change Z", "what is this project", or any request to understand or diagnose a codebase. One tool call returns the map, the files that matter and sliced reads; answer from that with file:line citations.
argument-hint: "[@repo] <question>  |  ingest <git url, path, document or zip>  |  targets  |  caption [file]"
---
Answer `$ARGUMENTS` using the context-graph MCP tools.

Which repo:
- `@slug` at the start of the argument names an indexed repo; strip it and pass it as `repo` to every tool call.
- No `@slug`: pass `repo` empty. The tools default to the project Claude Code was started in, unless the question
  names an ingested repo or a file that exists in only one of them. Every tool result starts with `[repo: <slug>]`;
  if that is not the repo the user meant, repeat the call with `repo` set.
- `targets` lists what this project can answer about; `targets all` also shows the per-user store of cached
  clones. `ingest <url or path>` attaches a repo and reports its slug; it does not change the default. Clones are
  shared across projects, so a URL ingested before is only fetched, not cloned again. `forget <slug>` detaches;
  `prune` (dry run) and `prune apply` remove cached clones no project uses.

Procedure:
1. If the argument is `targets` or `targets all`, call `targets` (with `all=true` for the second) and show the
   table. `ingest <url or path>` → call `ingest` and report what was indexed, its slug, and whether it was cloned
   or already cached. `forget <slug>` → call `forget`. `prune` → call `prune` (dry run) and show the list;
   `prune apply` → call `prune` with `apply=true`. `caption [file]` → call `caption` (with `repo` if `@slug`
   was given) and report how many figures were described, or that no vision model is configured and where to
   set `MODEL_VISION_NAME` (`~/.config/context-graph/.env`). `index_status` → call it and show the result,
   including the `ocr`, `captions` and `figures` fields. Stop after any of these.
2. Call `context_pack` with the question (and `repo` if given). It returns the one-page map, the files the graph
   ranked highest, and numbered slices of the top three around the matched symbols, within a token budget.
3. For "how does A reach B" also call `path`; for "what breaks if I change X" also call `deps`.
4. Only if something essential is missing, call `read_file` for a specific range or `grep` for a specific
   pattern. At most four extra calls. Do not run shell commands; the tools are the only way to read the repo.
   Do not investigate files the symptom does not involve.
5. Answer in this shape:
   - Cause or answer, two to four sentences, stated as a conclusion.
   - Evidence: bullets of `path:line` with what the line shows. Every claim about code gets one.
   - Fix or next check: what to change, or the single command that decides it. Any namespace, resource name
     or flag in that command must come from a cited line, or be marked as an assumption.
   - Last line: `context-graph: <repo slug>, <n> tool calls, files read: <list>`.

Rules:
- The map and the ranking are hints from an automatically built graph. Confirm against the lines shown
  before stating a cause. Never cite a line you have not seen.
- The repo may contain no bug. If the code is consistent, say so, name the one or two most likely external
  causes (cluster state, timing, credentials, environment) and stop.
- If the pack lacks what is needed and the extra calls do not find it, say `cannot confirm: <what>`.
- Anything from general knowledge rather than the repo is labelled as such.
- Documents (PDF, DOCX, PPTX, XLSX, images) appear as extracted text with `# [page n]` and `[figure id]` markers;
  cite them as `file:line` like any file and mention the page when a marker is nearby. The lines after a figure
  marker are OCR text read from the image. A `[caption]` line is a model's description of the image, not text
  from the page: treat numbers in it as approximate and say so. If a question is about a chart or diagram and
  its figure has OCR text but no `[caption]`, say that the figure has not been described and that `/ctx caption`
  with a configured vision model would add a description; do not guess what the chart shows.
