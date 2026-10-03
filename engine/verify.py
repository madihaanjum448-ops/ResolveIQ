"""Close a case only when the re-read system data proves the fix."""


def verify(case_class, *, ordered, received, tolerance_pct=2.0, grn_ids=(), credit_note_value=None,
           hold_value=0.0, corrected_price=None, po_price=None, payable_invoices=None):
    tol = ordered * tolerance_pct / 100
    if case_class in ("PARTIAL_WAIT", "PARTIAL_OVERDUE", "UNKNOWN_NEEDS_EVIDENCE"):
        if len(grn_ids) != len(set(grn_ids)):
            return False, "duplicate GRN"
        return (abs(ordered - received) <= tol,
                f"GRN total {received} vs ordered {ordered}")
    if case_class == "SHORT_CONFIRMED":
        if abs(ordered - received) <= tol:
            return True, "goods delivered after all"
        ok = credit_note_value is not None and abs(credit_note_value - hold_value) <= 1
        return ok, f"credit note {credit_note_value} vs held {hold_value}"
    if case_class == "PRICE_MISMATCH":
        ok = corrected_price is not None and abs(corrected_price - po_price) <= po_price * 0.01
        return ok, f"corrected price {corrected_price} vs PO {po_price}"
    if case_class == "DUPLICATE_INVOICE":
        return payable_invoices == 1, f"payable invoices: {payable_invoices}"
    if case_class == "WITHIN_TOLERANCE":
        return True, "logged only"
    return False, "not closable by the system; a human closes it"
