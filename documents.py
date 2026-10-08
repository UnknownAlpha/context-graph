"""Turn documents and images into citable text, once, cached by content hash.

Supported: .pdf .docx .pptx .xlsx .csv .png .jpg .jpeg .webp .tif .tiff
Output: <repo>/.repomap/text/<hash>.txt, a plain text file with marker lines so line numbers are stable and
citable (`docs/runbook.pdf:120` means line 120 of the extracted text):

    # [page 3]                 PDF page boundary
    ## Heading text            heading from DOCX styles, PPTX titles, XLSX sheet names, or PDF font-size heuristics
    [figure a1b2c3d4]          an embedded image; OCR text follows on the next lines, and a caption line
    [caption] ...              if a vision model described it (standalone tools only, see vision.py)

OCR (RapidOCR, ONNX, local, no model endpoint) runs only on pages with no usable text layer and on embedded
images. Everything here is deterministic; a caption is the only inferred content and is tagged as such.
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
    return {"documents": "ready" if available() else "missing: " + ", ".join(_missing),
            "ocr": "ready" if _ocr() is not None else "unavailable"}


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


def ocr_image_bytes(data: bytes) -> str:
    """Text found in an image, reading order top-to-bottom, or '' when OCR is unavailable or finds nothing."""
    eng = _ocr()
    if eng is None or Image is None:
        return ""
    try:
        img = Image.open(io.BytesIO(data)).convert("RGB")
        if min(img.size) < OCR_MIN_IMAGE_SIDE:
            return ""
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
        lines.append(f"[figure {fid}] scanned page, OCR text:")
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
            lines.append(f"[figure {fid}] embedded image" + (", OCR text:" if ocr else " (no text recognised)"))
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
                lines.append(f"[figure {fid}] image on slide {si}" + (", OCR text:" if ocr else " (no text recognised)"))
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


def _image_file(path: Path):
    data = path.read_bytes()
    fid = _fig_id(data)
    ocr = ocr_image_bytes(data)
    lines = [f"[figure {fid}] image file" + (", OCR text:" if ocr else " (no text recognised)")]
    lines.extend(ocr.splitlines())
    return lines, [{"id": fid, "page": None, "kind": "image", "bytes": data}]


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


def extracted_path(repo: str, rel: str):
    """Path of the cached text for a document, extracting it first if needed. None when unsupported/unavailable."""
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
    if out.exists():
        return out
    try:
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
    meta.write_text(json.dumps({"source": rel, "hash": h, "lines": len(lines), "figures": figmeta}, indent=1),
                    encoding="utf-8")
    return out


def read_document(repo: str, rel: str) -> str:
    p = extracted_path(repo, rel)
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
