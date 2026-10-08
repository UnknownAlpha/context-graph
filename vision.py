"""Optional figure descriptions from a user-configured vision model. Standalone tools only.

The Claude Code plugin never calls this. server.py and agent.py call `describe_figures(repo)` after an ingest
when MODEL_VISION_* is configured; otherwise documents keep OCR text only. Every description is cached by the
figure's hash and inserted into the extracted text as a `[caption]` line tagged as model output, so later reads
and citations treat it as inferred, never as a fact read from the page.

Settings (environment or .env):
  MODEL_VISION_BASE_URL   OpenAI-compatible endpoint that accepts image input (defaults to MODEL_BASE_URL)
  MODEL_VISION_API_KEY    key, "none", or "cmd:<command>" (defaults to MODEL_API_KEY)
  MODEL_VISION_NAME       a vision-capable model id. Empty = captioning off.
  MODEL_VISION_MAX_FIGURES  per document, default 40
"""
import base64
import json
import os
from pathlib import Path

import config
import documents

PROMPT = ("Describe this figure for someone who cannot see it. State what kind of figure it is (chart, diagram, "
          "screenshot, table, photo), what it shows, the labels and axes you can read, and the main trend or "
          "relationship. Give numbers only if they are printed on the figure; if you estimate a value from a chart, "
          "say 'approximately'. Do not speculate beyond what is visible. Three to six sentences.")


def configured() -> bool:
    return bool(os.environ.get("MODEL_VISION_NAME"))


def _client():
    import httpx
    from openai import OpenAI
    base = (os.environ.get("MODEL_VISION_BASE_URL") or config.MODEL_BASE_URL).rstrip("/")
    key = config._resolve_key(os.environ.get("MODEL_VISION_API_KEY") or "") or config.MODEL_API_KEY
    http = httpx.Client(verify=config.MODEL_VERIFY_TLS, timeout=config.MODEL_TIMEOUT)
    return OpenAI(base_url=base, api_key=key if key != "none" else "none", http_client=http)


def describe_image(png_path: Path) -> str:
    cl = _client()
    data = base64.b64encode(png_path.read_bytes()).decode()
    res = cl.chat.completions.create(
        model=os.environ["MODEL_VISION_NAME"], temperature=0,
        messages=[{"role": "user", "content": [
            {"type": "text", "text": PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}}]}])
    return (res.choices[0].message.content or "").strip()


def describe_figures(repo: str, rel: str = None, limit: int = None) -> dict:
    """Caption figures in one document (rel) or every document in the repo. Returns counts."""
    if not configured():
        return {"captioned": 0, "skipped": 0, "reason": "MODEL_VISION_NAME not set; OCR text only"}
    from common import iter_files
    limit = limit or int(os.environ.get("MODEL_VISION_MAX_FIGURES", "40"))
    cache = documents.cache_dir(repo) / "captions.json"
    try:
        done = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else {}
    except ValueError:
        done = {}
    rels = [rel] if rel else [r for r in iter_files(repo) if documents.is_document(r)]
    captioned = skipped = 0
    for r in rels:
        n = 0
        for fid, png, page in documents.figures_for(repo, r):
            if n >= limit:
                skipped += 1
                continue
            if fid in done:
                documents.add_caption(repo, r, fid, done[fid])
                continue
            if not png.exists():
                continue
            try:
                cap = describe_image(png)
            except Exception as e:  # noqa: BLE001
                cap = f"(vision model error: {type(e).__name__})"
            tagged = f"(model description, inferred) {cap}"
            done[fid] = tagged
            documents.add_caption(repo, r, fid, tagged)
            captioned += 1
            n += 1
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(done, indent=1), encoding="utf-8")
    return {"captioned": captioned, "skipped": skipped, "model": os.environ.get("MODEL_VISION_NAME")}


if __name__ == "__main__":
    import sys
    print(describe_figures(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))
