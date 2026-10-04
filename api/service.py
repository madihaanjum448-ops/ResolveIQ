"""Case service: Unified store (Postgres / SQLite) + adapters (X = structured API, Y = CSV + PDF invoices).
Implements the Capability-Aware Integration Layer, deterministic matching, classifier, policy gate,
and verification tracking.
"""
from typing import Any, Dict, List, Optional, Tuple, Union
import csv
import io
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from engine.classify import CaseFacts, classify, Result, DEFAULT_ACTION, ALLOWED_ACTIONS, SUPPLIER_FACING
from engine import llm
from engine.facts import build_facts
from engine.policy import gate, draft_for, link_email, looks_injected, lint_draft
from engine.router import PROFILES, pick, evidence, validate_invoice
from engine.verify import verify
from engine.normalise import load_table
from engine.db import get_connection, db_session, reset_database, close_all_connections

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.getenv("DATA_DIR", ROOT / "data" / "demo"))
MODE = os.getenv("MODE", "suggestion")
MAX_LOOPS = 3
_clock = {"today": None}  # demo time travel
_LAST_REPORTS: Dict[str, Any] = {}


def today() -> date:
    return _clock["today"] or date.today()


def set_today(d: Optional[str]):
    _clock["today"] = date.fromisoformat(d) if d else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db():
    """Returns an active DB connection adapter."""
    return get_connection()


def log(con, case_id: str, type_: str, detail: Any):
    con.execute(
        "INSERT INTO events(case_id, at, type, detail) VALUES(?,?,?,?)",
        (case_id, _now(), type_, json.dumps(detail) if not isinstance(detail, str) else detail)
    )


# ---------------------------------------------------------------- deterministic 3-way matcher
def match_po_grn_invoice(
    po: Dict[str, Any],
    grns: List[Dict[str, Any]],
    invoices: List[Dict[str, Any]],
    tolerance_pct: float = 2.0,
    price_tolerance_pct: float = 1.0,
) -> Dict[str, Any]:
    """
    Core deterministic 3-way match calculation.
    Compares Purchase Order, Goods Receipt Notes (GRN), and Supplier Invoices.
    """
    po_num = po.get("po_number", "")
    ordered_qty = int(po.get("qty", 0))
    po_price = float(po.get("price", 0.0))
    received_qty = sum(int(g.get("qty", 0)) for g in grns)

    discrepancies = []
    qty_tol = ordered_qty * (tolerance_pct / 100.0)
    qty_gap = ordered_qty - received_qty

    if qty_gap > qty_tol:
        discrepancies.append(f"Short delivery: ordered {ordered_qty}, received {received_qty} (gap: {qty_gap})")
    elif received_qty - ordered_qty > qty_tol:
        discrepancies.append(f"Over delivery: received {received_qty} > ordered {ordered_qty}")

    # Check invoices for price variance and duplicates
    inv_keys = [(str(i.get("supplier", "")).lower(), str(i.get("number", "")).strip().upper()) for i in invoices]
    duplicate_invs = [k[1] for k in set(inv_keys) if inv_keys.count(k) > 1 and k[1]]
    if duplicate_invs:
        discrepancies.append(f"Duplicate invoice detected: {duplicate_invs}")

    first_inv = invoices[0] if invoices else {"price": po_price, "qty": ordered_qty}
    inv_price = float(first_inv.get("price", po_price))
    invoiced_qty = sum(int(i.get("qty", 0)) for i in invoices)
    price_variance = round(inv_price - po_price, 2)
    price_tol = po_price * (price_tolerance_pct / 100.0)

    if abs(price_variance) > price_tol:
        discrepancies.append(f"Price mismatch: PO price ₹{po_price:.2f} vs Invoice price ₹{inv_price:.2f}")

    # Recommended hold value calculation
    recommended_hold = 0.0
    if abs(price_variance) > price_tol:
        recommended_hold = max(0.0, round(price_variance * float(first_inv.get("qty", ordered_qty)), 2))
    elif qty_gap > qty_tol:
        recommended_hold = round(qty_gap * po_price, 2)

    is_matched = len(discrepancies) == 0

    return {
        "matched": is_matched,
        "po_number": po_num,
        "ordered_qty": ordered_qty,
        "received_qty": received_qty,
        "invoiced_qty": invoiced_qty,
        "qty_gap": qty_gap,
        "po_price": po_price,
        "inv_price": inv_price,
        "price_variance": price_variance,
        "recommended_hold": recommended_hold,
        "discrepancies": discrepancies,
        "duplicate_invoices": duplicate_invs,
    }


# ---------------------------------------------------------------- capability layer
def profile_for(con, customer: str) -> Dict[str, Dict[str, Any]]:
    """Loads capability profile from DB or defaults to built-in demo profiles."""
    customer = customer.upper()
    cur = con.execute(
        "SELECT capability, provider, fallback, healthy, max_age_min FROM capability_profile WHERE customer_id=?",
        (customer,)
    )
    rows = cur.fetchall()
    
    if rows:
        prof = {}
        for r in rows:
            row_dict = dict(r) if hasattr(r, "keys") else {
                "capability": r[0], "provider": r[1], "fallback": r[2], "healthy": r[3], "max_age_min": r[4]
            }
            prof[row_dict["capability"]] = {
                "provider": row_dict["provider"],
                "fallback": row_dict["fallback"],
                "healthy": bool(row_dict["healthy"]),
                "max_age_min": int(row_dict["max_age_min"] or 15),
            }
    else:
        # Fallback to default in-memory PROFILES
        base_prof = PROFILES.get(customer, PROFILES["Y"])
        prof = {k: dict(v) for k, v in base_prof.items()}

    # Apply health overrides from health table
    for row in con.execute("SELECT capability, healthy FROM health WHERE customer=?", (customer,)).fetchall():
        row_dict = dict(row) if hasattr(row, "keys") else {"capability": row[0], "healthy": row[1]}
        cap = row_dict["capability"]
        if cap in prof:
            prof[cap]["healthy"] = bool(row_dict["healthy"])

    return prof


def set_health(customer: str, capability: str, healthy: bool):
    customer = customer.upper()
    con = db()
    try:
        # Update or insert into health table
        con.execute("DELETE FROM health WHERE customer=? AND capability=?", (customer, capability))
        con.execute("INSERT INTO health(customer, capability, healthy, updated_at) VALUES(?,?,?,?)",
                    (customer, capability, int(healthy), _now()))
        con.execute("INSERT INTO path_log(at, customer, capability, path, note) VALUES(?,?,?,?,?)",
                    (_now(), customer, capability, "-", f"health set to {healthy}"))
        con.commit()
    finally:
        con.close()


def route_capability(customer: str, cap: str) -> Dict[str, Any]:
    """Determines active provider path (NATIVE, FALLBACK, or MANUAL) and logs it."""
    customer = customer.upper()
    con = db()
    try:
        prof = profile_for(con, customer)
        path = pick(cap, prof)
        con.execute("INSERT INTO path_log(at, customer, capability, path, note) VALUES(?,?,?,?,?)",
                    (_now(), customer, cap, path, ""))
        con.commit()
        p = prof.get(cap, {})
        return {
            "customer": customer,
            "capability": cap,
            "path": path,
            "provider": p.get("provider", "FALLBACK"),
            "fallback": p.get("fallback"),
            "healthy": p.get("healthy", True),
        }
    finally:
        con.close()


def _route(con, customer: str, cap: str) -> str:
    path = pick(cap, profile_for(con, customer))
    con.execute("INSERT INTO path_log(at, customer, capability, path, note) VALUES(?,?,?,?,?)",
                (_now(), customer, cap, path, ""))
    return path


def _native_erp() -> Dict[str, Any]:
    erp_file = DATA / "customer_x" / "erp.json"
    return json.loads(erp_file.read_text(encoding="utf-8"))


def _csv(name: str) -> List[Dict[str, Any]]:
    """Loads messy data via Smart CSV/Excel Cleaner."""
    base = DATA / "customer_y" / name
    path = base.with_suffix(".xlsx") if base.with_suffix(".xlsx").exists() else base
    kind = {"po.csv": "po", "grn.csv": "grn", "invoices.csv": "invoice"}[name]
    rows, report = load_table(path, kind)
    _LAST_REPORTS[name] = report
    return rows


def load_reports() -> Dict[str, Any]:
    return _LAST_REPORTS


def read_pdf_invoice(path: Path) -> Dict[str, Any]:
    """Fallback invoice extraction: regex on PDF text first; LLM only if regex fails validation."""
    from pypdf import PdfReader
    try:
        text = "\n".join(p.extract_text() or "" for p in PdfReader(str(path)).pages)
    except Exception as e:
        return {"_error": f"PDF parse error: {e}", "_text": ""}

    def grab(pat, cast=str):
        m = re.search(pat, text, re.I)
        return cast(m.group(1).replace(",", "")) if m else None

    inv = {
        "number": grab(r"Invoice\s*No[.:]*\s*([\w/-]+)"),
        "po_number": grab(r"PO\s*No[.:]*\s*([\w-]+)"),
        "qty": grab(r"Qty[.:]*\s*([\d,]+)", int),
        "price": grab(r"Rate[.:]*\s*(?:Rs\.?|INR)?\s*([\d,.]+)", float),
        "total": grab(r"Total[.:]*\s*(?:Rs\.?|INR)?\s*([\d,.]+)", float),
    }
    method, conf = "pdf_regex", 0.9
    if validate_invoice(inv):
        try:
            from engine.llm import extract_invoice
            inv, method, conf = extract_invoice(text), "pdf_llm", 0.7
        except Exception as e:
            return {"_error": f"extraction failed: {e}", "_text": text[:500]}
    inv["_method"], inv["_confidence"] = method, conf
    return inv


def load(con, customer: str, kind: str) -> List[Dict[str, Any]]:
    """Returns list of canonical evidence records for kind in {po, grn, invoice}."""
    path = _route(con, customer, kind)
    out = []
    if path == "NATIVE":
        try:
            erp = _native_erp()
        except Exception as e:
            con.execute("INSERT INTO path_log(at, customer, capability, path, note) VALUES(?,?,?,?,?)",
                        (_now(), customer, kind, "FALLBACK", f"native error: {e}"))
            path = "FALLBACK"
        else:
            for row in erp.get(kind + "s", []):
                out.append(evidence(kind, row, source="erp_api", method="native"))
            return out

    if path == "FALLBACK":
        if kind == "invoice" and customer == "Y":
            inv_dir = DATA / "customer_y" / "invoices"
            if inv_dir.exists():
                for pdf in sorted(inv_dir.glob("*.pdf")):
                    inv = read_pdf_invoice(pdf)
                    if "_error" in inv:
                        out.append(evidence(kind, inv, source=pdf.name, method="failed", confidence=0))
                        continue
                    m, c = inv.pop("_method"), inv.pop("_confidence")
                    out.append(evidence(kind, inv, source=pdf.name, method=m, confidence=c))
        else:
            src = {"po": "po.csv", "grn": "grn.csv", "invoice": "invoices.csv"}[kind]
            for row in _csv(src):
                out.append(evidence(kind, row, source=src, method="csv"))
        return out

    return []  # MANUAL


# ---------------------------------------------------------------- cases
def _snapshot(con, customer: str, po_number: str):
    pos = [e for e in load(con, customer, "po") if e["data"].get("po_number") == po_number]
    grns = [e for e in load(con, customer, "grn") if e["data"].get("po_number") == po_number]
    invs = [e for e in load(con, customer, "invoice") if e["data"].get("po_number") == po_number]
    return pos, grns, invs


def scan(customer: str) -> List[str]:
    """WF2: read PO/GRN/invoice via router, open a case for anything outside tolerance."""
    customer = customer.upper()
    con = db()
    opened = []
    try:
        for po in load(con, customer, "po"):
            p = po["data"]
            po_num = p.get("po_number", "")
            _, grns, invs = _snapshot(con, customer, po_num)
            bad = [i for i in invs if i["method"] == "failed" or validate_invoice(i["data"])]
            facts = CaseFacts(
                po_num,
                int(p["qty"]),
                float(p["price"]),
                sum(int(g["data"]["qty"]) for g in grns),
                [i["data"] for i in invs if i not in bad],
                today=today(),
            )
            res = classify(facts)
            if res.case_class == "WITHIN_TOLERANCE" and not bad:
                continue

            cur = con.execute("SELECT id FROM cases WHERE customer=? AND po_number=?", (customer, po_num)).fetchone()
            if cur:
                continue

            count_res = con.execute("SELECT COUNT(*) FROM cases").fetchone()
            count_cases = count_res[0] if count_res else 0
            cid = f"CASE-{count_cases + 101}"
            
            con.execute(
                "INSERT INTO cases(id, customer, po_number, supplier, status, case_class, gap, hold, created, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (cid, customer, po_num, p.get("supplier", ""), "OPEN", res.case_class, res.gap_qty, res.hold_value, _now(), _now())
            )
            for e in [po, *grns, *invs]:
                _store_ev(con, cid, e)
            log(con, cid, "opened", {"class": res.case_class, "reasons": res.reasons})
            if bad:
                log(con, cid, "manual_task", "invoice extraction failed validation; human review with source document")
            opened.append(cid)
        con.commit()
    finally:
        con.close()
    return opened


def _store_ev(con, cid: str, e: Dict[str, Any]):
    con.execute(
        "INSERT INTO evidence(case_id, kind, data, provenance, method, confidence, is_claim, fetched_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (cid, e["kind"], json.dumps(e["data"]), e["provenance"], e["method"], e["confidence"], int(e["is_claim"]), e["fetched_at"])
    )


def ingest_email(message_id: str, subject: str, body: str, extraction: Optional[dict] = None) -> Dict[str, Any]:
    """WF1: dedupe, link, store LLM extraction as a CLAIM. Returns case id or None."""
    con = db()
    try:
        if con.execute("SELECT 1 FROM emails WHERE message_id=?", (message_id,)).fetchone():
            return {"duplicate": True}

        open_cases = {}
        for r in con.execute("SELECT id, po_number FROM cases WHERE status!='CLOSED'").fetchall():
            r_dict = dict(r) if hasattr(r, "keys") else {"id": r[0], "po_number": r[1]}
            open_cases[r_dict["id"]] = r_dict["po_number"]

        cid, method = link_email(subject, body, open_cases)
        if not cid and extraction and extraction.get("po_number"):
            cid, method = link_email("", "PO " + str(extraction["po_number"]), open_cases)
            method = "llm" if cid else method

        con.execute(
            "INSERT INTO emails(message_id, case_id, subject, body, method, received_at) VALUES(?,?,?,?,?,?)",
            (message_id, cid, subject, body, method, _now())
        )

        if cid:
            ext = dict(extraction or {})
            ext["injection"] = bool(ext.get("injection")) or looks_injected(body)
            _store_ev(
                con, cid,
                evidence("email_claim", ext, source=f"email:{message_id}", method="llm_extract",
                         confidence=float(ext.get("confidence", 0.8)), is_claim=True)
            )
            log(con, cid, "email_linked", {"method": method, "intent": ext.get("intent"), "injection": ext["injection"]})
        con.commit()
        return {"case_id": cid, "method": method}
    finally:
        con.close()


def _claims(con, cid: str) -> List[Dict[str, Any]]:
    rows = con.execute("SELECT data FROM evidence WHERE case_id=? AND kind='email_claim' ORDER BY id", (cid,)).fetchall()
    claims = []
    for r in rows:
        raw_data = r["data"] if hasattr(r, "keys") else r[0]
        claims.append(json.loads(raw_data))
    return claims


def investigate(cid: str, proposed_action: Optional[str] = None) -> Dict[str, Any]:
    """WF3: rebuild facts from latest evidence -> classify (code) -> propose -> policy gate -> draft."""
    con = db()
    try:
        case_row = con.execute("SELECT * FROM cases WHERE id=?", (cid,)).fetchone()
        if not case_row:
            return {"error": "case not found"}
        case = dict(case_row) if hasattr(case_row, "keys") else {
            "id": case_row[0], "customer": case_row[1], "po_number": case_row[2],
            "supplier": case_row[3], "status": case_row[4], "case_class": case_row[5],
            "gap": case_row[6], "hold": case_row[7], "action": case_row[8],
            "decision": case_row[9], "reasons": case_row[10], "draft": case_row[11],
            "attempts": case_row[12], "created": case_row[13]
        }
        
        pos, grns, invs = _snapshot(con, case["customer"], case["po_number"])
        p = pos[0]["data"]
        claims = _claims(con, cid)
        facts = build_facts(
            p,
            [g["data"] for g in grns],
            [i["data"] for i in invs if i["method"] != "failed" and not validate_invoice(i["data"])],
            claims,
            today(),
        )
        res = classify(facts)
        allowed = sorted(ALLOWED_ACTIONS[res.case_class])

        if proposed_action is None and llm.available() and (claims or res.case_class == "UNKNOWN_NEEDS_EVIDENCE"):
            try:
                dx = llm.diagnose(
                    {"po": p["po_number"], "ordered": facts.ordered_qty, "received": facts.received_qty,
                     "code_class": res.case_class, "today": str(today())},
                    [{k: c.get(k) for k in ("intent", "transit_qty", "eta", "summary")} for c in claims],
                    allowed
                )
                proposed_action = dx["proposed_action"]
                log(con, cid, "llm_diagnosis", dx)
                if dx.get("class_hint") and dx["class_hint"] != res.case_class:
                    log(con, cid, "contradiction_denied", f"LLM suggested {dx['class_hint']}; code facts say {res.case_class}")
            except Exception as e:
                log(con, cid, "llm_diagnosis_failed", str(e))

        action = proposed_action if proposed_action in ALLOWED_ACTIONS[res.case_class] else DEFAULT_ACTION[res.case_class]
        inv = facts.invoices[0] if facts.invoices else {"price": facts.po_price}
        token = f"[{cid}]"
        ctx = {
            "supplier": case.get("supplier") or "Supplier", "po": p["po_number"], "received": facts.received_qty,
            "ordered": facts.ordered_qty, "gap": res.gap_qty, "eta": facts.transit_eta or "the agreed date",
            "hold": res.hold_value, "inv_price": inv["price"], "po_price": facts.po_price, "token": token
        }
        draft = draft_for(action, ctx)
        allowed_nums = {facts.received_qty, facts.ordered_qty, res.gap_qty, res.hold_value, inv["price"], facts.po_price,
                        cid.split("-")[1], *re.findall(r"\d+", p["po_number"])}
        if facts.transit_eta:
            allowed_nums |= set(re.findall(r"\d+", str(facts.transit_eta)))

        if draft and llm.available():
            try:
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
        done_keys = {
            (r["key"] if hasattr(r, "keys") else r[0])
            for r in con.execute("SELECT key FROM action_keys").fetchall()
        }
        decision, reasons = gate(
            res.case_class, action, mode=MODE, hold_value=res.hold_value,
            msme_flag=bool(p.get("msme") in ("1", 1, True, "true")),
            injection_flag=any(c.get("injection") for c in claims), qty_provenance=qty_prov,
            draft=draft, allowed_numbers=allowed_nums,
            action_key=f"{cid}:{action}:{case.get('attempts', 0)}",
            done_keys=done_keys,
            attempt=case.get("attempts", 0), max_attempts=MAX_LOOPS, human_flags=res.flags
        )
        status = {"LOG": "CLOSED", "WAIT": "WAITING", "BLOCK_DUPLICATE": "AWAITING_APPROVAL",
                  "ESCALATE": "ESCALATED"}.get(action, "AWAITING_APPROVAL")
        if decision == "BLOCKED":
            status = "ESCALATED"
        if status == "CLOSED":
            ok, why = verify(res.case_class, ordered=facts.ordered_qty, received=facts.received_qty)
            log(con, cid, "verified", {"ok": ok, "why": why})
            status = "CLOSED" if ok else "ESCALATED"

        con.execute(
            "UPDATE cases SET case_class=?, gap=?, hold=?, action=?, decision=?, reasons=?, draft=?, status=?, updated_at=? WHERE id=?",
            (res.case_class, res.gap_qty, res.hold_value, action, decision, json.dumps(res.reasons + reasons),
             draft, status, _now(), cid)
        )
        log(con, cid, "investigated", {"class": res.case_class, "action": action, "decision": decision,
                                       "reasons": res.reasons + reasons, "hold_recommended": res.hold_value})
        con.commit()
        return get_case(cid)
    finally:
        con.close()


def approve(cid: str, edited_draft: Optional[str] = None, approver: str = "buyer") -> Dict[str, Any]:
    """WF4: human approval. Re-lints an edited draft. Returns what n8n should send."""
    con = db()
    try:
        case_row = con.execute("SELECT * FROM cases WHERE id=?", (cid,)).fetchone()
        if not case_row:
            return {"ok": False, "error": "case not found"}
        case = dict(case_row) if hasattr(case_row, "keys") else {
            "id": case_row[0], "customer": case_row[1], "po_number": case_row[2],
            "supplier": case_row[3], "status": case_row[4], "case_class": case_row[5],
            "gap": case_row[6], "hold": case_row[7], "action": case_row[8],
            "decision": case_row[9], "reasons": case_row[10], "draft": case_row[11],
            "attempts": case_row[12]
        }
        if case["status"] != "AWAITING_APPROVAL":
            return {"ok": False, "error": f"status is {case['status']}"}
        key = f"{cid}:{case['action']}:{case['attempts']}"
        if con.execute("SELECT 1 FROM action_keys WHERE key=?", (key,)).fetchone():
            return {"ok": False, "error": "already sent"}

        draft = edited_draft or case["draft"]
        if edited_draft:
            nums = set(re.findall(r"\d[\d,]*(?:\.\d+)?", case["draft"] or ""))
            problems = lint_draft(edited_draft, nums)
            if problems:
                return {"ok": False, "error": "edited draft failed lint", "problems": problems}

        con.execute("INSERT INTO action_keys(key, created_at) VALUES(?,?)", (key, _now()))
        con.execute("UPDATE cases SET status='WAITING', attempts=attempts+1, draft=?, updated_at=? WHERE id=?", (draft, _now(), cid))
        log(con, cid, "approved", {"by": approver, "action": case["action"]})
        con.commit()

        po_ev = con.execute("SELECT data FROM evidence WHERE case_id=? AND kind='po'", (cid,)).fetchone()
        sup = json.loads(po_ev["data"] if hasattr(po_ev, "keys") else po_ev[0]) if po_ev else {}
        return {
            "ok": True,
            "send": case["action"] in SUPPLIER_FACING,
            "to": sup.get("supplier_email", ""),
            "subject": f"{case['po_number']} {'[' + cid + ']'}",
            "body": draft,
        }
    finally:
        con.close()


def reject(cid: str, reason: str = "") -> Dict[str, Any]:
    con = db()
    try:
        con.execute("UPDATE cases SET status='OPEN', draft=NULL, updated_at=? WHERE id=?", (_now(), cid))
        log(con, cid, "rejected", reason or "buyer rejected draft; case back to OPEN for a new decision")
        con.commit()
        return get_case(cid)
    finally:
        con.close()


def verify_case(cid: str) -> Dict[str, Any]:
    """WF5: re-read the system of record through the router; close only if it proves the fix."""
    con = db()
    try:
        case_row = con.execute("SELECT * FROM cases WHERE id=?", (cid,)).fetchone()
        if not case_row:
            return {"ok": False, "status": "NOT_FOUND"}
        case = dict(case_row) if hasattr(case_row, "keys") else {
            "id": case_row[0], "customer": case_row[1], "po_number": case_row[2],
            "supplier": case_row[3], "status": case_row[4], "case_class": case_row[5],
            "gap": case_row[6], "hold": case_row[7], "action": case_row[8],
            "attempts": case_row[12]
        }
        pos, grns, invs = _snapshot(con, case["customer"], case["po_number"])
        p = pos[0]["data"]
        received = sum(int(g["data"]["qty"]) for g in grns)
        ok, why = verify(
            case["case_class"],
            ordered=int(p["qty"]),
            received=received,
            grn_ids=[g["data"].get("grn_id") for g in grns],
            hold_value=case["hold"],
            credit_note_value=next((float(i["data"]["credit_note"]) for i in invs if i["data"].get("credit_note")), None),
            corrected_price=next((float(i["data"]["price"]) for i in invs[::-1]), None),
            po_price=float(p["price"]),
            payable_invoices=len({i["data"]["number"] for i in invs if i["data"].get("status") != "blocked"}),
        )
        for g in grns:
            _store_ev(con, cid, g)

        if ok:
            status = "CLOSED"
        elif case.get("attempts", 0) >= MAX_LOOPS:
            status = "ESCALATED"
        else:
            status = "WAITING"

        con.execute("UPDATE cases SET status=?, updated_at=? WHERE id=?", (status, _now(), cid))
        
        # Write to dedicated verifications table
        con.execute(
            "INSERT INTO verifications(case_id, at, case_class, ok, why, status, source, details) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (cid, _now(), case["case_class"], int(ok), why, status, grns[0]["method"] if grns else "none", json.dumps({"received": received, "ordered": int(p["qty"])}))
        )
        log(con, cid, "verified", {"ok": ok, "why": why, "status": status, "source": grns[0]["method"] if grns else "none"})
        con.commit()
        return {"ok": ok, "why": why, "status": status}
    finally:
        con.close()


def get_case(cid: str) -> Dict[str, Any]:
    con = db()
    try:
        r = con.execute("SELECT * FROM cases WHERE id=?", (cid,)).fetchone()
        if not r:
            return {}
        c = dict(r) if hasattr(r, "keys") else {
            "id": r[0], "customer": r[1], "po_number": r[2], "supplier": r[3],
            "status": r[4], "case_class": r[5], "gap": r[6], "hold": r[7],
            "action": r[8], "decision": r[9], "reasons": r[10], "draft": r[11],
            "attempts": r[12], "created": r[13], "updated_at": r[14] if len(r) > 14 else r[13]
        }
        ev_rows = con.execute("SELECT * FROM evidence WHERE case_id=? ORDER BY id", (cid,)).fetchall()
        c["evidence"] = [dict(row) if hasattr(row, "keys") else {
            "id": row[0], "case_id": row[1], "kind": row[2], "data": row[3],
            "provenance": row[4], "method": row[5], "confidence": row[6],
            "is_claim": row[7], "fetched_at": row[8]
        } for row in ev_rows]

        event_rows = con.execute("SELECT * FROM events WHERE case_id=? ORDER BY id", (cid,)).fetchall()
        c["timeline"] = [dict(row) if hasattr(row, "keys") else {
            "id": row[0], "case_id": row[1], "at": row[2], "type": row[3], "detail": row[4]
        } for row in event_rows]
        return c
    finally:
        con.close()


def list_cases() -> List[Dict[str, Any]]:
    con = db()
    try:
        rows = con.execute("SELECT * FROM cases ORDER BY created DESC").fetchall()
        return [dict(r) if hasattr(r, "keys") else {
            "id": r[0], "customer": r[1], "po_number": r[2], "supplier": r[3],
            "status": r[4], "case_class": r[5], "gap": r[6], "hold": r[7],
            "action": r[8], "decision": r[9], "reasons": r[10], "draft": r[11],
            "attempts": r[12], "created": r[13], "updated_at": r[14] if len(r) > 14 else r[13]
        } for r in rows]
    finally:
        con.close()


def path_log(limit: int = 50) -> List[Dict[str, Any]]:
    con = db()
    try:
        rows = con.execute("SELECT * FROM path_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) if hasattr(r, "keys") else {
            "id": r[0], "at": r[1], "customer": r[2], "capability": r[3], "path": r[4], "note": r[5]
        } for r in rows]
    finally:
        con.close()


# ---------------------------------------------------------------- demo helpers
def simulate_grn(customer: str, po_number: str, qty: int) -> str:
    """Warehouse records the missing goods (in X's ERP, or Y's Excel/CSV)."""
    gid = f"GRN-{po_number}-{datetime.now().strftime('%H%M%S')}"
    if customer == "X":
        f = DATA / "customer_x" / "erp.json"
        erp = json.loads(f.read_text(encoding="utf-8"))
        erp["grns"].append({"grn_id": gid, "po_number": po_number, "qty": qty})
        f.write_text(json.dumps(erp, indent=2), encoding="utf-8")
    else:
        with open(DATA / "customer_y" / "grn.csv", "a", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow([gid, po_number, qty])
    return gid


def reset():
    reset_database()
    from data.make_demo_data import build
    build()
