"""Thin FastAPI wrapper. n8n calls these endpoints; all logic is in service.py."""
from typing import Optional
from datetime import date

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from api import service as s
from engine import llm
from engine.policy import lint_draft

app = FastAPI(title="Procurement Exception Resolver")


class Email(BaseModel):
    message_id: str
    subject: str = ""
    body: str = ""
    extraction: Optional[dict] = None   # n8n passes its LLM node output; if None we call the LLM here


class Approve(BaseModel):
    edited_draft: Optional[str] = None
    approver: str = "buyer"


class Health(BaseModel):
    customer: str
    capability: str
    healthy: bool


@app.get("/health")
def health():
    return {"ok": True, "mode": s.MODE, "today": str(s.today())}


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
    if not r["ok"]:
        raise HTTPException(409, r)
    return r


@app.post("/cases/{cid}/reject")
def reject(cid: str, reason: str = ""):
    return s.reject(cid, reason)


@app.post("/cases/{cid}/verify")         # WF5
def verify(cid: str):
    return s.verify_case(cid)


@app.get("/cases")
def cases(status: Optional[str] = None):
    return [c for c in s.list_cases() if not status or c["status"] == status]


@app.get("/cases/{cid}")
def case(cid: str):
    return s.get_case(cid)


@app.post("/lint")
def lint(draft: str, numbers: str = ""):
    return {"problems": lint_draft(draft, set(numbers.split(",")) if numbers else set())}


# ---- demo controls
@app.post("/admin/health")
def set_health(h: Health):
    s.set_health(h.customer.upper(), h.capability, h.healthy)
    return {"ok": True}


@app.post("/admin/clock")
def clock(today: Optional[str] = None):     # e.g. jump past the ETA
    s.set_today(today)
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
