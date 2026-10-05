"""Smart CSV/Excel Cleaner & Loader for messy real-world small-shop supplier data.
Handles:
- Header detection anywhere in top 20 rows (skipping logos, title blocks, metadata)
- Merged cells in Excel (.xlsx, .xlsm, .xls)
- Extensive column alias matching & fuzzy heuristic fallback (English, Hinglish, regional variations)
- Rich currency & numeric parsing (Rs., INR, ₹, /-, parenthesized negatives, unit suffixes like '100 pcs')
- Multi-format date parser (ISO, Indian DD/MM/YYYY, DD-Mon-YYYY, US, Excel serial numbers)
- PO normalization with Indic digit support (१००१ -> 1001 -> PO-1001)
- Subtotal, grand total, and summary row filtering
- Deduplication with provenance (GRN deduplication with fix log; Invoices preserved for duplicate detection)
- Line item total cross-checks and comprehensive audit reporting (fixed, rejected, warnings).
"""
import csv
import io
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

ALIASES = {
    "po_number": [
        "po_number", "po", "po no", "po no.", "po#", "purchase order", "po number", "order no",
        "order number", "po reference", "purchase_order", "po ref", "order ref", "purchase order number",
        "purchase order no", "purchase order ref", "order reference", "पीओ", "पी.ओ.", "पीओ नं"
    ],
    "grn_id": [
        "grn_id", "grn", "grn no", "grn no.", "grn number", "receipt no", "receipt #", "mrn",
        "mrn no", "inward no", "inward #", "challan no", "dc no", "delivery challan", "delivery note",
        "inward challan no", "inward challan", "goods receipt no", "goods receipt note"
    ],
    "number": [
        "number", "invoice", "invoice no", "invoice no.", "inv no", "inv no.", "inv#", "invoice#",
        "bill no", "bill no.", "bill#", "invoice number", "tax invoice no", "bill ref", "bill number"
    ],
    "qty": [
        "qty", "quantity", "qty recd", "received qty", "recd qty", "qty received", "ordered qty",
        "order qty", "qty ordered", "billed qty", "invoiced qty", "bill qty", "pcs", "units", "nos",
        "pieces", "quantity (nos)", "quantity nos", "qty in pcs", "qty (pcs)", "recd quantity"
    ],
    "price": [
        "price", "rate", "unit price", "rate/unit", "price per unit", "unit rate", "item rate",
        "rate (inr)", "rate (rs)", "cost", "unit cost", "basic rate", "item rate (inr)", "item rate (rs)",
        "rate in inr", "price in inr", "unit rate in inr"
    ],
    "total": [
        "total", "amount", "line total", "value", "net amount", "gross amount", "total amount",
        "item total", "line amount", "net value", "total (inr)", "total (rs)", "total value",
        "line total (inr)", "net total"
    ],
    "supplier": [
        "supplier", "vendor", "party", "party name", "supplier name", "vendor name", "seller",
        "party/vendor", "merchant"
    ],
    "supplier_email": [
        "supplier_email", "email", "vendor email", "supplier email", "party email", "contact email"
    ],
    "sku": [
        "sku", "item", "item name", "description", "product", "item description", "material",
        "product description", "item code", "part number"
    ],
    "msme": [
        "msme", "is_msme", "msme?", "msme status", "udyam", "msme registered"
    ],
    "date": [
        "date", "grn date", "invoice date", "po date", "recd date", "receipt date", "received on",
        "date received", "order date", "inward date"
    ],
    "uom": [
        "uom", "unit", "units of measure", "pack", "packaging", "uom code"
    ],
    "tax_inclusive": [
        "tax_inclusive", "incl gst", "gst inclusive", "rate incl tax", "inclusive of taxes", "incl tax"
    ],
    "credit_note": [
        "credit_note", "credit note", "cn amount", "cn value", "credit note no", "credit note amount"
    ],
    "status": [
        "status", "inv status", "invoice status"
    ],
}

_LOOKUP = {re.sub(r"[^a-z0-9#]", "", a): k for k, v in ALIASES.items() for a in v}
REQUIRED = {
    "po": ["po_number", "qty", "price"],
    "grn": ["po_number", "qty"],
    "invoice": ["po_number", "number", "qty", "price"],
}
SUBTOTAL_PATTERNS = re.compile(r"\b(sub\s*total|grand\s*total|total\s*qty|summary|page\s*\d+)\b", re.I)


def _clean_str(val: Any) -> str:
    if val is None:
        return ""
    return str(val).strip()


def _key(header_cell: Any) -> Optional[str]:
    raw_str = _clean_str(header_cell).lower()
    if not raw_str:
        return None

    # Direct cleaned lookup
    cleaned = re.sub(r"[^a-z0-9#]", "", raw_str)
    if cleaned in _LOOKUP:
        return _LOOKUP[cleaned]

    # Strip bracketed unit / currency qualifiers: (nos), (inr), (rs), (pcs), (kg)
    stripped = re.sub(r"\([^)]*\)", "", raw_str).strip()
    cleaned_stripped = re.sub(r"[^a-z0-9#]", "", stripped)
    if cleaned_stripped in _LOOKUP:
        return _LOOKUP[cleaned_stripped]

    # Fuzzy keyword heuristics
    if "email" in raw_str and any(w in raw_str for w in ("supplier", "vendor", "party", "seller", "contact")):
        return "supplier_email"
    if any(w in raw_str for w in ("purchase order", "po no", "po #", "order no", "order #")) or (raw_str.startswith("po") and ("no" in raw_str or "num" in raw_str or "ref" in raw_str)):
        return "po_number"
    if any(w in raw_str for w in ("grn", "mrn", "inward", "challan", "receipt no", "receipt #", "goods receipt")):
        return "grn_id"
    if any(w in raw_str for w in ("tax invoice", "invoice", "inv no", "inv #", "inv#", "bill no", "bill #", "bill#")):
        return "number"
    if any(w in raw_str for w in ("qty", "quantity", "pcs", "units", "pieces")):
        return "qty"
    if any(w in raw_str for w in ("rate", "unit price", "price", "unit cost", "cost per")):
        return "price"
    if any(w in raw_str for w in ("total value", "line total", "total amount", "net amount", "total")):
        return "total"
    if any(w in raw_str for w in ("supplier", "vendor", "party name", "seller")):
        return "supplier"
    if any(w in raw_str for w in ("item name", "item desc", "product", "material", "sku")):
        return "sku"
    if "msme" in raw_str or "udyam" in raw_str:
        return "msme"
    if "date" in raw_str:
        return "date"

    return None


def norm_po(v: Any) -> Optional[str]:
    """Standardizes PO number to PO-<digits> or canonical ID."""
    if v is None:
        return None
    from engine.policy import ascii_digits
    text = ascii_digits(_clean_str(v))
    m = re.search(r"(\d{3,})", text)
    if m:
        return f"PO-{m.group(1)}"
    cleaned = re.sub(r"[^\w-]", "", text).upper()
    return cleaned if cleaned else None


def num(v: Any) -> Optional[float]:
    """Parses money, quantities, floats with currency symbols, Indian commas, and unit suffixes."""
    if v is None or _clean_str(v) in ("", "-", "NA", "N/A", "null", "None", "nil"):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = _clean_str(v)
    # Strip currency symbols and common suffix units
    s = re.sub(r"(?i)rs\.?|inr|₹|\$|€|/-|\bpcs\b|\bunits\b|\bnos\b|\bbox\b|\bkg\b|\bmt\b", "", s).strip()
    s = s.replace(",", "").replace(" ", "")
    if not s:
        return None
    neg = (s.startswith("(") and s.endswith(")")) or s.endswith("-")
    s = s.strip("()-")
    try:
        f = float(s)
        return -f if neg else f
    except ValueError:
        return None


def parse_date(v: Any) -> Optional[str]:
    """Parses dates from various string formats or Excel serial integers."""
    if v is None or _clean_str(v) in ("", "-", "NA", "N/A"):
        return None
    if isinstance(v, (date, datetime)):
        return (v.date() if isinstance(v, datetime) else v).isoformat()
    if isinstance(v, (int, float)) and 30000 <= v <= 60000:
        excel_base = date(1899, 12, 30)
        return (excel_base + timedelta(days=int(v))).isoformat()

    s = _clean_str(v)
    for fmt in (
        "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d %b %Y",
        "%d-%b-%y", "%d-%b-%Y", "%d/%m/%y", "%m/%d/%Y", "%b %d, %Y",
        "%B %d, %Y", "%Y/%m/%d"
    ):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _read_raw(source: Union[str, Path, bytes, io.IOBase]) -> List[List[Any]]:
    """Reads CSV/TSV or Excel into a 2D list of cells with merged-cell resolution."""
    if isinstance(source, (str, Path)):
        p = Path(source)
        if p.suffix.lower() in (".xlsx", ".xlsm", ".xltx"):
            from openpyxl import load_workbook
            wb = load_workbook(p, data_only=True)
            ws = wb.active
            merged = {}
            for rng in ws.merged_cells.ranges:
                top_val = ws.cell(rng.min_row, rng.min_col).value
                for r in range(rng.min_row, rng.max_row + 1):
                    for c in range(rng.min_col, rng.max_col + 1):
                        merged[(r, c)] = top_val
            return [[merged.get((r, c), ws.cell(r, c).value) for c in range(1, ws.max_column + 1)]
                    for r in range(1, ws.max_row + 1)]
        
        encodings = ["utf-8-sig", "utf-8", "latin-1", "cp1252"]
        for enc in encodings:
            try:
                with open(p, newline="", encoding=enc) as fh:
                    sample = fh.read(2048)
                    fh.seek(0)
                    dialect = csv.Sniffer().sniff(sample) if sample else "excel"
                    return list(csv.reader(fh, dialect))
            except Exception:
                continue
        with open(p, newline="", encoding="utf-8", errors="replace") as fh:
            return list(csv.reader(fh))

    if isinstance(source, bytes):
        try:
            from openpyxl import load_workbook
            wb = load_workbook(io.BytesIO(source), data_only=True)
            ws = wb.active
            return [[ws.cell(r, c).value for c in range(1, ws.max_column + 1)] for r in range(1, ws.max_row + 1)]
        except Exception:
            text = source.decode("utf-8-sig", errors="replace")
            return list(csv.reader(io.StringIO(text)))

    if isinstance(source, io.IOBase):
        return list(csv.reader(source))

    return []


def load_table(source: Union[str, Path, bytes, io.IOBase], kind: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Main loader function.
    Reads messy tabular data, identifies headers, cleans data types, normalizes POs,
    filters non-data rows, and returns (clean_rows, audit_report).
    
    kind must be one of: 'po', 'grn', 'invoice'
    """
    filename = Path(source).name if isinstance(source, (str, Path)) else "upload_data"
    raw = _read_raw(source)
    report = {
        "file": filename,
        "total_rows_read": len(raw),
        "clean_rows_count": 0,
        "fixed": [],
        "rejected": [],
        "warnings": [],
    }

    if not raw:
        report["rejected"].append({"row": 0, "reason": "file is empty", "data": []})
        return [], report

    # 1. Search for header row in top 20 rows
    hdr_i = None
    required_cols = REQUIRED.get(kind, [])
    for i, row in enumerate(raw[:20]):
        matched_keys = {_key(c) for c in row if c is not None} - {None}
        if set(required_cols) <= matched_keys:
            hdr_i = i
            break

    if hdr_i is None:
        report["rejected"].append({
            "row": "header",
            "reason": f"No valid header containing required columns {required_cols} found in first 20 rows",
            "data": raw[:5]
        })
        return [], report

    if hdr_i > 0:
        report["fixed"].append(f"Header located on row {hdr_i + 1} (skipped {hdr_i} pre-header rows)")

    header_row = raw[hdr_i]
    cols = [_key(c) for c in header_row]

    for c, k in zip(header_row, cols):
        if k and _clean_str(c).lower() != k:
            report["fixed"].append(f"Mapped header column '{_clean_str(c)}' -> '{k}'")

    # 2. Process data rows
    rows: List[Dict[str, Any]] = []
    seen_signatures = set()

    for n, r in enumerate(raw[hdr_i + 1:], start=hdr_i + 2):
        cells = [_clean_str(c) for c in r if c is not None and _clean_str(c)]
        if not cells:
            continue

        # Check for subtotal / summary row
        row_text = " ".join(cells[:4])
        if SUBTOTAL_PATTERNS.search(row_text):
            po_idx = cols.index("po_number") if "po_number" in cols else -1
            po_val = r[po_idx] if 0 <= po_idx < len(r) else ""
            if not norm_po(po_val):
                report["fixed"].append(f"Row {n}: Subtotal/summary row skipped ('{row_text[:40]}')")
                continue

        d = {k: v for k, v in zip(cols, r) if k}
        out: Dict[str, Any] = {}
        bad_reason = None

        for k, v in d.items():
            if k == "po_number":
                out[k] = norm_po(v)
                if out[k] and _clean_str(v) != out[k]:
                    report["fixed"].append(f"Row {n}: Standardized PO '{_clean_str(v)}' -> '{out[k]}'")
            elif k in ("qty", "price", "total", "credit_note"):
                out[k] = num(v)
                if v not in (None, "") and out[k] is None:
                    bad_reason = f"Field '{k}' with value '{_clean_str(v)}' could not be parsed as a number"
                elif isinstance(v, str) and v.strip() and out[k] is not None:
                    if _clean_str(v) != str(out[k]):
                        report["fixed"].append(f"Row {n}: Sanitized numeric {k} '{_clean_str(v)}' -> {out[k]:g}")
            elif k == "date":
                out[k] = parse_date(v)
                if v not in (None, "") and out[k] is None:
                    report["warnings"].append(f"Row {n}: Date value '{_clean_str(v)}' could not be parsed into ISO date")
            elif k == "msme":
                out[k] = 1 if _clean_str(v).lower() in ("1", "yes", "y", "true") else 0
            elif k == "tax_inclusive":
                out[k] = _clean_str(v).lower() in ("1", "yes", "y", "true")
            else:
                out[k] = _clean_str(v) if v is not None else ""

        # Validate required columns
        missing = [k for k in required_cols if out.get(k) in (None, "")]
        if bad_reason or missing:
            report["rejected"].append({
                "row": n,
                "reason": bad_reason or f"Missing required fields: {missing}",
                "data": [str(c) if c is not None else "" for c in r]
            })
            continue

        # Convert integers where applicable
        if isinstance(out.get("qty"), float) and out["qty"].is_integer():
            out["qty"] = int(out["qty"])

        # Default or cross-check invoice totals
        if kind == "invoice":
            if out.get("total") is None and out.get("qty") is not None and out.get("price") is not None:
                out["total"] = round(out["qty"] * out["price"], 2)
            elif out.get("total") is not None and out.get("qty") is not None and out.get("price") is not None:
                expected_total = round(out["qty"] * out["price"], 2)
                if abs(out["total"] - expected_total) > 1.0:
                    report["warnings"].append(
                        f"Row {n}: Invoice total ({out['total']}) does not equal qty * price ({expected_total})"
                    )

        # Deduplication logic
        sig = tuple(sorted((k, str(v)) for k, v in out.items()))
        if sig in seen_signatures and kind != "invoice":
            report["fixed"].append(f"Row {n}: Exact duplicate row ignored (already recorded)")
            continue
        seen_signatures.add(sig)

        if kind == "grn" and out.get("grn_id") and any(x.get("grn_id") == out["grn_id"] for x in rows):
            report["rejected"].append({
                "row": n,
                "reason": f"GRN ID '{out['grn_id']}' duplicated with conflicting data",
                "data": [str(c) if c is not None else "" for c in r]
            })
            continue

        rows.append(out)

    report["clean_rows_count"] = len(rows)
    return rows, report
