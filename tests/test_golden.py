"""Golden-set tests. stdlib only; does not need the api/ package.   python -m unittest tests.test_golden -v"""
import json
import tempfile
import unittest
from pathlib import Path

from engine import llm
from eval import golden_eval as g
from eval.run_eval import oracle_extract, run_case as base_run_case, TODAY

CASES = g.load()
TRUTH = {e: c["gt_claims"][i] for c in CASES for i, e in enumerate(c["emails"])}


def fake_llm(system, user, model=None, as_json=True):
    """Stand-in for the LLM that answers with the true extraction (plumbing test only)."""
    text = user.split("<email>\n", 1)[1].rsplit("\n</email>", 1)[0]
    t = TRUTH[text]
    return {"intent": t["intent"], "po_number": None, "transit_qty": t["transit_qty"], "lr_number": None, "eta": t["eta"],
            "language": t["language"], "injection": t["injection"], "confidence": 0.9, "summary": "stub"}


class GoldenFile(unittest.TestCase):
    def test_labels_are_internally_consistent(self):
        self.assertEqual(g.validate(CASES), [])

    def test_size_and_coverage(self):
        self.assertGreaterEqual(len(CASES), 20)
        langs = {c["language"] for c in CASES}
        self.assertTrue({"en", "hinglish", "hi"} <= langs)
        tags = {t for c in CASES for t in c["tags"]}
        for needed in ("injection", "conflict", "ambiguous", "boundary", "duplicate", "price", "relative-date"):
            self.assertIn(needed, tags)

    def test_engine_agrees_with_hand_labels_under_oracle(self):
        for c in CASES:
            p = g.run_case(c, lambda e, c=c: c["gt_claims"][c["emails"].index(e)])
            self.assertEqual((p["cls"], p["action"]), (c["label"], c["gold_action"]), c["id"])
            self.assertAlmostEqual(p["hold"], c["expected_hold"], places=1, msg=c["id"])

    def test_mirrors_the_original_runner(self):
        for c in CASES:
            mine = g.run_case(c, lambda e, c=c: oracle_extract(c, e))
            base = base_run_case(c, lambda e, c=c: oracle_extract(c, e))
            self.assertEqual((mine["cls"], mine["action"], mine["decision"]), (base["cls"], base["action"], base["decision"]), c["id"])


class Arms(unittest.TestCase):
    def test_plain_match_is_worse_than_rules(self):
        s = {a: g.score(CASES, [g.arm_r0(c) if a == "R0" else g.run_case(c, g.regex_extract_plus) for c in CASES], a)
             for a in ("R0", "R1P")}
        self.assertLess(s["R0"]["class_acc"], s["R1P"]["class_acc"])

    def test_rules_abstain_on_ambiguous_kal(self):
        out = g.regex_extract_plus("baaki 10 piece kal bhej diye, LR 1")
        self.assertIsNone(out["eta"])      # 'kal' alone could be yesterday or tomorrow

    def test_llm_arm_plumbing_with_stub(self):
        orig = llm._chat
        llm._chat = fake_llm
        try:
            preds = [g.run_case(c, lambda e: llm.extract_email("", e, TODAY)) for c in CASES]
        finally:
            llm._chat = orig
        row = g.score(CASES, preds, "S")
        self.assertEqual(row["class_acc"], 1.0)
        self.assertEqual(row["llm_errors"], 0)
        self.assertEqual(row["language_id_acc"], 1.0)

    def test_broken_llm_is_counted_not_fatal(self):
        def boom(*a, **k):
            raise RuntimeError("rate limited")
        orig = llm._chat
        llm._chat = boom
        try:
            preds = [g.run_case(c, lambda e: llm.extract_email("", e, TODAY)) for c in CASES]
        finally:
            llm._chat = orig
        row = g.score(CASES, preds, "S")
        self.assertEqual(row["llm_errors"], sum(len(c["emails"]) for c in CASES))
        self.assertEqual(row["unsafe_actions"], 0)

    def test_cache_avoids_second_call(self):
        calls = []
        orig, orig_cache = llm._chat, g.CACHE
        g.CACHE = Path(tempfile.mkdtemp()) / "cache.json"
        llm._chat = lambda s, u, m=None, j=True: calls.append(1) or {"ok": True}
        try:
            g.install_cache(0)
            llm._chat("s", "u")
            llm._chat("s", "u")
        finally:
            llm._chat, g.CACHE = orig, orig_cache
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
