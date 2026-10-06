"""Run the cases in cases.json through both locators and a grep baseline. No model needed.

Usage: python compare.py <repo> [--show] [--holdout-only] [--all]
Scores hit@3 / hit@5: did the top-N files contain every expected file?
By default tuning cases and holdout cases are reported separately.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

import graphify_locator as A
import treesitter_locator as B
from common import body_index, est_tokens, load_params

HERE = Path(__file__).resolve().parent


def load_cases(holdout=None):
    cases = json.loads((HERE / "cases.json").read_text(encoding="utf-8"))
    if holdout is None:
        return cases
    return [c for c in cases if bool(c.get("holdout")) == holdout]


def graphify_cli(repo, q, budget=600):
    try:
        out = subprocess.run(["graphify", "query", q, "--budget", str(budget)], cwd=repo,
                             capture_output=True, text=True, timeout=120).stdout
    except Exception as e:  # noqa: BLE001
        out = f"error: {e}"
    files = []
    for ln in out.splitlines():
        if "[src=" in ln:
            f = ln.split("[src=")[1].split()[0]
            if f and not f.startswith("loc=") and f not in files:
                files.append(f)
    return out, files


def tools(repo, with_cli=True):
    P = load_params()
    grep = body_index(repo)
    t = {
        "A graphify+post": lambda q: A.locate(repo, q, 300, params=P),
        "B tree-sitter": lambda q: B.locate(repo, q, 300, params=P),
        "grep (BM25)": lambda q: ("\n".join(f"- {f}" for f, _ in grep.rank(q, 5)), [f for f, _ in grep.rank(q, 5)]),
    }
    if with_cli:
        t = {"graphify-cli": lambda q: graphify_cli(repo, q), **t}
    return t


def score(repo, cases, tool_fns, show=False):
    hits3 = {k: 0 for k in tool_fns}
    hits5 = {k: 0 for k in tool_fns}
    toks = {k: 0 for k in tool_fns}
    for c in cases:
        q, want = c["question"], c["expected_files"]
        if show:
            print("\n" + "=" * 100 + f"\n{c['id']}{' [holdout]' if c.get('holdout') else ''}: {q}\nneed: {want}")
        for name, fn in tool_fns.items():
            txt, files = fn(q)
            ok3 = all(w in files[:3] for w in want)
            ok5 = all(w in files[:5] for w in want)
            hits3[name] += ok3
            hits5[name] += ok5
            toks[name] += est_tokens(txt)
            if show:
                print(f"  {'OK ' if ok3 else ('ok5' if ok5 else '-- ')} {name:16s} ~{est_tokens(txt):4d} tok  top3={files[:3]}")
    return hits3, hits5, toks


def main():
    repo = sys.argv[1]
    show = "--show" in sys.argv
    with_cli = "--all" in sys.argv
    print("build A:", A.build(repo))
    t = time.time()
    print("build B:", B.build(repo), f"in {time.time() - t:.2f}s")
    print("params:", load_params())
    fns = tools(repo, with_cli)
    groups = [("holdout", load_cases(True))] if "--holdout-only" in sys.argv else \
             [("tuning", load_cases(False)), ("holdout", load_cases(True))]
    for label, cases in groups:
        h3, h5, tk = score(repo, cases, fns, show)
        print(f"\n== {label} cases ({len(cases)}) ==")
        print(f"{'tool':18s} hit@3   hit@5   avg tokens")
        for k in fns:
            print(f"{k:18s} {h3[k]:>2}/{len(cases):<4} {h5[k]:>2}/{len(cases):<4} {tk[k] // max(1, len(cases))}")


if __name__ == "__main__":
    main()
