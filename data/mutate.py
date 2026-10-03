"""Mutations: re-save a clean scenario the way real small-shop exports look. Labels are inherited.
Each mutation only changes FORMAT, never meaning, so the system must reach the same answer."""
import csv
import random
from pathlib import Path

ALT_NAMES = {
    "po": {"po_number": "PO No.", "qty": "Order Qty", "price": "Rate", "supplier": "Party Name", "sku": "Item Name"},
    "grn": {"po_number": "Purchase Order", "qty": "Qty Recd", "grn_id": "GRN No", "date": "Recd Date"},
    "invoice": {"po_number": "PO#", "number": "Bill No", "qty": "Billed Qty", "price": "Rate/Unit", "total": "Amount"},
}
DATE_FMTS = ["%d/%m/%Y", "%d-%b-%y", "%d.%m.%Y", "%Y-%m-%d"]


def _money(v):
    v = float(v)
    s = f"{v:,.2f}" if v >= 1000 or not v.is_integer() else f"{int(v)}"
    return random.choice([f"Rs. {s}", s, f"₹{s}"])


def mutate(kind, rows, muts, rng):
    """Returns (header, data_rows, title_rows, merged_title). muts: subset of MUTATIONS keys."""
    random.seed(rng)
    cols = list(dict.fromkeys(k for r in rows for k in r))
    header = [ALT_NAMES[kind].get(c, c) if "alias" in muts else c for c in cols]
    if "case" in muts:
        header = [h.upper() if i % 2 else f" {h} " for i, h in enumerate(header)]
    data = []
    for r in rows:
        out = []
        for c in cols:
            v = r.get(c, "")
            if c == "po_number" and "po_mess" in muts:
                n = str(v).split("-")[1]
                v = random.choice([f" po {n} ", f"PO{n}", f"po-{n}", f"PO - {n}"])
            elif c in ("qty",) and "num_text" in muts and isinstance(v, (int, float)) and abs(v) >= 1000:
                v = f"{v:,}"
            elif c in ("qty",) and "num_text" in muts:
                v = str(v)
            elif c in ("price", "total") and "num_text" in muts:
                v = _money(v)
            elif c == "date" and "dates" in muts and v:
                from datetime import date
                v = date.fromisoformat(v).strftime(random.choice(DATE_FMTS))
            elif isinstance(v, bool):
                v = "Yes" if v else "No"
            out.append(v)
        data.append(out)
    if "dup_row" in muts and kind == "grn" and data:
        data.insert(1, list(data[0]))                      # copy-paste of an identical row
    if "subtotal" in muts:
        data.append([""] * len(cols))
        tot = [""] * len(cols)
        tot[0] = "Total"
        if "qty" in cols:
            tot[cols.index("qty")] = sum(float(r.get("qty", 0)) for r in rows)
        data.append(tot)
    titles = [["Sharma Traders Pvt Ltd"], [f"{kind.upper()} register - Oct 2026"], []] if "header_offset" in muts else []
    return header, data, titles


MUTATIONS = ["alias", "case", "po_mess", "num_text", "dates", "dup_row", "subtotal", "header_offset", "xlsx_merged"]


def write(path: Path, kind, rows, muts, rng=0):
    header, data, titles = mutate(kind, rows, muts, rng)
    if "xlsx_merged" in muts:
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.append(["Sharma Traders Pvt Ltd - " + kind.upper() + " register"])
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=max(2, len(header)))
        ws.append([])
        for t in titles[1:]:
            ws.append(t)
        ws.append(header)
        for d in data:
            ws.append(d)
        out = path.with_suffix(".xlsx")
        wb.save(out)
        return out
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        for t in titles:
            w.writerow(t)
        w.writerow(header)
        w.writerows(data)
    return path
