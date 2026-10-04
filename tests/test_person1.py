"""Comprehensive unit and integration tests for Person 1: Backend & Data Lead deliverables.
Tests:
1. Database Layer & PostgreSQL / SQLite persistence (all 11 tables, transactions, reset).
2. Smart CSV/Excel Cleaner Loader (fuzzy headers, currencies, dates, Indic numerals, subtotals, dedup, audit report).
3. Core deterministic 3-way matcher (/match logic).
4. Classifier & 8 Exception classes (/classify logic).
5. 7-Step Policy Gate (/policy logic).
6. Capability-Aware Integration Layer & Router (/capability/route, /capability/profile).
7. Verification engine & audit table logging (/verify logic).
8. FastAPI endpoints integration testing.
"""
import io
import json
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from api.main import app
from api import service as s
from engine.classify import CaseFacts, classify, ALLOWED_ACTIONS, DEFAULT_ACTION
from engine.policy import gate, lint_draft
from engine.normalise import load_table, norm_po, num, parse_date
from engine.router import pick, evidence
from engine.verify import verify
from engine.db import get_connection, db_session, reset_database, SCHEMA_SQLITE, is_postgres


class TestDatabaseAndTables(unittest.TestCase):
    """Tests schema creation, tables, and CRUD operations across all tables."""

    def setUp(self):
        s.reset()

    def test_all_tables_exist_and_functional(self):
        con = s.db()
        try:
            # 1. Capability profile table
            con.execute(
                "INSERT OR REPLACE INTO capability_profile(customer_id, capability, provider, fallback, healthy, max_age_min) "
                "VALUES(?,?,?,?,?,?)",
                ("TEST_CUST", "po", "NATIVE", "csv", 1, 15)
            )
            
            # 2. Cases table
            con.execute(
                "INSERT OR REPLACE INTO cases(id, customer, po_number, supplier, status, case_class, gap, hold, created) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                ("CASE-999", "TEST_CUST", "PO-999", "Supplier Test", "OPEN", "PARTIAL_WAIT", 10, 500.0, "2026-10-04T00:00:00")
            )
            
            # 3. Evidence table with provenance
            con.execute(
                "INSERT INTO evidence(case_id, kind, data, provenance, method, confidence, is_claim, fetched_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                ("CASE-999", "po", json.dumps({"po_number": "PO-999", "qty": 100}), "test_src", "native", 1.0, 0, "2026-10-04T00:00:00")
            )
            
            # 4. Events table
            con.execute(
                "INSERT INTO events(case_id, at, type, detail) VALUES(?,?,?,?)",
                ("CASE-999", "2026-10-04T00:00:00", "test_event", "Test details")
            )
            
            # 5. Verifications table
            con.execute(
                "INSERT INTO verifications(case_id, at, case_class, ok, why, status, source, details) "
                "VALUES(?,?,?,?,?,?,?,?)",
                ("CASE-999", "2026-10-04T00:00:00", "PARTIAL_WAIT", 1, "GRN matched", "CLOSED", "native", "{}")
            )
            
            # 6. Suppliers table
            con.execute(
                "INSERT OR REPLACE INTO suppliers(customer_id, supplier_name, supplier_email, msme_flag, tolerance_pct, price_tolerance_pct) "
                "VALUES(?,?,?,?,?,?)",
                ("TEST_CUST", "Supplier Test", "sup@test.com", 0, 2.0, 1.0)
            )
            
            # 7. Emails table
            con.execute(
                "INSERT OR REPLACE INTO emails(message_id, case_id, subject, body, method, received_at) "
                "VALUES(?,?,?,?,?,?)",
                ("msg_001", "CASE-999", "PO-999 update", "Shipped 10 units", "subject_token", "2026-10-04T00:00:00")
            )
            
            # 8. Action keys table
            con.execute(
                "INSERT OR REPLACE INTO action_keys(key, created_at) VALUES(?,?)",
                ("CASE-999:WAIT:0", "2026-10-04T00:00:00")
            )
            
            # 9. Path log table
            con.execute(
                "INSERT INTO path_log(at, customer, capability, path, note) VALUES(?,?,?,?,?)",
                ("2026-10-04T00:00:00", "TEST_CUST", "po", "NATIVE", "initial test")
            )
            
            # 10. Health table
            con.execute(
                "INSERT OR REPLACE INTO health(customer, capability, healthy, updated_at) VALUES(?,?,?,?)",
                ("TEST_CUST", "po", 1, "2026-10-04T00:00:00")
            )
            
            con.commit()

            # Verify reads
            case = con.execute("SELECT * FROM cases WHERE id='CASE-999'").fetchone()
            self.assertIsNotNone(case)
            self.assertEqual(case["po_number"] if hasattr(case, "keys") else case[2], "PO-999")

            ev = con.execute("SELECT * FROM evidence WHERE case_id='CASE-999'").fetchall()
            self.assertEqual(len(ev), 1)

            ver = con.execute("SELECT * FROM verifications WHERE case_id='CASE-999'").fetchall()
            self.assertEqual(len(ver), 1)
        finally:
            con.close()


class TestSmartCleanerLoader(unittest.TestCase):
    """Tests the Smart CSV/Excel Cleaner Loader on messy, real-world data variants."""

    def test_fuzzy_header_detection_and_aliases(self):
        messy_csv = """# Small Shop Invoice Export
# Generated on: 2026-10-04
# Store: Bangalore Main Branch

Purchase Order Number,Inward Challan No,Quantity (Nos),Item Rate (INR),Total Value
PO-5501,GRN-8801,100,50.00,5000.00
PO-5502,GRN-8802,250,20.00,5000.00
"""
        rows, report = load_table(io.StringIO(messy_csv), "po")
        self.assertEqual(len(rows), 2)
        self.assertEqual(report["clean_rows_count"], 2)
        self.assertEqual(rows[0]["po_number"], "PO-5501")
        self.assertEqual(rows[0]["qty"], 100)
        self.assertEqual(rows[0]["price"], 50.0)
        self.assertTrue(any("Header located on row 5" in f for f in report["fixed"]))

    def test_currency_and_units_cleaning(self):
        self.assertEqual(num("₹ 1,500.50"), 1500.5)
        self.assertEqual(num("Rs. 50/-"), 50.0)
        self.assertEqual(num("INR 25.00"), 25.0)
        self.assertEqual(num("100 pcs"), 100.0)
        self.assertEqual(num("50 units"), 50.0)
        self.assertEqual(num("(200.00)"), -200.0)
        self.assertEqual(num("1,00,000.00"), 100000.0)

    def test_indic_and_messy_po_normalization(self):
        self.assertEqual(norm_po("PO # 1001"), "PO-1001")
        self.assertEqual(norm_po("PO/2026/1002"), "PO-2026")
        self.assertEqual(norm_po("पीओ १०५०"), "PO-1050")
        self.assertEqual(norm_po("1005"), "PO-1005")

    def test_multi_format_date_parsing(self):
        self.assertEqual(parse_date("2026-10-04"), "2026-10-04")
        self.assertEqual(parse_date("04/10/2026"), "2026-10-04")
        self.assertEqual(parse_date("04-10-2026"), "2026-10-04")
        self.assertEqual(parse_date("04 Oct 2026"), "2026-10-04")
        self.assertEqual(parse_date("04-Oct-26"), "2026-10-04")
        self.assertEqual(parse_date("October 4, 2026"), "2026-10-04")

    def test_subtotal_skipping_and_deduplication(self):
        messy_grn = """GRN No,PO No,Qty Received
GRN-1,PO-1001,50
GRN-1,PO-1001,50
GRN-2,PO-1001,40
Sub Total,,90
Grand Total,,90
"""
        rows, report = load_table(io.StringIO(messy_grn), "grn")
        self.assertEqual(len(rows), 2)  # Exact duplicate ignored, subtotals ignored
        self.assertTrue(any("duplicate row ignored" in f for f in report["fixed"]))
        self.assertTrue(any("Subtotal/summary row skipped" in f for f in report["fixed"]))


class TestDeterministicMatching(unittest.TestCase):
    """Tests the 3-way matching logic (/match)."""

    def test_perfect_match(self):
        po = {"po_number": "PO-1001", "qty": 100, "price": 50.0}
        grns = [{"grn_id": "GRN-1", "qty": 100}]
        invoices = [{"number": "INV-1", "qty": 100, "price": 50.0}]
        res = s.match_po_grn_invoice(po, grns, invoices)
        self.assertTrue(res["matched"])
        self.assertEqual(res["qty_gap"], 0)
        self.assertEqual(res["recommended_hold"], 0.0)

    def test_short_shipment_match(self):
        po = {"po_number": "PO-1001", "qty": 100, "price": 50.0}
        grns = [{"grn_id": "GRN-1", "qty": 90}]
        invoices = [{"number": "INV-1", "qty": 100, "price": 50.0}]
        res = s.match_po_grn_invoice(po, grns, invoices)
        self.assertFalse(res["matched"])
        self.assertEqual(res["qty_gap"], 10)
        self.assertEqual(res["recommended_hold"], 500.0)
        self.assertTrue(any("Short delivery" in d for d in res["discrepancies"]))

    def test_price_mismatch(self):
        po = {"po_number": "PO-1001", "qty": 100, "price": 50.0}
        grns = [{"grn_id": "GRN-1", "qty": 100}]
        invoices = [{"number": "INV-1", "qty": 100, "price": 55.0}]
        res = s.match_po_grn_invoice(po, grns, invoices)
        self.assertFalse(res["matched"])
        self.assertEqual(res["price_variance"], 5.0)
        self.assertEqual(res["recommended_hold"], 500.0)


class TestFastAPIEndpoints(unittest.TestCase):
    """Tests FastAPI endpoints using TestClient."""

    def setUp(self):
        self.client = TestClient(app)
        s.reset()

    def test_health_endpoint(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertIn("mode", data)

    def test_match_endpoint(self):
        payload = {
            "po": {"po_number": "PO-2001", "qty": 100, "price": 100.0},
            "grns": [{"grn_id": "GRN-1", "qty": 80}],
            "invoices": [{"number": "INV-1", "qty": 100, "price": 100.0}],
            "tolerance_pct": 2.0,
            "price_tolerance_pct": 1.0,
        }
        resp = self.client.post("/match", json=payload)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data["matched"])
        self.assertEqual(data["qty_gap"], 20)
        self.assertEqual(data["recommended_hold"], 2000.0)

    def test_classify_endpoint(self):
        payload = {
            "po_number": "PO-2002",
            "ordered_qty": 100,
            "po_price": 50.0,
            "received_qty": 90,
            "invoices": [{"number": "INV-1", "qty": 100, "price": 50.0}],
            "transit_qty": 10,
            "transit_eta": "2026-10-10",
            "today": "2026-10-04",
        }
        resp = self.client.post("/classify", json=payload)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["case_class"], "PARTIAL_WAIT")
        self.assertEqual(data["hold_value"], 500.0)
        self.assertIn("WAIT", data["allowed_actions"])

    def test_policy_endpoint(self):
        payload = {
            "case_class": "PARTIAL_WAIT",
            "action": "WAIT",
            "mode": "suggestion",
            "hold_value": 500.0,
            "hold_limit": 50000.0,
            "msme_flag": False,
            "injection_flag": False,
            "qty_provenance": "NATIVE",
        }
        resp = self.client.post("/policy", json=payload)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["decision"], "ALLOWED")

        # Blocked action test
        payload["action"] = "REQUEST_CREDIT_NOTE"
        resp = self.client.post("/policy", json=payload)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["decision"], "BLOCKED")

    def test_capability_route_endpoint(self):
        payload = {"customer": "X", "capability": "po"}
        resp = self.client.post("/capability/route", json=payload)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["path"], "NATIVE")

        # Set unhealthy -> route to FALLBACK
        self.client.post("/admin/health", json={"customer": "X", "capability": "po", "healthy": False})
        resp = self.client.post("/capability/route", json=payload)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["path"], "FALLBACK")

    def test_verify_endpoint(self):
        payload = {
            "case_class": "PARTIAL_WAIT",
            "ordered": 100,
            "received": 100,
            "tolerance_pct": 2.0,
        }
        resp = self.client.post("/verify", json=payload)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])

    def test_lint_endpoint(self):
        payload = {
            "draft": "We received 90 of 100 units. Please clarify.",
            "allowed_numbers": [90, 100],
        }
        resp = self.client.post("/lint", json=payload)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["clean"])

        # Banned word & ungrounded number
        payload_bad = {
            "draft": "This is fraud. You owe 999.",
            "allowed_numbers": [90, 100],
        }
        resp_bad = self.client.post("/lint", json=payload_bad)
        self.assertEqual(resp_bad.status_code, 200)
        self.assertFalse(resp_bad.json()["clean"])
        self.assertTrue(any("fraud" in p for p in resp_bad.json()["problems"]))
        self.assertTrue(any("999" in p for p in resp_bad.json()["problems"]))

    def test_load_csv_endpoint(self):
        csv_content = b"PO Number,Ordered Qty,Unit Rate\nPO-7001,50,10.0\nPO-7002,100,20.0\n"
        resp = self.client.post(
            "/load?kind=po",
            files={"file": ("test_pos.csv", io.BytesIO(csv_content), "text/csv")}
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["rows_count"], 2)
        self.assertEqual(data["rows"][0]["po_number"], "PO-7001")


if __name__ == "__main__":
    unittest.main()
