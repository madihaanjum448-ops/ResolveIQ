"""Builds the demo data: the SAME purchase situations for Company X (structured ERP JSON)
and Company Y (CSV exports + emailed text-based PDF invoices)."""
import csv
import json
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = Path(os.getenv("DATA_DIR", ROOT / "demo"))

SUP = {"name": "Sharma Traders", "email": os.getenv("SUPPLIER_EMAIL", "supplier.demo@example.com")}
POS = [  # po_number, sku, qty, price, msme
    ("PO-1001", "Basmati Rice 5kg", 100, 50.0, 0),
    ("PO-1002", "Sunflower Oil 1L", 200, 20.0, 0),
    ("PO-1003", "Detergent 1kg", 50, 100.0, 0),
    ("PO-1004", "Tea 250g", 100, 10.0, 0),
    ("PO-1005", "Toor Dal 1kg", 60, 40.0, 1),
]
GRNS = [("GRN-1", "PO-1001", 90), ("GRN-2", "PO-1002", 200), ("GRN-3", "PO-1003", 50),
        ("GRN-4", "PO-1004", 99), ("GRN-5", "PO-1005", 50)]
INVS = [  # number, po, qty, price
    ("INV-501", "PO-1001", 100, 50.0), ("INV-502", "PO-1002", 200, 22.0), ("INV-503", "PO-1003", 50, 100.0),
    ("INV-503", "PO-1003", 50, 100.0), ("INV-504", "PO-1004", 100, 10.0), ("INV-505", "PO-1005", 60, 40.0),
]


def _pdf(path, number, po, qty, price):
    lines = [f"{SUP['name']} - TAX INVOICE", "GSTIN: 29ABCDE1234F1Z5", f"Invoice No: {number}",
             f"PO No: {po}", "Item: as per PO", f"Qty: {qty}", f"Rate: Rs. {price:.2f}",
             f"Total: Rs. {qty * price:.2f}", "Thank you for your business"]
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        c = canvas.Canvas(str(path), pagesize=A4)
        y = 800
        for line in lines:
            c.drawString(60, y, line)
            y -= 22
        c.save()
    except (ImportError, Exception):
        # Pure-Python fallback minimal PDF writer
        content = "BT\n/F1 12 Tf\n"
        y = 800
        for l in lines:
            escaped = l.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            content += f"1 0 0 1 60 {y} Tm\n({escaped}) Tj\n"
            y -= 22
        content += "ET\n"
        stream_bytes = content.encode("latin-1")
        stream_len = len(stream_bytes)
        
        pdf_text = f"""%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [3 0 R] /Count 1 >>
endobj
3 0 obj
<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>
endobj
4 0 obj
<< /Length {stream_len} >>
stream
{content}endstream
endobj
5 0 obj
<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>
endobj
xref
0 6
0000000000 65535 f 
0000000009 00000 n 
0000000058 00000 n 
0000000115 00000 n 
0000000244 00000 n 
0000000340 00000 n 
trailer
<< /Size 6 /Root 1 0 R >>
startxref
420
%%EOF"""
        with open(path, "wb") as f:
            f.write(pdf_text.encode("latin-1"))


def build():
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "customer_x").mkdir(parents=True)
    (OUT / "customer_y" / "invoices").mkdir(parents=True)
    po_rows = [{"po_number": p, "sku": s, "qty": q, "price": pr, "supplier": SUP["name"],
                "supplier_email": SUP["email"], "msme": m} for p, s, q, pr, m in POS]
    grn_rows = [{"grn_id": g, "po_number": p, "qty": q} for g, p, q in GRNS]
    inv_rows = [{"number": n, "po_number": p, "qty": q, "price": pr, "total": q * pr} for n, p, q, pr in INVS]
    (OUT / "customer_x" / "erp.json").write_text(json.dumps({"pos": po_rows, "grns": grn_rows, "invoices": inv_rows}, indent=2))
    for name, rows in [("po.csv", po_rows), ("grn.csv", grn_rows), ("invoices.csv", inv_rows)]:
        with open(OUT / "customer_y" / name, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    for i, (n, p, q, pr) in enumerate(INVS):
        _pdf(OUT / "customer_y" / "invoices" / f"{i:02d}_{n}.pdf", n, p, q, pr)


if __name__ == "__main__":
    import sys
    if (OUT / "customer_x" / "erp.json").exists() and "--force" not in sys.argv:
        print("demo data exists; use --force to rebuild")
        raise SystemExit
    build()
    print("demo data written to", OUT)
