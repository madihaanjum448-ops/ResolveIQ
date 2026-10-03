"""Golden core: hand-built scenarios with the exact expected class, policy decision and hold value.
TODAY is fixed so ETAs are deterministic. Claims are the CORRECT extraction of the supplier email
(the team's hand-written emails test the LLM separately). Check every label by hand before trusting results.
decision: ALLOWED = system may act alone; NEEDS_HUMAN = approval/escalation; BLOCKED = refused."""
from datetime import date, timedelta

TODAY = date(2026, 10, 4)
D = lambda n: (TODAY + timedelta(days=n)).isoformat()


def po(n, qty, price, **kw):
    return {"po_number": f"PO-{n}", "sku": kw.pop("sku", "Basmati Rice 5kg"), "qty": qty, "price": price,
            "supplier": kw.pop("supplier", "Sharma Traders"), "msme": kw.pop("msme", 0), **kw}


def grn(n, i, qty, **kw):
    return {"grn_id": f"GRN-{n}-{i}", "po_number": f"PO-{n}", "qty": qty, "date": D(-3), **kw}


def inv(n, num, qty, price, **kw):
    return {"number": num, "po_number": f"PO-{n}", "qty": qty, "price": price, "total": round(qty * price, 2),
            "supplier": kw.pop("supplier", "Sharma Traders"), **kw}


TRANSIT = lambda q, eta: {"intent": "IN_TRANSIT", "transit_qty": q, "eta": eta, "injection": False}
SHORT = {"intent": "CONFIRMS_SHORTAGE", "injection": False}

SCENARIOS = [
    # id, what it tests, po, grns, invoices, claims, expected (class, decision, hold)
    ("G01", "partial shipment, ETA in future -> wait, no dispute",
     po(2001, 100, 50), [grn(2001, 1, 90)], [inv(2001, "INV-01", 100, 50)], [TRANSIT(10, D(2))],
     ("PARTIAL_WAIT", "ALLOWED", 500.0)),
    ("G02", "ETA passed, no GRN -> reminder needs approval",
     po(2002, 100, 50), [grn(2002, 1, 90)], [inv(2002, "INV-02", 100, 50)], [TRANSIT(10, D(-2))],
     ("PARTIAL_OVERDUE", "NEEDS_HUMAN", 500.0)),
    ("G03", "short, no explanation",
     po(2003, 100, 50), [grn(2003, 1, 90)], [inv(2003, "INV-03", 100, 50)], [],
     ("UNKNOWN_NEEDS_EVIDENCE", "NEEDS_HUMAN", 500.0)),
    ("G04", "supplier confirms shortage, MSME supplier",
     po(2004, 60, 40, msme=1), [grn(2004, 1, 50)], [inv(2004, "INV-04", 60, 40)], [SHORT],
     ("SHORT_CONFIRMED", "NEEDS_HUMAN", 400.0)),
    ("G05", "price mismatch 22 vs 20",
     po(2005, 200, 20), [grn(2005, 1, 200)], [inv(2005, "INV-05", 200, 22)], [],
     ("PRICE_MISMATCH", "NEEDS_HUMAN", 400.0)),
    ("G06", "same invoice number twice from same supplier -> block",
     po(2006, 100, 50), [grn(2006, 1, 100)], [inv(2006, "INV-06", 100, 50), inv(2006, "INV-06", 100, 50)], [],
     ("DUPLICATE_INVOICE", "ALLOWED", 5000.0)),
    ("G07", "same invoice number, different suppliers -> NOT a duplicate",
     po(2007, 100, 50), [grn(2007, 1, 100)],
     [inv(2007, "INV-07", 50, 50), inv(2007, "INV-07", 50, 50, supplier="Gupta Agencies")], [],
     ("WITHIN_TOLERANCE", "ALLOWED", 0.0)),
    ("G08", "gap exactly at 2% tolerance",
     po(2008, 100, 50), [grn(2008, 1, 98)], [inv(2008, "INV-08", 100, 50)], [],
     ("WITHIN_TOLERANCE", "ALLOWED", 0.0)),
    ("G09", "gap just over tolerance (3 of 100)",
     po(2009, 100, 50), [grn(2009, 1, 97)], [inv(2009, "INV-09", 100, 50)], [],
     ("UNKNOWN_NEEDS_EVIDENCE", "NEEDS_HUMAN", 150.0)),
    ("G10", "in-transit covers only part of the gap (8 of 10)",
     po(2010, 100, 50), [grn(2010, 1, 90)], [inv(2010, "INV-10", 100, 50)], [TRANSIT(8, D(2))],
     ("UNKNOWN_NEEDS_EVIDENCE", "NEEDS_HUMAN", 500.0)),
    ("G11", "ETA is today -> still waiting",
     po(2011, 100, 50), [grn(2011, 1, 90)], [inv(2011, "INV-11", 100, 50)], [TRANSIT(10, D(0))],
     ("PARTIAL_WAIT", "ALLOWED", 500.0)),
    ("G12", "three GRNs add up to the full order",
     po(2012, 100, 50), [grn(2012, 1, 40), grn(2012, 2, 30), grn(2012, 3, 30)], [inv(2012, "INV-12", 100, 50)], [],
     ("WITHIN_TOLERANCE", "ALLOWED", 0.0)),
    ("G13", "GRN row copy-pasted twice -> counted once",
     po(2013, 100, 50), [grn(2013, 1, 90), grn(2013, 1, 90)], [inv(2013, "INV-13", 100, 50)], [],
     ("UNKNOWN_NEEDS_EVIDENCE", "NEEDS_HUMAN", 500.0)),
    ("G14", "return booked as negative GRN -> human",
     po(2014, 100, 50), [grn(2014, 1, 100), grn(2014, 2, -5)], [inv(2014, "INV-14", 100, 50)], [],
     ("UNKNOWN_NEEDS_EVIDENCE", "NEEDS_HUMAN", 250.0)),
    ("G15", "PO in pieces, GRN in boxes -> human",
     po(2015, 100, 50, uom="pcs"), [grn(2015, 1, 10, uom="box")], [inv(2015, "INV-15", 100, 50, uom="pcs")], [],
     ("UNKNOWN_NEEDS_EVIDENCE", "NEEDS_HUMAN", 4500.0)),
    ("G16", "invoice rate includes GST -> human, not a blind price dispute",
     po(2016, 100, 100), [grn(2016, 1, 100)], [inv(2016, "INV-16", 100, 118, tax_inclusive=True)], [],
     ("PRICE_MISMATCH", "NEEDS_HUMAN", 1800.0)),
    ("G17", "multi-issue: price AND short together -> human",
     po(2017, 100, 50), [grn(2017, 1, 90)], [inv(2017, "INV-17", 100, 55)], [],
     ("PRICE_MISMATCH", "NEEDS_HUMAN", 500.0)),
    ("G18", "over-delivery -> escalate only",
     po(2018, 100, 50), [grn(2018, 1, 110)], [inv(2018, "INV-18", 100, 50)], [],
     ("OVER_DELIVERY", "NEEDS_HUMAN", 0.0)),
    ("G19", "injection in supplier email -> flagged, human",
     po(2019, 100, 50), [grn(2019, 1, 90)], [inv(2019, "INV-19", 100, 50)],
     [{"intent": "OTHER", "injection": True}],
     ("UNKNOWN_NEEDS_EVIDENCE", "NEEDS_HUMAN", 500.0)),
    ("G20", "two emails disagree (10 vs 15 in transit) -> never guess",
     po(2020, 100, 50), [grn(2020, 1, 90)], [inv(2020, "INV-20", 100, 50)], [TRANSIT(10, D(2)), TRANSIT(15, D(3))],
     ("UNKNOWN_NEEDS_EVIDENCE", "NEEDS_HUMAN", 500.0)),
    ("G21", "price differs by paise (49.99 vs 50) -> within tolerance",
     po(2021, 100, 50), [grn(2021, 1, 100)], [inv(2021, "INV-21", 100, 49.99)], [],
     ("WITHIN_TOLERANCE", "ALLOWED", 0.0)),
]

# Split by SCENARIO (all mutations of one scenario stay together) so test data never leaks into dev.
DEV = {"G01", "G05", "G09", "G13", "G17", "G20"}
