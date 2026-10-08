"""Locator A: post-processes graphify-out/graph.json into a clean, compact locator.

Build (no LLM, no human):  python graphify_locator.py build <repo>   -> <repo>/graphify-out/locator.json + page.md
Query:                     python graphify_locator.py locate <repo> "<question>" [--budget 600]
Page:                      python graphify_locator.py page <repo> [--budget 1500]
"""
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import networkx as nx

from common import (FileIndex, body_index, est_tokens, iter_files, literal_edges, load_params, read, spread,
                    tokens, trim)

DROP_RELATIONS = {"contains", "method", "imports", "imports_from"}   # derivable from ids / noise for a locator
DROP_LABEL_WORDS = {"template", "manifest", "ui", "file", "the", "concept"}


def _norm(label: str) -> frozenset:
    return frozenset(t for t in tokens(label) if t not in DROP_LABEL_WORDS)


def build(repo: str):
    root = Path(repo)
    g = json.loads((root / "graphify-out" / "graph.json").read_text(encoding="utf-8"))
    nodes = {n["id"]: n for n in g["nodes"]}

    # 1. drop external/stdlib nodes and nodes with no source file
    keep = {i for i, n in nodes.items() if not n.get("external") and n.get("source_file")}

    # 2. merge duplicates: same file + same normalised label; concept nodes into a
    #    non-concept node whose label tokens contain theirs.
    alias = {}
    by_key = {}
    for i in sorted(keep, key=lambda x: (nodes[x].get("file_type") == "concept", x)):
        n = nodes[i]
        key = (n["source_file"], _norm(n["label"]))
        if key in by_key:
            alias[i] = by_key[key]
        else:
            by_key[key] = i
    concepts = [i for i in keep if i not in alias and nodes[i].get("file_type") == "concept"]
    real = [i for i in keep if i not in alias and nodes[i].get("file_type") != "concept"]
    for c in concepts:
        ct = _norm(nodes[c]["label"])
        best = None
        for r in real:
            rt = _norm(nodes[r]["label"])
            if ct and ct <= rt and len(ct) / len(rt) >= 0.6:
                best = r
                break
        if best:
            alias[c] = best

    def canon(i):
        while i in alias:
            i = alias[i]
        return i

    G = nx.Graph()
    for i in keep:
        if i in alias:
            continue
        n = nodes[i]
        G.add_node(i, label=n["label"], file=n["source_file"], loc=n.get("source_location") or "",
                   rationale=n.get("rationale") or "", kind=n.get("file_type", "code"))
    # file nodes give a natural place to aggregate and to hang literal edges on
    files = {G.nodes[i]["file"] for i in G.nodes}
    for f in files:
        G.add_node("file:" + f, label=f, file=f, loc="", rationale="", kind="file")
    for i in list(G.nodes):
        if not i.startswith("file:"):
            G.add_edge(i, "file:" + G.nodes[i]["file"], relation="in_file", weight=0.5)

    kept_edges = 0
    for e in g["links"]:
        if e["relation"] in DROP_RELATIONS:
            continue
        s, t = canon(e["source"]), canon(e["target"])
        if s in G and t in G and s != t:
            G.add_edge(s, t, relation=e["relation"], weight=float(e.get("weight", 1.0)),
                       loc=e.get("source_location") or "")
            kept_edges += 1

    # 3. deterministic literal-match pass fills in cross-file string couplings
    texts = {rel: read(repo, rel) for rel in iter_files(repo)}
    lit, litw = defaultdict(set), defaultdict(float)
    for a, b, frag, w in literal_edges(texts):
        lit[(a, b)].add(frag)
        litw[(a, b)] += w
    for (a, b), frags in lit.items():
        fa, fb = "file:" + a, "file:" + b
        if fa not in G:
            G.add_node(fa, label=a, file=a, loc="", rationale="", kind="file")
        if fb not in G:
            G.add_node(fb, label=b, file=b, loc="", rationale="", kind="file")
        G.add_edge(fa, fb, relation="shares_literal", weight=min(1.0, litw[(a, b)]),
                   frags=sorted(frags, key=len, reverse=True)[:6])

    pr = nx.pagerank(G, weight="weight")
    data = nx.node_link_data(G, edges="links")
    data["pagerank"] = pr
    data["stats"] = {"graphify_nodes": len(nodes), "graphify_edges": len(g["links"]),
                     "merged": len(alias), "kept_nodes": G.number_of_nodes(), "kept_edges": G.number_of_edges(),
                     "graphify_edges_kept": kept_edges, "literal_pairs": len(lit)}
    out = root / "graphify-out" / "locator.json"
    out.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    (root / "graphify-out" / "page.md").write_text(page(repo, 1500), encoding="utf-8")
    return data["stats"]


_G_CACHE = {}


def newest_source_mtime(repo: str) -> float:
    return max((Path(repo, rel).stat().st_mtime for rel in iter_files(repo)), default=0.0)


def ensure_built(repo: str) -> bool:
    """Rebuild locator.json when graphify's graph or any repo file is newer than it. Returns True if rebuilt."""
    out = Path(repo, "graphify-out", "locator.json")
    src = Path(repo, "graphify-out", "graph.json")
    if not src.exists():
        return False
    if not out.exists() or out.stat().st_mtime < max(src.stat().st_mtime, newest_source_mtime(repo)):
        build(repo)
        return True
    return False


def _load(repo: str):
    p = Path(repo) / "graphify-out" / "locator.json"
    key = (str(p), p.stat().st_mtime)
    if key not in _G_CACHE:
        d = json.loads(p.read_text(encoding="utf-8"))
        _G_CACHE.clear()
        _LOC_CACHE.clear()
        _G_CACHE[key] = (nx.node_link_graph(d, edges="links"), d["pagerank"])
    return _G_CACHE[key]


def page(repo: str, budget: int = 1500) -> str:
    """Always-in-context map: top files by PageRank with their top entities and one-line rationales."""
    G, pr = _load(repo)
    per_file = defaultdict(list)
    for i in G.nodes:
        if not i.startswith("file:"):
            per_file[G.nodes[i]["file"]].append(i)
    frank = {f: pr.get("file:" + f, 0) + sum(pr[i] for i in ids) for f, ids in per_file.items()}
    lines = ["# Repo map (auto-generated; hints only, confirm by reading the file)"]
    for f in sorted(frank, key=lambda x: -frank[x]):
        ids = sorted(per_file[f], key=lambda i: -pr[i])
        ents = ", ".join(G.nodes[i]["label"] + (f"@{G.nodes[i]['loc']}" if G.nodes[i]["loc"] else "") for i in ids[:6])
        lines.append(f"- {f}: {ents}")
        rat = next((G.nodes[i]["rationale"] for i in ids if G.nodes[i]["rationale"]), "")
        if rat:
            lines.append(f"    {rat[:160]}")
        nbr = set()
        for i in ids + ["file:" + f]:
            for j in G.neighbors(i):
                jf = G.nodes[j]["file"]
                if jf != f:
                    nbr.add(jf)
        if nbr:
            lines.append(f"    links: {', '.join(sorted(nbr)[:6])}")
    return trim(lines, budget)


_LOC_CACHE = {}


def _node_index(repo: str, G, rationale_weight: float) -> FileIndex:
    key = (repo, rationale_weight)
    if key not in _LOC_CACHE:
        counters = {}
        for i in G.nodes:
            n = G.nodes[i]
            c = Counter(tokens(n["label"]) + tokens(n["file"]))
            if rationale_weight > 0 and n["rationale"]:
                for t in tokens(n["rationale"]):
                    c[t] += rationale_weight
            counters[i] = c
        _LOC_CACHE[key] = FileIndex(counters=counters)
    return _LOC_CACHE[key]


def locate(repo: str, question: str, budget: int = 300, k: int = 5, params: dict = None):
    """Seed on nodes and file bodies matching the question, spread one or two hops, rank files."""
    P = params or load_params()
    G, _ = _load(repo)
    idx = _node_index(repo, G, P["rationale_weight"])
    seeds = idx.score(question, P["structured_boost"], P["common_penalty"])
    # grep over file bodies is the floor; the graph adds a boost on top
    body = body_index(repo).score(question, P["structured_boost"], P["common_penalty"])
    top = max(body.values(), default=1.0) or 1.0
    for rel, s in body.items():
        seeds["file:" + rel] = seeds.get("file:" + rel, 0) + P["body_weight"] * s / top
    if not seeds:
        return "nothing matches; fall back to grep", []
    pr = spread(G, seeds, hops=int(P["hops"]), decay=P["decay"])
    fscore, why = defaultdict(float), defaultdict(list)
    for i, s in pr.items():
        if i not in G:          # seeded from a file the graph does not know yet
            if i.startswith("file:"):
                fscore[i[5:]] += s
            continue
        f = G.nodes[i]["file"]
        fscore[f] += s
        if not i.startswith("file:"):
            why[f].append((s, G.nodes[i]["label"], G.nodes[i]["loc"]))
    ranked = sorted(fscore.items(), key=lambda x: -x[1])[:k]
    top_files = {f for f, _ in ranked}
    lines = [f"Files most likely involved for: {question}"]
    for f, s in ranked:
        ents = ", ".join(f"{l}{'@' + loc if loc else ''}" for _, l, loc in sorted(why[f], reverse=True)[:3])
        lines.append(f"- {f}  ({ents})" if ents else f"- {f}")
        # the single strongest literal coupling to another ranked file is the useful surprise
        fn = "file:" + f
        best = None
        if fn in G:
            for j in G.neighbors(fn):
                e = G.edges[fn, j]
                if e.get("relation") == "shares_literal" and G.nodes[j]["file"] in top_files:
                    if best is None or e.get("weight", 0) > best[0]:
                        best = (e.get("weight", 0), G.nodes[j]["file"], e.get("frags", [])[:2])
        if best:
            lines.append(f"    shares literals with {best[1]}: {', '.join(best[2])}")
    return trim(lines, budget), [f for f, _ in ranked]


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
