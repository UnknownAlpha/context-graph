"""Clone or refresh a git repo into workspaces/<slug> and build its graph. No LLM involved.

  python ingest.py <git-url-or-local-path> [--branch b]

Private hosts: put GIT_TOKEN_GITLAB / GIT_TOKEN_GITHUB (or GIT_TOKEN_<HOST-with-dashes>) in .env;
the token is injected into the https clone URL for that host only and never written to disk.
"""
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import config  # noqa: F401  (loads .env)
import treesitter_locator as B

HERE = Path(__file__).resolve().parent


def store_root() -> Path:
    """Where clones live, once per user: $CONTEXT_GRAPH_STORE, else the plugin data dir Claude Code provides,
    else ~/.local/share/context-graph. Projects attach to clones here instead of copying them."""
    for var in ("CONTEXT_GRAPH_STORE", "CLAUDE_PLUGIN_DATA"):
        v = os.environ.get(var)
        if v:
            return Path(v).expanduser() / "workspaces"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "context-graph" / "workspaces"


WORKSPACES = store_root()
GIT_URL_RE = re.compile(r"(?:https?://|git@)[\w.\-]+[:/][\w.\-/~]+?(?:\.git)?(?=[\s\)\]>\"']|$)")


@dataclass
class Repo:
    slug: str
    path: str
    url: str
    commit: str
    branch: str
    files: int
    symbols: int
    built_s: float
    cloned: bool
    refreshed: bool = False


def find_git_url(text: str):
    m = GIT_URL_RE.search(text or "")
    return m.group(0).rstrip(".") if m else None


def slug_for(url: str) -> str:
    p = urlparse(url if "://" in url else "ssh://" + url.replace(":", "/", 1))
    parts = [x for x in p.path.split("/") if x]
    name = "-".join(parts[-2:]) if parts else "repo"
    return re.sub(r"[^a-z0-9._-]+", "-", name.lower().removesuffix(".git")).strip("-")[:80] or "repo"


def _auth_url(url: str) -> str:
    """Inject a host-specific token from the environment into an https URL."""
    if not url.startswith("https://"):
        return url
    host = urlparse(url).hostname or ""
    key = "GIT_TOKEN_" + re.sub(r"[^A-Z0-9]+", "_", host.upper()).strip("_")
    token = os.environ.get(key) or ("github.com" in host and os.environ.get("GIT_TOKEN_GITHUB")) \
        or ("gitlab" in host and os.environ.get("GIT_TOKEN_GITLAB")) or ""
    if not token:
        return url
    user = "oauth2" if "gitlab" in host else "x-access-token"
    return url.replace("https://", f"https://{user}:{token}@", 1)


def _git(args, cwd=None, timeout=600):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        # never echo a URL that may carry a token
        raise RuntimeError(re.sub(r"https://[^@\s]+@", "https://<redacted>@", (r.stderr or r.stdout).strip()[-400:]))
    return r.stdout.strip()


def ingest(url_or_path: str, branch: str = None) -> Repo:
    t0 = time.time()
    WORKSPACES.mkdir(parents=True, exist_ok=True)
    # a .ctx folder inside someone's repo must never be committed: make it ignore itself
    ctx = WORKSPACES.parent if WORKSPACES.name == "workspaces" and WORKSPACES.parent.name == ".ctx" else None
    if ctx is not None and not (ctx / ".gitignore").exists():
        (ctx / ".gitignore").write_text("*\n", encoding="utf-8")
    local = Path(url_or_path).expanduser()
    refreshed = False
    if local.is_dir():
        path, url, cloned = local.resolve(), str(local), False
    elif local.is_file():
        # a single document or an archive: copy/extract into a workspace so it is indexed like any corpus
        import shutil
        import zipfile
        slug = re.sub(r"[^a-z0-9._-]+", "-", local.stem.lower()).strip("-")[:80] or "file"
        path = WORKSPACES / slug
        path.mkdir(parents=True, exist_ok=True)
        if local.suffix.lower() == ".zip":
            with zipfile.ZipFile(local) as z:
                for m in z.infolist():
                    if m.filename.startswith("/") or ".." in Path(m.filename).parts:
                        continue
                    z.extract(m, path)
        else:
            shutil.copy2(local, path / local.name)
        url, cloned, refreshed = str(local), False, True
    else:
        url = url_or_path
        path = WORKSPACES / slug_for(url)
        if (path / ".git").is_dir():
            before = _git(["rev-parse", "HEAD"], cwd=path)
            _git(["fetch", "--depth", "1", "origin"] + ([branch] if branch else []), cwd=path)
            _git(["reset", "--hard", f"origin/{branch}" if branch else "FETCH_HEAD"], cwd=path)
            cloned = False
            refreshed = _git(["rev-parse", "HEAD"], cwd=path) != before
        else:
            args = ["clone", "--depth", "1"] + (["--branch", branch] if branch else []) + [_auth_url(url), str(path)]
            _git(args, timeout=1800)
            cloned = True
            refreshed = True
    commit = _git(["rev-parse", "--short", "HEAD"], cwd=path) if (path / ".git").is_dir() else "-"
    br = branch or (_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path) if (path / ".git").is_dir() else "-")
    B.ensure_built(str(path))
    stats = B.stats(str(path))
    return Repo(slug=path.name, path=str(path), url=url, commit=commit, branch=br,
                files=stats.get("files", 0), symbols=stats.get("symbols", 0),
                built_s=round(time.time() - t0, 1), cloned=cloned, refreshed=refreshed)


def list_workspaces():
    out = []
    if WORKSPACES.exists():
        for p in sorted(WORKSPACES.iterdir()):
            if (p / ".repomap" / "map.json").exists():
                out.append(p.name)
    return out


if __name__ == "__main__":
    b = sys.argv[sys.argv.index("--branch") + 1] if "--branch" in sys.argv else None
    r = ingest(sys.argv[1], b)
    print(r)
