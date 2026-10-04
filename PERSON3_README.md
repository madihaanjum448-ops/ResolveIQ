# Person 3: dashboard, data and evaluation

| File | What it is |
|---|---|
| eval/golden_cases.json | 26 hand-labelled golden cases (English, Hinglish, Hindi script; partial, overdue, vague/ambiguous ETA, shortage, denial, conflict, injection x3, price, duplicate, over-delivery, tolerance boundaries). Never edit labels to improve a score. |
| eval/golden_eval.py | Runner: `--validate`, offline arms R0/R1/R1P/ORACLE, live arms S/L with a disk cache and `--sleep` for rate limits. Writes eval/golden_results.json and eval/golden_report.md. Refuses to run S/L without a key (writes nothing). |
| tests/test_golden.py | Label consistency, engine-vs-labels, runner mirror, LLM stub, broken LLM, cache. |
| tests/test_llm_robustness.py | Retry/backoff/error-reporting tests against a local fake OpenAI-compatible server (429, 5xx, null content, truncation, malformed JSON, 401, timeout), evaluator honesty checks, Windows DB-reset fallback. No network or key needed. |
| dashboard/eval_view.py + dashboard/app.py | Evaluation tab: arm legend, golden results, per-case matrix, failures, LLM-error warnings, disclaimers. |

Arms: R0 plain match; R1 rules; R1P improved rules (tuned on these cases, optimistic); S structured LLM extraction + deterministic engine + policy gate; L raw LLM classification (deliberately weaker: no tolerance rule, no policy gate); ORACLE true extraction (plumbing check, not a result).

Commands
    python -W ignore -m unittest tests.test_all
    python -m unittest tests.test_golden tests.test_llm_robustness
    python -m eval.golden_eval --validate
    python -m eval.golden_eval --arms R0,R1,R1P,ORACLE                 # offline, no key
    python -m eval.golden_eval --arms R0,R1,R1P,S,L --sleep 7          # live; needs LLM_API_KEY (+ provider vars, see .env.example)

LLM failures: transient errors are retried with exponential backoff inside engine/llm.py. A call that still fails is
counted in `llm_errors` and its cause is listed under "LLM errors" in the report. Failures are never cached.
`schema_errors` (L output outside the allowed vocabulary) and `incoherent_pairs` (an action the policy table would
never allow for the predicted class) are model-quality diagnostics, kept separate from infrastructure errors.

The golden set is synthetic and test-only (tune on the generated dev split). Hindi-script cases are marked
needs_native_check. Not production performance; no rupee savings are claimed.
