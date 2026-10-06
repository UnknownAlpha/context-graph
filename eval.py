"""Run every case through the chosen modes, judge the answers with GLM, write a report.

  python eval.py <repo> [--modes agent,grep-agent,single] [--cases id1,id2] [--holdout-only] [--max-steps 8]
"""
import argparse
import json
import statistics
import sys
from datetime import datetime
from pathlib import Path

import agent
import config

HERE = Path(__file__).resolve().parent

JUDGE = """You grade a diagnosis of a repository problem against a reference rubric.
Score 2 if the answer identifies the same root cause and the key file(s) as the rubric.
Score 1 if it is partially right or right but unconfirmed / vague about where.
Score 0 if it is wrong, hallucinated, or gives no diagnosis.
Reply with JSON only: {"score": 0|1|2, "reason": "<one sentence>"}

Question: %s

Rubric (reference answer): %s

Candidate answer:
%s
"""


def judge(cl, q, rubric, answer):
    if not answer.strip():
        return 0, "no answer"
    res = cl.chat.completions.create(model=config.GLM_MODEL, temperature=0,
                                     messages=[{"role": "user", "content": JUDGE % (q, rubric, answer)}])
    txt = (res.choices[0].message.content or "").strip()
    try:
        s = txt[txt.index("{"): txt.rindex("}") + 1]
        d = json.loads(s)
        return int(d.get("score", 0)), str(d.get("reason", ""))[:200]
    except (ValueError, TypeError):
        return 0, "judge output unparseable: " + txt[:100]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("--modes", default="auto,agent,grep-agent")
    ap.add_argument("--cases", default="")
    ap.add_argument("--holdout-only", action="store_true")
    ap.add_argument("--max-steps", type=int, default=8)
    a = ap.parse_args()
    modes = [m for m in a.modes.split(",") if m]
    cases = json.loads((HERE / "cases.json").read_text(encoding="utf-8"))
    if a.cases:
        want = set(a.cases.split(","))
        cases = [c for c in cases if c["id"] in want]
    if a.holdout_only:
        cases = [c for c in cases if c.get("holdout")]
    cl = config.client()
    run_dir = HERE / "runs" / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-eval")
    rows = []
    for c in cases:
        for mode in modes:
            print(f"\n== {c['id']} [{mode}]", file=sys.stderr, flush=True)
            r = agent.run(a.repo, c["question"], mode, a.max_steps, verbose=True)
            agent.save(r, run_dir, c["id"])
            score, reason = judge(cl, c["question"], c["rubric"], r.answer)
            hit = all(f in r.files_read for f in c["expected_files"])
            rows.append({"id": c["id"], "holdout": c.get("holdout", False), "mode": mode + (" (esc)" if r.escalated else ""),
                         "files_hit": hit, "score": score, "reason": reason, "tokens": r.prompt_tokens + r.completion_tokens,
                         "tool_calls": len(r.calls), "seconds": r.seconds, "error": r.error,
                         "citations_bad": len(r.citations_bad)})
            print(f"   files_hit={hit} score={score} tokens={rows[-1]['tokens']} calls={len(r.calls)} {r.seconds}s"
                  + (f" ERROR {r.error}" if r.error else ""), file=sys.stderr, flush=True)
    (run_dir / "results.json").write_text(json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8")

    md = [f"# Eval {run_dir.name}", f"repo: {a.repo}  model: {config.GLM_MODEL}  thinking: {'on' if config.GLM_THINKING else 'off'}  cases: {len(cases)}", "",
          "Primary metric: time to a correct answer (self-hosted model). Tokens are a GPU-capacity proxy.", "",
          "| mode | accuracy (score 2) | mean score | files hit | median s | mean s | max s | mean tokens | mean tool calls | errors |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for mode in modes:
        rs = [r for r in rows if r["mode"].split(" ")[0] == mode]
        if not rs:
            continue
        secs = [r["seconds"] for r in rs]
        md.append(f"| {mode} | {sum(r['score'] == 2 for r in rs)}/{len(rs)} | {statistics.mean(r['score'] for r in rs):.2f} | "
                  f"{sum(r['files_hit'] for r in rs)}/{len(rs)} | {statistics.median(secs):.0f} | {statistics.mean(secs):.0f} | {max(secs):.0f} | "
                  f"{int(statistics.mean(r['tokens'] for r in rs))} | {statistics.mean(r['tool_calls'] for r in rs):.1f} | "
                  f"{sum(1 for r in rs if r['error'])} |")
    md += ["", "| case | holdout | mode | files hit | score | tokens | calls | judge reason |", "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        md.append(f"| {r['id']} | {'y' if r['holdout'] else ''} | {r['mode']} | {'y' if r['files_hit'] else 'n'} | {r['score']} | "
                  f"{r['tokens']} | {r['tool_calls']} | {r['reason'].replace('|', '/')} |")
    (run_dir / "report.md").write_text("\n".join(md), encoding="utf-8")
    print("\n".join(md))
    print(f"\nreport: {run_dir / 'report.md'}")


if __name__ == "__main__":
    main()
