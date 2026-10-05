"""LLM transport robustness, tested against a local fake OpenAI-compatible server (no network, no key).
   python -m unittest tests.test_llm_robustness -v
Each scenario scripts the sequence of replies the server gives, so we can prove which failures are retried,
which fail fast, and that the cause is reported instead of swallowed."""
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from engine import llm
from eval import golden_eval as g


def ok(content, finish="stop"):
    return 200, {}, json.dumps({"choices": [{"message": {"content": content}, "finish_reason": finish}]})


class Fake:
    """Serves the scripted replies in order (the last one repeats). Records requests."""

    def __init__(self, replies):
        self.replies, self.seen, self.headers = list(replies), 0, []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.headers.append(dict(self.headers))
                i = min(outer.seen, len(outer.replies) - 1)
                outer.seen += 1
                status, hdrs, body = outer.replies[i]
                self.send_response(status)
                for k, v in hdrs.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body.encode())))
                self.end_headers()
                self.wfile.write(body.encode())

            def log_message(self, *a):
                pass

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class Transport(unittest.TestCase):
    def setUp(self):
        self.saved = (llm.PROVIDER, llm.BASE, llm.KEY, llm.BACKOFF_BASE, llm.BACKOFF_MAX, llm.MAX_RETRIES, llm.TIMEOUT)
        llm.PROVIDER, llm.KEY, llm.BACKOFF_BASE, llm.BACKOFF_MAX, llm.MAX_RETRIES, llm.TIMEOUT = "openai", "k", 0.01, 0.05, 4, 2

    def tearDown(self):
        llm.PROVIDER, llm.BASE, llm.KEY, llm.BACKOFF_BASE, llm.BACKOFF_MAX, llm.MAX_RETRIES, llm.TIMEOUT = self.saved

    def run_with(self, replies, **kw):
        f = Fake(replies)
        llm.BASE = f.url
        try:
            return f, llm._chat("sys", "usr", **kw)
        finally:
            f.close()

    def fails_with(self, replies):
        f = Fake(replies)
        llm.BASE = f.url
        try:
            with self.assertRaises(llm.LLMError) as cm:
                llm._chat("sys", "usr")
            return f, cm.exception
        finally:
            f.close()

    def test_clean_call(self):
        f, out = self.run_with([ok('{"a": 1}')])
        self.assertEqual((out, f.seen), ({"a": 1}, 1))

    def test_user_agent_is_sent(self):
        f, _ = self.run_with([ok('{"a": 1}')])
        self.assertEqual(f.headers[0].get("User-Agent"), "resolveiq/1.0")

    def test_429_with_retry_after_then_success(self):
        f, out = self.run_with([(429, {"Retry-After": "0"}, "{}"), (429, {}, "{}"), ok('{"a": 1}')])
        self.assertEqual((out, f.seen), ({"a": 1}, 3))

    def test_5xx_then_success(self):
        f, out = self.run_with([(503, {}, "unavailable"), (500, {}, "oops"), ok('{"a": 1}')])
        self.assertEqual((out, f.seen), ({"a": 1}, 3))

    def test_groq_json_validate_failed_400_is_retried(self):
        body = json.dumps({"error": {"code": "json_validate_failed", "message": "Failed to generate JSON"}})
        f, out = self.run_with([(400, {}, body), ok('{"a": 1}')])
        self.assertEqual((out, f.seen), ({"a": 1}, 2))

    def test_null_content_from_reasoning_model_is_retried_not_typeerror(self):
        f, out = self.run_with([ok(None), ok("   "), ok('{"a": 1}')])
        self.assertEqual((out, f.seen), ({"a": 1}, 3))

    def test_truncated_generation_is_retried(self):
        f, out = self.run_with([ok('{"a": 1', finish="length"), ok('{"a": 1}')])
        self.assertEqual((out, f.seen), ({"a": 1}, 2))

    def test_malformed_json_is_retried_and_fences_tolerated(self):
        f, out = self.run_with([ok("sorry, here you go"), ok('```json\n{"a": 1}\n```')])
        self.assertEqual((out, f.seen), ({"a": 1}, 2))

    def test_401_fails_fast_without_retry(self):
        f, e = self.fails_with([(401, {}, '{"error":"invalid api key"}')])
        self.assertEqual((e.kind, e.status, e.retriable, f.seen), ("http_error", 401, False, 1))

    def test_persistent_429_gives_up_after_budget_and_reports_cause(self):
        f, e = self.fails_with([(429, {}, "rate limit")])
        self.assertEqual((e.kind, e.status, f.seen), ("http_error", 429, llm.MAX_RETRIES + 1))
        self.assertIn("429", str(e))

    def test_timeout_is_retried_then_reported(self):
        llm.TIMEOUT, llm.MAX_RETRIES = 0.2, 1

        class Slow(BaseHTTPRequestHandler):
            def do_POST(self):
                import time
                time.sleep(0.6)

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), Slow)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        llm.BASE = f"http://127.0.0.1:{srv.server_port}"
        try:
            with self.assertRaises(llm.LLMError) as cm:
                llm._chat("s", "u")
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertIn(cm.exception.kind, ("timeout", "network"))

    def test_connection_refused_is_a_clean_error(self):
        llm.BASE, llm.MAX_RETRIES = "http://127.0.0.1:1", 1
        with self.assertRaises(llm.LLMError) as cm:
            llm._chat("s", "u")
        self.assertEqual(cm.exception.kind, "network")

    def test_extract_email_keeps_cause_in_message(self):
        f = Fake([ok('{"intent": "NOPE"}')])
        llm.BASE = f.url
        try:
            with self.assertRaises(ValueError) as cm:
                llm.extract_email("", "hello", "2026-10-04")
        finally:
            f.close()
        self.assertIn("schema check failed", str(cm.exception))


class EvaluatorHonesty(unittest.TestCase):
    CASE = g.load()[0]

    def test_arm_l_reports_the_real_cause(self):
        def boom(*a, **k):
            raise llm.LLMError("http_error", "rate limit", 429)
        with mock.patch.object(llm, "_chat", boom):
            p = g.arm_l(self.CASE)
        self.assertEqual((p["llm_errors"], p["cls"]), (1, None))
        self.assertIn("429", p["error_details"][0])

    def test_arm_l_schema_failure_is_not_an_infrastructure_error(self):
        with mock.patch.object(llm, "_chat", lambda *a, **k: {"cls": "NOT_A_CLASS", "action": "LOG"}):
            p = g.arm_l(self.CASE)
        self.assertEqual((p["llm_errors"], p["schema_errors"]), (0, 1))

    def test_arm_l_keeps_a_wrong_but_valid_answer_as_a_plain_miss(self):
        with mock.patch.object(llm, "_chat", lambda *a, **k: {"cls": "SHORT_CONFIRMED", "action": "REQUEST_CREDIT_NOTE"}):
            p = g.arm_l(self.CASE)
        self.assertEqual((p["llm_errors"], p["schema_errors"]), (0, 0))

    def test_incoherent_pair_is_counted(self):
        pred = {"cls": "PARTIAL_WAIT", "action": "REQUEST_CORRECTED_INVOICE", "decision": "ALLOWED", "hold": None,
                "claims": [], "flagged_injection": False, "llm_errors": 0, "error_details": []}
        row = g.score([self.CASE], [pred], "L")
        self.assertEqual(row["incoherent_pairs"], 1)

    def test_run_case_records_error_causes(self):
        def boom(e):
            raise ValueError("LLM extraction invalid twice (HTTPError 429)")
        c = next(x for x in g.load() if x["emails"])
        p = g.run_case(c, boom)
        self.assertEqual(p["llm_errors"], len(c["emails"]))
        self.assertIn("429", p["error_details"][0])

    def test_report_lists_llm_errors(self):
        cases = g.load()[:2]
        mk = lambda: {"cls": None, "action": None, "decision": "ALLOWED", "hold": None, "claims": [],
                      "flagged_injection": False, "llm_errors": 1, "schema_errors": 0, "error_details": ["LLMError: timeout"]}
        results = {"L": [mk(), mk()]}
        summary = [g.score(cases, results["L"], "L")]
        out = Path(tempfile.mkdtemp()) / "r.md"
        g.write_report(cases, results, summary, out)
        text = out.read_text(encoding="utf-8")
        self.assertIn("WARNING: arm L had 2 LLM call failure", text)
        self.assertIn("LLM errors (real failures, not hidden)", text)
        self.assertIn("LLMError: timeout", text)

    def test_cache_roundtrips_hindi_and_survives_corruption(self):
        old = g.CACHE
        g.CACHE = Path(tempfile.mkdtemp()) / "c.json"
        try:
            g._save_cache({"k": {"summary": "शेष माल कल निकलेगा"}})
            self.assertEqual(g._load_cache()["k"]["summary"], "शेष माल कल निकलेगा")
            g.CACHE.write_bytes(b"{not json")
            self.assertEqual(g._load_cache(), {})
        finally:
            g.CACHE = old

    def test_failures_are_never_cached(self):
        old, orig = g.CACHE, llm._chat
        g.CACHE = Path(tempfile.mkdtemp()) / "c.json"
        calls = []

        def flaky(*a, **k):
            calls.append(1)
            if len(calls) == 1:
                raise llm.LLMError("http_error", "x", 429)
            return {"ok": True}
        llm._chat = flaky
        try:
            g.install_cache(0)
            with self.assertRaises(llm.LLMError):
                llm._chat("s", "u")
            self.assertEqual(llm._chat("s", "u"), {"ok": True})   # the failure was not remembered
        finally:
            llm._chat, g.CACHE = orig, old

    def test_s_and_l_refuse_to_run_without_a_key_and_write_nothing(self):
        import subprocess, sys
        env = {k: v for k, v in os.environ.items() if not k.startswith("LLM_")}
        before = (Path(g.HERE) / "golden_results.json").read_bytes() if (Path(g.HERE) / "golden_results.json").exists() else b""
        r = subprocess.run([sys.executable, "-m", "eval.golden_eval", "--arms", "S,L"], capture_output=True, text=True, env=env,
                           cwd=str(Path(g.HERE).parent))
        self.assertEqual(r.returncode, 3)
        self.assertIn("LLM_API_KEY", r.stderr)
        after = (Path(g.HERE) / "golden_results.json").read_bytes() if (Path(g.HERE) / "golden_results.json").exists() else b""
        self.assertEqual(before, after)


class WindowsReset(unittest.TestCase):
    def test_reset_falls_back_to_in_place_wipe_when_file_is_locked(self):
        tmp = tempfile.mkdtemp()
        os.environ["DB_PATH"], os.environ["DATA_DIR"] = os.path.join(tmp, "t.db"), os.path.join(tmp, "demo")
        from api import service as s
        s.DB_PATH, s.DATA = os.path.join(tmp, "t.db"), Path(tmp) / "demo"
        s.reset()
        con = s.db()
        con.execute("insert into events(case_id, at, type, detail) values('x','now','t','d')")
        con.commit()
        con.close()
        real_remove = os.remove

        def locked(path):
            if str(path) == s.DB_PATH:
                raise PermissionError(32, "The process cannot access the file because it is being used by another process")
            return real_remove(path)
        with mock.patch("os.remove", locked):
            s.reset()                      # must not raise
        con = s.db()
        self.assertEqual(con.execute("select count(*) from events").fetchone()[0], 0)
        con.close()


if __name__ == "__main__":
    unittest.main()
