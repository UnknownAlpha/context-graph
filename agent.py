"""Run a question against a repo with GLM.

  single      locate -> read slices of the top files -> one completion, no tools (cheapest)
  auto        single, then a capped agent loop only if the answer says it could not confirm (default for the server)
  agent       GLM gets the repo map + tools locate/grep/read_file/deps/path and drives itself
  grep-agent  same loop without the locate tool (control: does the graph earn its tokens?)

One-off:      python agent.py <repo> "<question>" --mode auto [--all-modes]
Interactive:  python agent.py <repo> --chat --mode auto     (/mode single, /reset, /quit)
Transcripts land in runs/<timestamp>/.
"""
import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path

import config
import graphify_locator as A
import treesitter_locator as B
from common import est_tokens
from tools import Tools, check_citations

HERE = Path(__file__).resolve().parent
MODES = ("single", "auto", "agent", "grep-agent")
FALLBACK_STEPS = 6
HEDGES = ("cannot confirm", "can't confirm", "could not confirm", "couldn't confirm", "not enough in the repo",
          "not enough information", "would need to read", "unable to determine", "cannot determine")

RULES = """You are diagnosing problems in a code repository. You can only see what you read.

Rules:
- The repo map below and the `locate` tool are hints from an automatically built knowledge graph. They can be wrong or incomplete. Never state a cause you have not confirmed by reading the file.
- Prefer: locate (if available) -> read_file on the two or three files it names -> grep only for what is still unclear. Use `deps` before saying a change is safe and `path` for "how does A reach B" questions. Aim for at most six tool calls.
- A fact you have confirmed once is settled. Do not re-read or re-derive it.
- The repository may contain no bug. If the code is internally consistent, the cause is outside the repo (cluster state, sync timing, credentials, environment). Say that plainly, name the one or two most likely external causes, and stop. Do not keep searching for a hidden bug.
- Every claim about code must name the file and line, e.g. portal/app/app.py:286.
- When you have found the cause, or established that the repo is consistent, stop calling tools and answer. Structure: Cause (2-4 sentences), Evidence (file:line bullets), Fix or next check (what to change, or which command decides it).
- If the repo does not contain enough to decide, say what is missing instead of guessing."""

SINGLE_TAIL = ("You have the full text or the relevant regions of the files above; there are no tools and no further reads. "
               "Treat what is in these files as confirmed. Diagnose now: state the cause as a conclusion, "
               "cite file:line for each step of the chain, and if the files are internally consistent say so "
               "and name the external check that decides it. Do not say you could not confirm something that "
               "is visible in the text above. If something essential is genuinely not in the text, say exactly "
               "'cannot confirm: <what>' so it can be fetched.")


@dataclass
class Result:
    mode: str
    question: str
    answer: str = ""
    calls: list = field(default_factory=list)      # [name, args, result_tokens]
    files_read: list = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    steps: int = 0
    seconds: float = 0.0
    error: str = ""
    thinking: bool = True
    escalated: bool = False
    citations_ok: list = field(default_factory=list)
    citations_bad: list = field(default_factory=list)
    messages: list = field(default_factory=list)   # conversation for chat continuation


def repo_map(repo: str, budget: int = 1200) -> str:
    if Path(repo, "graphify-out", "graph.json").exists():
        A.ensure_built(repo)
        return A.page(repo, budget)
    B.ensure_built(repo)
    return B.page(repo, budget)


def system_prompt(repo: str) -> str:
    return RULES + "\n\n" + repo_map(repo)


def _usage(res, r: Result):
    u = getattr(res, "usage", None)
    if u:
        r.prompt_tokens += u.prompt_tokens or 0
        r.completion_tokens += u.completion_tokens or 0


def _assistant_dict(msg) -> dict:
    """Only role/content/tool_calls go back to the server; reasoning_content is dropped."""
    d = {"role": "assistant", "content": msg.content or ""}
    if msg.tool_calls:
        d["tool_calls"] = [{"id": tc.id, "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                           for tc in msg.tool_calls]
    return d


def _clean_history(messages):
    """Keep only user/assistant text turns from a prior conversation (drop tool traffic and old system prompts)."""
    out = []
    for m in messages or []:
        if m.get("role") in ("user", "assistant") and m.get("content") and not m.get("tool_calls"):
            out.append({"role": m["role"], "content": m["content"]})
    return out


def _single(cl, repo, question, tools: Tools, r: Result, history):
    loc = tools.locate(question, 300)
    hits = tools.locate_hits(question, 5)[:3]
    if not hits:
        hits = [(f, []) for f in [ln[2:].split("  (")[0].strip() for ln in loc.splitlines() if ln.startswith("- ")][:3]]
    bodies = "\n\n".join(tools.read_slices(f, lines) for f, lines in hits)
    msgs = [{"role": "system", "content": system_prompt(repo)}] + _clean_history(history)
    msgs.append({"role": "user", "content": f"Problem: {question}\n\nLocator output:\n{loc}\n\n{bodies}\n\n{SINGLE_TAIL}"})
    res = cl.chat.completions.create(model=config.GLM_MODEL, messages=msgs, extra_body=config.EXTRA_BODY)
    _usage(res, r)
    r.steps += 1
    msg = res.choices[0].message
    msgs.append(_assistant_dict(msg))
    return msg.content or "", msgs


def _loop(cl, repo, question, tools: Tools, r: Result, history, max_steps):
    msgs = [{"role": "system", "content": system_prompt(repo)}] + _clean_history(history)
    msgs.append({"role": "user", "content": question})
    for step in range(max_steps):
        res = cl.chat.completions.create(model=config.GLM_MODEL, messages=msgs, tools=tools.schemas(),
                                         tool_choice="auto", extra_body=config.EXTRA_BODY)
        _usage(res, r)
        r.steps += 1
        msg = res.choices[0].message
        msgs.append(_assistant_dict(msg))
        if not msg.tool_calls:
            return msg.content or "", msgs
        for tc in msg.tool_calls:
            out = tools.dispatch(tc.function.name, tc.function.arguments)
            if r.verbose:
                print(f"  [{step + 1}] {tc.function.name}({tc.function.arguments[:120]}) -> ~{est_tokens(out)} tok",
                      file=sys.stderr, flush=True)
            msgs.append({"role": "tool", "tool_call_id": tc.id, "content": out})
    msgs.append({"role": "user", "content": "Stop using tools. Give your best diagnosis from what you have read."})
    res = cl.chat.completions.create(model=config.GLM_MODEL, messages=msgs, extra_body=config.EXTRA_BODY)
    _usage(res, r)
    r.steps += 1
    msgs.append(_assistant_dict(res.choices[0].message))
    return res.choices[0].message.content or "", msgs


def hedged(answer: str) -> bool:
    a = (answer or "").lower()
    return not a.strip() or any(h in a for h in HEDGES)


def run(repo: str, question: str, mode: str = "auto", max_steps: int = 8,
        messages: list = None, verbose: bool = True) -> Result:
    assert mode in MODES, mode
    repo = str(Path(repo).resolve())
    cl = config.client()
    r = Result(mode=mode, question=question, thinking=config.GLM_THINKING)
    r.verbose = verbose
    t0 = time.time()
    tools = Tools(repo, use_locator=(mode != "grep-agent"))
    try:
        if mode in ("single", "auto"):
            r.answer, r.messages = _single(cl, repo, question, tools, r, messages)
            if mode == "auto" and hedged(r.answer):
                r.escalated = True
                if verbose:
                    print("  single answer hedged; escalating to capped agent loop", file=sys.stderr, flush=True)
                r.answer, r.messages = _loop(cl, repo, question, tools, r, messages, FALLBACK_STEPS)
        else:
            r.answer, r.messages = _loop(cl, repo, question, tools, r, messages, max_steps)
    except Exception as e:  # noqa: BLE001
        r.error = f"{type(e).__name__}: {e}"
    r.calls = [list(c) for c in tools.calls]
    r.files_read = list(tools.files_read)
    r.citations_ok, r.citations_bad = check_citations(repo, r.answer)
    r.seconds = round(time.time() - t0, 1)
    del r.verbose
    return r


# ------------------------------------------------------------------ transcripts
def save(r: Result, run_dir: Path, name: str) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    d = asdict(r)
    (run_dir / f"{name}-{r.mode}.json").write_text(json.dumps(d, indent=1, ensure_ascii=False), encoding="utf-8")
    md = [f"# {r.question}",
          f"mode: {r.mode}{' (escalated)' if r.escalated else ''} | thinking: {'on' if r.thinking else 'off'} | steps: {r.steps} | "
          f"tokens: {r.prompt_tokens} in / {r.completion_tokens} out | {r.seconds}s | citations ok {len(r.citations_ok)} bad {len(r.citations_bad)}",
          "", "## Tool calls"]
    md += [f"- {n}({json.dumps(a, ensure_ascii=False)[:200]}) -> ~{t} tok" for n, a, t in r.calls] or ["(none)"]
    md += ["", "## Files read", *([f"- {f}" for f in r.files_read] or ["(none)"]), "", "## Answer", r.answer or f"(no answer) {r.error}"]
    if r.citations_bad:
        md += ["", "## Unverifiable citations", *[f"- {c}" for c in r.citations_bad]]
    p = run_dir / f"{name}-{r.mode}.md"
    p.write_text("\n".join(md), encoding="utf-8")
    return p


def slug(q: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", q.lower()).strip("-")[:50] or "q"


def summary(r: Result) -> str:
    return (f"[{r.mode}{' -> agent' if r.escalated else ''}] steps={r.steps} tools={len(r.calls)} files={len(r.files_read)} "
            f"tokens={r.prompt_tokens}+{r.completion_tokens} time={r.seconds}s citations={len(r.citations_ok)} ok/{len(r.citations_bad)} bad"
            + (f" ERROR {r.error}" if r.error else ""))


# ------------------------------------------------------------------------- CLI
def chat(repo: str, mode: str, max_steps: int, run_dir: Path):
    print(f"chat mode ({mode}). /mode {'|'.join(MODES)}, /reset, /quit", flush=True)
    history = None
    n = 0
    while True:
        try:
            q = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not q:
            continue
        if q in ("/quit", "/exit"):
            break
        if q == "/reset":
            history = None
            print("history cleared")
            continue
        if q.startswith("/mode"):
            m = q.split()[-1]
            if m in MODES:
                mode = m
                print(f"mode {mode}")
            else:
                print(f"modes: {', '.join(MODES)}")
            continue
        r = run(repo, q, mode, max_steps, messages=history)
        history = _clean_history(r.messages)
        n += 1
        save(r, run_dir, f"chat{n:02d}-{slug(q)}")
        print("\n" + (r.answer or f"(no answer) {r.error}"))
        print("\n" + summary(r))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repo")
    ap.add_argument("question", nargs="?")
    ap.add_argument("--mode", default="auto", choices=MODES)
    ap.add_argument("--all-modes", action="store_true")
    ap.add_argument("--chat", action="store_true")
    ap.add_argument("--max-steps", type=int, default=8)
    a = ap.parse_args()
    run_dir = HERE / "runs" / datetime.now().strftime("%Y%m%d-%H%M%S")
    if a.chat:
        return chat(a.repo, a.mode, a.max_steps, run_dir)
    if not a.question:
        ap.error("give a question or --chat")
    for mode in (MODES if a.all_modes else (a.mode,)):
        print(f"\n===== {mode} =====", flush=True)
        r = run(a.repo, a.question, mode, a.max_steps)
        p = save(r, run_dir, slug(a.question))
        print("\n" + (r.answer or f"(no answer) {r.error}"))
        print("\n" + summary(r) + f"\ntranscript: {p}")


if __name__ == "__main__":
    main()
