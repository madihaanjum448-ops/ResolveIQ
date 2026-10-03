"""Score email extraction on many messy permutations with known answers.
   python -m eval.run_emails --extractor regex           # R1 baseline, free, no key
   python -m eval.run_emails --extractor llm --n 6       # 5 facts x 6 variants = 30 LLM calls (free-tier friendly)
LLM answers are cached in eval/llm_cache.json, so reruns cost nothing."""
import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path

from data.email_variants import all_variants, BASES, TODAY
from engine.classify import classify
from engine.facts import build_facts
from engine.policy import link_email, looks_injected, ascii_digits
from eval.run_eval import regex_extract

CACHE = Path(__file__).parent / "llm_cache.json"
CASES = {"CASE-101": "PO-1001", "CASE-102": "PO-1002", "CASE-103": "PO-1005"}


def llm_extract(subject, body, cache):
    from engine import llm
    k = hashlib.sha1(f"{llm.EXTRACT_MODEL}|{subject}|{body}".encode()).hexdigest()
    if k not in cache:
        try:
            cache[k] = llm.extract_email(subject, body, TODAY.isoformat())
        except Exception as e:
            cache[k] = {"intent": "OTHER", "injection": False, "_error": str(e)}
        CACHE.write_text(json.dumps(cache, indent=0, ensure_ascii=False))
    return cache[k]


def eq(a, b):
    if b is None:
        return a in (None, "", 0)
    try:
        return float(ascii_digits(str(a))) == float(b)
    except (TypeError, ValueError):
        return str(a).strip() == str(b)


def case_class(base, claims):
    po = {"po_number": f"PO-{base['po_n']}", "qty": base["ordered"], "price": 50.0}
    f = build_facts(po, [{"qty": base["received"]}], [{"number": "I", "qty": base["ordered"], "price": 50.0}], claims, TODAY)
    return classify(f).case_class


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--extractor", choices=["regex", "llm"], default="regex")
    ap.add_argument("--n", type=int, default=8, help="variants per fact set")
    a = ap.parse_args()
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    variants = all_variants(a.n)
    bases = {b["id"]: b for b in BASES}
    tally, by = defaultdict(int), defaultdict(lambda: [0, 0])
    misses = []
    for v in variants:
        exp = v["expected"]
        got = regex_extract(v["body"]) if a.extractor == "regex" else llm_extract(v["subject"], v["body"], cache)
        got["injection"] = bool(got.get("injection")) or looks_injected(v["body"])   # rule backstop, as in the product
        linked, _ = link_email(v["subject"], v["body"], CASES)
        if not linked and got.get("po_number"):                       # product: LLM's PO is the 3rd linking step
            linked, _ = link_email("", "PO " + str(got["po_number"]), CASES)
        has_po = exp["po_number"].split("-")[1] in ascii_digits(v["subject"] + " " + v["body"])
        link_ok = (CASES.get(linked) == exp["po_number"]) if has_po else linked is None   # no PO anywhere -> triage is right
        checks = {"link": link_ok, "intent": got.get("intent") == exp["intent"],
                  "qty": eq(got.get("transit_qty"), exp["transit_qty"]), "eta": eq(got.get("eta"), exp["eta"]),
                  "injection": got["injection"] == exp["injection"]}
        gt_claim = {k: exp[k] for k in ("intent", "transit_qty", "eta", "injection")}
        checks["final_class"] = case_class(bases[v["base"]], [got]) == case_class(bases[v["base"]], [gt_claim])
        for k, ok in checks.items():
            tally[k] += ok
        allok = all(checks.values())
        tally["all"] += allok
        for key in [f"lang:{v['lang']}"] + [f"noise:{x}" for x in (v["noise"] or ["clean"])]:
            by[key][0] += allok
            by[key][1] += 1
        if not allok:
            misses.append((v["id"], v["lang"], v["noise"], [k for k, ok in checks.items() if not ok]))
    n = len(variants)
    print(f"Email permutations: {len(BASES)} fact sets x {a.n} = {n} emails, extractor={a.extractor}")
    for k in ["link", "intent", "qty", "eta", "injection", "final_class", "all"]:
        print(f"  {k:12s} {tally[k]}/{n}")
    print("  by language / noise (all fields correct):")
    for k in sorted(by):
        print(f"    {k:22s} {by[k][0]}/{by[k][1]}")
    print(f"  misses: {len(misses)}")
    for m in misses[:15]:
        print("   ", m)


if __name__ == "__main__":
    main()
