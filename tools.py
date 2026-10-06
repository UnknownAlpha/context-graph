"""Tools the model may call. All paths are jailed to the repo root."""
import json
import re
from pathlib import Path

import graphify_locator as A
import treesitter_locator as B
from common import est_tokens, iter_files, read, trim

MAX_GREP_HITS = 30
READ_MAX_TOKENS = 5000
SLICE_MARGIN = 35          # lines around a matched symbol when reading a slice
SLICE_MAX_TOKENS = 2500    # per file in single mode


class Tools:
    def __init__(self, repo: str, use_locator: bool = True):
        self.repo = str(Path(repo).resolve())
        self.use_locator = use_locator
        self.has_graph = Path(self.repo, "graphify-out", "graph.json").exists()
        if self.has_graph:
            A.ensure_built(self.repo)
        B.ensure_built(self.repo)          # B always exists: deps/path/slices come from it
        self.calls = []          # (name, args, result_tokens)
        self.files_read = []

    # ------------------------------------------------------------------ tools
    def locate(self, question: str, budget: int = 300) -> str:
        loc = A.locate if self.has_graph else B.locate
        txt, _ = loc(self.repo, question, budget)
        return txt

    def locate_hits(self, question: str, k: int = 5):
        """[(file, [symbol lines])] from the deterministic graph, for slice-aware reads."""
        return B.locate_hits(self.repo, question, k)

    def grep(self, pattern: str, max_hits: int = MAX_GREP_HITS) -> str:
        try:
            rx = re.compile(pattern, re.I)
        except re.error as e:
            return f"bad regex: {e}"
        out = []
        for rel in iter_files(self.repo):
            for i, ln in enumerate(read(self.repo, rel).splitlines(), 1):
                if rx.search(ln):
                    out.append(f"{rel}:{i}: {ln.strip()[:160]}")
                    if len(out) >= max_hits:
                        out.append(f"... stopped at {max_hits} hits; narrow the pattern")
                        return "\n".join(out)
        return "\n".join(out) or "no matches"

    def read_file(self, path: str, start: int = 1, end: int = None, max_tokens: int = READ_MAX_TOKENS) -> str:
        rel = self._safe(path)
        if rel is None:
            return "refused: path must be relative and inside the repo"
        p = Path(self.repo, rel)
        if not p.is_file():
            return f"no such file: {rel}"
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, int(start or 1))
        end = min(len(lines), int(end)) if end else len(lines)
        body = [f"{i}: {lines[i - 1]}" for i in range(start, end + 1)]
        txt = "\n".join(body)
        if est_tokens(txt) > max_tokens:
            txt = trim(body, max_tokens) + f"\n(file has {len(lines)} lines; ask for a narrower range)"
        if rel not in self.files_read:
            self.files_read.append(rel)
        return f"== {rel} lines {start}-{end} of {len(lines)} ==\n{txt}"

    def read_slices(self, path: str, around_lines, max_tokens: int = SLICE_MAX_TOKENS) -> str:
        """Regions around the given lines, merged; whole file if it fits or no lines were given."""
        rel = self._safe(path)
        if rel is None or not Path(self.repo, rel).is_file():
            return f"no such file: {path}"
        lines = Path(self.repo, rel).read_text(encoding="utf-8", errors="replace").splitlines()
        n = len(lines)
        if est_tokens("\n".join(lines)) <= max_tokens or not around_lines:
            return self.read_file(rel, 1, n, max_tokens)
        spans = sorted((max(1, l - SLICE_MARGIN), min(n, l + SLICE_MARGIN)) for l in around_lines if l)
        merged = []
        for s, e in spans:
            if merged and s <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        out, used = [f"== {rel} ({n} lines; showing regions around matched symbols) =="], 0
        for s, e in merged:
            chunk = "\n".join(f"{i}: {lines[i - 1]}" for i in range(s, e + 1))
            t = est_tokens(chunk)
            if used + t > max_tokens:
                out.append(f"... ({rel}: more regions omitted; ask for lines {s}-{e} if needed)")
                break
            out.append(f"-- lines {s}-{e} --\n{chunk}")
            used += t
        if rel not in self.files_read:
            self.files_read.append(rel)
        return "\n".join(out)

    def deps(self, path: str) -> str:
        rel = self._safe(path)
        return B.deps(self.repo, rel) if rel else "refused: bad path"

    def path(self, a: str, b: str) -> str:
        return B.path(self.repo, a, b)

    def _safe(self, path: str):
        path = (path or "").strip().replace("\\", "/")
        if path.startswith("./"):
            path = path[2:]
        if not path or path.startswith("/") or ".." in path.split("/") or ":" in path:
            return None
        return path

    # --------------------------------------------------------------- plumbing
    def schemas(self):
        s = [
            {"type": "function", "function": {
                "name": "grep",
                "description": "Regex search over every file in the repo. Returns path:line: text, max 30 hits.",
                "parameters": {"type": "object", "properties": {
                    "pattern": {"type": "string", "description": "Python regex, case-insensitive"}},
                    "required": ["pattern"]}}},
            {"type": "function", "function": {
                "name": "read_file",
                "description": "Read a repo file with line numbers. Pass start/end for a range; files over ~5000 tokens are cut, pass a range.",
                "parameters": {"type": "object", "properties": {
                    "path": {"type": "string", "description": "repo-relative path, e.g. portal/app/app.py"},
                    "start": {"type": "integer"}, "end": {"type": "integer"}},
                    "required": ["path"]}}},
            {"type": "function", "function": {
                "name": "deps",
                "description": "Knowledge-graph blast radius: which files reference symbols defined in this file or share "
                               "names/strings with it, one and two hops. Use before claiming a change is safe.",
                "parameters": {"type": "object", "properties": {
                    "path": {"type": "string", "description": "repo-relative file path"}}, "required": ["path"]}}},
            {"type": "function", "function": {
                "name": "path",
                "description": "Knowledge-graph shortest chain between two files or symbol names, with the relation on each hop. "
                               "Use for 'how does A reach B' questions.",
                "parameters": {"type": "object", "properties": {
                    "a": {"type": "string"}, "b": {"type": "string"}}, "required": ["a", "b"]}}},
        ]
        if self.use_locator:
            s.insert(0, {"type": "function", "function": {
                "name": "locate",
                "description": "Ask the repo knowledge graph which files are most likely involved in a problem. "
                               "Returns a ranked list of files with the entities that matched and the string "
                               "couplings between them. Results are hints: confirm by reading the file.",
                "parameters": {"type": "object", "properties": {
                    "question": {"type": "string", "description": "the problem or symptom in plain words"}},
                    "required": ["question"]}}})
        return s

    def dispatch(self, name: str, args) -> str:
        if isinstance(args, str):
            try:
                args = json.loads(args or "{}")
            except ValueError:
                return "tool arguments were not valid JSON"
        fn = {"locate": self.locate, "grep": self.grep, "read_file": self.read_file,
              "deps": self.deps, "path": self.path}.get(name)
        if fn is None or (name == "locate" and not self.use_locator):
            return f"unknown tool {name}"
        try:
            out = fn(**{k: v for k, v in (args or {}).items() if v is not None})
        except TypeError as e:
            out = f"bad arguments for {name}: {e}"
        self.calls.append((name, args, est_tokens(out)))
        return out


# ------------------------------------------------------------ citation check
_CITE = re.compile(r"(?<![\w/])([\w.\-]+(?:/[\w.\-]+)+\.[A-Za-z0-9]+|[\w.\-]+\.[A-Za-z0-9]{1,5}):(\d+)(?:-(\d+))?")


def check_citations(repo: str, answer: str):
    """Return (ok, bad) lists of 'file:line' citations; bad = file missing or line out of range."""
    ok, bad, seen = [], [], set()
    files = {}
    for m in _CITE.finditer(answer or ""):
        rel, line = m.group(1), int(m.group(2))
        key = (rel, line)
        if key in seen:
            continue
        seen.add(key)
        if rel not in files:
            p = Path(repo, rel)
            if not p.is_file():
                # allow bare basenames the model shortened, e.g. app.py:286
                cands = [r for r in iter_files(repo) if r.endswith("/" + rel) or r == rel]
                p = Path(repo, cands[0]) if len(cands) == 1 else None
            files[rel] = len(p.read_text(encoding="utf-8", errors="replace").splitlines()) if p and p.is_file() else -1
        n = files[rel]
        (ok if 0 < line <= n else bad).append(f"{rel}:{line}")
    return ok, bad
