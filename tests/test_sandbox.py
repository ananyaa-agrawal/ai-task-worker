import io

import pypdf
from fastapi.testclient import TestClient

from taskworker.sandbox.app import STATE, app

FORM = dict(vendor_id="V-101", invoice_number="NW-2347", invoice_date="2026-10-03", amount="186912.00",
            currency="INR", due_date="2026-11-02", po_number="", notes="")


def client(chaos=None) -> TestClient:
    STATE.reset(chaos)
    c = TestClient(app)
    c.post("/erp/login", data={"username": "ap.clerk", "password": "ledger-demo-2026", "next": "/erp/invoices"})
    return c


def test_invoice_pdf_has_extractable_total():
    c = client()
    text = pypdf.PdfReader(io.BytesIO(c.get("/mail/a/a03/NW-2347.pdf").content)).pages[0].extract_text()
    assert "TOTAL DUE INR 1,86,912.00" in text and "02 Nov 2026" in text


def test_validation_rejects_formatted_amount_and_dates():
    c = client()
    r = c.post("/erp/invoices", data=FORM | {"amount": "1,86,912.00", "due_date": "02/11/2026"})
    assert r.status_code == 400 and "plain number" in r.text and "YYYY-MM-DD" in r.text


def test_duplicate_rejected():
    c = client()
    assert c.post("/erp/invoices", data=FORM).status_code == 200
    r = c.post("/erp/invoices", data=FORM)
    assert r.status_code == 400 and "Duplicate invoice" in r.text


def test_fail_after_commit_creates_record_but_returns_503():
    c = client({"create_fail_after_commit": 1})
    r = c.post("/erp/invoices", data=FORM)
    assert r.status_code == 503
    assert any(i["invoice_number"] == "NW-2347" for i in c.get("/__state").json()["invoices"])


def test_session_expiry_redirects_to_login():
    c = client({"session_ttl_requests": 2})  # the post-login redirect is request 1
    c.get("/erp/invoices")
    r = c.get("/erp/invoices")
    assert "/erp/login" in str(r.url) and "expired" in r.text
