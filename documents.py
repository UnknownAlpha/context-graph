"""Turn documents and images into citable text, once, cached by content hash.

Supported: .pdf .docx .pptx .xlsx .csv .png .jpg .jpeg .webp .tif .tiff
Output: <repo>/.repomap/text/<hash>.txt, a plain text file with marker lines so line numbers are stable and
citable (`docs/runbook.pdf:120` means line 120 of the extracted text):

    # [page 3]                 PDF page boundary
    ## Heading text            heading from DOCX styles, PPTX titles, XLSX sheet names, or PDF font-size heuristics
    [figure a1b2c3d4]          an embedded image; OCR text follows on the next lines, and a caption line
    [caption] ...              if a vision model described it (vision.py; off unless MODEL_VISION_NAME is set)

Figures: embedded pictures in DOCX/PPTX, and on every PDF page the raster images and the vector drawings (charts,
diagrams) found through pdfium's page objects, cropped from a render of the page so labels and axes come along.
A page with no text layer is one figure. OCR runs on every figure: RapidOCR (ONNX, local, the default) or, when
MODEL_OCR_NAME is set, a user-served document/vision model. Everything here is deterministic except the OCR text
of a model engine and captions, which are marked as such.
Dependencies are the `docs` extra; when missing, documents are skipped and `status()` says so.
"""
import hashlib
import io
import json
import os
import re
from pathlib import Path

DOC_EXT = {".pdf", ".docx", ".pptx", ".xlsx", ".xlsm", ".csv"}
IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"}
MAX_DOC_BYTES = 60_000_000
OCR_MIN_CHARS_PER_PAGE = 40       # below this a PDF page is treated as scanned and OCR'd
OCR_MAX_PAGES = 400               # cap per document so a huge scan cannot stall ingest
OCR_MIN_IMAGE_SIDE = 80           # skip icons and bullets
FIG_MIN_PT = 60                   # a drawing or image smaller than this (points, both sides) is a glyph or a rule
FIG_MAX_PER_PAGE = 6              # most figures kept per PDF page
FIG_GAP_PT = 12                   # objects closer than this are the same figure
FIG_RENDER_SCALE = 2.0
MAX_IMAGES_AT_BUILD = int(os.environ.get("CONTEXT_GRAPH_MAX_IMAGES", "60"))   # more image files than this: OCR on demand
_IMAGE_COUNT = {}                 # repo -> number of image files, counted once per process

_missing = []
try:
    import pypdf
except ImportError:  # pragma: no cover
    pypdf = None; _missing.append("pypdf")
try:
    import pypdfium2
except ImportError:  # pragma: no cover
    pypdfium2 = None; _missing.append("pypdfium2")
try:
    import docx as _docx
except ImportError:  # pragma: no cover
    _docx = None; _missing.append("python-docx")
try:
    import pptx as _pptx
except ImportError:  # pragma: no cover
    _pptx = None; _missing.append("python-pptx")
try:
    import openpyxl
except ImportError:  # pragma: no cover
    openpyxl = None; _missing.append("openpyxl")
try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None; _missing.append("pillow")

_OCR = None
_OCR_FAILED = False


def available() -> bool:
    return not _missing


def status() -> dict:
    """Readiness of extraction, which OCR engine is active, and whether captions are configured."""
    model_ocr = os.environ.get("MODEL_OCR_NAME", "")
    if model_ocr:
        ocr = f"model {model_ocr}" + (" (rapidocr fallback)" if _ocr() is not None else " (no local fallback)")
    else:
        ocr = "rapidocr" if _ocr() is not None else "unavailable"
    vis = os.environ.get("MODEL_VISION_NAME")
    return {"documents": "ready" if available() else "missing: " + ", ".join(_missing),
            "ocr": ocr,
            "captions": f"model {vis}" if vis else "off (set MODEL_VISION_NAME)"}


def _ocr():
    """Lazy RapidOCR instance; None when the package or its models are not available."""
    global _OCR, _OCR_FAILED
    if _OCR is None and not _OCR_FAILED:
        try:
            from rapidocr_onnxruntime import RapidOCR
            _OCR = RapidOCR()
        except Exception:  # noqa: BLE001
            _OCR_FAILED = True
    return _OCR


def ocr_engine() -> str:
    return "model" if os.environ.get("MODEL_OCR_NAME") else "rapidocr"


def ocr_image_bytes(data: bytes) -> str:
    """Text found in an image, reading order top-to-bottom, or '' when OCR is unavailable or finds nothing.

    With MODEL_OCR_NAME set the image goes to that model (vision.transcribe_image); RapidOCR is the default and
    the fallback when the model call fails.
    """
    if Image is None:
        return ""
    try:
        img = Image.open(io.BytesIO(data)).convert("RGB")
        if min(img.size) < OCR_MIN_IMAGE_SIDE:
            return ""
    except Exception:  # noqa: BLE001
        return ""
    if os.environ.get("MODEL_OCR_NAME"):
        try:
            import vision
            text = vision.transcribe_image(data)
            if text:
                return text
        except Exception:  # noqa: BLE001
            pass
    eng = _ocr()
    if eng is None:
        return ""
    try:
        import numpy as np
        result, _ = eng(np.asarray(img))
    except Exception:  # noqa: BLE001
        return ""
    if not result:
        return ""
    # result: [ [box, text, score], ... ]; sort by box top-left y then x
    rows = sorted(result, key=lambda r: (round(r[0][0][1] / 12), r[0][0][0]))
    return "\n".join(r[1].strip() for r in rows if r[1] and r[1].strip())


def _fig_id(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()[:8]


def _ocr_label() -> str:
    return "OCR text (model):" if ocr_engine() == "model" else "OCR text:"


# ------------------------------------------------------------------ figures on PDF pages
def _merge_boxes(boxes, gap: float):
    """Union overlapping or nearby boxes until stable. boxes: [l, b, r, t] in PDF points."""
    boxes = [list(b) for b in boxes]
    changed = True
    while changed:
        changed = False
        out = []
        while boxes:
            a = boxes.pop()
            i = 0
            while i < len(boxes):
                b = boxes[i]
                if a[0] - gap <= b[2] and b[0] - gap <= a[2] and a[1] - gap <= b[3] and b[1] - gap <= a[3]:
                    a = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
                    boxes.pop(i)
                    changed = True
                else:
                    i += 1
            out.append(a)
        boxes = out
    return boxes


def _page_figure_boxes(page):
    """Bounding boxes of figures on a pdfium page: images, and clusters of vector drawing objects.

    Text objects are not seeds, so paragraphs never become figures, but text that sits inside a drawing (axis
    labels, legend) is part of the crop because the crop is taken from the rendered page.
    """
    import pypdfium2.raw as raw
    w, h = page.get_size()
    seeds, thin = [], []
    for obj in page.get_objects(max_depth=4):
        if obj.type == raw.FPDF_PAGEOBJ_TEXT:
            continue
        try:
            l, b, r, t = obj.get_bounds()
        except Exception:  # noqa: BLE001
            continue
        bw, bh = r - l, t - b
        if bw <= 0 or bh <= 0:
            continue
        if bw >= 0.9 * w and bh >= 0.9 * h:
            continue                                     # page background
        if obj.type == raw.FPDF_PAGEOBJ_IMAGE or (bw >= 20 and bh >= 20):
            seeds.append([l, b, r, t])
        else:
            thin.append([l, b, r, t])                    # rules, axes, ticks: join a figure, never start one
    if not seeds:
        return []
    boxes = _merge_boxes(seeds + thin, FIG_GAP_PT)
    seedboxes = _merge_boxes(seeds, FIG_GAP_PT)
    keep = []
    for bx in boxes:
        if not any(sb[0] >= bx[0] - 0.1 and sb[2] <= bx[2] + 0.1 and sb[1] >= bx[1] - 0.1 and sb[3] <= bx[3] + 0.1
                   for sb in seedboxes):
            continue
        if bx[2] - bx[0] >= FIG_MIN_PT and bx[3] - bx[1] >= FIG_MIN_PT:
            keep.append(bx)
    keep.sort(key=lambda b: (-b[3], b[0]))               # top of page first
    return keep[:FIG_MAX_PER_PAGE]


def _crop_figures(page, boxes, scale: float = FIG_RENDER_SCALE):
    """Render the page once and crop each box (with a small margin) to PNG bytes."""
    if not boxes or Image is None:
        return []
    w, h = page.get_size()
    img = page.render(scale=scale).to_pil()
    out = []
    for l, b, r, t in boxes:
        m = 6
        px = (max(0, int((l - m) * scale)), max(0, int((h - t - m) * scale)),
              min(img.width, int((r + m) * scale)), min(img.height, int((h - b + m) * scale)))
        if px[2] - px[0] < 10 or px[3] - px[1] < 10:
            continue
        buf = io.BytesIO(); img.crop(px).save(buf, format="PNG")
        out.append(buf.getvalue())
    return out


# ------------------------------------------------------------------ extractors
def _pdf(path: Path):
    lines, figures = [], []
    reader = pypdf.PdfReader(str(path))
    n = len(reader.pages)
    pdfium = pypdfium2.PdfDocument(str(path)) if pypdfium2 is not None else None
    for i in range(n):
        lines.append(f"# [page {i + 1}]")
        text = ""
        try:
            text = reader.pages[i].extract_text() or ""
        except Exception:  # noqa: BLE001
            text = ""
        if len(text.strip()) >= OCR_MIN_CHARS_PER_PAGE or pdfium is None or i >= OCR_MAX_PAGES:
            for ln in text.splitlines():
                ln = ln.rstrip()
                if ln:
                    lines.append(ln)
            if pdfium is not None and i < OCR_MAX_PAGES:
                try:
                    page = pdfium[i]
                    crops = _crop_figures(page, _page_figure_boxes(page))
                except Exception:  # noqa: BLE001
                    crops = []
                seen = set()
                for data in crops:
                    fid = _fig_id(data)
                    if fid in seen:
                        continue
                    seen.add(fid)
                    figures.append({"id": fid, "page": i + 1, "kind": "figure", "bytes": data})
                    ocr = ocr_image_bytes(data)
                    lines.append(f"[figure {fid}] figure on page {i + 1}" + (f", {_ocr_label()}" if ocr else " (no text recognised)"))
                    lines.extend(ocr.splitlines())
            continue
        # scanned or image-only page: render and OCR
        try:
            page = pdfium[i]
            bitmap = page.render(scale=2.0)
            img = bitmap.to_pil()
            buf = io.BytesIO(); img.save(buf, format="PNG"); data = buf.getvalue()
        except Exception:  # noqa: BLE001
            continue
        fid = _fig_id(data)
        figures.append({"id": fid, "page": i + 1, "kind": "scanned-page", "bytes": data})
        lines.append(f"[figure {fid}] scanned page, {_ocr_label()}")
        ocr = ocr_image_bytes(data)
        lines.extend(ocr.splitlines() if ocr else ["(no text recognised)"])
    return lines, figures


def _docx_file(path: Path):
    lines, figures = [], []
    d = _docx.Document(str(path))
    for p in d.paragraphs:
        t = p.text.strip()
        if not t:
            continue
        style = (p.style.name or "") if p.style is not None else ""
        m = re.match(r"Heading (\d)", style)
        if m or style == "Title":
            level = int(m.group(1)) if m else 1
            lines.append("#" * min(level + 1, 6) + " " + t)
        else:
            lines.append(t)
    for ti, table in enumerate(d.tables, 1):
        lines.append(f"## [table {ti}]")
        for row in table.rows:
            cells = [c.text.strip().replace("\n", " ") for c in row.cells]
            if any(cells):
                lines.append(" | ".join(cells))
    for rel in d.part.rels.values():
        if "image" in rel.reltype:
            try:
                data = rel.target_part.blob
            except Exception:  # noqa: BLE001
                continue
            fid = _fig_id(data)
            figures.append({"id": fid, "page": None, "kind": "image", "bytes": data})
            ocr = ocr_image_bytes(data)
            lines.append(f"[figure {fid}] embedded image" + (f", {_ocr_label()}" if ocr else " (no text recognised)"))
            lines.extend(ocr.splitlines())
    return lines, figures


def _pptx_file(path: Path):
    lines, figures = [], []
    prs = _pptx.Presentation(str(path))
    for si, slide in enumerate(prs.slides, 1):
        title = slide.shapes.title.text.strip() if slide.shapes.title is not None and slide.shapes.title.text else ""
        lines.append(f"# [page {si}]")
        if title:
            lines.append("## " + title)
        for shape in slide.shapes:
            if shape.has_text_frame and shape != slide.shapes.title:
                for para in shape.text_frame.paragraphs:
                    t = "".join(r.text for r in para.runs).strip()
                    if t:
                        lines.append(("  " * para.level) + "- " + t)
            if getattr(shape, "shape_type", None) == 13 and hasattr(shape, "image"):  # PICTURE
                try:
                    data = shape.image.blob
                except Exception:  # noqa: BLE001
                    continue
                fid = _fig_id(data)
                figures.append({"id": fid, "page": si, "kind": "image", "bytes": data})
                ocr = ocr_image_bytes(data)
                lines.append(f"[figure {fid}] image on slide {si}" + (f", {_ocr_label()}" if ocr else " (no text recognised)"))
                lines.extend(ocr.splitlines())
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                lines.append("[notes] " + notes.replace("\n", " "))
    return lines, figures


def _xlsx_file(path: Path):
    lines = []
    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    for ws in wb.worksheets:
        lines.append(f"## {ws.title}")
        for r, row in enumerate(ws.iter_rows(values_only=True), 1):
            if r > 5000:
                lines.append("(sheet truncated at 5000 rows)")
                break
            cells = ["" if v is None else str(v) for v in row]
            if any(c.strip() for c in cells):
                lines.append(" | ".join(cells).rstrip(" |"))
    return lines, []


def _csv_file(path: Path):
    text = path.read_text(encoding="utf-8", errors="replace")
    return [ln.rstrip() for ln in text.splitlines()[:5000]], []


def _image_file(path: Path, ocr: bool = True):
    data = path.read_bytes()
    fid = _fig_id(data)
    if not ocr:
        return [f"[figure {fid}] image file (not read at index time: this repo has many image files; "
                "read_file on it runs OCR and captioning on demand)"], [{"id": fid, "page": None, "kind": "image", "bytes": data}]
    text = ocr_image_bytes(data)
    lines = [f"[figure {fid}] image file" + (f", {_ocr_label()}" if text else " (no text recognised)")]
    lines.extend(text.splitlines())
    return lines, [{"id": fid, "page": None, "kind": "image", "bytes": data}]


def image_policy(repo: str) -> dict:
    """Code repos carry hundreds of icons and logos; OCR on all of them at build time would take minutes for
    nothing. Up to MAX_IMAGES_AT_BUILD image files are read at build; above that, image files are listed by name
    and read (OCR + caption) only when a tool opens them. Documents (PDF, DOCX, ...) are always read."""
    key = str(Path(repo).resolve())
    if key not in _IMAGE_COUNT:
        from common import iter_files
        _IMAGE_COUNT[key] = sum(1 for r in iter_files(repo) if Path(r).suffix.lower() in IMG_EXT)
    n = _IMAGE_COUNT[key]
    return {"images": n, "limit": MAX_IMAGES_AT_BUILD, "ocr_at_build": n <= MAX_IMAGES_AT_BUILD}


_EXTRACTORS = {".pdf": _pdf, ".docx": _docx_file, ".pptx": _pptx_file, ".xlsx": _xlsx_file, ".xlsm": _xlsx_file,
               ".csv": _csv_file, **{e: _image_file for e in IMG_EXT}}


# ------------------------------------------------------------------ cache
def is_document(rel: str) -> bool:
    return Path(rel).suffix.lower() in _EXTRACTORS


def _hash_file(p: Path) -> str:
    h = hashlib.sha1()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def cache_dir(repo: str) -> Path:
    return Path(repo, ".repomap", "text")


def extracted_path(repo: str, rel: str, full: bool = False):
    """Path of the cached text for a document, extracting it first if needed. None when unsupported/unavailable.
    full=True (a tool is opening this file) also reads an image file that the build skipped under the image policy."""
    src = Path(repo, rel)
    ext = src.suffix.lower()
    if ext not in _EXTRACTORS or not src.is_file() or src.stat().st_size > MAX_DOC_BYTES:
        return None
    if not available() and ext not in IMG_EXT and ext != ".csv":
        return None
    cdir = cache_dir(repo)
    cdir.mkdir(parents=True, exist_ok=True)
    gi = cdir.parent / ".gitignore"
    if not gi.exists():
        gi.write_text("*\n", encoding="utf-8")
    h = _hash_file(src)
    out = cdir / f"{h}.txt"
    meta = cdir / f"{h}.json"
    deferred = ext in IMG_EXT and not full and not image_policy(repo)["ocr_at_build"]
    if out.exists():
        if not (full and meta.exists() and '"deferred": true' in meta.read_text(encoding="utf-8", errors="replace")):
            return out
    try:
        if ext in IMG_EXT:
            lines, figures = _image_file(src, ocr=not deferred)
        else:
            lines, figures = _EXTRACTORS[ext](src)
    except Exception as e:  # noqa: BLE001
        lines, figures = [f"(extraction failed: {type(e).__name__}: {e})"], []
    header = [f"# {rel}", f"(extracted text; cite as {rel}:<line>; page markers and figures inline)", ""]
    out.write_text("\n".join(header + lines) + "\n", encoding="utf-8")
    figdir = cdir / "figures"
    figdir.mkdir(exist_ok=True)
    figmeta = []
    for f in figures:
        fp = figdir / f"{f['id']}.png"
        if not fp.exists():
            try:
                if Image is not None:
                    Image.open(io.BytesIO(f["bytes"])).convert("RGB").save(fp, format="PNG")
                else:
                    fp.write_bytes(f["bytes"])
            except Exception:  # noqa: BLE001
                continue
        figmeta.append({"id": f["id"], "page": f["page"], "kind": f["kind"], "file": str(fp.name)})
    meta.write_text(json.dumps({"source": rel, "hash": h, "lines": len(lines), "figures": figmeta, "deferred": deferred},
                               indent=1), encoding="utf-8")
    if figmeta and not deferred and os.environ.get("MODEL_VISION_NAME") and not os.environ.get("CONTEXT_GRAPH_NO_CAPTIONS"):
        try:
            import vision
            vision.describe_figures(repo, rel)
        except Exception:  # noqa: BLE001
            pass                                     # captions are optional; the text is already usable
    return out


def read_document(repo: str, rel: str, full: bool = False) -> str:
    p = extracted_path(repo, rel, full=full)
    return p.read_text(encoding="utf-8", errors="replace") if p else ""


def figures_for(repo: str, rel: str):
    """[(figure id, png path, page)] for a document, from its cache metadata."""
    p = extracted_path(repo, rel)
    if not p:
        return []
    meta = p.with_suffix(".json")
    try:
        d = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [(f["id"], cache_dir(repo) / "figures" / f["file"], f.get("page")) for f in d.get("figures", [])]


def add_caption(repo: str, rel: str, fig_id: str, caption: str) -> bool:
    """Insert a `[caption] ...` line after a figure's marker in the cached text. Idempotent."""
    p = extracted_path(repo, rel)
    if not p:
        return False
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith(f"[figure {fig_id}]"):
            if i + 1 < len(lines) and lines[i + 1].startswith("[caption]"):
                lines[i + 1] = "[caption] " + caption.strip().replace("\n", " ")
            else:
                lines.insert(i + 1, "[caption] " + caption.strip().replace("\n", " "))
            p.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return True
    return False
