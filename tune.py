"""Grid-search the locator parameters against the tuning cases; report holdout separately.

Usage: python tune.py <repo> [--locator A|B] [--write]
Score = hit@3 on tuning cases, tiebreak hit@5, then fewer hops (cheaper). --write saves params.json.
"""
import itertools
import json
import sys
from pathlib import Path

import graphify_locator as A
import treesitter_locator as B
from common import DEFAULT_PARAMS, load_params
from compare import load_cases

HERE = Path(__file__).resolve().parent

GRID = {
    "decay": [0.3, 0.6, 0.9],
    "hops": [1, 2],
    "body_weight": [0.5, 1.0, 2.0],
    "structured_boost": [1.0, 2.0, 3.0],
    "common_penalty": [0.3, 1.0],
    "rationale_weight": [0.5, 1.0],
}


def evaluate(loc, repo, cases, P):
    h3 = h5 = 0
    for c in cases:
        _, files = loc(repo, c["question"], 300, params=P)
        want = c["expected_files"]
        h3 += all(w in files[:3] for w in want)
        h5 += all(w in files[:5] for w in want)
    return h3, h5


def main():
    repo = sys.argv[1]
    which = sys.argv[sys.argv.index("--locator") + 1] if "--locator" in sys.argv else "A"
    loc = A.locate if which == "A" else B.locate
    (A if which == "A" else B).build(repo)
    tuning, holdout = load_cases(False), load_cases(True)
    base = load_params()
    b3, b5 = evaluate(loc, repo, tuning, base)
    hb3, hb5 = evaluate(loc, repo, holdout, base)
    print(f"current params: tuning hit@3 {b3}/{len(tuning)} hit@5 {b5}/{len(tuning)} | holdout hit@3 {hb3}/{len(holdout)} hit@5 {hb5}/{len(holdout)}")
    keys = list(GRID)
    results = []
    for combo in itertools.product(*(GRID[k] for k in keys)):
        P = dict(DEFAULT_PARAMS, **dict(zip(keys, combo)))
        h3, h5 = evaluate(loc, repo, tuning, P)
        results.append((h3, h5, -P["hops"], P))
    results.sort(key=lambda r: (r[0], r[1], r[2]), reverse=True)
    print(f"\ntried {len(results)} combinations; top 5 on tuning cases:")
    for h3, h5, _, P in results[:5]:
        o3, o5 = evaluate(loc, repo, holdout, P)
        print(f"  tuning {h3}/{len(tuning)} @3  {h5}/{len(tuning)} @5 | holdout {o3}/{len(holdout)} @3  {o5}/{len(holdout)} @5 | {P}")
    best = results[0][3]
    if "--write" in sys.argv:
        (HERE / "params.json").write_text(json.dumps(best, indent=1), encoding="utf-8")
        print(f"\nwrote params.json: {best}")
    else:
        print("\n(add --write to save the best combination to params.json)")


if __name__ == "__main__":
    main()
