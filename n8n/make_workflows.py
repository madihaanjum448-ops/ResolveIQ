"""Generates importable n8n workflow JSON files (n8n 2.x node types).
   python n8n/make_workflows.py   -> writes n8n/WF*.json
All HTTP calls go to the ResolveIQ API at http://localhost:8000 (n8n started with `npx n8n`)."""
import json
import uuid
from pathlib import Path

API = "http://localhost:8000"
N8N = "http://localhost:5678"
OUT = Path(__file__).parent


def node(name, type_, params, pos, version, **extra):
    n = {"parameters": params, "id": str(uuid.uuid4()), "name": name, "type": f"n8n-nodes-base.{type_}",
         "typeVersion": version, "position": pos}
    n.update(extra)
    return n


def http(name, url, pos, body=None, method="POST"):
    p = {"method": method, "url": url, "options": {}}
    if body:
        p.update({"sendBody": True, "specifyBody": "json", "jsonBody": body})
    return node(name, "httpRequest", p, pos, 4.2)


def webhook(name, path, pos):
    return node(name, "webhook", {"httpMethod": "POST", "path": path, "responseMode": "onReceived", "options": {}},
                pos, 2, webhookId=str(uuid.uuid4()))


def noop(name, pos):
    return node(name, "noOp", {}, pos, 1)


def cond(left, op, right="", single=False, typ="string"):
    c = {"id": str(uuid.uuid4()), "leftValue": left, "rightValue": right, "operator": {"type": typ, "operation": op}}
    if single:
        c["operator"]["singleValue"] = True
    return c


def if_node(name, c, pos):
    return node(name, "if", {"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose",
                                                        "version": 2},
                                            "conditions": [c], "combinator": "and"},
                             "looseTypeValidation": True, "options": {}}, pos, 2.2)


def switch_status(name, statuses, pos):
    rules = [{"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
                             "conditions": [cond("={{ $json.status }}", "equals", s)], "combinator": "and"},
              "renameOutput": True, "outputKey": s} for s in statuses]
    return node(name, "switch", {"rules": {"values": rules}, "options": {}}, pos, 3.2)


def link(*pairs):
    """pairs: (from_name, to_name, from_output_index=0)"""
    conns = {}
    for p in pairs:
        src, dst, idx = (p + (0,))[:3]
        outs = conns.setdefault(src, {"main": []})["main"]
        while len(outs) <= idx:
            outs.append([])
        outs[idx].append({"node": dst, "type": "main", "index": 0})
    return conns


def save(fname, name, nodes, conns, note):
    wf = {"name": name, "nodes": nodes, "connections": conns, "active": False,
          "settings": {"executionOrder": "v1"}, "pinData": {}, "meta": {"note": note}}
    (OUT / fname).write_text(json.dumps(wf, indent=2))
    print("wrote", fname)


STATUSES = ["AWAITING_APPROVAL", "WAITING", "ESCALATED", "CLOSED"]

# ---------------- WF2 ERP sync: manual / every 5 min / webhook -> scan -> investigate each new case
save("WF2_scan.json", "WF2 ERP Sync (scan)", [
    node("Run manually", "manualTrigger", {}, [0, 0], 1),
    node("Every 5 minutes", "scheduleTrigger", {"rule": {"interval": [{"field": "minutes", "minutesInterval": 5}]}},
         [0, 180], 1.2),
    webhook("Scan webhook", "scan", [0, 360]),
    http("Scan ERP / files", f"={API}/scan/{{{{ $json.body?.customer || 'X' }}}}", [260, 180]),
    node("One item per new case", "splitOut", {"fieldToSplitOut": "opened", "options": {}}, [500, 180], 1),
    http("Investigate case", f"={API}/cases/{{{{ $json.opened }}}}/investigate", [740, 180]),
    switch_status("Route by status", STATUSES, [980, 180]),
    noop("Needs approval (dashboard)", [1240, 0]),
    noop("Waiting (WF5 re-checks)", [1240, 140]),
    noop("Escalated: notify buyer (add Gmail later)", [1240, 280]),
    noop("Closed", [1240, 420]),
], link(("Run manually", "Scan ERP / files"), ("Every 5 minutes", "Scan ERP / files"),
        ("Scan webhook", "Scan ERP / files"), ("Scan ERP / files", "One item per new case"),
        ("One item per new case", "Investigate case"), ("Investigate case", "Route by status"),
        ("Route by status", "Needs approval (dashboard)", 0), ("Route by status", "Waiting (WF5 re-checks)", 1),
        ("Route by status", "Escalated: notify buyer (add Gmail later)", 2), ("Route by status", "Closed", 3)),
    "If 'One item per new case' gets an empty list, cases already exist: click Reset demo in the dashboard.")

# ---------------- WF1 email intake: webhook (or Gmail, disabled) -> API links + LLM-extracts -> investigate
save("WF1_email_intake.json", "WF1 Email Intake", [
    webhook("Email in (webhook)", "email-in", [0, 0]),
    node("Gmail Trigger (enable later)", "gmailTrigger",
         {"pollTimes": {"item": [{"mode": "everyMinute"}]}, "simple": False, "filters": {}, "options": {}},
         [0, 220], 1.2, disabled=True),
    node("Map Gmail to email shape", "set", {"assignments": {"assignments": [
        {"id": str(uuid.uuid4()), "name": "body.message_id", "value": "={{ $json.id }}", "type": "string"},
        {"id": str(uuid.uuid4()), "name": "body.subject", "value": "={{ $json.subject }}", "type": "string"},
        {"id": str(uuid.uuid4()), "name": "body.body", "value": "={{ $json.text }}", "type": "string"}]},
        "options": {}}, [240, 220], 3.4),
    http("Store email (API links + Claude extracts)", f"{API}/emails", [480, 0],
         body="={{ JSON.stringify({ message_id: $json.body.message_id || ('wh-' + $execution.id), "
              "subject: $json.body.subject || '', body: $json.body.body || '', extraction: null }) }}"),
    if_node("Linked to a case?", cond("={{ $json.case_id }}", "notEmpty", single=True), [720, 0]),
    http("Investigate case", f"={API}/cases/{{{{ $json.case_id }}}}/investigate", [960, -100]),
    noop("Not linked: triage queue (notify buyer later)", [960, 120]),
    switch_status("Route by status", STATUSES, [1200, -100]),
    noop("Needs approval (dashboard)", [1460, -260]),
    noop("Waiting (WF5 re-checks)", [1460, -120]),
    noop("Escalated: notify buyer (add Gmail later)", [1460, 20]),
    noop("Closed", [1460, 160]),
], link(("Email in (webhook)", "Store email (API links + Claude extracts)"),
        ("Gmail Trigger (enable later)", "Map Gmail to email shape"),
        ("Map Gmail to email shape", "Store email (API links + Claude extracts)"),
        ("Store email (API links + Claude extracts)", "Linked to a case?"),
        ("Linked to a case?", "Investigate case", 0), ("Linked to a case?", "Not linked: triage queue (notify buyer later)", 1),
        ("Investigate case", "Route by status"),
        ("Route by status", "Needs approval (dashboard)", 0), ("Route by status", "Waiting (WF5 re-checks)", 1),
        ("Route by status", "Escalated: notify buyer (add Gmail later)", 2), ("Route by status", "Closed", 3)),
    "Duplicate message_id returns {duplicate:true} and stops at 'Not linked'. To use Gmail: enable the Gmail "
    "Trigger, add your Gmail credential, and disable the webhook.")

# ---------------- WF4 send: dashboard 'Approve & send' -> Gmail -> start WF5
save("WF4_send.json", "WF4 Send approved email", [
    webhook("Approved (from dashboard)", "send-supplier-email", [0, 0]),
    if_node("Supplier-facing?", cond("={{ $json.body.send }}", "true", single=True, typ="boolean"), [240, 0]),
    node("Gmail: send to supplier", "gmail", {"sendTo": "={{ $json.body.to }}", "subject": "={{ $json.body.subject }}",
                                              "emailType": "text", "message": "={{ $json.body.body }}",
                                              "options": {"appendAttribution": False}}, [480, -100], 2.1),
    http("Start wait-verify (WF5)", f"{N8N}/webhook/verify-case", [720, -100],
         body="={{ JSON.stringify({ case_id: $('Approved (from dashboard)').item.json.body.case_id }) }}"),
    noop("Internal action only", [480, 120]),
], link(("Approved (from dashboard)", "Supplier-facing?"), ("Supplier-facing?", "Gmail: send to supplier", 0),
        ("Supplier-facing?", "Internal action only", 1), ("Gmail: send to supplier", "Start wait-verify (WF5)")),
    "Add your Gmail credential to the Gmail node. Activate this workflow so the production URL works.")

# ---------------- WF5 wait & verify: wait -> verify -> loop while WAITING (API escalates after 3)
save("WF5_wait_verify.json", "WF5 Wait and Verify", [
    webhook("Verify case (webhook)", "verify-case", [0, 0]),
    node("Wait (demo: 1 min)", "wait", {"resume": "timeInterval", "amount": 1, "unit": "minutes"}, [240, 0], 1.1,
         webhookId=str(uuid.uuid4())),
    http("Re-read system and verify", f"={API}/cases/{{{{ $('Verify case (webhook)').item.json.body.case_id }}}}/verify",
         [480, 0]),
    switch_status("Route by status", ["CLOSED", "WAITING", "ESCALATED"], [720, 0]),
    noop("Closed: verified fixed", [980, -140]),
    noop("Escalated: notify manager (add Gmail later)", [980, 140]),
], link(("Verify case (webhook)", "Wait (demo: 1 min)"), ("Wait (demo: 1 min)", "Re-read system and verify"),
        ("Re-read system and verify", "Route by status"), ("Route by status", "Closed: verified fixed", 0),
        ("Route by status", "Wait (demo: 1 min)", 1), ("Route by status", "Escalated: notify manager (add Gmail later)", 2)),
    "WAITING loops back to Wait. The API escalates after 3 attempts, so the loop is bounded.")

# ---------------- WF6 error handler
save("WF6_error.json", "WF6 Error Handler", [
    node("On any workflow error", "errorTrigger", {}, [0, 0], 1),
    node("Gmail: alert owner", "gmail", {
        "sendTo": "your-email@gmail.com",
        "subject": "=ResolveIQ: {{ $json.workflow.name }} failed",
        "emailType": "text",
        "message": "=Workflow: {{ $json.workflow.name }}\nNode: {{ $json.execution.lastNodeExecuted }}\n"
                   "Error: {{ $json.execution.error.message }}\nExecution: {{ $json.execution.url }}",
        "options": {"appendAttribution": False}}, [260, 0], 2.1),
], link(("On any workflow error", "Gmail: alert owner")),
    "Set this as the Error Workflow in the settings of WF1, WF2, WF4 and WF5. Put your email in the Gmail node.")

# ---------------- WF0 capability router monitor: watch the router's path log, alert on NATIVE -> FALLBACK switches
save("WF0_capability_router.json", "WF0 Capability Router (monitor)", [
    node("Every minute", "scheduleTrigger", {"rule": {"interval": [{"field": "minutes", "minutesInterval": 1}]}},
         [0, 0], 1.2),
    webhook("Check now (webhook)", "router-check", [0, 180]),
    http("Read router path log", f"{API}/admin/paths", [260, 80], method="GET"),
    if_node("Native source failed or switched?",
            cond("={{ String($json.note || '') + ' ' + String($json.path || '') }}", "regex",
                 "(native error|health set to False|MANUAL)"), [520, 80]),
    node("Keep one alert per capability", "removeDuplicates",
         {"compare": "selectedFields", "fieldsToCompare": "customer,capability", "options": {}}, [780, 0], 1.1),
    noop("Alert owner: router switched to FALLBACK (add Gmail later)", [1040, 0]),
    noop("All capabilities healthy", [780, 200]),
], link(("Every minute", "Read router path log"), ("Check now (webhook)", "Read router path log"),
        ("Read router path log", "Native source failed or switched?"),
        ("Native source failed or switched?", "Keep one alert per capability", 0),
        ("Native source failed or switched?", "All capabilities healthy", 1),
        ("Keep one alert per capability", "Alert owner: router switched to FALLBACK (add Gmail later)")),
    "The routing decision itself runs in tested Python (engine/router.py) on every read. WF0 watches the router "
    "and alerts when a customer's native source fails and the router falls back. Demo: Break native in the dashboard, "
    "then curl the router-check webhook.")
