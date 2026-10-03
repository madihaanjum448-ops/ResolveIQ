# Solo plan — Sat 3 Oct 22:30 → Sun 4 Oct 10:00

Spec = your friend's PRD. The starter code already covers the PRD's engine, API, dashboard, router (X/Y),
policy gate, verification, LLM extraction + diagnosis + drafts (lint-guarded), eval and bake-off.
**What only you can do:** keys, Gmail, n8n workflows, real eval numbers, the demo.

## PRD → code map
| PRD | Where | Status |
|---|---|---|
| FR1–3 adapters, router, provenance | `api/service.py` `load()`, `engine/router.py` | ✅ |
| FR4–5 matcher + 8 classes, money in code | `engine/classify.py` | ✅ |
| FR6–7 Gmail intake, LLM extraction | `engine/llm.py` EMAIL_PROMPT + n8n WF1 | code ✅ · **n8n: you** |
| FR8 LLM diagnosis (allowed set only, contradiction denied) | `investigate()` | ✅ |
| FR9–10 7-step gate, LLM draft + lint, template fallback | `engine/policy.py`, `investigate()` | ✅ |
| FR11 approval | dashboard button → n8n webhook WF4 | dashboard ✅ · **n8n: you** |
| FR12 wait-verify, max 3 loops | `verify_case()` + n8n WF5 | code ✅ · **n8n: you** |
| FR13 error workflow | n8n WF6 | **you** |
| FR14 PDF invoices (text PDFs) | `read_pdf_invoice()` | ✅ |
| FR15 dashboard | `dashboard/app.py` | ✅ |
| FR16 eval 4 arms + X/Y equivalence | `eval/run_eval.py`, test `test_x_equals_y` | code ✅ · **run with key: you** |
| C3 20-email bake-off | `eval/bakeoff.py` | code ✅ · **run: you** |

Deliberate differences from the PRD (say so in the README): SQLite instead of Postgres (n8n talks only HTTP to the API);
audit log = `events` table; verifications are logged as `verified` events.

## Timeline
| Time | Do this | Done when |
|---|---|---|
| 22:30–23:00 | `./run_local.sh`. Get Claude API key → `.env`. **Check the PS22 BRD line on the portal.** | Dashboard shows cases; tests OK |
| 23:00–23:20 | `python -m eval.bakeoff --models claude-sonnet-5-5,claude-haiku-4-5-20251001` → pick extraction model | Table saved to README |
| 23:20–00:00 | `brew install node` → `npx n8n`. Make 2 Gmails (system + "supplier"). Add Gmail credential in n8n | n8n opens at :5678 |
| 00:00–01:00 | **WF1 Email intake** (n8n/BUILD_GUIDE.md). Use `extraction: null` so the API calls Claude — fewer nodes | Email from supplier Gmail → case timeline shows `email_linked` |
| 01:00–01:40 | **WF2 Scan** + **WF4 Send** (webhook → Gmail send) | Approve in dashboard → email arrives in supplier inbox |
| 01:40–02:20 | **WF5 Wait-verify** + **WF6 Error** | ✅ Demo steps 1–2 work end to end. **Feature freeze.** |
| 02:20–03:00 | `python -m eval.run_eval --arms R0,R1,S,L` → paste table into README | `eval/results.json` |
| 03:00–04:30 | **Sleep.** Set an alarm. | |
| 04:30–05:30 | Rehearse DEMO_SCRIPT.md: X vs Y, break native, injection email. Export all n8n workflows to `n8n/` | All 6 demo steps work |
| 05:30–06:30 | 4 slides: where it sits · real vs simulated · how we differ · limits & future | |
| 06:30–07:30 | Fix only the top 3 problems. Rerun eval once | |
| 07:30–08:30 | 🎥 Record ≤ 4 min (2 takes + backup screen recording) | |
| 08:30–09:15 | Push to GitHub, fresh clone in a new folder, `./run_local.sh` works | |
| **09:30** | 🚀 Submit (30 min buffer) | |

**Stuck >30 min?** Gmail OAuth → Webhook node + curl. n8n LLM node → `extraction: null`. Anything else → skip it and note it under Limits.
