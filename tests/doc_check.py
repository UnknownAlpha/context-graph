"""Build real document fixtures (DOCX, PPTX, XLSX, PNG with text, scanned PDF, text PDF) and run them through
extraction, OCR, the graph, locate, context_pack and the citation checker. No model needed.

  .venv/bin/python tests/doc_check.py
"""
import io
import shutil
import sys
import tempfile
from pathlib import Path

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
    content = b"BT /F1 18 Tf 50 700 Td (Network policy design: AdminNetworkPolicy denies all other namespaces) Tj ET"
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out = b"%PDF-1.4\n"; offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out)); out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode() + b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    (repo / "notes" / "netpol-design.pdf").write_bytes(out)
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
    print("\nALL PASS" if ok else "\nSOME FAILED", "| fixtures in", repo)
    if ok:
        shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
