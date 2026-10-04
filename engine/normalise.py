"""Messy CSV/Excel -> clean canonical rows, with a report of what was fixed or rejected.
Handles what small-shop exports really look like: header not on row 1, merged cells, column-name variants,
numbers as text ("1,000", "Rs. 50.00"), mixed date formats, PO numbers with spaces/case, blank and subtotal rows,
exact duplicate rows. Anything it cannot read safely is REJECTED with a reason, never guessed."""
import csv
import re
from datetime import date, datetime
from pathlib import Path
from typing import Optional

ALIASES = {
    "po_number": ["po_number", "po", "po no", "po no.", "po#", "purchase order", "po number", "order no", "order number"],
    "grn_id": ["grn_id", "grn", "grn no", "grn no.", "grn number", "receipt no", "mrn", "inward no"],
    "number": ["number", "invoice", "invoice no", "invoice no.", "inv no", "bill no", "invoice number"],
    "qty": ["qty", "quantity", "qty recd", "received qty", "recd qty", "qty received", "ordered qty", "order qty",
            "qty ordered", "billed qty", "pcs", "units"],
    "price": ["price", "rate", "unit price", "rate/unit", "price per unit", "unit rate"],
    "total": ["total", "amount", "line total", "value", "net amount"],
    "supplier": ["supplier", "vendor", "party", "party name", "supplier name", "vendor name"],
    "supplier_email": ["supplier_email", "email", "vendor email", "supplier email"],
    "sku": ["sku", "item", "item name", "description", "product"],
    "msme": ["msme", "is_msme", "msme?"],
    "date": ["date", "grn date", "invoice date", "po date", "recd date", "received on"],
    "uom": ["uom", "unit", "units of measure", "pack"],
    "tax_inclusive": ["tax_inclusive", "incl gst", "gst inclusive", "rate incl tax"],
    "credit_note": ["credit_note", "credit note", "cn amount"],
    "status": ["status"],
}
_LOOKUP = {re.sub(r"[^a-z0-9#]", "", a): k for k, v in ALIASES.items() for a in v}
REQUIRED = {"po": ["po_number", "qty", "price"], "grn": ["po_number", "qty"], "invoice": ["po_number", "number", "qty", "price"]}
SUBTOTAL = re.compile(r"\b(sub\s*total|grand\s*total|total)\b", re.I)


def _key(h) -> Optional[str]:
    return _LOOKUP.get(re.sub(r"[^a-z0-9#]", "", str(h or "").strip().lower()))


def norm_po(v) -> Optional[str]:
    from engine.policy import ascii_digits
    m = re.search(r"(\d{3,})", ascii_digits(str(v or "")))
    return f"PO-{m.group(1)}" if m else None


def num(v) -> Optional[float]:
    if v is None or str(v).strip() in ("", "-", "NA", "N/A"):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"(?i)rs\.?|inr|₹|/-|\s", "", str(v)).replace(",", "")
    neg = s.startswith("(") and s.endswith(")")
    try:
        f = float(s.strip("()"))
    except ValueError:
        return None
    return -f if neg else f


def parse_date(v) -> Optional[str]:
    if isinstance(v, (date, datetime)):
        return (v.date() if isinstance(v, datetime) else v).isoformat()
    s = str(v or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d %b %Y", "%d-%b-%y", "%d/%m/%y", "%b %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _read_raw(path: Path):
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook
        ws = load_workbook(path, data_only=True).active
        merged = {}
        for rng in ws.merged_cells.ranges:          # merged cells: copy the top-left value into the whole range
            v = ws.cell(rng.min_row, rng.min_col).value
            for r in range(rng.min_row, rng.max_row + 1):
                for c in range(rng.min_col, rng.max_col + 1):
                    merged[(r, c)] = v
        return [[merged.get((r, c), ws.cell(r, c).value) for c in range(1, ws.max_column + 1)]
                for r in range(1, ws.max_row + 1)]
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.reader(fh))


def load_table(path, kind: str):
    """Returns (rows, report). kind in {po, grn, invoice}."""
    raw = _read_raw(Path(path))
    report = {"file": Path(path).name, "fixed": [], "rejected": []}
    hdr_i = None
    for i, row in enumerate(raw[:15]):                     # header may not be on row 1
        keys = {_key(c) for c in row} - {None}
        if set(REQUIRED[kind]) <= keys:
            hdr_i = i
            break
    if hdr_i is None:
        report["rejected"].append({"row": "header", "reason": f"no header with {REQUIRED[kind]} in first 15 rows"})
        return [], report
    if hdr_i:
        report["fixed"].append(f"header found on row {hdr_i + 1}")
    cols = [_key(c) for c in raw[hdr_i]]
    for c, k in zip(raw[hdr_i], cols):
        if k and str(c).strip().lower() != k:
            report["fixed"].append(f"column '{str(c).strip()}' -> {k}")
    rows, seen = [], set()
    for n, r in enumerate(raw[hdr_i + 1:], start=hdr_i + 2):
        cells = [c for c in r if str(c or "").strip()]
        if not cells:
            continue
        if any(SUBTOTAL.search(str(c)) for c in r[:3]) and not norm_po(r[cols.index("po_number")] if "po_number" in cols else ""):
            report["fixed"].append(f"row {n}: subtotal row skipped")
            continue
        d = {k: v for k, v in zip(cols, r) if k}
        out, bad = {}, None
        for k, v in d.items():
            if k == "po_number":
                out[k] = norm_po(v)
                if out[k] and str(v).strip() != out[k]:
                    report["fixed"].append(f"row {n}: PO '{v}' -> {out[k]}")
            elif k in ("qty", "price", "total", "credit_note"):
                out[k] = num(v)
                if v not in (None, "") and out[k] is None:
                    bad = f"{k} '{v}' is not a number"
                elif isinstance(v, str) and v.strip():
                    report["fixed"].append(f"row {n}: {k} text '{v}' -> {out[k]:g}")
            elif k == "date":
                out[k] = parse_date(v)
            elif k == "msme":
                out[k] = 1 if str(v).strip().lower() in ("1", "yes", "y", "true") else 0
            elif k == "tax_inclusive":
                out[k] = str(v).strip().lower() in ("1", "yes", "y", "true")
            else:
                out[k] = str(v).strip() if v is not None else ""
        missing = [k for k in REQUIRED[kind] if out.get(k) in (None, "")]
        if bad or missing:
            report["rejected"].append({"row": n, "reason": bad or f"missing {missing}", "data": [str(c) for c in r]})
            continue
        out["qty"] = int(out["qty"]) if float(out["qty"]).is_integer() else out["qty"]
        if kind == "invoice" and out.get("total") is None:
            out["total"] = round(out["qty"] * out["price"], 2)
        sig = tuple(sorted((k, str(v)) for k, v in out.items()))
        if sig in seen and kind != "invoice":              # copy-pasted receipt row -> count once.
            # (Invoices are NEVER de-duplicated here: a repeated bill is exactly what the classifier must catch.)
            report["fixed"].append(f"row {n}: exact duplicate row ignored")
            continue
        seen.add(sig)
        if kind == "grn" and out.get("grn_id") and any(x.get("grn_id") == out["grn_id"] for x in rows):
            report["rejected"].append({"row": n, "reason": f"GRN id {out['grn_id']} repeated with different data"})
            continue
        rows.append(out)
    return rows, report
