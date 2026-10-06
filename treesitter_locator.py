"""Locator B: deterministic repo map in the style of Aider's repo-map.

tree-sitter finds definitions and references in code (Python, JS, JS inside HTML);
YAML and Markdown contribute names and literal strings; PageRank ranks files.
No LLM anywhere, rebuilds in well under a second.

Build:  python treesitter_locator.py build <repo>            -> <repo>/.repomap.json + page
Query:  python treesitter_locator.py locate <repo> "<question>" [--budget 600]
Page:   python treesitter_locator.py page <repo> [--budget 1500]
"""
import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import networkx as nx
from tree_sitter_language_pack import get_parser

from common import (FileIndex, body_index, est_tokens, iter_files, kind_of, literal_edges, load_params, read,
                    spread, tokens, trim)

DEFS = {
    "python": {"function_definition", "class_definition"},
    "javascript": {"function_declaration", "class_declaration", "method_definition", "variable_declarator"},
    "typescript": {"function_declaration", "class_declaration", "method_definition", "interface_declaration",
                   "type_alias_declaration", "enum_declaration", "variable_declarator"},
    "tsx": {"function_declaration", "class_declaration", "method_definition", "interface_declaration",
            "type_alias_declaration", "variable_declarator"},
    "go": {"function_declaration", "method_declaration", "type_spec"},
    "java": {"method_declaration", "class_declaration", "interface_declaration", "enum_declaration", "constructor_declaration"},
    "rust": {"function_item", "struct_item", "enum_item", "trait_item", "type_item", "const_item"},
    "ruby": {"method", "class", "module", "singleton_method"},
    "kotlin": {"function_declaration", "class_declaration", "object_declaration"},
    "c": {"function_definition", "struct_specifier", "type_definition"},
    "cpp": {"function_definition", "class_specifier", "struct_specifier", "type_definition"},
}
_LANG_BY_EXT = {".py": "python", ".js": "javascript", ".mjs": "javascript", ".jsx": "javascript", ".html": "javascript",
                ".ts": "typescript", ".tsx": "tsx", ".go": "go", ".java": "java", ".rs": "rust", ".rb": "ruby",
                ".kt": "kotlin", ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp"}
_IDENT_TYPES = {"identifier", "property_identifier", "attribute", "type_identifier", "field_identifier"}
AMBIGUOUS_DEFS = 3          # a name defined in more files than this gets no reference edges
TEST_PENALTY = 0.5          # tests rank below the code they test unless the question is about tests
_TEST_RE = re.compile(r"(_test\.\w+$|\.test\.\w+$|(^|/)(tests?|testdata|__tests__|spec)/)")


def _def_name(node, src: bytes):
    """Name of a definition node, handling languages that nest it (C/C++ declarators)."""
    n = node.child_by_field_name("name")
    if n is None:
        d = node.child_by_field_name("declarator")
        while d is not None and d.type not in ("identifier", "field_identifier"):
            d = d.child_by_field_name("declarator") or next((c for c in d.children if "declarator" in c.type), None)
        n = d
    return src[n.start_byte:n.end_byte].decode(errors="replace") if n is not None else None
COMMON_NAMES = {"self", "print", "len", "str", "int", "dict", "list", "set", "open", "range", "get", "append",
                "join", "format", "match", "group", "read", "write", "json", "os", "re", "sys", "document",
                "window", "console", "fetch", "then", "catch", "push", "forEach", "map", "filter", "value",
                "textContent", "innerHTML", "hidden", "disabled", "className", "length", "indexOf", "test",
                "startsWith", "endsWith", "strip", "split", "items", "keys", "isinstance", "sorted", "float",
                "e", "f", "m", "n", "r", "s", "v", "k", "d", "a", "b", "c", "el", "id", "$", "log"}


def _lang(rel: str):
    return _LANG_BY_EXT.get(Path(rel).suffix.lower())


_PARSERS = {}


def _parser(lang):
    if lang not in _PARSERS:
        try:
            _PARSERS[lang] = get_parser(lang)
        except Exception:  # noqa: BLE001  grammar missing from the pack
            _PARSERS[lang] = None
    return _PARSERS[lang]


def _source_for(rel: str, text: str):
    if rel.endswith(".html"):
        return "\n".join(m.group(1) for m in re.finditer(r"<script[^>]*>(.*?)</script>", text, re.S))
    return text


def _walk(node):
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(n.children)


def extract(rel: str, text: str):
    """Return (defs: {name: line}, refs: Counter(name))."""
    lang = _lang(rel)
    defs, refs = {}, Counter()
    parser = _parser(lang) if lang else None
    if parser is None:
        return defs, refs
    src = _source_for(rel, text).encode()
    tree = parser.parse(src)
    for n in _walk(tree.root_node):
        if n.type in DEFS[lang]:
            if n.type == "variable_declarator":
                val = n.child_by_field_name("value")
                if not val or val.type not in ("function_expression", "arrow_function", "function"):
                    continue
            name = _def_name(n, src)
            if name and name not in COMMON_NAMES:
                defs.setdefault(name, n.start_point[0] + 1)
        elif n.type in _IDENT_TYPES:
            if n.type == "attribute":
                attr = n.child_by_field_name("attribute")
                if attr is None:
                    continue
                n = attr
            if n.parent is not None and n.parent.type in DEFS[lang] and n.parent.child_by_field_name("name") == n:
                continue
            name = src[n.start_byte:n.end_byte].decode(errors="replace")
            if name not in COMMON_NAMES and len(name) > 2:
                refs[name] += 1
    return defs, refs


_YAML_NAME = re.compile(r"^\s*(?:-\s+)?(?:name|secretName|serviceAccountName|configMap|image|path|host):\s*[\"']?([A-Za-z0-9_.\-/{}$ ]+?)[\"']?\s*(?:#.*)?$", re.M)


def yaml_names(text: str):
    out = set()
    for m in _YAML_NAME.finditer(text):
        v = re.sub(r"\{\{.*?\}\}", "", m.group(1)).strip(" -/")
        # a bare generic word ("tenant", "quota") is not a symbol; keep structured names only
        if len(v) >= 4 and not v.startswith("{") and (re.search(r"[-/_.]", v) or len(v) >= 10):
            out.add(v)
    return out


def build(repo: str):
    texts = {rel: read(repo, rel) for rel in iter_files(repo)}
    G = nx.Graph()
    defined_in = defaultdict(set)
    per_file = {}
    for rel, txt in texts.items():
        G.add_node("file:" + rel, kind="file", file=rel)
        defs, refs = extract(rel, txt)
        if kind_of(rel) == "yaml":
            defs.update({n: 0 for n in yaml_names(txt)})
        per_file[rel] = {"defs": defs, "refs": refs}
        for name in defs:
            defined_in[name].add(rel)
    # A name defined in many files (String, Error, expected, main) cannot be resolved to one
    # target, so it gets no reference edges; it still exists as a symbol of each file.
    ambiguous = {name for name, fs in defined_in.items() if len(fs) > AMBIGUOUS_DEFS}
    # definition and reference edges (Aider style: referencing file -> defining file's symbol)
    for rel, d in per_file.items():
        for name, line in d["defs"].items():
            sid = f"sym:{name}@{rel}"
            G.add_node(sid, kind="sym", file=rel, name=name, line=line, ambiguous=name in ambiguous)
            G.add_edge("file:" + rel, sid, relation="defines", weight=0.3 if name in ambiguous else 1.0)
        for name, cnt in d["refs"].items():
            if name in ambiguous:
                continue
            for other in defined_in.get(name, ()):
                if other != rel:
                    G.add_edge("file:" + rel, f"sym:{name}@{other}", relation="references",
                               weight=1.0 + math.log(1 + cnt))
    # YAML/text files mention names defined elsewhere (e.g. docs mention app.py symbols, CI mentions paths)
    for rel, txt in texts.items():
        if _lang(rel):
            continue
        words = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", txt))
        for name in (words & set(defined_in)) - ambiguous:
            for other in defined_in[name]:
                if other != rel:
                    G.add_edge("file:" + rel, f"sym:{name}@{other}", relation="mentions", weight=0.7)
    lit, litw = defaultdict(set), defaultdict(float)
    for a, b, frag, w in literal_edges(texts):
        lit[(a, b)].add(frag)
        litw[(a, b)] += w
    for (a, b), frags in lit.items():
        G.add_edge("file:" + a, "file:" + b, relation="shares_literal", weight=min(1.0, litw[(a, b)]),
                   frags=sorted(frags, key=len, reverse=True)[:6])
    pr = nx.pagerank(G, weight="weight")
    data = nx.node_link_data(G, edges="links")
    data["pagerank"] = pr
    data["stats"] = {"files": len(texts), "symbols": sum(len(d["defs"]) for d in per_file.values()),
                     "nodes": G.number_of_nodes(), "edges": G.number_of_edges(), "literal_pairs": len(lit)}
    out = Path(repo, ".repomap")
    out.mkdir(exist_ok=True)
    (out / "map.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    (out / "page.md").write_text(page(repo, 1500), encoding="utf-8")
    return data["stats"]


_G_CACHE = {}


def ensure_built(repo: str) -> bool:
    """Rebuild map.json when any repo file is newer than it. Returns True if rebuilt."""
    out = Path(repo, ".repomap", "map.json")
    newest = max((Path(repo, rel).stat().st_mtime for rel in iter_files(repo)), default=0.0)
    if not out.exists() or out.stat().st_mtime < newest:
        build(repo)
        return True
    return False


def _load(repo: str):
    p = Path(repo, ".repomap", "map.json")
    key = (str(p), p.stat().st_mtime)
    if key not in _G_CACHE:
        d = json.loads(p.read_text(encoding="utf-8"))
        _G_CACHE.clear()
        _IDX_CACHE.pop(repo, None)
        _G_CACHE[key] = (nx.node_link_graph(d, edges="links"), d["pagerank"])
    return _G_CACHE[key]


def stats(repo: str) -> dict:
    d = json.loads(Path(repo, ".repomap", "map.json").read_text(encoding="utf-8"))
    return d.get("stats", {})


def _file_rank(G, pr):
    files = [n for n in G.nodes if n.startswith("file:")]
    return {f: pr[f] + sum(pr[s] for s in G.neighbors(f) if s.startswith("sym:") and G.nodes[s]["file"] == G.nodes[f]["file"])
            for f in files}


def _file_syms(G, pr, f):
    rel = G.nodes[f]["file"]
    syms = [s for s in G.neighbors(f) if s.startswith("sym:") and G.nodes[s]["file"] == rel]
    # distinctive names first: ambiguous ones (String, Error, main) only if nothing else exists
    return sorted(syms, key=lambda s: (G.nodes[s].get("ambiguous", False), -pr[s]))


def page(repo: str, budget: int = 1500, big_threshold: int = 60) -> str:
    """One-page map. Small repos: every file. Large repos: top-level directories, then the most central files."""
    G, pr = _load(repo)
    frank = _file_rank(G, pr)
    order = sorted(frank, key=lambda x: -frank[x])
    lines = ["# Repo map (deterministic graph: tree-sitter symbols + string couplings; hints only)"]
    if len(order) > big_threshold:
        dirs = defaultdict(list)
        for f in order:
            rel = G.nodes[f]["file"]
            dirs[rel.split("/")[0] if "/" in rel else "(root)"].append(f)
        lines.append(f"{len(order)} files. Directories by importance:")
        for d, fs in sorted(dirs.items(), key=lambda kv: -sum(frank[f] for f in kv[1]))[:25]:
            names = []
            for f in fs[:3]:
                names += [G.nodes[s]["name"] for s in _file_syms(G, pr, f)[:2]]
            lines.append(f"- {d}/ ({len(fs)} files): " + ", ".join(names[:6]))
        lines.append("Most connected files:")
        order = order[:20]
    for f in order:
        rel = G.nodes[f]["file"]
        syms = _file_syms(G, pr, f)
        shown = ", ".join(G.nodes[s]["name"] + (f":{G.nodes[s]['line']}" if G.nodes[s]["line"] else "") for s in syms[:8])
        lines.append(f"- {rel}: {shown}" if shown else f"- {rel}")
        nbr = {G.nodes[j]["file"] for j in G.neighbors(f) if G.nodes[j]["file"] != rel}
        for s in syms:
            nbr |= {G.nodes[j]["file"] for j in G.neighbors(s) if G.nodes[j]["file"] != rel}
        if nbr:
            lines.append(f"    links: {', '.join(sorted(nbr)[:6])}")
    return trim(lines, budget)


# ------------------------------------------------------------ graph queries
def _file_neighbors(G, rel: str):
    """Other files coupled to rel, with the relation and the symbol or literal that couples them."""
    out = defaultdict(set)
    fn = "file:" + rel
    if fn not in G:
        return out
    for j in G.neighbors(fn):
        e = G.edges[fn, j]
        if j.startswith("file:"):
            out[G.nodes[j]["file"]].add("shares " + ", ".join(e.get("frags", [])[:2]))
        elif G.nodes[j]["file"] != rel:
            out[G.nodes[j]["file"]].add(f"{e.get('relation')} {G.nodes[j]['name']}")
    for s in G.neighbors(fn):
        if s.startswith("sym:") and G.nodes[s]["file"] == rel:
            for j in G.neighbors(s):
                if j.startswith("file:") and G.nodes[j]["file"] != rel:
                    out[G.nodes[j]["file"]].add(f"uses {G.nodes[s]['name']}")
    return out


def deps(repo: str, rel: str, budget: int = 400) -> str:
    """Blast radius: files that reference symbols defined in rel or share literals with it, then one more hop."""
    G, _ = _load(repo)
    if "file:" + rel not in G:
        return f"{rel} is not in the graph (not a source/yaml/doc file, or not indexed yet)"
    first = _file_neighbors(G, rel)
    lines = [f"Files coupled to {rel} ({len(first)} direct):"]
    for f, hows in sorted(first.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"- {f}: " + "; ".join(sorted(hows)[:3]))
    second = set()
    for f in first:
        second |= set(_file_neighbors(G, f)) - set(first) - {rel}
    if second:
        lines.append(f"Second hop ({len(second)}): " + ", ".join(sorted(second)[:12]))
    return trim(lines, budget)


def path(repo: str, a: str, b: str, budget: int = 300) -> str:
    """Shortest chain of files/symbols between two names (file paths or symbol names)."""
    G, _ = _load(repo)

    def resolve(x):
        x = x.strip()
        if "file:" + x in G:
            return "file:" + x
        cands = [n for n in G.nodes if n.startswith("sym:") and G.nodes[n]["name"] == x] or \
                [n for n in G.nodes if x.lower() in (G.nodes[n].get("name") or G.nodes[n]["file"]).lower()]
        return cands[0] if cands else None

    na, nb = resolve(a), resolve(b)
    if not na or not nb:
        return f"could not resolve {'both' if not na and not nb else (a if not na else b)} in the graph"
    try:
        p = nx.shortest_path(G, na, nb)
    except nx.NetworkXNoPath:
        return f"no path between {a} and {b} in the graph"
    lines = [f"Path {a} -> {b} ({len(p) - 1} hops):"]
    for u, v in zip(p, p[1:]):
        e = G.edges[u, v]
        lab = lambda n: G.nodes[n]["file"] if n.startswith("file:") else f"{G.nodes[n]['name']} ({G.nodes[n]['file']}:{G.nodes[n]['line']})"
        rel = e.get("relation", "")
        if rel == "shares_literal":
            rel += " " + ", ".join(e.get("frags", [])[:2])
        lines.append(f"  {lab(u)} --{rel}--> {lab(v)}")
    return trim(lines, budget)


_IDX_CACHE = {}


def locate(repo: str, question: str, budget: int = 300, k: int = 5, params: dict = None):
    P = params or load_params()
    G, _ = _load(repo)
    if repo not in _IDX_CACHE:
        texts = {i: (G.nodes[i]["name"] + " " if i.startswith("sym:") else "") + G.nodes[i]["file"] for i in G.nodes}
        _IDX_CACHE[repo] = FileIndex(texts)
    seeds = _IDX_CACHE[repo].score(question, P["structured_boost"], P["common_penalty"])
    body = body_index(repo).score(question, P["structured_boost"], P["common_penalty"])
    top = max(body.values(), default=1.0) or 1.0
    for rel, s in body.items():
        seeds["file:" + rel] = seeds.get("file:" + rel, 0) + P["body_weight"] * s / top
    if not seeds:
        return "no symbols or files match; fall back to grep", []
    pr = spread(G, seeds, hops=int(P["hops"]), decay=P["decay"])
    fscore, why = defaultdict(float), defaultdict(list)
    for i, s in pr.items():
        if i not in G:
            if i.startswith("file:"):
                fscore[i[5:]] += s
            continue
        f = G.nodes[i]["file"]
        fscore[f] += s
        if i.startswith("sym:") and not G.nodes[i].get("ambiguous"):
            why[f].append((s, G.nodes[i]["name"], G.nodes[i]["line"]))
    about_tests = any(t in ("test", "tests", "testing", "spec") for t in tokens(question))
    if not about_tests:
        for f in list(fscore):
            if _TEST_RE.search(f):
                fscore[f] *= TEST_PENALTY
    ranked = sorted(fscore.items(), key=lambda x: -x[1])[:k]
    top_files = {f for f, _ in ranked}
    lines = [f"Files most likely involved for: {question}"]
    hits = []   # (file, [lines of matched symbols]) for slice-aware reads
    for f, s in ranked:
        top_syms = sorted(why[f], reverse=True)[:4]
        hits.append((f, [l for _, _, l in top_syms if l]))
        ents = ", ".join(f"{n}{':' + str(l) if l else ''}" for _, n, l in top_syms)
        lines.append(f"- {f}  ({ents})" if ents else f"- {f}")
        fn = "file:" + f
        best = None
        for j in (G.neighbors(fn) if fn in G else ()):
            e = G.edges[fn, j]
            if e.get("relation") == "shares_literal" and G.nodes[j]["file"] in top_files:
                if best is None or e.get("weight", 0) > best[0]:
                    best = (e.get("weight", 0), G.nodes[j]["file"], e.get("frags", [])[:2])
        if best:
            lines.append(f"    shares literals with {best[1]}: {', '.join(best[2])}")
    locate.last_hits = hits
    return trim(lines, budget), [f for f, _ in ranked]


def locate_hits(repo: str, question: str, k: int = 5, params: dict = None):
    """[(file, [symbol lines])] for the top files; used by the pipeline to read slices instead of whole files."""
    locate(repo, question, 300, k, params)
    return list(getattr(locate, "last_hits", []))


if __name__ == "__main__":
    cmd, repo = sys.argv[1], sys.argv[2]
    budget = int(sys.argv[sys.argv.index("--budget") + 1]) if "--budget" in sys.argv else None
    if cmd == "build":
        print(json.dumps(build(repo), indent=1))
    elif cmd == "page":
        print(page(repo, budget or 1500))
    elif cmd == "locate":
        txt, _ = locate(repo, sys.argv[3], budget or 600)
        print(txt)
        print(f"\n[~{est_tokens(txt)} tokens]")
    elif cmd == "deps":
        print(deps(repo, sys.argv[3]))
    elif cmd == "path":
        print(path(repo, sys.argv[3], sys.argv[4]))
