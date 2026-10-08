"""Shared helpers for both locators: file walking, tokenising, literal matching, ranking."""
import math
import os
import re
from collections import Counter, defaultdict
from pathlib import Path

CODE_EXT = {".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".html", ".sh", ".go", ".java", ".rb", ".rs", ".kt",
            ".c", ".h", ".cpp", ".cc", ".hpp", ".tf", ".toml", ".json"}
YAML_EXT = {".yaml", ".yml"}
TEXT_EXT = {".md", ".txt", ".rst"}
SKIP_DIRS = {".git", "graphify-out", ".repomap", ".ctx", "node_modules", ".venv", "venv", "__pycache__", ".vscode", ".claude",
             ".idea", "dist", "build", "vendor", "third_party", "target", ".tox", ".mypy_cache", ".pytest_cache",
             "site-packages", "coverage", ".next", ".cache"}
MAX_FILE_BYTES = 400_000   # skip minified bundles, lockfiles, generated blobs
STOP = {"the", "and", "for", "with", "this", "that", "from", "into", "are", "not", "but", "all", "any",
        "can", "has", "have", "was", "were", "will", "its", "our", "your", "you", "use", "used", "one",
        "two", "true", "false", "none", "null", "name", "value", "type", "kind", "spec", "metadata",
        "apiversion", "when", "then", "than", "does", "what", "which", "where", "why", "how", "after",
        "before", "never", "always", "should", "must", "return", "returns", "self", "def", "var", "function"}


DOC_EXT = {".pdf", ".docx", ".pptx", ".xlsx", ".xlsm", ".csv", ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"}
MAX_DOC_BYTES = 60_000_000


def kind_of(path: str) -> str:
    ext = Path(path).suffix.lower()
    if ext in CODE_EXT:
        return "code"
    if ext in YAML_EXT:
        return "yaml"
    if ext in TEXT_EXT:
        return "text"
    if ext in DOC_EXT:
        return "doc"
    return ""


def iter_files(root: str):
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in filenames:
            p = Path(dirpath) / f
            rel = p.relative_to(root).as_posix()
            kind = kind_of(rel)
            if not kind or f.endswith((".min.js", ".lock", "-lock.json")):
                continue
            try:
                if p.stat().st_size > (MAX_DOC_BYTES if kind == "doc" else MAX_FILE_BYTES):
                    continue
            except OSError:
                continue
            yield rel


def read(root: str, rel: str) -> str:
    """File text; for documents and images, the cached extracted text (see documents.py)."""
    if kind_of(rel) == "doc":
        try:
            import documents
            return documents.read_document(str(root), rel)
        except Exception:  # noqa: BLE001
            return ""
    try:
        return (Path(root) / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


_SPLIT = re.compile(r"[^A-Za-z0-9]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def tokens(s: str):
    out = []
    for chunk in _SPLIT.split(s or ""):
        for part in _CAMEL.split(chunk):
            t = part.lower()
            if len(t) >= 3 and t not in STOP and not t.isdigit():
                out.append(t)
    return out


def est_tokens(s: str) -> int:
    return max(1, len(s) // 4)


# ----------------------------------------------------------------- literal matching
# Placeholders that templates and format strings use; removed before comparing fragments.
_PLACEHOLDER = re.compile(r"%\([^)]*\)[sdfr]|%[sdfr]|\{\{[^}]*\}\}|\{[^{}]*\}|\$\{[^}]*\}|\$[A-Za-z_][A-Za-z0-9_]*|\+ *[A-Za-z_.]+ *\+?")
_STR = re.compile(r'"([^"\\\n]{4,})"|\'([^\'\\\n]{4,})\'|`([^`\n]{4,})`')
_YAML_SCALAR = re.compile(r"^\s*(?:-\s+)?[A-Za-z0-9_.\-/]+:\s+(.+?)\s*(?:#.*)?$", re.M)
_YAML_ARG = re.compile(r"^\s*-\s+(--?[A-Za-z0-9\-]+=?.*?)\s*$", re.M)


def _keep(frag: str) -> bool:
    frag = frag.strip(" \t'\"`,;:")
    if len(frag) < 5 or len(frag) > 80:
        return False
    if not re.search(r"[A-Za-z]", frag):
        return False
    # Needs some structure so plain words such as "quota" do not match everywhere.
    return bool(re.search(r"[-/_.:]", frag) or re.search(r"[a-z][A-Z]", frag))


def literal_fragments(text: str, kind: str):
    frags = set()
    cands = []
    if kind == "code":
        for m in _STR.finditer(text):
            cands.append(next(g for g in m.groups() if g is not None))
    elif kind == "yaml":
        cands += [m.group(1) for m in _YAML_SCALAR.finditer(text)]
        cands += [m.group(1) for m in _YAML_ARG.finditer(text)]
        cands += [next(g for g in m.groups() if g is not None) for m in _STR.finditer(text)]
    else:
        cands += re.findall(r"`([^`\n]{4,})`", text)
        if kind == "doc":
            # extracted documents have no code quoting: take structured tokens (hostnames, paths, ids, env names)
            cands += re.findall(r"\b[A-Za-z0-9][A-Za-z0-9_.-]*(?:[./:_-][A-Za-z0-9_.-]+)+\b", text)
    for c in cands:
        for piece in _PLACEHOLDER.split(c):
            piece = piece.strip(" \t'\"`,;:")
            if _keep(piece):
                frags.add(piece)
            # also the last path segment / suffix, e.g. "-quota" from "%s-quota" -> "-quota"
    return frags


_IMPORTY = re.compile(r"^(?:[a-z0-9-]+\.)+[a-z]{2,}/|^(?:@[\w-]+/)?[\w-]+/[\w./-]+$|^(?:k8s\.io|sigs\.k8s\.io|golang\.org|google\.golang\.org)/")


def literal_edges(texts: dict, min_len: int = 5, max_share: float = 0.2, max_files: int = 8):
    """Return [(fileA, fileB, fragment, weight)] for fragments that appear in both files.

    A fragment shared by many files (repo name, GitLab host, an import path) says little, so its
    weight is 1/(files-1), anything shared by more than max_files or max_share of the corpus is dropped,
    and import-path-shaped strings are skipped outright.
    """
    owners = defaultdict(set)
    for rel, txt in texts.items():
        for f in literal_fragments(txt, kind_of(rel)):
            if len(f) >= min_len and not _IMPORTY.match(f):
                owners[f].add(rel)
    n = max(1, len(texts))
    cap = max(3, min(max_files, int(n * max_share)))
    edges = []
    for frag, files in owners.items():
        if 1 < len(files) <= cap:
            fl = sorted(files)
            w = 1.0 / (len(fl) - 1)
            for i in range(len(fl)):
                for j in range(i + 1, len(fl)):
                    edges.append((fl[i], fl[j], frag, w))
    return edges


def spread(G, seeds: dict, hops: int = 1, decay: float = 0.5):
    """Spreading activation: seed scores flow along weighted edges for a few hops.

    Unlike full PageRank this does not drift toward global hubs; a file is boosted
    only if it is directly coupled to something the question matched.
    """
    score = dict(seeds)
    frontier = dict(seeds)
    for _ in range(hops):
        nxt = defaultdict(float)
        for n, s in frontier.items():
            if n not in G:
                continue
            deg = sum(G.edges[n, m].get("weight", 1.0) for m in G.neighbors(n)) or 1.0
            for m in G.neighbors(n):
                nxt[m] += s * decay * G.edges[n, m].get("weight", 1.0) / deg
        for m, s in nxt.items():
            score[m] = score.get(m, 0.0) + s
        frontier = nxt
    return score


# ----------------------------------------------------------------- tunable params
import json as _json

HERE = Path(__file__).resolve().parent
DEFAULT_PARAMS = {
    "decay": 0.6,            # how much seed score flows to neighbours per hop
    "hops": 1,               # graph hops from seeds
    "body_weight": 1.0,      # weight of plain grep over file bodies relative to node-label matches
    "structured_boost": 1.0, # multiplier for query tokens that look like identifiers (digits, camelCase, a-b, A_B)
    "common_penalty": 1.0,   # multiplier for query tokens present in more than half the files
    "rationale_weight": 1.0, # weight of rationale text vs labels when seeding graph nodes
}


def load_params(path: Path = None) -> dict:
    p = path or HERE / "params.json"
    out = dict(DEFAULT_PARAMS)
    if p.exists():
        try:
            out.update(_json.loads(p.read_text(encoding="utf-8")))
        except ValueError:
            pass
    return out


_CHUNK = re.compile(r"[A-Za-z0-9_.\-/]+")


def structured_tokens(query: str):
    """Tokens coming from query chunks that look like identifiers rather than prose."""
    out = set()
    for ch in _CHUNK.findall(query or ""):
        core = ch.strip(".-/_")
        if len(core) < 3:
            continue
        looks = (any(c.isdigit() for c in core) or _CAMEL.search(core) is not None
                 or re.search(r"[A-Za-z][-_./][A-Za-z]", core) is not None
                 or (core.isupper() and len(core) >= 3))
        if looks:
            out.update(tokens(core))
    return out


# ----------------------------------------------------------------- grep baseline
class FileIndex:
    """BM25-lite over token counts. Used as the grep baseline and for seeding."""

    def __init__(self, texts: dict = None, counters: dict = None):
        if counters is None:
            counters = {rel: Counter(tokens(txt) + tokens(rel) * 3) for rel, txt in (texts or {}).items()}
        self.tf = counters
        df = Counter()
        for c in self.tf.values():
            df.update(k for k, v in c.items() if v > 0)
        n = max(1, len(self.tf))
        self.n = n
        self.share = {t: d / n for t, d in df.items()}
        self.idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}
        self.avg = sum(sum(c.values()) for c in self.tf.values()) / n

    def score(self, query: str, structured_boost: float = 1.0, common_penalty: float = 1.0):
        q = tokens(query)
        st = structured_tokens(query) if structured_boost != 1.0 else set()
        w = {}
        for t in q:
            m = 1.0
            if t in st:
                m *= structured_boost
            if self.share.get(t, 0) > 0.5:
                m *= common_penalty
            w[t] = w.get(t, 0) + m
        out = {}
        for rel, c in self.tf.items():
            L = sum(c.values()) or 1.0
            s = 0.0
            for t, m in w.items():
                f = c.get(t, 0)
                if f:
                    s += m * self.idf.get(t, 0) * (f * 2.2) / (f + 1.2 * (0.25 + 0.75 * L / self.avg))
            if s:
                out[rel] = s
        return out

    def rank(self, query: str, k: int = 5, **kw):
        return sorted(self.score(query, **kw).items(), key=lambda x: -x[1])[:k]


_BODY_CACHE = {}


def body_index(repo: str) -> "FileIndex":
    """FileIndex over file bodies, cached per repo for the life of the process."""
    key = str(Path(repo).resolve())
    if key not in _BODY_CACHE:
        _BODY_CACHE[key] = FileIndex({rel: read(repo, rel) for rel in iter_files(repo)})
    return _BODY_CACHE[key]


def trim(lines, budget: int):
    out, used = [], 0
    for ln in lines:
        t = est_tokens(ln) + 1
        if used + t > budget:
            out.append(f"... (trimmed at ~{budget} tokens)")
            break
        out.append(ln)
        used += t
    return "\n".join(out)
