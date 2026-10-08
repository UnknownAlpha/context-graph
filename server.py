"""OpenAI-compatible chat endpoint in front of the repo pipeline.

  .venv/bin/python server.py [--host 127.0.0.1] [--port 8765]

POST /v1/chat/completions   OpenAI schema. The repo is taken from the first git URL anywhere in the
                            conversation (cloned and indexed on first sight), or from the `model` field when it
                            names an already-indexed workspace. Streaming requests get one content chunk.
GET  /v1/models             indexed workspaces, so clients can pick a repo by name.
POST /ingest {"url": ...}   clone/refresh + index without asking a question.

Client-visible extras: every answer ends with a footer line (mode, tokens, index commit, citation check) and
the response carries a `context_graph` object with the same data.
"""
import argparse
import json
import re
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

import agent
import config
import ingest

HERE = Path(__file__).resolve().parent
app = FastAPI(title="context-graph", version="0.1")
_LOCK = threading.Lock()          # one ingest/answer at a time per process; the GPU is the bottleneck anyway
_REPOS = {}                       # slug -> ingest.Repo
_MODE_RE = re.compile(r"^\s*/mode\s+(single|auto|agent|grep-agent)\s*$", re.I)


class ChatRequest(BaseModel):
    model: str = "context-graph"
    messages: list
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    user: str | None = None
    metadata: dict | None = None


_WRAPPER = re.compile(r"<current_datetime>.*?</current_datetime>|^\s*```\w*\s*$|^\s*```\s*$", re.S | re.M)


def _text(m) -> str:
    c = m.get("content")
    if isinstance(c, list):                       # OpenAI multi-part content
        c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    # VS Code's custom-endpoint provider prepends a <current_datetime> tag and fences the user text
    return _WRAPPER.sub("", c or "").strip()


def _resolve_repo(req: ChatRequest):
    """Repo from the most recent git URL in the conversation, else from the model name, else a local path in the model name."""
    for m in reversed(req.messages):
        if m.get("role") == "user":
            url = ingest.find_git_url(_text(m))
            if url:
                return url, "url"
    if req.model in ingest.list_workspaces():
        return str(ingest.WORKSPACES / req.model), "workspace"
    if Path(req.model).expanduser().is_dir():
        return str(Path(req.model).expanduser()), "path"
    return None, None


def _mode(req: ChatRequest) -> str:
    md = (req.metadata or {}).get("mode")
    if md in agent.MODES:
        return md
    for m in req.messages:
        mm = _MODE_RE.match(_text(m)) if m.get("role") == "user" else None
        if mm:
            return mm.group(1).lower()
    return "auto"


def _question_and_history(req: ChatRequest):
    turns = [m for m in req.messages if m.get("role") in ("user", "assistant")]
    turns = [m for m in turns if not _MODE_RE.match(_text(m))]
    if not turns or turns[-1].get("role") != "user":
        raise HTTPException(400, "last message must be from the user")
    q = _text(turns[-1])
    url = ingest.find_git_url(q)
    if url:
        q = q.replace(url, "").strip(" \n:-,")
    hist = [{"role": m["role"], "content": _text(m)} for m in turns[:-1]]
    return q, hist


def _caption(path: str) -> dict:
    """Describe figures with the user's vision model if one is configured; otherwise OCR text only."""
    try:
        import vision
        return vision.describe_figures(path)
    except Exception as e:  # noqa: BLE001
        return {"captioned": 0, "error": f"{type(e).__name__}: {e}"}


def _footer(r: agent.Result, repo: ingest.Repo) -> str:
    cit = f"{len(r.citations_ok)} citations verified" + (f", {len(r.citations_bad)} NOT found: {', '.join(r.citations_bad[:4])}" if r.citations_bad else "")
    return (f"\n\n— context-graph: {repo.slug}@{repo.commit} | mode {r.mode}{' -> agent' if r.escalated else ''} | "
            f"{r.prompt_tokens + r.completion_tokens} tokens | {len(r.calls)} tool calls | {r.seconds}s | {cit}")


def _completion(req_model: str, content: str, extra: dict, usage: dict):
    return {"id": "chatcmpl-" + uuid.uuid4().hex[:24], "object": "chat.completion", "created": int(time.time()),
            "model": req_model, "choices": [{"index": 0, "finish_reason": "stop",
                                             "message": {"role": "assistant", "content": content}}],
            "usage": usage, "context_graph": extra}


def _stream(req_model: str, content: str):
    cid = "chatcmpl-" + uuid.uuid4().hex[:24]

    def gen():
        head = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()), "model": req_model,
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]}
        yield f"data: {json.dumps(head)}\n\n"
        for i in range(0, len(content), 400):
            chunk = dict(head, choices=[{"index": 0, "delta": {"content": content[i:i + 400]}, "finish_reason": None}])
            yield f"data: {json.dumps(chunk)}\n\n"
        yield f"data: {json.dumps(dict(head, choices=[{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]))}\n\n"
        yield "data: [DONE]\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/v1/models")
@app.get("/models")
@app.get("/v1/v1/models")
def models():
    now = int(time.time())
    data = [{"id": "context-graph", "object": "model", "created": now, "owned_by": "local"}]
    data += [{"id": s, "object": "model", "created": now, "owned_by": "workspace"} for s in ingest.list_workspaces()]
    return {"object": "list", "data": data}


class IngestRequest(BaseModel):
    url: str
    branch: str | None = None


@app.post("/ingest")
def ingest_ep(req: IngestRequest):
    with _LOCK:
        try:
            r = ingest.ingest(req.url, req.branch)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"ingest failed: {e}")
        figs = _caption(r.path)
    _REPOS[r.slug] = r
    return {**r.__dict__, "figures": figs}


@app.post("/v1/chat/completions")
@app.post("/chat/completions")
@app.post("/v1/v1/chat/completions")
def chat(req: ChatRequest):
    target, how = _resolve_repo(req)
    if not target:
        msg = ("Tell me which repository to work on: paste a git URL (https://... or git@...) in your message, "
               "or set the model to one of: " + (", ".join(ingest.list_workspaces()) or "(none indexed yet)"))
        return _stream(req.model, msg) if req.stream else _completion(req.model, msg, {"error": "no repo"}, {})
    question, history = _question_and_history(req)
    mode = _mode(req)
    with _LOCK:
        try:
            repo = ingest.ingest(target) if how != "path" else ingest.ingest(target)
        except Exception as e:  # noqa: BLE001
            msg = f"Could not clone or index {target}: {e}"
            return _stream(req.model, msg) if req.stream else _completion(req.model, msg, {"error": str(e)}, {})
        _REPOS[repo.slug] = repo
        figs = _caption(repo.path) if (repo.cloned or repo.refreshed) else {}
        if not question:
            msg = (f"Indexed {repo.slug} at {repo.commit} ({repo.branch}): {repo.files} files, {repo.symbols} symbols, "
                   f"{repo.built_s}s. Figures described: {figs.get('captioned', 0)}. "
                   f"Ask a question about it. Modes: /mode single|auto|agent|grep-agent (current: {mode}).")
            return _stream(req.model, msg) if req.stream else _completion(req.model, msg, repo.__dict__, {})
        r = agent.run(repo.path, question, mode, messages=history, verbose=False)
    if r.error and not r.answer:
        hint = ""
        if "Unauthorized" in r.error or "401" in r.error:
            hint = (" The model endpoint rejected the credentials. If MODEL_API_KEY uses a command such as "
                    "`cmd:oc whoami -t`, that login has probably expired: renew it and restart the server.")
        msg = f"context-graph could not get an answer from the model ({config.MODEL_NAME}): {r.error}.{hint}"
        print(f"[context-graph] {repo.slug}: {r.error}", flush=True)
        return _stream(req.model, msg) if req.stream else _completion(req.model, msg, {"error": r.error}, {})
    run_dir = HERE / "runs" / "server"
    agent.save(r, run_dir, f"{repo.slug}-{int(time.time())}-{agent.slug(question)[:30]}")
    content = (r.answer or "") + _footer(r, repo)
    extra = {"repo": repo.slug, "commit": repo.commit, "mode": r.mode, "escalated": r.escalated,
             "tool_calls": r.calls, "files_read": r.files_read, "citations_ok": r.citations_ok,
             "citations_bad": r.citations_bad, "seconds": r.seconds}
    usage = {"prompt_tokens": r.prompt_tokens, "completion_tokens": r.completion_tokens,
             "total_tokens": r.prompt_tokens + r.completion_tokens}
    if req.stream:
        return _stream(req.model, content)
    return JSONResponse(_completion(req.model, content, extra, usage))


if __name__ == "__main__":
    import uvicorn
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    config.require()
    print(f"context-graph serving on http://{a.host}:{a.port}/v1  (model {config.MODEL_NAME})", flush=True)
    uvicorn.run(app, host=a.host, port=a.port, log_level="info")
