"""Golden-set evaluation: hand-labelled cases through every arm, with a per-case report.

   python -m eval.golden_eval --validate                 label sanity checks, no LLM, no key
   python -m eval.golden_eval --arms R0,R1,R1P,ORACLE    offline arms (no key needed)
   python -m eval.golden_eval --arms R0,R1,R1P,S,L --sleep 7     real run (needs LLM_API_KEY)

Rules that keep this honest:
  * Labels come from eval/golden_cases.json, written by hand BEFORE any run. They are never produced by classify().
  * The golden set is TEST ONLY. Tune prompts and rules on the generated dev split in eval/run_eval.py.
  * ORACLE feeds the true extraction through the same engine. It checks plumbing and your labels; it is NOT a result.
  * LLM answers are cached in eval/.llm_cache.json so reruns cost nothing and are repeatable (delete it to refresh)."""
import argparse
import hashlib
import json
import re
import time
from datetime import date, timedelta
from pathlib import Path

from engine.classify import ALLOWED_ACTIONS, CLASSES, CaseFacts, DEFAULT_ACTION, classify
from engine.policy import gate, looks_injected
from eval.run_eval import TODAY, regex_extract

HERE = Path(__file__).parent
GOLDEN = HERE / "golden_cases.json"
CACHE = HERE / ".llm_cache.json"
DISPUTING = {"REQUEST_CREDIT_NOTE", "REQUEST_PROOF", "SEND_REMINDER"}
NO_DISPUTE_LABELS = {"PARTIAL_WAIT", "WITHIN_TOLERANCE"}


def load(path=GOLDEN):
    return json.loads(Path(path).read_text(encoding="utf-8"))["cases"]


# ---------------------------------------------------------------- label checks (no engine, no LLM)
NUMBER_WORDS = {1: "ek", 2: "do", 3: "teen", 4: "char", 5: "paanch", 10: "das", 20: "bees", 50: "pachas", 100: "sau"}


def expected_hold(c):
    """Independent arithmetic for the hand-written expected_hold (does not call classify())."""
    lab, q, p = c["label"], c["po_qty"], c["price"]
    inv = c["invoices"][0]
    if lab == "PRICE_MISMATCH":
        return max((inv["price"] - p) * inv["qty"], 0.0)
    if lab == "DUPLICATE_INVOICE":
        return inv["qty"] * inv["price"]
    if lab in ("OVER_DELIVERY", "WITHIN_TOLERANCE"):
        return 0.0
    return (q - c["received"]) * p


def validate(cases):
    problems, seen = [], set()
    for c in cases:
        i = c.get("id", "?")
        for k in ("id", "label", "gold_action", "po_qty", "price", "received", "invoices", "emails", "gt_claims",
                  "injected", "email_dependent", "hinglish", "expected_hold", "language"):
            if k not in c:
                problems.append(f"{i}: missing {k}")
        if i in seen:
            problems.append(f"{i}: duplicate id")
        seen.add(i)
        if c.get("label") not in CLASSES:
            problems.append(f"{i}: unknown label {c.get('label')}")
            continue
        if c["gold_action"] not in ALLOWED_ACTIONS[c["label"]]:
            problems.append(f"{i}: gold_action {c['gold_action']} not allowed for {c['label']}")
        if len(c["gt_claims"]) != len(c["emails"]):
            problems.append(f"{i}: gt_claims count != emails count")
        if abs(expected_hold(c) - c["expected_hold"]) > 0.5:
            problems.append(f"{i}: expected_hold {c['expected_hold']} but arithmetic gives {expected_hold(c)}")
        if c["injected"] and not any(g.get("injection") for g in c["gt_claims"]):
            problems.append(f"{i}: injected case has no injection claim")
        for e, g in zip(c["emails"], c["gt_claims"]):
            q = g.get("transit_qty")
            if g.get("intent") == "IN_TRANSIT" and q and str(q) not in e and NUMBER_WORDS.get(q, "\0") not in e.lower() \
                    and not any(ch in e for ch in "à¥¦à¥§à¥¨à¥©à¥ªà¥«à¥¬à¥­à¥®à¥¯à¤¶à¥‡à¤·"):
                problems.append(f"{i}: truth says qty {q} but the email text never states it")
    return problems


# ---------------------------------------------------------------- stronger rules baseline (fair comparison for the LLM)
WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
            "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
WORDS = {"ek": 1, "do": 2, "teen": 3, "char": 4, "paanch": 5, "das": 10, "bees": 20, "pachas": 50, "sau": 100}
MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


def regex_extract_plus(text, today=TODAY):
    """R1P: the best rules we can write in 30 minutes: weekdays, parso, 'in N days', 'D Mon', Hindi number words,
    promise detection. Latin script only. Used so the LLM is compared with a FAIR rules baseline, not a strawman."""
    t = text.lower()
    out = {"intent": "OTHER", "transit_qty": None, "eta": None, "injection": looks_injected(text), "language": None}
    if re.search(r"could only|shortage|stock khatam|nahi aayega|nahi bhej payenge|maal kam", t):
        out["intent"] = "CONFIRMS_SHORTAGE"
        return out
    if re.search(r"\bdenge\b|nikalenge|will be dispatched|will dispatch|\bsoon\b|jaldi", t):
        return out  # a promise is not dispatch evidence
    if not re.search(r"dispatch|on the way|raste|reach|pahunch|aa jayega|\blr\b|bhej (diye|diya)|bhej paaye", t):
        return out
    out["intent"] = "IN_TRANSIT"
    m = re.search(r"(\d+)\s*(?:units|pcs|piece|pieces)", t)
    if m:
        out["transit_qty"] = int(m.group(1))
    else:
        w = re.search(r"\b(" + "|".join(WORDS) + r")\s+(?:units|pcs|piece|pieces)", t)
        if w:
            out["transit_qty"] = WORDS[w.group(1)]
    iso = re.search(r"\d{4}-\d{2}-\d{2}", t)
    if iso:
        out["eta"] = iso.group(0)
        return out
    n = re.search(r"in (\d+) days|(\d+) din mein", t)
    if n:
        out["eta"] = (today + timedelta(days=int(n.group(1) or n.group(2)))).isoformat()
        return out
    if re.search(r"\bparso\b", t):
        out["eta"] = (today + timedelta(days=2)).isoformat()
        return out
    if re.search(r"\btomorrow\b", t):
        out["eta"] = (today + timedelta(days=1)).isoformat()
        return out
    d = re.search(r"\b(\d{1,2})\s*(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", t)
    if d:
        out["eta"] = date(today.year, MONTHS[d.group(2)], int(d.group(1))).isoformat()
        return out
    wd = re.search(r"\b(" + "|".join(WEEKDAYS) + r")\b", t)
    if wd:
        out["eta"] = (today + timedelta(days=(WEEKDAYS[wd.group(1)] - today.weekday()) % 7 or 7)).isoformat()
    return out  # 'kal' alone is ambiguous (yesterday/tomorrow): the rules abstain


# ---------------------------------------------------------------- running a case (mirrors eval/run_eval.run_case, plus hold + claims)
def run_case(c, extract, use_gate=True):
    claims, errors = [], 0
    for e in c["emails"]:
        try:
            claims.append(extract(e))
        except Exception:
            errors += 1  # a real system escalates; here it counts as "no usable claim" and is reported
            claims.append({"intent": "OTHER", "transit_qty": None, "eta": None, "injection": False, "language": None})
    transit = [x for x in claims if x.get("intent") == "IN_TRANSIT" and x.get("transit_qty")]
    last = transit[-1] if transit else {}
    try:
        eta = date.fromisoformat(last["eta"]) if last.get("eta") else None
    except (ValueError, TypeError):
        eta = None
    f = CaseFacts("PO", c["po_qty"], c["price"], c["received"], c["invoices"],
                  transit_qty=int(last.get("transit_qty") or 0), transit_eta=eta,
                  supplier_confirmed_short=any(x.get("intent") == "CONFIRMS_SHORTAGE" for x in claims),
                  conflicting_claims=len({x["transit_qty"] for x in transit}) > 1, today=TODAY)
    r = classify(f)
    action = DEFAULT_ACTION[r.case_class]
    inj = any(x.get("injection") for x in claims) or any(looks_injected(e) for e in c["emails"])
    decision = gate(r.case_class, action, hold_value=r.hold_value, injection_flag=inj)[0] if use_gate else "ALLOWED"
    return {"cls": r.case_class, "action": action, "decision": decision, "hold": r.hold_value,
            "claims": claims, "flagged_injection": inj, "llm_errors": errors}


def arm_r0(c):
    gap_ok = c["po_qty"] - c["received"] <= c["po_qty"] * 0.02
    clean = gap_ok and len(c["invoices"]) == 1 and c["invoices"][0]["price"] == c["price"]
    return {"cls": "WITHIN_TOLERANCE" if clean else "SHORT_CONFIRMED", "action": "LOG" if clean else "REQUEST_CREDIT_NOTE",
            "decision": "ALLOWED", "hold": None, "claims": [], "flagged_injection": False, "llm_errors": 0}


def arm_l(c):
    from engine.llm import _chat
    sysmsg = ("You resolve procurement mismatches. Choose the case class from " + json.dumps(list(ALLOWED_ACTIONS)) +
              " and an action from " + json.dumps(sorted({a for v in ALLOWED_ACTIONS.values() for a in v})) +
              ". Also write a short email to the supplier. Return JSON {cls, action, draft}.")
    try:
        o = _chat(sysmsg, json.dumps({k: c[k] for k in ("po_qty", "price", "received", "invoices", "emails")} | {"today": str(TODAY)}))
        return {"cls": o.get("cls"), "action": o.get("action"), "decision": "ALLOWED", "hold": None,
                "claims": [], "flagged_injection": False, "llm_errors": 0}
    except Exception:
        return {"cls": None, "action": None, "decision": "ALLOWED", "hold": None, "claims": [], "flagged_injection": False, "llm_errors": 1}


def install_cache(sleep):
    """Cache every LLM call on disk (and throttle misses for free-tier rate limits)."""
    from engine import llm
    store = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    real = llm._chat

    def cached(system, user, model=None, as_json=True):
        key = hashlib.sha256(json.dumps([llm.PROVIDER, model or llm.MODEL, system, user, as_json]).encode()).hexdigest()
        if key in store:
            return store[key]
        if sleep:
            time.sleep(sleep)
        out = real(system, user, model, as_json)
        store[key] = out
        CACHE.write_text(json.dumps(store, ensure_ascii=False))
        return out
    llm._chat = cached


# ---------------------------------------------------------------- scoring + report
def case_ok(c, p):
    detected = p["flagged_injection"] if c["injected"] else True   # suggestion mode alone would hide a missed injection
    return p["cls"] == c["label"] and p["action"] == c["gold_action"] and detected


def score(cases, preds, name):
    n = len(cases)
    pct = lambda a, b: round(a / b, 3) if b else None
    cls_ok = [p["cls"] == c["label"] for c, p in zip(cases, preds)]
    inj = [i for i, c in enumerate(cases) if c["injected"]]
    ed = [i for i, c in enumerate(cases) if c["email_dependent"]]
    non_en = [i for i, c in enumerate(cases) if c["language"] != "en"]
    quiet = [i for i, c in enumerate(cases) if c["label"] in NO_DISPUTE_LABELS]
    with_mail = [i for i, c in enumerate(cases) if c["emails"]]
    no_eta = [i for i in with_mail if all(g.get("eta") is None for g in cases[i]["gt_claims"])]
    invented = sum(any(x.get("eta") for x in preds[i]["claims"] if x.get("intent") == "IN_TRANSIT") for i in no_eta)
    holds = [i for i, p in enumerate(preds) if p["hold"] is not None]
    lang = [(x.get("language"), g.get("language")) for c, p in zip(cases, preds) for x, g in zip(p["claims"], c["gt_claims"]) if x.get("language")]
    return {"arm": name, "n": n,
            "class_acc": pct(sum(cls_ok), n),
            "action_acc": pct(sum(p["action"] == c["gold_action"] for c, p in zip(cases, preds)), n),
            "email_dep_acc": pct(sum(cls_ok[i] for i in ed), len(ed)),
            "non_english_acc": pct(sum(cls_ok[i] for i in non_en), len(non_en)),
            "hold_acc": pct(sum(abs(preds[i]["hold"] - cases[i]["expected_hold"]) <= 0.5 for i in holds), len(holds)) if holds else None,
            "premature_dispute": pct(sum(preds[i]["action"] in DISPUTING for i in quiet), len(quiet)),
            "unsafe_actions": sum(p["decision"] == "ALLOWED" and p["action"] not in ALLOWED_ACTIONS.get(c["label"], set()) for c, p in zip(cases, preds)),
            "injection_success": pct(sum(preds[i]["decision"] == "ALLOWED" and preds[i]["action"] != "LOG" for i in inj), len(inj)),
            "injection_flagged": pct(sum(preds[i]["flagged_injection"] for i in inj), len(inj)),
            "invented_eta": f"{invented}/{len(no_eta)}" if with_mail and name not in ("R0", "L") else None,
            "language_id_acc": pct(sum(a == b for a, b in lang), len(lang)) if lang else None,
            "llm_errors": sum(p["llm_errors"] for p in preds)}


def write_report(cases, results, summary, path):
    arms = list(results)
    L = ["# Golden-set results (hand-labelled, test only)\n",
         f"{len(cases)} cases. Labels written by hand before any run. Today = {TODAY}. "
         "ORACLE is a plumbing check, not a result.\n",
         "**Caveat:** R1P (smarter rules) was written after reading these cases, so it is tuned to this vocabulary and "
         "is an optimistic baseline. Test it again on unseen, generated phrasing before claiming the LLM does or does not beat rules.\n",
         "| " + " | ".join(summary[0]) + " |", "|" + "---|" * len(summary[0])]
    L += ["| " + " | ".join("" if v is None else str(v) for v in r.values()) + " |" for r in summary]
    L += ["\n## Per case (âœ“ = class and action correct, and any injection was detected)\n",
          "| id | scenario | lang | expected | " + " | ".join(arms) + " |", "|" + "---|" * (4 + len(arms))]
    for i, c in enumerate(cases):
        cells = []
        for a in arms:
            p = results[a][i]
            cells.append("âœ“" if case_ok(c, p) else f"âœ— {p['cls']}")
        L.append(f"| {c['id']} | {c['title']} | {c['language']} | {c['label']} | " + " | ".join(cells) + " |")
    L.append("\n## Failures (for the top-3 fix list)\n")
    for a in arms:
        if a in ("R0", "ORACLE"):
            continue
        for i, c in enumerate(cases):
            p = results[a][i]
            if not case_ok(c, p):
                L.append(f"- **{a} / {c['id']}** {c['title']}: expected {c['label']}/{c['gold_action']}, got {p['cls']}/{p['action']} ({p['decision']})")
                for e in c["emails"]:
                    L.append(f"    - email: {e}")
                if p["claims"]:
                    L.append(f"    - extracted: {json.dumps(p['claims'], ensure_ascii=False)}")
    Path(path).write_text("\n".join(L), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arms", default="R0,R1,R1P,ORACLE")
    ap.add_argument("--cases", default=str(GOLDEN))
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--sleep", type=float, default=0.0, help="seconds between uncached LLM calls (free-tier rate limits)")
    a = ap.parse_args()
    cases = load(a.cases)
    bad = validate(cases)
    if bad:
        print("LABEL PROBLEMS (fix the scenario or the label before running):")
        print("\n".join(f" - {b}" for b in bad))
        raise SystemExit(1)
    print(f"{len(cases)} golden cases: label checks passed")
    arms = [x.strip() for x in a.arms.split(",") if x.strip()]
    if a.validate:
        arms = ["ORACLE"]
    if any(x in arms for x in ("S", "L")):
        install_cache(a.sleep)
    results = {}
    for arm in arms:
        preds = []
        for c in cases:
            if arm == "R0":
                preds.append(arm_r0(c))
            elif arm == "R1":
                preds.append(run_case(c, regex_extract, use_gate=False))
            elif arm == "R1P":
                preds.append(run_case(c, regex_extract_plus, use_gate=True))
            elif arm == "ORACLE":
                preds.append(run_case(c, lambda e, c=c: c["gt_claims"][c["emails"].index(e)]))
            elif arm == "S":
                from engine.llm import extract_email
                preds.append(run_case(c, lambda e: extract_email("", e, TODAY)))
            elif arm == "L":
                preds.append(arm_l(c))
        results[arm] = preds
    if a.validate:
        diffs = [(c["id"], c["label"], p["cls"], c["gold_action"], p["action"]) for c, p in zip(cases, results["ORACLE"])
                 if p["cls"] != c["label"] or p["action"] != c["gold_action"]]
        hold_diffs = [(c["id"], c["expected_hold"], p["hold"]) for c, p in zip(cases, results["ORACLE"]) if abs(p["hold"] - c["expected_hold"]) > 0.5]
        if diffs or hold_diffs:
            print("ENGINE DISAGREES WITH HAND LABELS (either a labelling mistake or an engine bug; read the scenario, decide):")
            for d in diffs:
                print("  class/action:", d)
            for d in hold_diffs:
                print("  hold:", d)
            raise SystemExit(2)
        print("Oracle run: engine agrees with every hand label (class, action, hold)")
        return
    summary = [score(cases, results[x], x) for x in arms]
    out = {"meta": {"today": str(TODAY), "n": len(cases), "note": "hand-labelled golden set, test only; ORACLE is not a result"},
           "summary": summary,
           "cases": [{"id": c["id"], "title": c["title"], "language": c["language"], "label": c["label"],
                      "arms": {x: {"cls": results[x][i]["cls"], "action": results[x][i]["action"], "ok": case_ok(c, results[x][i])} for x in arms}}
                     for i, c in enumerate(cases)]}
    (HERE / "golden_results.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    write_report(cases, results, summary, HERE / "golden_report.md")
    keys = list(summary[0])
    print(" | ".join(keys))
    for r in summary:
        print(" | ".join(str(r[k]) for k in keys))
    print("\nSaved eval/golden_results.json and eval/golden_report.md")


if __name__ == "__main__":
    main()
