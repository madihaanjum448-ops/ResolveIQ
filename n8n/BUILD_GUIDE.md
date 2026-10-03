# n8n workflows — build guide (P2 owns this)

n8n never touches the database. Every workflow calls the API with **HTTP Request** nodes.
Running with `npx n8n` (no Docker): the API is **`http://localhost:8000`** — use that everywhere below where it says `http://api:8000`.

Build in this order. Export each as JSON into this folder (`n8n/WF1_email_intake.json` …) when it works.

---

## WF2 — ERP Sync (build first, 15 min)
1. **Schedule Trigger** — every 5 min (for the demo also add a **Manual Trigger**).
2. **HTTP Request** — `POST http://api:8000/scan/{{ $env.CUSTOMER || 'X' }}` (or hardcode `X`).
3. **Split Out** — field `opened`.
4. **HTTP Request** — `POST http://api:8000/cases/{{ $json.opened }}/investigate`.
5. **Switch** on `{{ $json.status }}`:
   - `AWAITING_APPROVAL` → Execute Workflow **WF4**
   - `WAITING` → Execute Workflow **WF5**
   - `ESCALATED` → **Gmail: Send** to the buyer ("Case {{id}} needs you: {{reasons}}")
   - `CLOSED` → No-op

## WF1 — Email Intake (the "messy input" — 45 min)
1. **Gmail Trigger** — poll every minute, label `INBOX`, "Simplify" OFF so you get `id`, `subject`, `text`.
   *Fallback if OAuth fights you:* **Webhook** node `POST /webhook/email-in` and post emails with curl.
2. **Basic LLM Chain** (or **OpenAI** / **Groq** chat node) with **Structured Output Parser**:
   - Model: **Anthropic Chat Model** sub-node (Claude) with your key.
   - System prompt: copy `EMAIL_PROMPT` from `engine/llm.py` exactly.
   - User message: `TODAY: {{ $now.format('yyyy-MM-dd') }}\nSUBJECT: {{ $json.subject }}\n<email>\n{{ $json.text }}\n</email>`
   - Temperature 0. Output parser JSON schema = the keys in `EMAIL_PROMPT`.
   - On error: **retry once** (node setting "Retry On Fail", max 2 tries), then continue with `{"intent":"OTHER","confidence":0}`.
3. **HTTP Request** — `POST http://api:8000/emails` body:
   `{"message_id": "{{ $('Gmail Trigger').item.json.id }}", "subject": "...", "body": "...", "extraction": {{ JSON.stringify($json.output) }}}`
   (If the LLM node is painful, send `extraction: null` and the API calls the LLM itself — same prompt.)
4. **IF** `case_id` is empty → **Gmail: Send** to buyer "Unlinked supplier email — triage" (human triage queue).
5. Else → **HTTP Request** `POST /cases/{{case_id}}/investigate` → same **Switch** as WF2.

## WF4 — Approval (30 min)
Two ways; build A first, B only if time allows.
- **A. Dashboard button** (already built): dashboard calls `/approve`, then POSTs the result to the n8n **Webhook** `send-supplier-email`:
  1. **Webhook** `POST /webhook/send-supplier-email`
  2. **IF** `{{ $json.send }}` true → **Gmail: Send** to `{{ $json.to }}`, subject `{{ $json.subject }}` (contains `[CASE-xxx]` so replies link back), body `{{ $json.body }}`.
  3. Execute Workflow **WF5** with `case_id`.
- **B. Email approval**: **Gmail → Send and Wait for Response** (approval type) to the buyer with the draft → on approve `POST /cases/{id}/approve` → Gmail Send; on reject `POST /cases/{id}/reject`.

## WF5 — Wait & Verify (30 min)
Input: `case_id`.
1. **Wait** — for the demo: 1 minute (production: until ETA, or "On webhook call" when a reply arrives).
2. **HTTP Request** `POST /cases/{{case_id}}/verify`.
3. **Switch** on `status`: `CLOSED` → Gmail buyer "Closed, verified: {{why}}"; `WAITING` → loop back to **Wait** (API caps at 3 loops); `ESCALATED` → Gmail manager.

## WF6 — Error Handler (10 min)
1. **Error Trigger**.
2. **Gmail: Send** to owner: "Workflow {{ $json.workflow.name }} failed: {{ $json.execution.error.message }}".
3. Set this workflow as the **Error Workflow** in the settings of WF1, WF2, WF4, WF5.

## WF0 — Capability Router
The router runs inside the API (`engine/router.py`, called on every read). In n8n, show it as a small sub-workflow
`WF0` that calls `GET /admin/paths` and posts any NATIVE→FALLBACK switch to the owner. That satisfies "n8n calls the router through one sub-workflow" without moving logic out of tested Python.

---

### Testing without Gmail
```bash
curl -X POST localhost:8000/emails -H 'content-type: application/json' -d '{"message_id":"t1","subject":"Re: PO-1001","body":"baaki 10 transport se bhej diya, LR 4455, Tuesday tak pahunch jayega"}'
```
