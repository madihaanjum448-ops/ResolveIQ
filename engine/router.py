"""Capability-Aware Integration Layer: profile + router + normaliser."""
from datetime import datetime, timedelta, timezone

CAPABILITIES = ["po", "grn", "invoice", "shipment", "mismatch", "messaging", "approvals", "writeback", "verify"]

# Demo profiles. Switching X <-> Y in the demo = changing customer_id.
PROFILES = {
    "X": {c: {"provider": "NATIVE", "fallback": "csv", "healthy": True, "max_age_min": 15} for c in CAPABILITIES},
    "Y": {c: {"provider": "FALLBACK", "fallback": "csv", "healthy": True, "max_age_min": 15} for c in CAPABILITIES},
}
PROFILES["X"]["shipment"]["fallback"] = "email"
PROFILES["Y"]["invoice"]["fallback"] = "pdf"
PROFILES["Y"]["shipment"]["fallback"] = "email"
for cust in PROFILES:
    PROFILES[cust]["writeback"] = {"provider": "MANUAL", "fallback": None, "healthy": True, "max_age_min": 15}


def now():
    return datetime.now(timezone.utc)


def pick(cap, profile, at=None):
    at = at or now()
    p = profile[cap]
    last_ok = p.get("last_ok") or at
    if p["provider"] == "NATIVE" and p["healthy"] and at - last_ok <= timedelta(minutes=p["max_age_min"]):
        return "NATIVE"
    return "FALLBACK" if p.get("fallback") else "MANUAL"


def evidence(kind, data, *, source, method, confidence=1.0, is_claim=False):
    """Canonical evidence record. The engine only ever sees this shape."""
    return {"kind": kind, "data": data, "provenance": source, "method": method,
            "confidence": confidence, "is_claim": is_claim, "fetched_at": now().isoformat()}


def validate_invoice(inv: dict) -> list:
    """Checks applied to native AND fallback invoices before use."""
    errs = []
    for k in ("number", "po_number", "qty", "price", "total"):
        if inv.get(k) in (None, ""):
            errs.append(f"missing {k}")
    if not errs and abs(inv["qty"] * inv["price"] - inv["total"]) > 1:
        errs.append("line total does not add up")
    return errs
