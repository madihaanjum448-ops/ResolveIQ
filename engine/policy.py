"""Policy gate, draft lint, templated drafts, email-to-case linking."""
import os
import re
from .classify import ALLOWED_ACTIONS, SUPPLIER_FACING

BANNED = ["fraud", "cheat", "scam", "lie", "lying", "liar", "dishonest", "theft", "steal", "stole",
          "legal action", "blacklist", "penalty"]
INJECTION_PATTERNS = [r"ignore (all|your|previous)", r"approve (the|this)", r"system prompt",
                      r"you are now", r"disregard", r"override", r"mark (it|this|the case) (as )?(closed|resolved)"]


def kill_switch_on() -> bool:
    return os.getenv("KILL_SWITCH", "off").lower() == "on"


def looks_injected(text: str) -> bool:
    t = (text or "").lower()
    return any(re.search(p, t) for p in INJECTION_PATTERNS)


def lint_draft(draft: str, allowed_numbers: set) -> list:
    """Return problems. Empty list = clean. Numbers must come from the system of record."""
    problems = []
    low = draft.lower()
    for w in BANNED:
        if re.search(rf"\b{re.escape(w)}\b", low):
            problems.append(f"banned word: {w}")
    allowed = {_norm(str(n)) for n in allowed_numbers}
    for num in re.findall(r"\d[\d,]*(?:\.\d+)?", draft):
        if _norm(num) not in allowed:
            problems.append(f"ungrounded number: {num}")
    return problems


def _norm(s: str) -> str:
    s = s.replace(",", "")
    try:
        f = float(s)
        return str(int(f)) if f == int(f) else str(f)
    except ValueError:
        return s


def gate(case_class, action, *, mode="suggestion", hold_value=0.0, hold_limit=50000,
         msme_flag=False, injection_flag=False, qty_provenance="NATIVE",
         draft=None, allowed_numbers=None, action_key=None, done_keys=(), attempt=0, max_attempts=3):
    """Returns (decision, reasons). decision in ALLOWED | NEEDS_HUMAN | BLOCKED. Order matters."""
    reasons = []
    if action not in ALLOWED_ACTIONS.get(case_class, set()):
        return "BLOCKED", [f"{action} not allowed for {case_class}"]
    if kill_switch_on():
        return "BLOCKED", ["kill switch on"]
    if action_key and action_key in done_keys:
        return "BLOCKED", [f"duplicate action {action_key}"]
    if attempt >= max_attempts:
        return "NEEDS_HUMAN", [f"max attempts {max_attempts} reached"]
    if draft is not None:
        problems = lint_draft(draft, allowed_numbers or set())
        if problems:
            return "BLOCKED", ["draft lint failed: " + "; ".join(problems)]
    if injection_flag:
        reasons.append("injection flag on source email")
    if hold_value > hold_limit:
        reasons.append(f"hold {hold_value} above limit {hold_limit}")
    if msme_flag:
        reasons.append("MSME supplier: 45-day payment rule applies")
    if hold_value > 0 and qty_provenance not in ("NATIVE", "FALLBACK_SYSTEM", "HUMAN"):
        reasons.append(f"hold needs system-of-record quantities, got {qty_provenance}")
    if action in SUPPLIER_FACING and mode == "suggestion":
        reasons.append("suggestion mode: supplier-facing action needs approval")
    if action in SUPPLIER_FACING and mode == "assisted" and action != "SEND_REMINDER":
        reasons.append("assisted mode auto-sends reminders only")
    if action == "ESCALATE":
        reasons.append("escalation goes to a human")
    return ("NEEDS_HUMAN", reasons) if reasons else ("ALLOWED", ["all checks passed"])


TEMPLATES = {
    "SEND_REMINDER": "Dear {supplier},\n\nRegarding {po}: we have received {received} of {ordered} units so far. "
                     "The balance of {gap} units was expected by {eta} and is not yet recorded at our warehouse. "
                     "Could you please share the current dispatch status and LR/AWB details?\n\nRef: {token}\nThank you.",
    "REQUEST_PROOF": "Dear {supplier},\n\nRegarding {po}: our records show {received} of {ordered} units received. "
                     "Could you please share dispatch details (LR/AWB and expected date) for the balance of {gap} units?\n\n"
                     "Ref: {token}\nThank you.",
    "REQUEST_CREDIT_NOTE": "Dear {supplier},\n\nThank you for confirming the shortage on {po}. We received {received} of "
                           "{ordered} units. Please issue a credit note for {gap} units (value {hold}).\n\nRef: {token}\nThank you.",
    "REQUEST_CORRECTED_INVOICE": "Dear {supplier},\n\nRegarding {po}: the invoice price of {inv_price} per unit differs from "
                                 "the agreed PO price of {po_price}. Please share a corrected invoice.\n\nRef: {token}\nThank you.",
}


def draft_for(action, ctx: dict):
    t = TEMPLATES.get(action)
    return t.format(**ctx) if t else None


SUBJECT_TOKEN = re.compile(r"\[(CASE-\d+)\]", re.I)
PO_RE = re.compile(r"\bPO[-\s#:]*(\d{3,})\b", re.I)


def link_email(subject: str, body: str, open_cases: dict):
    """open_cases: {case_id: po_number}. Returns (case_id or None, method)."""
    m = SUBJECT_TOKEN.search(subject or "")
    if m and m.group(1).upper() in open_cases:
        return m.group(1).upper(), "subject_token"
    for text in (subject or "", body or ""):
        for po in PO_RE.findall(text):
            hits = [c for c, p in open_cases.items() if p.endswith(po)]
            if len(hits) == 1:
                return hits[0], "po_number"
    return None, "needs_llm_or_triage"
