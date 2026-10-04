"""One place that turns canonical rows + email claims into CaseFacts.
Used by the API, the tests and the golden evaluation, so all three judge cases identically."""
from datetime import date

from engine.classify import CaseFacts


def build_facts(po: dict, grns: list, invoices: list, claims: list, today: date) -> CaseFacts:
    flags = []
    if any(float(g["qty"]) < 0 for g in grns):
        flags.append("return / negative GRN in receipts: human must check")
    uoms = {str(x.get("uom", "")).strip().lower() for x in [po, *grns, *invoices] if str(x.get("uom", "")).strip()}
    if len(uoms) > 1:
        flags.append(f"unit mismatch across documents {sorted(uoms)}: human must convert")
    if any(i.get("tax_inclusive") for i in invoices):
        flags.append("invoice rate is tax-inclusive: human must compare like for like")
    transit = [c for c in claims if c.get("transit_qty")]
    last = transit[-1] if transit else {}
    return CaseFacts(
        po["po_number"], int(po["qty"]), float(po["price"]), int(sum(float(g["qty"]) for g in grns)), invoices,
        transit_qty=int(last.get("transit_qty") or 0),
        transit_eta=date.fromisoformat(last["eta"]) if last.get("eta") else None,
        supplier_confirmed_short=any(c.get("intent") == "CONFIRMS_SHORTAGE" for c in claims),
        conflicting_claims=len({int(c["transit_qty"]) for c in transit}) > 1,
        today=today, flags=flags)
