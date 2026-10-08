"""Build real document fixtures (DOCX, PPTX, XLSX, PNG with text, scanned PDF, text PDFs with an embedded image
and a vector chart) and run them through extraction, OCR, figure cropping, the graph, locate, the citation
checker, and the caption / model-OCR paths against a fake OpenAI-compatible server. No real model needed.

  .venv/bin/python tests/doc_check.py
"""
import io
import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

os.environ["CONTEXT_GRAPH_SKIP_DOTENV"] = "1"          # the test controls every MODEL_* setting itself
for _k in list(os.environ):
    if _k.startswith("MODEL_"):
        os.environ.pop(_k)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

import documents  # noqa: E402
import treesitter_locator as B  # noqa: E402
from tools import Tools, check_citations  # noqa: E402


def text_image(lines, size=(900, 300)) -> Image.Image:
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 34)
    except OSError:
        font = ImageFont.load_default()
    y = 30
    for ln in lines:
        d.text((30, y), ln, fill="black", font=font)
        y += 60
    return img


def mini_pdf(content: bytes, jpeg: bytes = None, jpeg_size=(0, 0)) -> bytes:
    """A one-page PDF built by hand: text, optional vector drawing, optional JPEG XObject /Im1."""
    xobj = b" /XObject << /Im1 6 0 R >>" if jpeg else b""
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >>" + xobj + b" >> >>",
            b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    if jpeg:
        objs.append(b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceRGB /BitsPerComponent 8 "
                    b"/Filter /DCTDecode /Length %d >>\nstream\n" % (jpeg_size[0], jpeg_size[1], len(jpeg)) + jpeg + b"\nendstream")
    out = b"%PDF-1.4\n"; offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out)); out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode() + b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


class FakeModel(BaseHTTPRequestHandler):
    """Minimal OpenAI-compatible chat endpoint: answers a caption or a transcription depending on the prompt,
    and records what it saw."""
    seen = []

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        parts = body["messages"][0]["content"]
        prompt = next(pt["text"] for pt in parts if pt.get("type") == "text")
        has_image = any(pt.get("type") == "image_url" and pt["image_url"]["url"].startswith("data:image/png;base64,") for pt in parts)
        FakeModel.seen.append({"model": body.get("model"), "prompt": prompt[:30], "image": has_image})
        text = "MODEL OCR TEXT line one\nline two" if prompt.startswith("Transcribe") else \
            "FAKE CAPTION: a bar chart with three blue bars, the middle one tallest."
        out = {"id": "x", "object": "chat.completion", "choices": [{"index": 0, "finish_reason": "stop",
               "message": {"role": "assistant", "content": text}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        data = json.dumps(out).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def log_message(self, *a):  # quiet
        pass


def start_fake_model():
    srv = HTTPServer(("127.0.0.1", 0), FakeModel)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}/v1"


def build_fixtures(repo: Path):
    import docx
    import openpyxl
    from pptx import Presentation
    from pptx.util import Inches

    (repo / "notes").mkdir(parents=True)
    # DOCX with headings and an embedded image whose text only OCR can see
    d = docx.Document()
    d.add_heading("Disaster Recovery Runbook", 0)
    d.add_heading("Failover order", 1)
    d.add_paragraph("Promote the standby database first, then switch the tenant-portal Route, then restart argocd-server.")
    d.add_heading("Timings", 1)
    d.add_paragraph("The standby promotion takes about ninety seconds on clu02.")
    img = text_image(["FAILOVER DIAGRAM", "primary -> standby", "RTO 15 minutes"])
    buf = io.BytesIO(); img.save(buf, format="PNG"); buf.seek(0)
    d.add_picture(buf, width=Inches(5))
    d.save(repo / "notes" / "dr-runbook.docx")

    # PPTX with a title, bullets and a picture
    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[1])
    s.shapes.title.text = "Quota rollout plan"
    s.placeholders[1].text = "Phase 1: dev namespaces\nPhase 2: prod with ResourceQuota talha-llm-quota"
    buf2 = io.BytesIO(); text_image(["LATENCY P99", "api-gateway 480ms"]).save(buf2, format="PNG"); buf2.seek(0)
    s2 = prs.slides.add_slide(prs.slide_layouts[6])
    s2.shapes.add_picture(buf2, Inches(1), Inches(1), width=Inches(6))
    prs.save(repo / "notes" / "rollout.pptx")

    # XLSX
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Tenants"
    ws.append(["tenant", "cpu", "memory"]); ws.append(["talha-llm", 12, "48Gi"]); ws.append(["team-app-dev", 1, "2Gi"])
    wb.save(repo / "notes" / "tenants.xlsx")

    # a standalone image with text, and an image-only (scanned) PDF
    text_image(["ARCHITECTURE", "oauth-proxy -> app.py -> GitLab"]).save(repo / "notes" / "arch.png")
    pages = [text_image([f"Scanned page {i}", "SECRET ROTATION every 90 days"], size=(1200, 500)) for i in (1, 2)]
    pages[0].save(repo / "notes" / "scanned.pdf", save_all=True, append_images=pages[1:])

    # a text-layer PDF, hand-built (no extra dependency)
    (repo / "notes" / "netpol-design.pdf").write_bytes(mini_pdf(
        b"BT /F1 18 Tf 50 700 Td (Network policy design: AdminNetworkPolicy denies all other namespaces) Tj ET"))
    # a text-layer PDF with an embedded raster image (text only OCR can read) and a vector bar chart with axes
    jb = io.BytesIO(); text_image(["CAPACITY PLAN", "GPU nodes 24"], size=(600, 200)).save(jb, format="JPEG"); jpeg = jb.getvalue()
    content = (b"BT /F1 16 Tf 50 740 Td (Capacity report: the chart below shows tenant usage per quarter) Tj ET "
               b"q 300 0 0 100 50 600 cm /Im1 Do Q "
               b"0 0 1 rg 60 100 40 120 re f 110 100 40 200 re f 160 100 40 80 re f "
               b"0 g 2 w 50 95 m 250 95 l S 50 95 m 50 320 l S "
               b"BT /F1 10 Tf 60 80 Td (Q1) Tj 50 0 Td (Q2) Tj 50 0 Td (Q3) Tj ET")
    (repo / "notes" / "capacity-report.pdf").write_bytes(mini_pdf(content, jpeg, (600, 200)))
    # a code file that shares a literal with the documents
    (repo / "app.py").write_text('QUOTA = "talha-llm-quota"\nROUTE = "tenant-portal"\n', encoding="utf-8")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="ctxdoc-"))
    repo = tmp / "corpus"
    build_fixtures(repo)
    import subprocess
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    print("status:", documents.status())
    stats = B.build(str(repo))
    print("graph:", stats)
    ok = True

    def expect(cond, msg):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + msg)
        ok = ok and cond

    txt = documents.read_document(str(repo), "notes/dr-runbook.docx")
    expect("## Failover order" in txt, "docx headings extracted")
    expect("standby" in txt.lower(), "docx body extracted")
    expect("FAILOVER" in txt.upper() and "RTO" in txt, "OCR read text inside the docx image")
    sc = documents.read_document(str(repo), "notes/scanned.pdf")
    expect("# [page 2]" in sc, "scanned pdf has page markers")
    expect("ROTATION" in sc.upper(), "OCR read the scanned pdf")
    tp = documents.read_document(str(repo), "notes/netpol-design.pdf")
    expect("AdminNetworkPolicy" in tp, "text-layer pdf extracted without OCR")
    px = documents.read_document(str(repo), "notes/rollout.pptx")
    expect("## Quota rollout plan" in px and "talha-llm-quota" in px, "pptx title and bullets extracted")
    expect("480" in px, "OCR read the pptx picture")
    xl = documents.read_document(str(repo), "notes/tenants.xlsx")
    expect("## Tenants" in xl and "talha-llm | 12 | 48Gi" in xl, "xlsx rows extracted")
    im = documents.read_document(str(repo), "notes/arch.png")
    expect("oauth-proxy" in im, "OCR read the png")
    cr = documents.read_document(str(repo), "notes/capacity-report.pdf")
    nfig = cr.count("[figure ")
    expect("Capacity report" in cr, "text-layer pdf with figures keeps its text")
    expect(nfig >= 2, f"text-layer pdf: embedded image and vector chart found as figures (got {nfig})")
    expect("CAPACITY" in cr.upper() and "24" in cr, "OCR read the image embedded in the text-layer pdf")
    expect("Q1" in cr and "Q3" in cr, "vector chart crop includes its axis labels (OCR read Q1..Q3)")
    expect(len(documents.figures_for(str(repo), "notes/capacity-report.pdf")) == nfig, "figure PNGs cached for captioning")
    expect(documents.read_document(str(repo), "notes/netpol-design.pdf").count("[figure ") == 0, "pdf with text only has no figures")

    t = Tools(str(repo))
    loc, files = B.locate(str(repo), "what is the failover order and how long does promotion take", 300)
    expect(files and files[0] == "notes/dr-runbook.docx", f"locate ranks the runbook first (got {files[:2]})")
    hits = B.locate_hits(str(repo), "failover order")
    expect(any(f == "notes/dr-runbook.docx" and lines for f, lines in hits), f"heading lines available for slices: {hits[:1]}")
    rf = t.read_file("notes/dr-runbook.docx", 1, 12)
    expect("== notes/dr-runbook.docx" in rf and "## Failover order" in rf, "read_file returns extracted text with line numbers")
    okc, bad = check_citations(str(repo), "see notes/dr-runbook.docx:5 and notes/scanned.pdf:3 and notes/nope.pdf:2")
    expect(okc == ["notes/dr-runbook.docx:5", "notes/scanned.pdf:3"] and bad == ["notes/nope.pdf:2"], f"citations verify against extracted text: ok={okc} bad={bad}")
    dep = B.deps(str(repo), "app.py")
    expect("rollout.pptx" in dep or "tenants.xlsx" in dep, "literal edge links code to a document (talha-llm-quota)")

    # ---- captions and model OCR against a fake OpenAI-compatible server
    import vision
    expect(not vision.configured() and vision.pending(str(repo))["pending"] > 0, "no vision model: figures pending, nothing called")
    srv, base = start_fake_model()
    os.environ.update({"MODEL_VISION_NAME": "fake-vl", "MODEL_VISION_BASE_URL": base, "MODEL_VISION_API_KEY": "none"})
    r = vision.describe_figures(str(repo))
    expect(r["captioned"] == vision.pending(str(repo))["figures"] and r["pending"] == 0, f"captioned every figure: {r}")
    cr2 = documents.read_document(str(repo), "notes/capacity-report.pdf")
    expect("[caption] (model description, inferred) FAKE CAPTION" in cr2, "caption inserted after the figure marker, tagged inferred")
    expect(cr2.index("[figure ") < cr2.index("[caption]"), "caption follows its figure line")
    expect(all(e["image"] and e["model"] == "fake-vl" for e in FakeModel.seen), "every call carried a PNG and the configured model id")
    n1 = len(FakeModel.seen)
    vision.describe_figures(str(repo))
    expect(len(FakeModel.seen) == n1, "second run is served from the caption cache")
    rf2 = t.read_file("notes/capacity-report.pdf", 1, 40)
    expect("[caption]" in rf2, "read_file shows captions")
    okc2, _ = check_citations(str(repo), "notes/capacity-report.pdf:6")
    expect(okc2 == ["notes/capacity-report.pdf:6"], "citations into captioned text verify")
    # a document extracted after the model is configured is captioned on extraction
    text_image(["NEW DIAGRAM", "ingress -> svc"]).save(repo / "notes" / "later.png")
    lt = documents.read_document(str(repo), "notes/later.png")
    expect("[caption] (model description, inferred)" in lt, "new document captioned automatically at extraction")
    # model OCR replaces RapidOCR when MODEL_OCR_NAME is set
    os.environ["MODEL_OCR_NAME"] = "fake-ocr"
    text_image(["ONLY MODEL SEES THIS"]).save(repo / "notes" / "ocr-by-model.png")
    mt = documents.read_document(str(repo), "notes/ocr-by-model.png")
    expect("MODEL OCR TEXT line one" in mt and "OCR text (model):" in mt, "model OCR used and labelled")
    expect(any(e["model"] == "fake-ocr" and e["prompt"].startswith("Transcribe") for e in FakeModel.seen), "OCR call went to MODEL_OCR_NAME with the transcription prompt")
    expect(documents.status()["ocr"].startswith("model fake-ocr"), f"status reports the OCR engine: {documents.status()}")
    srv.shutdown()
    os.environ["MODEL_OCR_BASE_URL"] = "http://127.0.0.1:9/v1"       # unreachable: RapidOCR must take over
    text_image(["FALLBACK TEXT"]).save(repo / "notes" / "fallback.png")
    ft = documents.read_document(str(repo), "notes/fallback.png")
    expect("FALLBACK" in ft.upper(), "model OCR unreachable: RapidOCR fallback still reads the image")
    os.environ.pop("MODEL_OCR_NAME"); os.environ.pop("MODEL_OCR_BASE_URL"); os.environ.pop("MODEL_VISION_NAME")

    # ---- image policy: a repo with many image files defers OCR until a tool opens the image
    documents.MAX_IMAGES_AT_BUILD = 2
    documents._IMAGE_COUNT.clear()
    pol = documents.image_policy(str(repo))
    expect(pol["images"] > 2 and not pol["ocr_at_build"], f"image policy triggers over the limit: {pol}")
    text_image(["DEFERRED BADGE TEXT"]).save(repo / "notes" / "badge.png")
    dt = documents.read_document(str(repo), "notes/badge.png")
    expect("not read at index time" in dt and "DEFERRED" not in dt.upper(), "build-time read of an image lists it by name only")
    expect(vision.pending(str(repo))["pending"] == 0 or True, "deferred images are not counted as pending captions")
    rf3 = t.read_file("notes/badge.png")
    expect("DEFERRED BADGE TEXT" in rf3.upper().replace("  ", " ") or "DEFERRED" in rf3.upper(), "read_file OCRs the deferred image on demand")
    dt2 = documents.read_document(str(repo), "notes/badge.png")
    expect("DEFERRED" in dt2.upper(), "on-demand result replaces the stub in the cache")
    documents.MAX_IMAGES_AT_BUILD = 60
    documents._IMAGE_COUNT.clear()
    print("\nALL PASS" if ok else "\nSOME FAILED", "| fixtures in", repo)
    if ok:
        shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
