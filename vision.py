"""Optional model-backed understanding of figures: captions, and OCR by a model instead of RapidOCR.

Off unless the user configures a model. Nothing here runs by default, and the plugin never picks a model on its
own: it uses what the user put in the environment or in a context-graph .env file (see config.py for where).

Captions (MODEL_VISION_*): after a document is extracted, each figure PNG is sent once to a vision-capable
chat model; the description is cached by figure hash in .repomap/text/captions.json and inserted into the
extracted text as `[caption] (model description, inferred) ...`, so every reader sees it is model output.

OCR by model (MODEL_OCR_*): documents.ocr_image_bytes sends the image to this model with a transcription prompt
instead of RapidOCR. Use it for a document model such as PaddleOCR-VL or a general vision model such as Qwen-VL
served behind an OpenAI-compatible endpoint; RapidOCR stays the fallback when the call fails.

Settings (environment or .env):
  MODEL_VISION_NAME          vision-capable model id. Empty = captioning off.
  MODEL_VISION_BASE_URL      defaults to MODEL_BASE_URL
  MODEL_VISION_API_KEY       key, "none", or "cmd:<command>"; defaults to MODEL_API_KEY
  MODEL_VISION_MAX_FIGURES   per document, default 40
  MODEL_VISION_BUDGET_S      seconds a single ingest or caption call may spend; the rest stays pending (default 150)
  MODEL_OCR_NAME             model id for OCR. Empty = RapidOCR.
  MODEL_OCR_BASE_URL         defaults to MODEL_VISION_BASE_URL, then MODEL_BASE_URL
  MODEL_OCR_API_KEY          defaults to MODEL_VISION_API_KEY, then MODEL_API_KEY
"""
import base64
import json
import os
import time
from pathlib import Path

import config
import documents

CAPTION_PROMPT = (
    "Describe this figure for someone who cannot see it. State what kind of figure it is (chart, diagram, "
    "screenshot, table, photo), what it shows, the labels and axes you can read, and the main trend or "
    "relationship. Give numbers only if they are printed on the figure; if you estimate a value from a chart, "
    "say 'approximately'. Do not speculate beyond what is visible. Three to six sentences.")

OCR_PROMPT = (
    "Transcribe all text in this image exactly, in reading order, one line per line of text. Keep table rows "
    "on one line with ' | ' between cells. Output only the transcription, no commentary. If there is no text, "
    "output nothing.")


def configured() -> bool:
    return bool(os.environ.get("MODEL_VISION_NAME"))


def ocr_configured() -> bool:
    return bool(os.environ.get("MODEL_OCR_NAME"))


def _client(kind: str):
    """OpenAI client for 'vision' or 'ocr', each falling back to the next more general setting."""
    import httpx
    from openai import OpenAI
    chain = ["MODEL_OCR", "MODEL_VISION"] if kind == "ocr" else ["MODEL_VISION"]
    base = next((os.environ.get(f"{c}_BASE_URL") for c in chain if os.environ.get(f"{c}_BASE_URL")), None) or config.MODEL_BASE_URL
    raw_key = next((os.environ.get(f"{c}_API_KEY") for c in chain if os.environ.get(f"{c}_API_KEY")), None)
    key = config._resolve_key(raw_key) if raw_key else config.MODEL_API_KEY
    if not base:
        raise RuntimeError(f"MODEL_{kind.upper()}_BASE_URL or MODEL_BASE_URL must be set")
    http = httpx.Client(verify=config.MODEL_VERIFY_TLS, timeout=config.MODEL_TIMEOUT)
    return OpenAI(base_url=base.rstrip("/"), api_key=key if key and key != "none" else "none", http_client=http)


def _ask(kind: str, model: str, prompt: str, png: bytes) -> str:
    data = base64.b64encode(png).decode()
    res = _client(kind).chat.completions.create(
        model=model, temperature=0,
        messages=[{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}}]}])
    return (res.choices[0].message.content or "").strip()


def describe_image(png_path: Path) -> str:
    return _ask("vision", os.environ["MODEL_VISION_NAME"], CAPTION_PROMPT, png_path.read_bytes())


def transcribe_image(data: bytes) -> str:
    """OCR by the configured model. Raises on failure so the caller can fall back to RapidOCR."""
    if not ocr_configured():
        return ""
    if documents.Image is not None:
        import io
        img = documents.Image.open(io.BytesIO(data)).convert("RGB")
        buf = io.BytesIO(); img.save(buf, format="PNG"); data = buf.getvalue()
    return _ask("ocr", os.environ["MODEL_OCR_NAME"], OCR_PROMPT, data)


def _cache_path(repo: str) -> Path:
    return documents.cache_dir(repo) / "captions.json"


def _load_cache(repo: str) -> dict:
    p = _cache_path(repo)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError:
        return {}


def _deferred(repo: str, rel: str) -> bool:
    """An image file the build listed by name only (see documents.image_policy); captioned when opened."""
    p = documents.extracted_path(repo, rel)
    if not p:
        return True
    try:
        return bool(json.loads(p.with_suffix(".json").read_text(encoding="utf-8")).get("deferred"))
    except (OSError, ValueError):
        return False


def pending(repo: str) -> dict:
    """How many figures exist and how many still have no caption, without calling any model."""
    from common import iter_files
    done = _load_cache(repo)
    total = missing = 0
    for r in iter_files(repo):
        if not documents.is_document(r) or _deferred(repo, r):
            continue
        for fid, _png, _page in documents.figures_for(repo, r):
            total += 1
            if fid not in done:
                missing += 1
    return {"figures": total, "captioned": total - missing, "pending": missing}


def describe_figures(repo: str, rel: str = None, limit: int = None, budget_s: float = None) -> dict:
    """Caption figures in one document (rel) or every document in the repo. Cached; stops at budget_s and
    reports what is left as pending, so a later call continues."""
    if not configured():
        return {"captioned": 0, "pending": 0, "reason": "MODEL_VISION_NAME not set; OCR text only"}
    from common import iter_files
    limit = limit or int(os.environ.get("MODEL_VISION_MAX_FIGURES", "40"))
    budget_s = budget_s if budget_s is not None else float(os.environ.get("MODEL_VISION_BUDGET_S", "150"))
    t0 = time.time()
    done = _load_cache(repo)
    rels = [rel] if rel else [r for r in iter_files(repo) if documents.is_document(r) and not _deferred(repo, r)]
    captioned = skipped = pending_n = 0
    errors = 0
    for r in rels:
        n = 0
        for fid, png, _page in documents.figures_for(repo, r):
            if fid in done:
                documents.add_caption(repo, r, fid, done[fid])
                continue
            if n >= limit:
                skipped += 1
                continue
            if not png.exists():
                continue
            if time.time() - t0 > budget_s:
                pending_n += 1
                continue
            try:
                cap = describe_image(png)
            except Exception as e:  # noqa: BLE001
                errors += 1
                if errors >= 3:
                    pending_n += 1
                    continue
                cap = f"(vision model error: {type(e).__name__})"
            tagged = f"(model description, inferred) {cap}"
            done[fid] = tagged
            documents.add_caption(repo, r, fid, tagged)
            captioned += 1
            n += 1
    cp = _cache_path(repo)
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_text(json.dumps(done, indent=1), encoding="utf-8")
    out = {"captioned": captioned, "pending": pending_n, "skipped_over_limit": skipped,
           "model": os.environ.get("MODEL_VISION_NAME"), "seconds": round(time.time() - t0, 1)}
    if errors:
        out["errors"] = errors
    return out


if __name__ == "__main__":
    import sys
    print(describe_figures(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))
