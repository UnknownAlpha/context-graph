"""Expose the locator tools to any MCP client (Claude Code, VS Code agent mode, ...) over stdio.

  context-graph-mcp [<project>]   # project defaults to $CONTEXT_GRAPH_REPO, else the current directory

Targets: the project itself plus every repo ingested into <project>/.ctx/workspaces, recorded in
<project>/.ctx/registry.json. Every tool takes an optional `repo` (a slug from `targets`); with none, the
question is routed by slug or file path it mentions, else the project. Nothing is sticky.

Tools:
  targets()                      indexed repos: slug, path, commit, files
  context_pack(question, repo)   one call: map + ranked files + sliced reads, within a token budget (use first)
  locate / read_file / grep / deps / path / repo_map / index_status   all take `repo`
  ingest(url)                    clone into .ctx/workspaces and register; does not change any default
"""
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:  # mcp SDK 2.x
    from mcp.server.mcpserver import MCPServer as _Server  # noqa: E402
except ImportError:  # mcp SDK 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # noqa: E402

try:  # a ToolError's message reaches the client verbatim; a plain exception is masked
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:
    try:
        from mcp.server.fastmcp.exceptions import ToolError
    except ImportError:
        ToolError = RuntimeError

try:
    from mcp.types import ToolAnnotations
    RO = {"annotations": ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)}
    RW = {"annotations": ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)}
except Exception:  # noqa: BLE001  older SDK without annotations
    RO = RW = {}

import agent  # noqa: E402
import treesitter_locator as B  # noqa: E402
from common import est_tokens, iter_files, tokens  # noqa: E402
from tools import Tools  # noqa: E402

mcp = _Server("context-graph")
MAX_UNTRACKED_FILES = 3000   # a folder this big with no .git is probably not a project
_TOOLS = {}                  # path -> Tools


# ------------------------------------------------------------------ project and registry
def _project() -> str:
    arg = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else None
    return str(Path(arg or os.environ.get("CONTEXT_GRAPH_REPO") or os.getcwd()).resolve())


def _guard(repo: str):
    """Refuse places that are clearly not a project: a home directory, a root, or a huge folder with no .git."""
    p = Path(repo)
    if p == Path.home() or p.parent == p or str(p) in ("/home", "/Users", "/mnt/c"):
        return (f"refusing to index {repo}: it looks like a home or root directory. "
                "Start Claude Code inside a project folder, or run `/ctx ingest <git url>` to clone one.")
    if not (p / ".git").exists() and not (p / ".repomap").exists():
        n = 0
        for _ in iter_files(repo):
            n += 1
            if n > MAX_UNTRACKED_FILES:
                return (f"refusing to index {repo}: more than {MAX_UNTRACKED_FILES} files and no .git. "
                        "Start Claude Code inside the project you mean, or run `/ctx ingest <git url>`.")
    return None


def _registry_path() -> Path:
    return Path(_project(), ".ctx", "registry.json")


def _load_registry() -> dict:
    p = _registry_path()
    try:
        reg = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError:
        reg = {}
    return {k: v for k, v in reg.items() if Path(v.get("path", "")).is_dir()}


def _save_registry(reg: dict):
    p = _registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    gi = p.parent / ".gitignore"
    if not gi.exists():
        gi.write_text("*\n", encoding="utf-8")
    p.write_text(json.dumps(reg, indent=1), encoding="utf-8")


def _store_index_path() -> Path:
    import ingest as ing
    return ing.store_root() / "store.json"


def _load_store() -> dict:
    p = _store_index_path()
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError:
        return {}


def _save_store(st: dict):
    p = _store_index_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, indent=1), encoding="utf-8")


def _load_registry_at(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _dir_size_mb(path: str) -> float:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return round(total / 1e6, 1)


def _targets() -> dict:
    """slug -> {path, url, commit, branch, files, symbols, indexed_at}; the project is always first."""
    proj = _project()
    out = {Path(proj).name: {"path": proj, "url": "", "commit": "", "branch": "", "files": 0, "symbols": 0,
                             "indexed_at": "", "project": True}}
    for slug, v in _load_registry().items():
        if Path(v["path"]).resolve() != Path(proj):
            out[slug] = {**v, "project": False}
    return out


def _tools_for(path: str) -> Tools:
    if path not in _TOOLS:
        why = _guard(path)
        if why:
            raise ToolError(why)
        _TOOLS[path] = Tools(path)
    return _TOOLS[path]


# ------------------------------------------------------------------ routing
def _resolve(repo: str = "", question: str = ""):
    """Return (slug, path). Explicit repo wins; else route by a slug or file path the question mentions; else project."""
    tg = _targets()
    proj_slug = next(iter(tg))
    r = (repo or "").strip().lstrip("@")
    if r:
        if r in tg:
            return r, tg[r]["path"]
        low = r.lower()
        hits = [s for s in tg if low in s.lower()] or [s for s in tg if s.lower() in low]
        if len(hits) == 1:
            return hits[0], tg[hits[0]]["path"]
        if Path(r).expanduser().is_dir():
            p = str(Path(r).expanduser().resolve())
            return Path(p).name, p
        raise ToolError(f"unknown repo '{r}'. Indexed targets: {', '.join(tg)}. Use targets() or ingest(<url>).")
    q = question or ""
    words = set(tokens(q)) | set(re.findall(r"[A-Za-z0-9_.\-]+", q))
    named = [s for s in tg if not tg[s]["project"]
             and (s in words or any(part in words for part in s.split("-") if len(part) > 3))]
    if len(named) == 1:
        return named[0], tg[named[0]]["path"]
    paths = re.findall(r"[\w.\-]+(?:/[\w.\-]+)+\.\w+", q)
    if paths:
        owners = [s for s in tg if all(Path(tg[s]["path"], p).is_file() for p in paths)]
        if len(owners) == 1:
            return owners[0], tg[owners[0]]["path"]
    return proj_slug, tg[proj_slug]["path"]


def _tag(slug: str) -> str:
    return f"[repo: {slug}]"


# ------------------------------------------------------------------ tools
@mcp.tool(**RO)
def targets(all: bool = False) -> str:
    """List the indexed repos this project can answer about: the project itself plus what it attached with
    ingest. Pass a slug as `repo` to any other tool, or let the question's wording choose. `all=true` also lists
    the per-user store of cached clones shared by every project on this machine, with size and last use."""
    tg = _targets()
    lines = ["| slug | path | commit | files | symbols | note |", "|---|---|---|---|---|---|"]
    for sl, v in tg.items():
        note = "current project (default)" if v["project"] else (v.get("url") or "")
        lines.append(f"| {sl} | {v['path']} | {v.get('commit') or ''} | {v.get('files') or ''} | {v.get('symbols') or ''} | {note} |")
    if all:
        st = _load_store()
        lines += ["", f"Shared store: {_store_index_path().parent}", "",
                  "| slug | url | commit | size MB | last used | attached by |", "|---|---|---|---|---|---|"]
        for sl, v in st.items():
            if not Path(v.get("path", "")).is_dir():
                continue
            n = len([a for a in v.get("attached", []) if Path(a).is_dir()])
            lines.append(f"| {sl} | {v.get('url', '')} | {v.get('commit', '')} | {_dir_size_mb(v['path'])} | "
                         f"{v.get('last_used', '')} | {n} project(s) |")
        lines += ["", "forget(slug) detaches from this project; prune() removes store clones no project uses."]
    return "\n".join(lines)


@mcp.tool(**RO)
def context_pack(question: str, repo: str = "", budget: int = 9000) -> str:
    """Build the whole context for a question in one call: the repo map, the ranked files the knowledge graph
    points at, and sliced reads around the matched symbols in the top three files, cut to `budget` tokens.
    `repo` is a slug from targets(); leave empty to answer about the current project, or about the repo the
    question names. Read it, then answer with file:line citations. If something essential is missing, call
    read_file or grep for just that. Say 'cannot confirm: <what>' rather than guessing."""
    slug, path = _resolve(repo, question)
    t = _tools_for(path)
    loc = t.locate(question, 300)
    hits = t.locate_hits(question, 5)[:3]
    if not hits:
        hits = [(f, []) for f in [ln[2:].split("  (")[0].strip() for ln in loc.splitlines() if ln.startswith("- ")][:3]]
    parts = [_tag(slug), agent.repo_map(path, 1200), "", "## Files the graph points at", loc, ""]
    used = sum(est_tokens(p) for p in parts)
    for f, lines in hits:
        body = t.read_slices(f, lines)
        if used + est_tokens(body) > budget:
            parts.append(f"(budget reached; {f} not included, read_file it if needed)")
            break
        parts.append(body)
        parts.append("")
        used += est_tokens(body)
    parts.append(f"— context_pack {_tag(slug)}: {len(hits)} files, ~{used} tokens, index {B.stats(path).get('files', '?')} files. "
                 "Hints only: confirm claims against the lines above; cite file:line.")
    return "\n".join(parts)


@mcp.tool(**RO)
def locate(question: str, repo: str = "") -> str:
    """Ask the repo knowledge graph which files are most likely involved in a problem or question.
    Returns a ranked list of files with matched entities and string couplings between them. Hints only."""
    slug, path = _resolve(repo, question)
    return _tag(slug) + "\n" + _tools_for(path).locate(question, 300)


@mcp.tool(**RO)
def grep(pattern: str, repo: str = "") -> str:
    """Case-insensitive regex search over every source, YAML and Markdown file in the repo. Max 30 hits."""
    slug, path = _resolve(repo)
    return _tag(slug) + "\n" + _tools_for(path).grep(pattern)


@mcp.tool(**RO)
def read_file(path: str, repo: str = "", start: int = 1, end: int = 0) -> str:
    """Read a repo-relative file with line numbers. Pass start/end to read a range; files longer than
    ~5000 tokens are cut, so narrow the range for big files."""
    slug, root = _resolve(repo, path)
    return _tag(slug) + "\n" + _tools_for(root).read_file(path, start, end or None)


@mcp.tool(**RO)
def deps(path: str, repo: str = "") -> str:
    """Knowledge-graph blast radius: files that reference symbols defined in this file or share names and
    strings with it, one and two hops. Use before claiming a change is safe."""
    slug, root = _resolve(repo, path)
    return _tag(slug) + "\n" + _tools_for(root).deps(path)


@mcp.tool(**RO)
def path(a: str, b: str, repo: str = "") -> str:
    """Shortest chain in the knowledge graph between two files or symbol names, with the relation on each hop."""
    slug, root = _resolve(repo, a + " " + b)
    return _tag(slug) + "\n" + _tools_for(root).path(a, b)


@mcp.tool(**RO)
def repo_map(repo: str = "") -> str:
    """The auto-generated one-page map of a repo: most connected files, their entities, and links."""
    slug, root = _resolve(repo)
    why = _guard(root)
    if why:
        return why
    return _tag(slug) + "\n" + agent.repo_map(root, 1200)


@mcp.tool(**RO)
def index_status(repo: str = "") -> str:
    """What the knowledge graph covers for one repo: path, files, symbols, edges, and when it was built."""
    slug, root = _resolve(repo)
    why = _guard(root)
    if why:
        return why
    B.ensure_built(root)
    p = Path(root, ".repomap", "map.json")
    built = time.strftime("%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime)) if p.exists() else "never"
    import documents
    from common import kind_of
    docs = [r for r in iter_files(root) if kind_of(r) == "doc"]
    return json.dumps({"repo": slug, "path": root, "built": built, **B.stats(root),
                       "documents": len(docs), **documents.status()}, indent=1)


@mcp.tool(**RW)
def ingest(url: str, branch: str = "", local: bool = False) -> str:
    """Make a git repository, a local folder, a single document (PDF, DOCX, PPTX, XLSX, image) or a zip available to
    this project and add it to targets(). Clones go to a per-user store
    shared by every project on this machine, so a URL is cloned once and only fetched afterwards; `local=true`
    clones into <project>/.ctx/workspaces instead, for a fully self-contained project. A local directory path
    is indexed in place and attached without copying. Does not change the default repo: pass the slug as
    `repo`, or name it in the question."""
    import ingest as ing
    ing.WORKSPACES = Path(_project(), ".ctx", "workspaces") if local else ing.store_root()
    try:
        r = ing.ingest(url, branch or None)
    except Exception as e:  # noqa: BLE001
        return f"ingest failed: {e}"
    now = time.strftime("%Y-%m-%d %H:%M")
    reg = _load_registry()
    reg[r.slug] = {"path": r.path, "url": r.url, "commit": r.commit, "branch": r.branch, "files": r.files,
                   "symbols": r.symbols, "indexed_at": now}
    _save_registry(reg)
    in_store = not local and Path(r.path).is_relative_to(ing.store_root())
    if in_store:
        st = _load_store()
        e = st.setdefault(r.slug, {"url": r.url, "path": r.path, "created": now, "attached": []})
        e.update({"commit": r.commit, "branch": r.branch, "files": r.files, "symbols": r.symbols, "last_used": now})
        if _project() not in e["attached"]:
            e["attached"].append(_project())
        _save_store(st)
    _TOOLS.pop(r.path, None)
    how = "cloned" if r.cloned else ("refreshed, new commits" if r.refreshed else "already cached, unchanged")
    where = "shared store" if in_store else ("project-local" if local else "in place")
    return (f"Indexed {r.slug} at {r.commit} ({r.branch}): {r.files} files, {r.symbols} symbols in {r.built_s}s "
            f"({how}, {where}). Use repo=\"{r.slug}\" (or @{r.slug} in /ctx) to ask about it; the current project "
            f"stays the default.\n\n" + targets())


@mcp.tool(**RW)
def forget(slug: str) -> str:
    """Detach a repo from this project's targets. A cached clone stays in the shared store for other projects;
    prune() removes clones nobody uses."""
    reg = _load_registry()
    if slug not in reg:
        return f"'{slug}' is not attached to this project. Attached: {', '.join(reg) or 'none'}."
    path = reg.pop(slug)["path"]
    _save_registry(reg)
    st = _load_store()
    if slug in st and _project() in st[slug].get("attached", []):
        st[slug]["attached"].remove(_project())
        _save_store(st)
    _TOOLS.pop(path, None)
    return f"Detached {slug} from this project. Still cached at {path}; prune() deletes clones no project uses."


@mcp.tool(**RW)
def prune(apply: bool = False, unused_days: int = 30) -> str:
    """Delete cached clones from the shared store that no existing project has attached, or that no project has
    used for `unused_days`. Dry run by default: lists what would go and the space freed; apply=true deletes."""
    import shutil
    st = _load_store()
    cutoff = time.time() - unused_days * 86400
    victims, lines = [], []
    for sl, v in list(st.items()):
        p = Path(v.get("path", ""))
        if not p.is_dir():
            st.pop(sl)
            continue
        attached = [a for a in v.get("attached", [])
                    if sl in _load_registry_at(Path(a, ".ctx", "registry.json"))]
        try:
            last = time.mktime(time.strptime(v.get("last_used") or v.get("created") or "1970-01-01 00:00", "%Y-%m-%d %H:%M"))
        except ValueError:
            last = 0
        if not attached or last < cutoff:
            victims.append((sl, str(p), _dir_size_mb(str(p)), "unattached" if not attached else f"unused {unused_days}+ days"))
    if not victims:
        _save_store(st)
        return "nothing to prune"
    total = sum(v[2] for v in victims)
    for sl, path, mb, why in victims:
        lines.append(f"- {sl}: {path} ({mb} MB, {why})")
        if apply:
            shutil.rmtree(path, ignore_errors=True)
            st.pop(sl, None)
    _save_store(st)
    head = f"{'Deleted' if apply else 'Would delete'} {len(victims)} clone(s), {round(total, 1)} MB:"
    return "\n".join([head, *lines] + ([] if apply else ["", "Run prune(apply=true) to delete."]))


def main():
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        return
    mcp.run()


if __name__ == "__main__":
    main()
