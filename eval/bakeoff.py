"""20-email model bake-off (PRD C3). Pick the model by measured numbers, not reputation.
   LLM_API_KEY=... python -m eval.bakeoff --models claude-sonnet-5-5,claude-haiku-4-5-20251001
Uses generated emails with known ground truth (>= 6 Hinglish). Hand-check the emails too."""
import argparse
import time

from engine import llm
from eval.run_eval import gen, TODAY


def pick_emails(n=20):
    cases = [c for c in gen() if c.get("gt_claims") and len(c["emails"]) == 1]
    hi = [c for c in cases if c["hinglish"]][:8]
    en = [c for c in cases if not c["hinglish"]][: n - len(hi)]
    return hi + en


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default=llm.EXTRACT_MODEL)
    a = ap.parse_args()
    emails = pick_emails()
    print(f"{len(emails)} emails, {sum(c['hinglish'] for c in emails)} Hinglish\n")
    print("model | intent | qty | eta | injection | all fields | invalid JSON | sec/email")
    for model in a.models.split(","):
        hits = {"intent": 0, "qty": 0, "eta": 0, "injection": 0, "all": 0}
        invalid, t0 = 0, time.time()
        for c in emails:
            gt = c["gt_claims"][0]
            try:
                o = llm.extract_email("", c["emails"][0], TODAY, model=model)
            except Exception:
                invalid += 1
                continue
            ok = {"intent": o["intent"] == gt["intent"],
                  "qty": (o.get("transit_qty") or None) == gt.get("transit_qty"),
                  "eta": o.get("eta") == gt.get("eta"),
                  "injection": o["injection"] == gt["injection"]}
            for k, v in ok.items():
                hits[k] += v
            hits["all"] += all(ok.values())
        n = len(emails)
        print(f"{model} | " + " | ".join(f"{hits[k] / n:.0%}" for k in ("intent", "qty", "eta", "injection", "all"))
              + f" | {invalid}/{n} | {(time.time() - t0) / n:.1f}")


if __name__ == "__main__":
    main()
