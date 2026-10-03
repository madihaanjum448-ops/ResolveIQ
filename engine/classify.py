"""Deterministic matcher + classifier. The LLM never computes classes or money."""
from typing import Optional
from dataclasses import dataclass, field
from datetime import date

CLASSES = [
    "WITHIN_TOLERANCE", "PARTIAL_WAIT", "PARTIAL_OVERDUE", "UNKNOWN_NEEDS_EVIDENCE",
    "SHORT_CONFIRMED", "PRICE_MISMATCH", "DUPLICATE_INVOICE", "OVER_DELIVERY",
]


@dataclass
class CaseFacts:
    po_number: str
    ordered_qty: int
    po_price: float
    received_qty: int                       # sum of GRNs (system of record)
    invoices: list                          # [{"number","qty","price"}]
    tolerance_pct: float = 2.0              # qty tolerance, % of ordered
    price_tolerance_pct: float = 1.0
    # Claims from email (never evidence on their own):
    transit_qty: int = 0
    transit_eta: Optional[date] = None
    supplier_confirmed_short: bool = False
    conflicting_claims: bool = False        # two sources disagree -> never guess
    today: date = field(default_factory=date.today)


@dataclass
class Result:
    case_class: str
    gap_qty: int
    hold_value: float                       # recommendation only
    reasons: list


def classify(f: CaseFacts) -> Result:
    r = []
    numbers = [i["number"] for i in f.invoices]
    if len(numbers) != len(set(numbers)):
        dup = [n for n in set(numbers) if numbers.count(n) > 1][0]
        inv = next(i for i in f.invoices if i["number"] == dup)
        return Result("DUPLICATE_INVOICE", 0, round(inv["qty"] * inv["price"], 2),
                      [f"invoice {dup} seen {numbers.count(dup)} times"])

    inv = f.invoices[0] if f.invoices else {"qty": f.ordered_qty, "price": f.po_price}
    if abs(inv["price"] - f.po_price) > f.po_price * f.price_tolerance_pct / 100:
        hold = round((inv["price"] - f.po_price) * inv["qty"], 2)
        return Result("PRICE_MISMATCH", 0, max(hold, 0.0),
                      [f"invoice price {inv['price']} vs PO price {f.po_price}"])

    tol = f.ordered_qty * f.tolerance_pct / 100
    if f.received_qty - f.ordered_qty > tol:
        return Result("OVER_DELIVERY", f.ordered_qty - f.received_qty, 0.0,
                      [f"received {f.received_qty} > ordered {f.ordered_qty}"])

    gap = f.ordered_qty - f.received_qty
    hold = round(gap * f.po_price, 2)
    if gap <= tol:
        return Result("WITHIN_TOLERANCE", gap, 0.0, [f"gap {gap} within tolerance {tol:g}"])
    if f.conflicting_claims:
        return Result("UNKNOWN_NEEDS_EVIDENCE", gap, hold, ["conflicting claims; not guessing"])
    if f.supplier_confirmed_short:
        return Result("SHORT_CONFIRMED", gap, hold, ["supplier confirmed shortage"])
    if f.transit_qty >= gap and f.transit_eta:
        if f.transit_eta >= f.today:
            return Result("PARTIAL_WAIT", gap, hold, [f"{f.transit_qty} in transit, ETA {f.transit_eta}"])
        return Result("PARTIAL_OVERDUE", gap, hold, [f"ETA {f.transit_eta} passed, no GRN"])
    return Result("UNKNOWN_NEEDS_EVIDENCE", gap, hold, ["no explanation for the gap yet"])


# Allowed actions per class (the policy gate enforces this).
ALLOWED_ACTIONS = {
    "WITHIN_TOLERANCE": {"LOG"},
    "PARTIAL_WAIT": {"WAIT", "LOG"},
    "PARTIAL_OVERDUE": {"SEND_REMINDER", "REQUEST_PROOF", "WAIT", "ESCALATE"},
    "UNKNOWN_NEEDS_EVIDENCE": {"REQUEST_PROOF", "WAIT", "ESCALATE"},
    "SHORT_CONFIRMED": {"REQUEST_CREDIT_NOTE", "ESCALATE"},
    "PRICE_MISMATCH": {"REQUEST_CORRECTED_INVOICE", "ESCALATE"},
    "DUPLICATE_INVOICE": {"BLOCK_DUPLICATE", "ESCALATE"},
    "OVER_DELIVERY": {"ESCALATE"},
}

DEFAULT_ACTION = {
    "WITHIN_TOLERANCE": "LOG", "PARTIAL_WAIT": "WAIT", "PARTIAL_OVERDUE": "SEND_REMINDER",
    "UNKNOWN_NEEDS_EVIDENCE": "REQUEST_PROOF", "SHORT_CONFIRMED": "REQUEST_CREDIT_NOTE",
    "PRICE_MISMATCH": "REQUEST_CORRECTED_INVOICE", "DUPLICATE_INVOICE": "BLOCK_DUPLICATE",
    "OVER_DELIVERY": "ESCALATE",
}

SUPPLIER_FACING = {"SEND_REMINDER", "REQUEST_PROOF", "REQUEST_CREDIT_NOTE", "REQUEST_CORRECTED_INVOICE"}
