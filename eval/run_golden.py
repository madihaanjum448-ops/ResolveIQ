"""Golden + mutation evaluation. Every scenario is written to real CSV/XLSX files (clean + 3 messy variants),
read back through the same normaliser the product uses, then judged by the same engine.
   python -m eval.run_golden            # all
   python -m eval.run_golden --split test
Reports COUNTS, not just percentages, and lists every failure."""
import argparse
import json
import random
import tempfile
from collections import Counter
from pathlib import Path

from data.golden import SCENARIOS, DEV, TODAY
from data.mutate import MUTATIONS, write
from engine.classify import classify, DEFAULT_ACTION
from engine.facts import build_facts
from engine.normalise import load_table
from engine.policy import gate

VARIANTS_PER_SCENARIO = 3


def judge(po_rows, grn_rows, inv_rows, claims, po_number):
    po = next(p for p in po_rows if p["po_number"] == po_number)
    grns = [g for g in grn_rows if g["po_number"] == po_number]
    invs = [i for i in inv_rows if i["po_number"] == po_number]
    f = build_facts(po, grns, invs, claims, TODAY)
    r = classify(f)
    action = DEFAULT_ACTION[r.case_class]
    decision, reasons = gate(r.case_class, action, hold_value=r.hold_value,
                             msme_flag=bool(po.get("msme")), human_flags=r.flags,
                             injection_flag=any(c.get("injection") for c in claims))
    return r.case_class, decision, r.hold_value, r.flags


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["dev", "test", "all"], default="all")
    ap.add_argument("--keep", help="folder to keep the generated messy files (for the demo / inspection)")
    a = ap.parse_args()
    root = Path(a.keep) if a.keep else Path(tempfile.mkdtemp())
    root.mkdir(parents=True, exist_ok=True)
    results, by_mut = [], Counter()
    for sid, desc, po, grns, invs, claims, (exp_cls, exp_dec, exp_hold) in SCENARIOS:
        split = "dev" if sid in DEV else "test"
        if a.split != "all" and split != a.split:
            continue
        rng = random.Random(sid)
        variants = [[]] + [rng.sample(MUTATIONS, rng.choice([2, 3])) for _ in range(VARIANTS_PER_SCENARIO)]
        for vi, muts in enumerate(variants):
            d = root / sid / f"v{vi}"
            d.mkdir(parents=True, exist_ok=True)
            files, reports = {}, []
            for kind, rows in (("po", [po]), ("grn", grns), ("invoice", invs)):
                path = write(d / f"{kind}.csv", kind, rows, muts, rng=hash((sid, vi, kind)))
                loaded, rep = load_table(path, kind)
                files[kind], _ = loaded, reports.append(rep)
            try:
                got = judge(files["po"], files["grn"], files["invoice"], claims, po["po_number"])
            except Exception as e:  # loader lost the PO etc.
                got = ("ERROR", "ERROR", None, [str(e)])
            ok = (got[0] == exp_cls, got[1] == exp_dec, got[2] is not None and abs(got[2] - exp_hold) < 0.01)
            results.append({"id": sid, "variant": vi, "split": split, "mutations": muts, "desc": desc,
                            "expected": [exp_cls, exp_dec, exp_hold], "got": list(got[:3]), "flags": got[3],
                            "class_ok": ok[0], "decision_ok": ok[1], "hold_ok": ok[2], "all_ok": all(ok),
                            "rejected_rows": sum(len(r["rejected"]) for r in reports),
                            "fixes": sum(len(r["fixed"]) for r in reports)})
            for m in muts or ["clean"]:
                by_mut[(m, all(ok))] += 1

    n = len(results)
    c = lambda k: sum(r[k] for r in results)
    clean = [r for r in results if r["variant"] == 0]
    messy = [r for r in results if r["variant"] > 0]
    print(f"Golden evaluation  ({len({r['id'] for r in results})} scenarios x {1 + VARIANTS_PER_SCENARIO} = {n} cases, split={a.split})")
    print(f"  class correct     {c('class_ok')}/{n}")
    print(f"  decision correct  {c('decision_ok')}/{n}")
    print(f"  hold correct      {c('hold_ok')}/{n}")
    print(f"  ALL correct       {c('all_ok')}/{n}   (clean {sum(r['all_ok'] for r in clean)}/{len(clean)}, "
          f"messy {sum(r['all_ok'] for r in messy)}/{len(messy)})")
    print(f"  loader fixes applied {c('fixes')}, rows rejected {c('rejected_rows')}")
    print("\n  by mutation (correct/total):")
    for m in ["clean"] + MUTATIONS:
        t = by_mut[(m, True)] + by_mut[(m, False)]
        if t:
            print(f"    {m:14s} {by_mut[(m, True)]}/{t}")
    fails = [r for r in results if not r["all_ok"]]
    if fails:
        print(f"\n  FAILURES ({len(fails)}):")
        for r in fails:
            print(f"    {r['id']} v{r['variant']} {r['mutations']}: expected {r['expected']} got {r['got']}  ({r['desc']})")
    out = Path(__file__).parent / "golden_results.json"
    out.write_text(json.dumps(results, indent=1))
    print(f"\n  saved {out.name}; messy files in {root}")


if __name__ == "__main__":
    main()
