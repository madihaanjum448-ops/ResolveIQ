"""FastAPI application for ResolveIQ: Procurement Exception Resolver.
Exposes core deterministic endpoints (/match, /classify, /policy, /capability/route, /verify, /lint, /load)
and case workflow endpoints for n8n orchestration and the dashboard.
"""
from typing import Any, Dict, List, Optional, Union
from datetime import date

from fastapi import FastAPI, HTTPException, UploadFile, File, Query
from pydantic import BaseModel, Field

from api import service as s
from engine import llm
from engine.classify import CaseFacts, classify, ALLOWED_ACTIONS, DEFAULT_ACTION
from engine.policy import lint_draft, gate
from engine.verify import verify as verify_logic
from engine.normalise import load_table

app = FastAPI(
    title="ResolveIQ - Procurement Exception Resolver API",
    description="Deterministic matching, capability-aware routing, classification, and policy gate engine.",
    version="3.0.0"
)


# ---------------------------------------------------------------- Request/Response Models
class MatchRequest(BaseModel):
    po: Dict[str, Any] = Field(..., description="Purchase Order details: {po_number, qty, price, ...}")
    grns: List[Dict[str, Any]] = Field(default_factory=list, description="Goods Receipt Notes: [{grn_id, qty, ...}]")
    invoices: List[Dict[str, Any]] = Field(default_factory=list, description="Invoices: [{number, qty, price, ...}]")
    tolerance_pct: float = Field(2.0, description="Quantity tolerance percentage")
    price_tolerance_pct: float = Field(1.0, description="Price tolerance percentage")


class ClassifyRequest(BaseModel):
    po_number: str
    ordered_qty: int
    po_price: float
    received_qty: int
    invoices: List[Dict[str, Any]] = Field(default_factory=list)
    tolerance_pct: float = 2.0
    price_tolerance_pct: float = 1.0
    transit_qty: int = 0
    transit_eta: Optional[date] = None
    supplier_confirmed_short: bool = False
    conflicting_claims: bool = False
    today: Optional[date] = None
    flags: List[str] = Field(default_factory=list)


class PolicyGateRequest(BaseModel):
    case_class: str
    action: str
    mode: str = "suggestion"  # suggestion | assisted | auto
    hold_value: float = 0.0
    hold_limit: float = 50000.0
    msme_flag: bool = False
    injection_flag: bool = False
    qty_provenance: str = "NATIVE"
    draft: Optional[str] = None
    allowed_numbers: Optional[List[Union[int, float, str]]] = None
    action_key: Optional[str] = None
    done_keys: List[str] = Field(default_factory=list)
    attempt: int = 0
    max_attempts: int = 3
    human_flags: List[str] = Field(default_factory=list)


class RouteRequest(BaseModel):
    customer: str
    capability: str


class CapabilityProfileUpdate(BaseModel):
    customer_id: str
    capability: str
    provider: str  # NATIVE | FALLBACK | MANUAL
    fallback: Optional[str] = None
    healthy: bool = True
    max_age_min: int = 15


class VerifyRequest(BaseModel):
    case_class: str
    ordered: int
    received: int
    tolerance_pct: float = 2.0
    grn_ids: List[str] = Field(default_factory=list)
    credit_note_value: Optional[float] = None
    hold_value: float = 0.0
    corrected_price: Optional[float] = None
    po_price: Optional[float] = None
    payable_invoices: Optional[int] = None


class Email(BaseModel):
    message_id: str
    subject: str = ""
    body: str = ""
    extraction: Optional[dict] = None


class Approve(BaseModel):
    edited_draft: Optional[str] = None
    approver: str = "buyer"


class Health(BaseModel):
    customer: str
    capability: str
    healthy: bool


class LintRequest(BaseModel):
    draft: str
    allowed_numbers: List[Union[int, float, str]] = Field(default_factory=list)


# ---------------------------------------------------------------- Core Deterministic Endpoints
@app.get("/health")
def health():
    return {"ok": True, "mode": s.MODE, "today": str(s.today())}


@app.post("/match")
def match_endpoint(req: MatchRequest):
    """Deterministic 3-way matching endpoint comparing PO, GRN(s), and Invoice(s)."""
    return s.match_po_grn_invoice(
        po=req.po,
        grns=req.grns,
        invoices=req.invoices,
        tolerance_pct=req.tolerance_pct,
        price_tolerance_pct=req.price_tolerance_pct,
    )


@app.post("/classify")
def classify_endpoint(req: ClassifyRequest):
    """Deterministic classifier mapping case facts to one of 8 canonical exception classes."""
    facts = CaseFacts(
        po_number=req.po_number,
        ordered_qty=req.ordered_qty,
        po_price=req.po_price,
        received_qty=req.received_qty,
        invoices=req.invoices,
        tolerance_pct=req.tolerance_pct,
        price_tolerance_pct=req.price_tolerance_pct,
        transit_qty=req.transit_qty,
        transit_eta=req.transit_eta,
        supplier_confirmed_short=req.supplier_confirmed_short,
        conflicting_claims=req.conflicting_claims,
        today=req.today or s.today(),
        flags=req.flags,
    )
    res = classify(facts)
    return {
        "case_class": res.case_class,
        "gap_qty": res.gap_qty,
        "hold_value": res.hold_value,
        "reasons": res.reasons,
        "flags": res.flags,
        "allowed_actions": sorted(list(ALLOWED_ACTIONS.get(res.case_class, set()))),
        "default_action": DEFAULT_ACTION.get(res.case_class, "ESCALATE"),
    }


@app.post("/policy")
def policy_gate_endpoint(req: PolicyGateRequest):
    """Deterministic 7-step Policy Gate enforcing boundaries on automated and proposed actions."""
    allowed_set = set(req.allowed_numbers) if req.allowed_numbers else None
    decision, reasons = gate(
        case_class=req.case_class,
        action=req.action,
        mode=req.mode,
        hold_value=req.hold_value,
        hold_limit=req.hold_limit,
        msme_flag=req.msme_flag,
        injection_flag=req.injection_flag,
        qty_provenance=req.qty_provenance,
        draft=req.draft,
        allowed_numbers=allowed_set,
        action_key=req.action_key,
        done_keys=set(req.done_keys),
        attempt=req.attempt,
        max_attempts=req.max_attempts,
        human_flags=req.human_flags,
    )
    return {
        "decision": decision,
        "reasons": reasons,
        "action": req.action,
        "is_supplier_facing": req.action in s.SUPPLIER_FACING,
    }


@app.post("/capability/route")
def route_capability_endpoint(req: RouteRequest):
    """Capability Router: Picks NATIVE, FALLBACK, or MANUAL for a given capability."""
    return s.route_capability(req.customer, req.capability)


@app.get("/capability/profile")
def get_capability_profile_endpoint(customer: str = Query(..., description="Customer ID (e.g. X, Y)")):
    """Returns the capability profile for a given customer."""
    con = s.db()
    try:
        prof = s.profile_for(con, customer)
        return {"customer": customer.upper(), "profile": prof}
    finally:
        con.close()


@app.put("/capability/profile")
def update_capability_profile_endpoint(update: CapabilityProfileUpdate):
    """Updates or inserts a customer capability profile entry in the database."""
    con = s.db()
    try:
        con.execute(
            "INSERT INTO capability_profile(customer_id, capability, provider, fallback, healthy, max_age_min) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT (customer_id, capability) DO UPDATE SET "
            "provider=excluded.provider, fallback=excluded.fallback, healthy=excluded.healthy, max_age_min=excluded.max_age_min",
            (update.customer_id.upper(), update.capability, update.provider.upper(), update.fallback,
             int(update.healthy), update.max_age_min)
        )
        con.commit()
        return {"ok": True, "updated": update.model_dump()}
    finally:
        con.close()


@app.post("/verify")
def verify_endpoint(req: VerifyRequest):
    """Deterministic verification logic validating whether closed criteria have been met."""
    ok, why = verify_logic(
        case_class=req.case_class,
        ordered=req.ordered,
        received=req.received,
        tolerance_pct=req.tolerance_pct,
        grn_ids=req.grn_ids,
        credit_note_value=req.credit_note_value,
        hold_value=req.hold_value,
        corrected_price=req.corrected_price,
        po_price=req.po_price,
        payable_invoices=req.payable_invoices,
    )
    return {"ok": ok, "why": why}


@app.post("/lint")
def lint_endpoint(req: LintRequest):
    """Lints draft text against forbidden accusatory words and ungrounded figures."""
    allowed_set = set(req.allowed_numbers)
    problems = lint_draft(req.draft, allowed_set)
    return {"problems": problems, "clean": len(problems) == 0}


@app.post("/load")
async def load_endpoint(kind: str = Query("po", description="Type: po, grn, or invoice"), file: UploadFile = File(...)):
    """Smart CSV/Excel Cleaner Loader endpoint accepting uploaded messy files."""
    contents = await file.read()
    rows, report = load_table(contents, kind.lower())
    return {"kind": kind.lower(), "rows_count": len(rows), "rows": rows, "report": report}


# ---------------------------------------------------------------- n8n & Workflow Endpoints
@app.post("/scan/{customer}")            # WF2
def scan(customer: str):
    return {"opened": s.scan(customer.upper())}


@app.post("/emails")                     # WF1
def emails(e: Email):
    ext = e.extraction
    if ext is None:
        try:
            ext = llm.extract_email(e.subject, e.body, s.today())
        except Exception as err:
            ext = {"intent": "OTHER", "injection": False, "confidence": 0, "summary": f"LLM failed: {err}"}
    return s.ingest_email(e.message_id, e.subject, e.body, ext)


@app.post("/cases/{cid}/investigate")    # WF3
def investigate(cid: str, proposed_action: Optional[str] = None):
    return s.investigate(cid, proposed_action)


@app.post("/cases/{cid}/approve")        # WF4
def approve(cid: str, a: Approve):
    r = s.approve(cid, a.edited_draft, a.approver)
    if not r.get("ok"):
        raise HTTPException(409, r)
    return r


@app.post("/cases/{cid}/reject")
def reject(cid: str, reason: str = ""):
    return s.reject(cid, reason)


@app.post("/cases/{cid}/verify")         # WF5
def verify_case_endpoint(cid: str):
    return s.verify_case(cid)


@app.get("/cases")
def cases(status: Optional[str] = None):
    return [c for c in s.list_cases() if not status or c["status"] == status]


@app.get("/cases/{cid}")
def case(cid: str):
    return s.get_case(cid)


# ---------------------------------------------------------------- Demo & Admin Controls
@app.post("/admin/health")
def set_health(h: Health):
    s.set_health(h.customer.upper(), h.capability, h.healthy)
    return {"ok": True}


@app.post("/admin/clock")
def clock(today_val: Optional[str] = None):
    s.set_today(today_val)
    return {"today": str(s.today())}


@app.post("/admin/grn")
def grn(customer: str, po_number: str, qty: int):
    return {"grn_id": s.simulate_grn(customer.upper(), po_number, qty)}


@app.post("/admin/reset")
def reset():
    s.reset()
    return {"ok": True}


@app.get("/admin/paths")
def paths():
    return s.path_log()
