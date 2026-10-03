"""Multilingual check: can the system read supplier emails and invoices in Indian languages?
   python -m eval.run_multilingual          (needs LLM_API_KEY in the environment)
Linking (PO number -> case) is rule-based and is checked WITHOUT the LLM too.
Reports counts per item and lists every miss."""
import json
from pathlib import Path

from engine import llm
from engine.policy import ascii_digits, link_email

DATA = json.loads((Path(__file__).resolve().parent.parent / "data" / "multilingual.json").read_text())
TODAY = "2026-10-04"


def same(a, b):
    if a is None or b is None:
        return (a in (None, "", 0)) == (b in (None, "", 0))
    if isinstance(b, (int, float)) and not isinstance(b, bool):
        try:
            return abs(float(ascii_digits(str(a)).replace(",", "")) - float(b)) < 0.01
        except ValueError:
            return False
    return ascii_digits(str(a)).strip().upper() == ascii_digits(str(b)).strip().upper()


def main():
    cases = {f"CASE-{i}": po for i, po in enumerate(["PO-1001", "PO-1002", "PO-1003", "PO-1005"], 101)}
    rows, misses = [], []
    print("EMAILS")
    for e in DATA["emails"]:
        linked, method = link_email(e["subject"], e["body"], cases)
        link_ok = cases.get(linked) == e["expected"]["po_number"]
        got, fields = None, {}
        if llm.available():
            try:
                got = llm.extract_email(e["subject"], e["body"], TODAY)
                fields = {k: same(got.get(k), v) for k, v in e["expected"].items() if k != "po_number"}
            except Exception as err:
                got = {"error": str(err)}
        ok = bool(link_ok and fields and all(fields.values()))
        rows.append(("email", e["id"], ok))
        print(f"  {e['id']:7s} {e['language']:28s} link={'ok' if link_ok else 'MISS'}({method})  "
              + ("fields=" + ",".join(f"{k}:{'ok' if v else 'MISS'}" for k, v in fields.items()) if fields else "fields=skipped (no LLM key)"))
        if not ok and (fields or not link_ok):
            misses.append((e["id"], e["expected"], got))
    print("INVOICES")
    for inv in DATA["invoices"]:
        fields = {}
        if llm.available():
            try:
                got = llm.extract_invoice(inv["text"])
                fields = {k: same(got.get(k), v) for k, v in inv["expected"].items()}
            except Exception as err:
                got = {"error": str(err)}
        ok = bool(fields) and all(fields.values())
        rows.append(("invoice", inv["id"], ok))
        print(f"  {inv['id']:7s} {inv['language']:28s} "
              + ("fields=" + ",".join(f"{k}:{'ok' if v else 'MISS'}" for k, v in fields.items()) if fields else "skipped (no LLM key)"))
        if not ok and fields:
            misses.append((inv["id"], inv["expected"], got))
    for kind in ("email", "invoice"):
        r = [x for x in rows if x[0] == kind]
        print(f"\n{kind}s fully correct: {sum(x[2] for x in r)}/{len(r)}")
    for m in misses:
        print(f"  MISS {m[0]}: expected {m[1]} got {m[2]}")


if __name__ == "__main__":
    main()
