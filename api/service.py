"""Case service: SQLite store + adapters (X = structured API-like JSON, Y = CSV + PDF invoices).
All business logic here is plain Python so it runs in tests without FastAPI."""
from typing import Optional
import csv
import json
import os
import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from engine.classify import CaseFacts, classify, DEFAULT_ACTION, ALLOWED_ACTIONS, SUPPLIER_FACING
from engine import llm
from engine.policy import gate, draft_for, link_email, looks_injected
from engine.router import PROFILES, pick, evidence, validate_invoice
from engine.verify import verify

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.getenv("DATA_DIR", ROOT / "data" / "demo"))
DB_PATH = os.getenv("DB_PATH", str(ROOT / "resolver.db"))
MODE = os.getenv("MODE", "suggestion")
MAX_LOOPS = 3
_clock = {"today": None}  # demo time travel


def today() -> date:
    return _clock["today"] or date.today()


def set_today(d: Optional[str]):
    _clock["today"] = date.fromisoformat(d) if d else None


# ---------------------------------------------------------------- DB
SCHEMA = """
create table if not exists cases(id text primary key, customer text, po_number text, supplier text,
  status text, case_class text, gap integer, hold real, action text, decision text, reasons text,
  draft text, attempts integer default 0, created text, unique(customer, po_number));
create table if not exists evidence(id integer primary key, case_id text, kind text, data text,
  provenance text, method text, confidence real, is_claim integer, fetched_at text);
create table if not exists events(id integer primary key, case_id text, at text, type text, detail text);
create table if not exists action_keys(key text primary key);
create table if not exists emails(message_id text primary key, case_id text, subject text, body text, method text);
create table if not exists path_log(id integer primary key, at text, customer text, capability text, path text, note text);
create table if not exists health(customer text, capability text, healthy integer, primary key(customer, capability));
"""


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(con, case_id, type_, detail):
    con.execute("insert into events(case_id, at, type, detail) values(?,?,?,?)",
                (case_id, _now(), type_, json.dumps(detail) if not isinstance(detail, str) else detail))


# ---------------------------------------------------------------- capability layer
def profile_for(con, customer):
    prof = {k: dict(v) for k, v in PROFILES[customer].items()}
    for row in con.execute("select capability, healthy from health where customer=?", (customer,)):
        prof[row["capability"]]["healthy"] = bool(row["healthy"])
    return prof


def set_health(customer, capability, healthy: bool):
    con = db()
    con.execute("insert or replace into health values(?,?,?)", (customer, capability, int(healthy)))
    con.execute("insert into path_log(at, customer, capability, path, note) values(?,?,?,?,?)",
                (_now(), customer, capability, "-", f"health set to {healthy}"))
    con.commit()


def _route(con, customer, cap):
    path = pick(cap, profile_for(con, customer))
    con.execute("insert into path_log(at, customer, capability, path, note) values(?,?,?,?,?)",
                (_now(), customer, cap, path, ""))
    return path


def _native_erp():
    return json.loads((DATA / "customer_x" / "erp.json").read_text())


def _csv(name):
    with open(DATA / "customer_y" / name, newline="") as fh:
        return list(csv.DictReader(fh))


def read_pdf_invoice(path: Path) -> dict:
    """Fallback invoice extraction: regex on PDF text first; LLM only if regex fails validation."""
    from pypdf import PdfReader
    text = "\n".join(p.extract_text() or "" for p in PdfReader(str(path)).pages)
    def grab(pat, cast=str):
        m = re.search(pat, text, re.I)
        return cast(m.group(1).replace(",", "")) if m else None
    inv = {"number": grab(r"Invoice\s*No[.:]*\s*([\w/-]+)"), "po_number": grab(r"PO\s*No[.:]*\s*([\w-]+)"),
           "qty": grab(r"Qty[.:]*\s*([\d,]+)", int), "price": grab(r"Rate[.:]*\s*(?:Rs\.?|INR)?\s*([\d,.]+)", float),
           "total": grab(r"Total[.:]*\s*(?:Rs\.?|INR)?\s*([\d,.]+)", float)}
    method, conf = "pdf_regex", 0.9
    if validate_invoice(inv):
        try:
            from engine.llm import extract_invoice
            inv, method, conf = extract_invoice(text), "pdf_llm", 0.7
        except Exception as e:  # LLM down or bad output -> human review
            return {"_error": f"extraction failed: {e}", "_text": text[:500]}
    inv["_method"], inv["_confidence"] = method, conf
    return inv


def load(con, customer, kind):
    """Returns list of canonical evidence records for kind in {po, grn, invoice}."""
    path = _route(con, customer, kind)
    out = []
    if path == "NATIVE":
        try:
            erp = _native_erp()
        except Exception as e:  # native down -> fallback, logged
            con.execute("insert into path_log(at, customer, capability, path, note) values(?,?,?,?,?)",
                        (_now(), customer, kind, "FALLBACK", f"native error: {e}"))
            path = "FALLBACK"
        else:
            for row in erp[kind + "s"]:
                out.append(evidence(kind, row, source="erp_api", method="native"))
            return out
    if path == "FALLBACK":
        if kind == "invoice" and customer == "Y":
            for pdf in sorted((DATA / "customer_y" / "invoices").glob("*.pdf")):
                inv = read_pdf_invoice(pdf)
                if "_error" in inv:
                    out.append(evidence(kind, inv, source=pdf.name, method="failed", confidence=0))
                    continue
                m, c = inv.pop("_method"), inv.pop("_confidence")
                out.append(evidence(kind, inv, source=pdf.name, method=m, confidence=c))
        else:
            src = {"po": "po.csv", "grn": "grn.csv", "invoice": "invoices.csv"}[kind]
            for row in _csv(src):
                row = {k: (float(v) if k in ("price", "total") else int(v) if k == "qty" else v) for k, v in row.items()}
                out.append(evidence(kind, row, source=src, method="csv"))
        return out
    return []  # MANUAL: caller creates a human task


# ---------------------------------------------------------------- cases
def _snapshot(con, customer, po_number):
    pos = [e for e in load(con, customer, "po") if e["data"]["po_number"] == po_number]
    grns = [e for e in load(con, customer, "grn") if e["data"]["po_number"] == po_number]
    invs = [e for e in load(con, customer, "invoice") if e["data"].get("po_number") == po_number]
    return pos, grns, invs


def scan(customer):
    """WF2: read PO/GRN/invoice via router, open a case for anything outside tolerance."""
    con = db()
    opened = []
    for po in load(con, customer, "po"):
        p = po["data"]
        _, grns, invs = _snapshot(con, customer, p["po_number"])
        bad = [i for i in invs if i["method"] == "failed" or validate_invoice(i["data"])]
        facts = CaseFacts(p["po_number"], int(p["qty"]), float(p["price"]),
                          sum(int(g["data"]["qty"]) for g in grns),
                          [i["data"] for i in invs if i not in bad], today=today())
        res = classify(facts)
        if res.case_class == "WITHIN_TOLERANCE" and not bad:
            continue
        cur = con.execute("select id from cases where customer=? and po_number=?", (customer, p["po_number"])).fetchone()
        if cur:
            continue
        cid = f"CASE-{con.execute('select count(*) from cases').fetchone()[0] + 101}"
        con.execute("insert into cases(id, customer, po_number, supplier, status, case_class, gap, hold, created) "
                    "values(?,?,?,?,?,?,?,?,?)", (cid, customer, p["po_number"], p.get("supplier", ""),
                                                  "OPEN", res.case_class, res.gap_qty, res.hold_value, _now()))
        for e in [po, *grns, *invs]:
            _store_ev(con, cid, e)
        log(con, cid, "opened", {"class": res.case_class, "reasons": res.reasons})
        if bad:
            log(con, cid, "manual_task", "invoice extraction failed validation; human review with source document")
        opened.append(cid)
    con.commit()
    return opened


def _store_ev(con, cid, e):
    con.execute("insert into evidence(case_id, kind, data, provenance, method, confidence, is_claim, fetched_at) "
                "values(?,?,?,?,?,?,?,?)", (cid, e["kind"], json.dumps(e["data"]), e["provenance"], e["method"],
                                            e["confidence"], int(e["is_claim"]), e["fetched_at"]))


def ingest_email(message_id, subject, body, extraction: Optional[dict] = None):
    """WF1: dedupe, link, store LLM extraction as a CLAIM. Returns case id or None (triage)."""
    con = db()
    if con.execute("select 1 from emails where message_id=?", (message_id,)).fetchone():
        return {"duplicate": True}
    open_cases = {r["id"]: r["po_number"] for r in con.execute("select id, po_number from cases where status!='CLOSED'")}
    cid, method = link_email(subject, body, open_cases)
    if not cid and extraction and extraction.get("po_number"):
        cid, method = link_email("", "PO " + str(extraction["po_number"]), open_cases)
        method = "llm" if cid else method
    con.execute("insert into emails values(?,?,?,?,?)", (message_id, cid, subject, body, method))
    if cid:
        ext = dict(extraction or {})
        ext["injection"] = bool(ext.get("injection")) or looks_injected(body)
        _store_ev(con, cid, evidence("email_claim", ext, source=f"email:{message_id}", method="llm_extract",
                                     confidence=float(ext.get("confidence", 0.8)), is_claim=True))
        log(con, cid, "email_linked", {"method": method, "intent": ext.get("intent"), "injection": ext["injection"]})
    con.commit()
    return {"case_id": cid, "method": method}


def _claims(con, cid):
    rows = con.execute("select data from evidence where case_id=? and kind='email_claim' order by id", (cid,)).fetchall()
    return [json.loads(r["data"]) for r in rows]


def investigate(cid, proposed_action=None):
    """WF3: rebuild facts from latest evidence -> classify (code) -> propose -> policy gate -> draft."""
    con = db()
    case = con.execute("select * from cases where id=?", (cid,)).fetchone()
    pos, grns, invs = _snapshot(con, case["customer"], case["po_number"])
    p = pos[0]["data"]
    claims = _claims(con, cid)
    transit = [c for c in claims if c.get("transit_qty")]
    qtys = {int(c["transit_qty"]) for c in transit}
    last = transit[-1] if transit else {}
    facts = CaseFacts(p["po_number"], int(p["qty"]), float(p["price"]), sum(int(g["data"]["qty"]) for g in grns),
                      [i["data"] for i in invs if i["method"] != "failed" and not validate_invoice(i["data"])],
                      transit_qty=int(last.get("transit_qty") or 0),
                      transit_eta=date.fromisoformat(last["eta"]) if last.get("eta") else None,
                      supplier_confirmed_short=any(c.get("intent") == "CONFIRMS_SHORTAGE" for c in claims),
                      conflicting_claims=len(qtys) > 1, today=today())
    res = classify(facts)                                   # code decides the class, always
    allowed = sorted(ALLOWED_ACTIONS[res.case_class])
    # FR8: LLM diagnosis only for email-dependent or UNKNOWN cases; it can only pick from the allowed set
    if proposed_action is None and llm.available() and (claims or res.case_class == "UNKNOWN_NEEDS_EVIDENCE"):
        try:
            dx = llm.diagnose({"po": p["po_number"], "ordered": facts.ordered_qty, "received": facts.received_qty,
                               "code_class": res.case_class, "today": str(today())},
                              [{k: c.get(k) for k in ("intent", "transit_qty", "eta", "summary")} for c in claims], allowed)
            proposed_action = dx["proposed_action"]
            log(con, cid, "llm_diagnosis", dx)
            if dx.get("class_hint") and dx["class_hint"] != res.case_class:
                log(con, cid, "contradiction_denied", f"LLM suggested {dx['class_hint']}; code facts say {res.case_class}")
        except Exception as e:
            log(con, cid, "llm_diagnosis_failed", str(e))
    action = proposed_action if proposed_action in ALLOWED_ACTIONS[res.case_class] else DEFAULT_ACTION[res.case_class]
    inv = facts.invoices[0] if facts.invoices else {"price": facts.po_price}
    token = f"[{cid}]"
    ctx = {"supplier": case["supplier"] or "Supplier", "po": p["po_number"], "received": facts.received_qty,
           "ordered": facts.ordered_qty, "gap": res.gap_qty, "eta": facts.transit_eta or "the agreed date",
           "hold": res.hold_value, "inv_price": inv["price"], "po_price": facts.po_price, "token": token}
    draft = draft_for(action, ctx)
    allowed_nums = {facts.received_qty, facts.ordered_qty, res.gap_qty, res.hold_value, inv["price"], facts.po_price,
                    cid.split("-")[1], *re.findall(r"\d+", p["po_number"])}
    if facts.transit_eta:
        allowed_nums |= set(re.findall(r"\d+", str(facts.transit_eta)))
    # LLM wording, but lint is the gatekeeper: a failing LLM draft falls back to the template
    if draft and llm.available():
        try:
            from engine.policy import lint_draft
            ld = llm.draft_email(action.replace("_", " ").lower(), {k: ctx[k] for k in ctx if k != "hold" or action == "REQUEST_CREDIT_NOTE"})
            problems = lint_draft(ld, allowed_nums)
            if not problems and token in ld:
                draft = ld
                log(con, cid, "llm_draft", "LLM draft passed lint")
            else:
                log(con, cid, "llm_draft_rejected", {"problems": problems or ["missing case token"], "fallback": "template"})
        except Exception as e:
            log(con, cid, "llm_draft_failed", str(e))
    qty_prov = "NATIVE" if grns and grns[0]["method"] == "native" else "FALLBACK_SYSTEM"
    decision, reasons = gate(res.case_class, action, mode=MODE, hold_value=res.hold_value,
                             msme_flag=bool(p.get("msme") in ("1", 1, True, "true")),
                             injection_flag=any(c.get("injection") for c in claims), qty_provenance=qty_prov,
                             draft=draft, allowed_numbers=allowed_nums,
                             action_key=f"{cid}:{action}:{case['attempts']}",
                             done_keys={r["key"] for r in con.execute("select key from action_keys")},
                             attempt=case["attempts"], max_attempts=MAX_LOOPS)
    status = {"LOG": "CLOSED", "WAIT": "WAITING", "BLOCK_DUPLICATE": "AWAITING_APPROVAL",
              "ESCALATE": "ESCALATED"}.get(action, "AWAITING_APPROVAL")
    if decision == "BLOCKED":
        status = "ESCALATED"
    if status == "CLOSED":   # rule: nothing reaches CLOSED without passing verification
        ok, why = verify(res.case_class, ordered=facts.ordered_qty, received=facts.received_qty)
        log(con, cid, "verified", {"ok": ok, "why": why})
        status = "CLOSED" if ok else "ESCALATED"
    con.execute("update cases set case_class=?, gap=?, hold=?, action=?, decision=?, reasons=?, draft=?, status=? where id=?",
                (res.case_class, res.gap_qty, res.hold_value, action, decision, json.dumps(res.reasons + reasons),
                 draft, status, cid))
    log(con, cid, "investigated", {"class": res.case_class, "action": action, "decision": decision,
                                   "reasons": res.reasons + reasons, "hold_recommended": res.hold_value})
    con.commit()
    return get_case(cid)


def approve(cid, edited_draft=None, approver="buyer"):
    """WF4: human approval. Re-lints an edited draft. Returns what n8n should send."""
    con = db()
    case = con.execute("select * from cases where id=?", (cid,)).fetchone()
    if case["status"] != "AWAITING_APPROVAL":
        return {"ok": False, "error": f"status is {case['status']}"}
    key = f"{cid}:{case['action']}:{case['attempts']}"
    if con.execute("select 1 from action_keys where key=?", (key,)).fetchone():
        return {"ok": False, "error": "already sent"}
    draft = edited_draft or case["draft"]
    if edited_draft:
        from engine.policy import lint_draft
        nums = set(re.findall(r"\d[\d,]*(?:\.\d+)?", case["draft"] or ""))
        problems = lint_draft(edited_draft, nums)
        if problems:
            return {"ok": False, "error": "edited draft failed lint", "problems": problems}
    con.execute("insert into action_keys values(?)", (key,))
    con.execute("update cases set status='WAITING', attempts=attempts+1, draft=? where id=?", (draft, cid))
    log(con, cid, "approved", {"by": approver, "action": case["action"]})
    con.commit()
    sup = json.loads(con.execute("select data from evidence where case_id=? and kind='po'", (cid,)).fetchone()["data"])
    return {"ok": True, "send": case["action"] in SUPPLIER_FACING, "to": sup.get("supplier_email", ""),
            "subject": f"{case['po_number']} {'[' + cid + ']'}", "body": draft}


def reject(cid, reason=""):
    con = db()
    con.execute("update cases set status='OPEN', draft=NULL where id=?", (cid,))   # PRD B2: rejected -> OPEN
    log(con, cid, "rejected", reason or "buyer rejected draft; case back to OPEN for a new decision")
    con.commit()
    return get_case(cid)


def verify_case(cid):
    """WF5: re-read the system of record through the router; close only if it proves the fix."""
    con = db()
    case = con.execute("select * from cases where id=?", (cid,)).fetchone()
    pos, grns, invs = _snapshot(con, case["customer"], case["po_number"])
    p = pos[0]["data"]
    received = sum(int(g["data"]["qty"]) for g in grns)
    ok, why = verify(case["case_class"], ordered=int(p["qty"]), received=received,
                     grn_ids=[g["data"].get("grn_id") for g in grns], hold_value=case["hold"],
                     credit_note_value=next((float(i["data"]["credit_note"]) for i in invs if i["data"].get("credit_note")), None),
                     corrected_price=next((float(i["data"]["price"]) for i in invs[::-1]), None), po_price=float(p["price"]),
                     payable_invoices=len({i["data"]["number"] for i in invs if i["data"].get("status") != "blocked"}))
    for g in grns:
        _store_ev(con, cid, g)
    if ok:
        status = "CLOSED"
    elif case["attempts"] >= MAX_LOOPS:
        status = "ESCALATED"
    else:
        status = "WAITING"
    con.execute("update cases set status=? where id=?", (status, cid))
    log(con, cid, "verified", {"ok": ok, "why": why, "status": status, "source": grns[0]["method"] if grns else "none"})
    con.commit()
    return {"ok": ok, "why": why, "status": status}


def get_case(cid):
    con = db()
    c = dict(con.execute("select * from cases where id=?", (cid,)).fetchone())
    c["evidence"] = [dict(r) for r in con.execute("select * from evidence where case_id=? order by id", (cid,))]
    c["timeline"] = [dict(r) for r in con.execute("select * from events where case_id=? order by id", (cid,))]
    return c


def list_cases():
    return [dict(r) for r in db().execute("select * from cases order by created desc")]


def path_log(limit=50):
    return [dict(r) for r in db().execute("select * from path_log order by id desc limit ?", (limit,))]


# ---------------------------------------------------------------- demo helpers
def simulate_grn(customer, po_number, qty):
    """Warehouse records the missing goods (in X's ERP, or Y's Excel/CSV)."""
    gid = f"GRN-{po_number}-{datetime.now().strftime('%H%M%S')}"
    if customer == "X":
        f = DATA / "customer_x" / "erp.json"
        erp = json.loads(f.read_text())
        erp["grns"].append({"grn_id": gid, "po_number": po_number, "qty": qty})
        f.write_text(json.dumps(erp, indent=2))
    else:
        with open(DATA / "customer_y" / "grn.csv", "a", newline="") as fh:
            csv.writer(fh).writerow([gid, po_number, qty])
    return gid


def reset():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    from data.make_demo_data import build
    build()
