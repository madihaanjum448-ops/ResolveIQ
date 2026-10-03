# ResolveIQ — Procurement Exception Resolver (PS22)

> Matching detects the problem; we investigate and resolve the exception.

A sidecar to the ERP. When PO, receipt (GRN) and invoice don't match, it opens a case, reads supplier emails
(including Hinglish), classifies the cause in deterministic code, picks a safe next step through a policy gate,
drafts a grounded follow-up for human approval, waits, then **re-reads the system of record** and closes only when the data proves the fix.
n8n orchestrates intake, approval, waiting and email.

```
Supplier email ─┐                       ┌─ Gmail send (after approval)
ERP / CSV / PDF ─┼─> n8n WF1/WF2 ──> API ─┤  classify → policy gate → draft → verify
                 │      (HTTP)       │    └─ Streamlit control room
                 └── Capability router: NATIVE (Company X) | FALLBACK (Company Y: CSV + PDF) | MANUAL
```

## Run
```bash
cp .env.example .env    # add LLM key
docker compose up --build
```
API `:8000/docs` · Dashboard `:8501` · n8n `:5678` (import workflows from `n8n/`).
Tests: `python -W ignore -m unittest tests.test_all` · Eval: `python -m eval.run_eval --arms R0,R1,S,L`

## Layout
| Path | What |
|---|---|
| `engine/classify.py` | 8 exception classes, tolerance, hold value (code, never LLM) |
| `engine/policy.py` | Policy gate, draft lint, injection check, email→case linking, templates |
| `engine/router.py` | Capability profiles X/Y, router, canonical evidence with provenance |
| `engine/verify.py` | Close-only-if-proven rules |
| `engine/llm.py` | Email + invoice extraction prompts, schema validation, retry once |
| `api/service.py` | Cases, adapters (ERP JSON / CSV / PDF), scan, investigate, approve, verify |
| `n8n/BUILD_GUIDE.md` | WF0–WF6 node by node |
| `eval/run_eval.py` | 150 synthetic cases, arms R0 / R1 / L / S |

## Results (synthetic, test split)
_Fill in after the final run. Report where S loses._

| Arm | Root-cause acc | Premature dispute | Unsafe actions | Injection success | Email-dependent acc | Hinglish acc |
|---|---|---|---|---|---|---|
| R0 plain match | | | | | | |
| R1 rules, no LLM | | | | | | |
| L LLM, no policy | | | | | | |
| S ours | | | | | | |

## Real vs simulated
Real: Gmail through n8n, LLM extraction, PDF invoice parsing, router, policy gate, verification.
Simulated: the ERP (ERP-shaped JSON for X, CSV exports for Y), warehouse GRN updates, carrier tracking.

## Limits
No scanned-invoice OCR, no ERP write-back, no photo adjudication, no WhatsApp, no GST reconciliation. Holds are recommendations only.
