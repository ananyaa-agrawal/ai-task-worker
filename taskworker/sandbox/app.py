"""Simulated company intranet: Mailroom (email), LedgerLite (accounts payable) and Teamspace (chat).

The worker only ever sees these apps through a browser, exactly as a human employee would.
Endpoints under /__ are for the test harness (reset, state inspection, fault injection)
and are blocked by the worker's browser policy.
"""

from __future__ import annotations

import re
import secrets
import threading
from dataclasses import asdict, dataclass
from datetime import date, datetime
from html import escape
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .data import ChatMessage, ErpInvoice, Vendor, World, fresh_world
from .pdfgen import render_invoice_pdf

ERP_USERNAME = "ap.clerk"
ERP_PASSWORD = "ledger-demo-2026"  # fictional credential for the local sandbox only
SESSION_COOKIE = "ll_session"


@dataclass
class Chaos:
    """Fault injection, configured per scenario through POST /__reset."""

    create_fail_after_commit: int = 0  # next N invoice creations commit, then return 503
    create_fail_before_commit: int = 0  # next N invoice creations return 503 without committing
    session_ttl_requests: int = 0  # ERP session expires after N authenticated requests (0 = never)
    erp_get_503_every: int = 0  # every Nth ERP page load returns a transient 503 (0 = never)


class State:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.reset()

    def reset(self, chaos: dict | None = None) -> None:
        self.world: World = fresh_world()
        self.chaos = Chaos(**(chaos or {}))
        self.sessions: dict[str, dict] = {}
        self.erp_get_counter = 0


STATE = State()
app = FastAPI(title="Acme intranet sandbox", docs_url=None, redoc_url=None)


# --------------------------------------------------------------------------- layout

CSS = """
*{box-sizing:border-box} body{margin:0;font:14px/1.45 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
color:#1d2433;background:#f4f6f9} a{color:var(--accent)} header{background:var(--accent);color:#fff;
padding:10px 22px;display:flex;align-items:center;gap:22px} header .brand{font-weight:700;font-size:17px}
header nav a{color:#fff;opacity:.9;text-decoration:none;margin-right:14px} header .user{margin-left:auto;opacity:.9}
main{max-width:1080px;margin:22px auto;padding:0 18px} .card{background:#fff;border:1px solid #dde2ea;
border-radius:8px;padding:18px 20px;margin-bottom:16px} h1{font-size:21px;margin:0 0 14px}
h2{font-size:16px;margin:0 0 10px} table{border-collapse:collapse;width:100%} th,td{text-align:left;
padding:7px 9px;border-bottom:1px solid #e8ebf0;vertical-align:top} th{font-size:12px;color:#5b6577;
text-transform:uppercase;letter-spacing:.03em} tr.unread td{font-weight:600} .muted{color:#6b7385}
.flash{padding:10px 14px;border-radius:6px;margin-bottom:14px} .flash.ok{background:#e5f6ea;border:1px solid #9fd8b0}
.flash.error{background:#fdecec;border:1px solid #f0a8a8} label{display:block;font-weight:600;margin:10px 0 4px}
input[type=text],input[type=password],select,textarea{width:100%;max-width:420px;padding:7px 9px;border:1px solid #c5ccd8;
border-radius:5px;font:inherit} textarea{max-width:100%} .hint{font-size:12px;color:#6b7385;font-weight:400}
button{background:var(--accent);color:#fff;border:0;border-radius:5px;padding:8px 14px;font:inherit;cursor:pointer}
button.danger{background:#b42318} .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;background:#eef1f6}
.badge.Open{background:#fff4d6} .badge.Paid{background:#e5f6ea} .badge.Payment{background:#e3ecff}
dl{display:grid;grid-template-columns:170px 1fr;gap:6px 12px;margin:0} dt{color:#5b6577} dd{margin:0}
pre.body{white-space:pre-wrap;font:inherit;margin:0} .apps{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}
.msg{padding:8px 0;border-bottom:1px solid #eef0f4} .msg b{margin-right:8px}
"""

ACCENTS = {"mail": "#1f5fbf", "erp": "#16794c", "chat": "#6c3fc4", "home": "#334155"}
NAV = {
    "mail": '<nav><a href="/mail">Inbox</a></nav>',
    "erp": '<nav><a href="/erp/invoices">Invoices</a><a href="/erp/invoices/new">New invoice</a>'
           '<a href="/erp/vendors">Vendors</a></nav>',
    "chat": '<nav><a href="/chat/c/finance">#finance</a><a href="/chat/c/general">#general</a></nav>',
    "home": "",
}
BRANDS = {"mail": "Mailroom", "erp": "LedgerLite AP", "chat": "Teamspace", "home": "Acme Robotics Intranet"}


def page(app_key: str, title: str, body: str, *, user: str = "", status: int = 200,
         flash: str = "", error: str | list[str] = "") -> HTMLResponse:
    notices = ""
    if flash:
        notices += f'<div class="flash ok" role="status">{escape(flash)}</div>'
    if error:
        errs = error if isinstance(error, list) else [error]
        items = "".join(f"<li>{escape(e)}</li>" for e in errs)
        notices += f'<div class="flash error" role="alert"><ul style="margin:0;padding-left:18px">{items}</ul></div>'
    user_html = f'<span class="user">{escape(user)}</span>' if user else ""
    if app_key == "erp" and user:
        user_html = (f'<span class="user">Signed in as {escape(user)} '
                     '<form method="post" action="/erp/logout" style="display:inline">'
                     '<button style="background:transparent;padding:0 0 0 8px;text-decoration:underline">'
                     'Sign out</button></form></span>')
    html = f"""<!doctype html><html><head><meta charset="utf-8"><title>{escape(title)} · {BRANDS[app_key]}</title>
<style>:root{{--accent:{ACCENTS[app_key]}}}{CSS}</style></head><body>
<header><a href="/" class="brand" style="color:#fff;text-decoration:none">{BRANDS[app_key]}</a>{NAV[app_key]}{user_html}</header>
<main>{notices}{body}</main></body></html>"""
    return HTMLResponse(html, status_code=status)


def service_unavailable(app_key: str, detail: str) -> HTMLResponse:
    body = (f'<div class="card"><h1>503 - Service temporarily unavailable</h1><p>{escape(detail)}</p>'
            '<p class="muted">Reference: upstream gateway timeout. Please try again shortly.</p></div>')
    return page(app_key, "Service unavailable", body, status=503)


# --------------------------------------------------------------------------- home

@app.get("/", response_class=HTMLResponse)
def home():
    apps = [
        ("/mail", "Mailroom", "Accounts payable mailbox (ap@acme-robotics.example)"),
        ("/erp/invoices", "LedgerLite AP", "Vendor invoices, payments and vendor master data"),
        ("/chat/c/finance", "Teamspace", "Team chat (#finance, #general)"),
    ]
    cards = "".join(f'<div class="card"><h2><a href="{u}">{n}</a></h2><p class="muted">{d}</p></div>'
                    for u, n, d in apps)
    return page("home", "Home", f'<h1>Acme Robotics Pvt Ltd - Intranet</h1><div class="apps">{cards}</div>')


# --------------------------------------------------------------------------- mail

def _fmt_dt(dt: datetime) -> str:
    return dt.strftime("%d %b %Y, %H:%M")


@app.get("/mail", response_class=HTMLResponse)
def mail_inbox(q: str = ""):
    with STATE.lock:
        emails = sorted(STATE.world.emails, key=lambda e: e.sent_at, reverse=True)
        if q:
            ql = q.lower()
            emails = [e for e in emails if ql in (e.subject + e.sender_name + e.sender_email + e.body).lower()]
        rows = "".join(
            f'<tr class="{"" if e.read else "unread"}"><td>{escape(e.sender_name)}<br>'
            f'<span class="muted" style="font-weight:400">{escape(e.sender_email)}</span></td>'
            f'<td><a href="/mail/m/{e.id}">{escape(e.subject)}</a>{" 📎" if e.attachments else ""}</td>'
            f'<td style="white-space:nowrap">{_fmt_dt(e.sent_at)}</td></tr>'
            for e in emails)
    search = (f'<form method="get" action="/mail" class="row" style="margin-bottom:12px">'
              f'<input type="text" name="q" value="{escape(q)}" placeholder="Search mail" aria-label="Search mail">'
              f'<button type="submit">Search</button></form>')
    empty = '<p class="muted">No messages match your search.</p>' if not rows else ""
    body = (f'<div class="card"><h1>Inbox</h1>{search}<table><thead><tr><th>From</th><th>Subject</th>'
            f'<th>Received</th></tr></thead><tbody>{rows}</tbody></table>{empty}</div>')
    return page("mail", "Inbox", body, user="ap@acme-robotics.example")


@app.get("/mail/m/{mid}", response_class=HTMLResponse)
def mail_message(mid: str):
    with STATE.lock:
        email = next((e for e in STATE.world.emails if e.id == mid), None)
        if not email:
            return page("mail", "Not found", '<div class="card"><h1>Message not found</h1></div>', status=404)
        email.read = True
        atts = "".join(
            f'<li><a href="/mail/a/{a.id}/{quote(a.filename)}">{escape(a.filename)}</a> '
            f'<span class="muted">(PDF)</span></li>' for a in email.attachments)
    att_html = f'<h2 style="margin-top:16px">Attachments</h2><ul>{atts}</ul>' if atts else ""
    body = f"""<div class="card"><p><a href="/mail">&larr; Back to inbox</a></p><h1>{escape(email.subject)}</h1>
<dl><dt>From</dt><dd>{escape(email.sender_name)} &lt;{escape(email.sender_email)}&gt;</dd>
<dt>To</dt><dd>ap@acme-robotics.example</dd><dt>Date</dt><dd>{_fmt_dt(email.sent_at)}</dd></dl>
<hr style="border:0;border-top:1px solid #e8ebf0;margin:14px 0"><pre class="body">{escape(email.body)}</pre>{att_html}</div>"""
    return page("mail", email.subject, body, user="ap@acme-robotics.example")


@app.get("/mail/a/{aid}/{filename}")
def mail_attachment(aid: str, filename: str):
    with STATE.lock:
        att = next((a for e in STATE.world.emails for a in e.attachments if a.id == aid), None)
    if not att:
        return Response("Not found", status_code=404)
    return Response(render_invoice_pdf(att.invoice), media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{att.filename}"'})


# --------------------------------------------------------------------------- erp: auth

def _erp_user(request: Request) -> tuple[str | None, str]:
    """Return (user, reason). Counts the request against the session TTL."""
    token = request.cookies.get(SESSION_COOKIE)
    sess = STATE.sessions.get(token or "")
    if not sess:
        return None, "Please sign in to continue."
    sess["count"] += 1
    ttl = STATE.chaos.session_ttl_requests
    if ttl and sess["count"] > ttl:
        STATE.sessions.pop(token, None)
        return None, "Your session has expired. Please sign in again."
    return sess["user"], ""


def _login_redirect(request: Request, reason: str) -> RedirectResponse:
    nxt = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    if request.method != "GET":
        nxt = "/erp/invoices"
    return RedirectResponse(f"/erp/login?next={quote(nxt)}&reason={quote(reason)}", status_code=303)


def _transient_get_failure() -> HTMLResponse | None:
    every = STATE.chaos.erp_get_503_every
    if not every:
        return None
    STATE.erp_get_counter += 1
    if STATE.erp_get_counter % every == 0:
        return service_unavailable("erp", "LedgerLite is under heavy load.")
    return None


def _audit(user: str, action: str, target: str, **details) -> None:
    STATE.world.audit.append({"ts": datetime.now().isoformat(timespec="seconds"), "user": user,
                              "action": action, "target": target, "details": details})


@app.get("/erp", include_in_schema=False)
def erp_root():
    return RedirectResponse("/erp/invoices", status_code=303)


@app.get("/erp/login", response_class=HTMLResponse)
def erp_login_form(next: str = "/erp/invoices", reason: str = "", error: str = ""):
    body = f"""<div class="card" style="max-width:460px"><h1>Sign in to LedgerLite</h1>
<form method="post" action="/erp/login"><input type="hidden" name="next" value="{escape(next)}">
<label for="username">Username</label><input type="text" id="username" name="username" autocomplete="username">
<label for="password">Password</label><input type="password" id="password" name="password" autocomplete="current-password">
<div style="margin-top:14px"><button type="submit">Sign in</button></div></form></div>"""
    return page("erp", "Sign in", body, flash=reason if reason and not error else "", error=error)


@app.post("/erp/login")
def erp_login(username: str = Form(""), password: str = Form(""), next: str = Form("/erp/invoices")):
    if username.strip() != ERP_USERNAME or password != ERP_PASSWORD:
        return erp_login_form(next=next, error="Invalid username or password.")
    token = secrets.token_hex(16)
    with STATE.lock:
        STATE.sessions[token] = {"user": ERP_USERNAME, "count": 0}
    if not next.startswith("/erp"):
        next = "/erp/invoices"
    resp = RedirectResponse(next, status_code=303)
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax")
    return resp


@app.post("/erp/logout")
def erp_logout(request: Request):
    STATE.sessions.pop(request.cookies.get(SESSION_COOKIE, ""), None)
    resp = RedirectResponse("/erp/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


# --------------------------------------------------------------------------- erp: invoices

def _vendor(world: World, vid: str) -> Vendor | None:
    return next((v for v in world.vendors if v.id == vid), None)


def _money(inv: ErpInvoice) -> str:
    return f"{inv.amount:,.2f} {inv.currency}"


@app.get("/erp/invoices", response_class=HTMLResponse)
def erp_invoices(request: Request, q: str = "", status: str = "", created: str = ""):
    with STATE.lock:
        user, reason = _erp_user(request)
        if not user:
            return _login_redirect(request, reason)
        if fail := _transient_get_failure():
            return fail
        world = STATE.world
        invs = sorted(world.invoices, key=lambda i: i.id, reverse=True)
        if q:
            ql = q.lower()
            invs = [i for i in invs if ql in i.invoice_number.lower()
                    or ql in (_vendor(world, i.vendor_id).name.lower())]
        if status:
            invs = [i for i in invs if i.status == status]
        rows = "".join(
            f'<tr><td><a href="/erp/invoices/{i.id}">{i.id}</a></td><td>{escape(_vendor(world, i.vendor_id).name)}</td>'
            f'<td>{escape(i.invoice_number)}</td><td>{i.invoice_date}</td><td style="text-align:right">{_money(i)}</td>'
            f'<td>{i.due_date}</td><td><span class="badge {i.status.split()[0]}">{i.status}</span></td></tr>'
            for i in invs)
    statuses = "".join(f'<option value="{s}" {"selected" if s == status else ""}>{s or "Any status"}</option>'
                       for s in ["", "Open", "Payment scheduled", "Paid"])
    search = (f'<form method="get" action="/erp/invoices" class="row" style="margin-bottom:12px">'
              f'<input type="text" name="q" value="{escape(q)}" placeholder="Vendor or invoice number" '
              f'aria-label="Search invoices" style="max-width:280px">'
              f'<select name="status" aria-label="Status" style="max-width:180px">{statuses}</select>'
              f'<button type="submit">Filter</button><a href="/erp/invoices/new" style="margin-left:auto">'
              f'+ New invoice</a></form>')
    empty = '<p class="muted">No invoices found.</p>' if not rows else ""
    body = (f'<div class="card"><h1>Vendor invoices</h1>{search}<table><thead><tr><th>ID</th><th>Vendor</th>'
            f'<th>Invoice #</th><th>Invoice date</th><th style="text-align:right">Amount</th><th>Due date</th>'
            f'<th>Status</th></tr></thead><tbody>{rows}</tbody></table>{empty}</div>')
    return page("erp", "Invoices", body, user=user)


def _invoice_form(world: World, values: dict, action: str, submit_label: str, title: str) -> str:
    opts = '<option value="">- Select vendor -</option>' + "".join(
        f'<option value="{v.id}" {"selected" if values.get("vendor_id") == v.id else ""}>{escape(v.name)}</option>'
        for v in world.vendors)
    cur = "".join(f'<option {"selected" if values.get("currency", "INR") == c else ""}>{c}</option>'
                  for c in ["INR", "USD", "EUR"])

    def field(name: str, label: str, hint: str = "") -> str:
        h = f' <span class="hint">{hint}</span>' if hint else ""
        return (f'<label for="{name}">{label}{h}</label><input type="text" id="{name}" name="{name}" '
                f'value="{escape(str(values.get(name, "")))}">')

    return f"""<div class="card"><h1>{title}</h1><form method="post" action="{action}">
<label for="vendor_id">Vendor</label><select id="vendor_id" name="vendor_id">{opts}</select>
<p class="hint" style="margin:4px 0 0">Vendor not listed? It must be onboarded under <a href="/erp/vendors">Vendors</a> first.</p>
{field("invoice_number", "Invoice number")}
{field("invoice_date", "Invoice date", "(YYYY-MM-DD)")}
{field("amount", "Total amount (incl. tax)", "(number only, e.g. 1234.50)")}
<label for="currency">Currency</label><select id="currency" name="currency" style="max-width:120px">{cur}</select>
{field("due_date", "Due date", "(YYYY-MM-DD)")}
{field("po_number", "PO number", "(optional)")}
<label for="notes">Notes <span class="hint">(optional)</span></label><textarea id="notes" name="notes" rows="2">{escape(values.get("notes", ""))}</textarea>
<div style="margin-top:16px"><button type="submit">{submit_label}</button></div></form></div>"""


@app.get("/erp/invoices/new", response_class=HTMLResponse)
def erp_new_invoice(request: Request):
    with STATE.lock:
        user, reason = _erp_user(request)
        if not user:
            return _login_redirect(request, reason)
        if fail := _transient_get_failure():
            return fail
        form = _invoice_form(STATE.world, {}, "/erp/invoices", "Save invoice", "New vendor invoice")
    return page("erp", "New invoice", form, user=user)


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
AMOUNT_RE = re.compile(r"^\d+(\.\d{1,2})?$")


def _validate_invoice(world: World, v: dict, existing_id: str | None = None) -> list[str]:
    errors = []
    if not _vendor(world, v["vendor_id"]):
        errors.append("Please select a vendor.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/_-]{1,30}", v["invoice_number"]):
        errors.append("Invoice number is required (letters, digits, '-', '/' or '_').")
    dates = {}
    for key, label in [("invoice_date", "Invoice date"), ("due_date", "Due date")]:
        if not DATE_RE.match(v[key]):
            errors.append(f"{label} must be in YYYY-MM-DD format.")
            continue
        try:
            dates[key] = date.fromisoformat(v[key])
        except ValueError:
            errors.append(f"{label} is not a valid calendar date.")
    if len(dates) == 2 and dates["due_date"] < dates["invoice_date"]:
        errors.append("Due date cannot be before the invoice date.")
    if not AMOUNT_RE.match(v["amount"]):
        errors.append("Amount must be a plain number like 1234.50 (no currency symbols or thousands separators).")
    elif float(v["amount"]) <= 0:
        errors.append("Amount must be greater than zero.")
    if v["currency"] not in {"INR", "USD", "EUR"}:
        errors.append("Unsupported currency.")
    if not errors:
        dup = next((i for i in world.invoices if i.vendor_id == v["vendor_id"] and i.id != existing_id
                    and i.invoice_number.lower() == v["invoice_number"].lower()), None)
        if dup:
            errors.append(f"Duplicate invoice: {v['invoice_number']} already exists for this vendor ({dup.id}).")
    return errors


@app.post("/erp/invoices", response_class=HTMLResponse)
def erp_create_invoice(request: Request, vendor_id: str = Form(""), invoice_number: str = Form(""),
                       invoice_date: str = Form(""), amount: str = Form(""), currency: str = Form("INR"),
                       due_date: str = Form(""), po_number: str = Form(""), notes: str = Form("")):
    values = {k: v.strip() for k, v in dict(vendor_id=vendor_id, invoice_number=invoice_number,
                                             invoice_date=invoice_date, amount=amount, currency=currency,
                                             due_date=due_date, po_number=po_number, notes=notes).items()}
    with STATE.lock:
        user, reason = _erp_user(request)
        if not user:
            return _login_redirect(request, reason)
        world, chaos = STATE.world, STATE.chaos
        if chaos.create_fail_before_commit > 0:
            chaos.create_fail_before_commit -= 1
            return service_unavailable("erp", "The invoice service did not respond in time. "
                                              "Your request may or may not have been processed.")
        errors = _validate_invoice(world, values)
        if errors:
            form = _invoice_form(world, values, "/erp/invoices", "Save invoice", "New vendor invoice")
            return page("erp", "New invoice", form, user=user, error=errors, status=400)
        world.next_invoice_seq += 1
        inv = ErpInvoice(id=f"INV-{world.next_invoice_seq}", vendor_id=values["vendor_id"],
                         invoice_number=values["invoice_number"], invoice_date=values["invoice_date"],
                         amount=float(values["amount"]), currency=values["currency"], due_date=values["due_date"],
                         status="Open", po_number=values["po_number"], notes=values["notes"],
                         created_at=datetime.now().strftime("%Y-%m-%d %H:%M"), created_by=user)
        world.invoices.append(inv)
        _audit(user, "invoice.create", inv.id, invoice_number=inv.invoice_number, amount=inv.amount)
        if chaos.create_fail_after_commit > 0:
            chaos.create_fail_after_commit -= 1
            return service_unavailable("erp", "The invoice service did not respond in time. "
                                              "Your request may or may not have been processed.")
    return RedirectResponse(f"/erp/invoices/{inv.id}?created=1", status_code=303)


@app.get("/erp/invoices/{iid}", response_class=HTMLResponse)
def erp_invoice_detail(request: Request, iid: str, created: str = "", msg: str = ""):
    with STATE.lock:
        user, reason = _erp_user(request)
        if not user:
            return _login_redirect(request, reason)
        if fail := _transient_get_failure():
            return fail
        world = STATE.world
        inv = next((i for i in world.invoices if i.id == iid), None)
        if not inv:
            return page("erp", "Not found", '<div class="card"><h1>Invoice not found</h1></div>', user=user, status=404)
        vendor = _vendor(world, inv.vendor_id)
    actions = f'<a href="/erp/invoices/{inv.id}/edit">Edit invoice</a>'
    if inv.status == "Open":
        actions += (f'<form method="post" action="/erp/invoices/{inv.id}/schedule-payment">'
                    f'<button type="submit">Schedule payment</button></form>')
    actions += (f'<form method="post" action="/erp/invoices/{inv.id}/delete">'
                f'<button type="submit" class="danger">Delete invoice</button></form>')
    body = f"""<div class="card"><p><a href="/erp/invoices">&larr; All invoices</a></p><h1>Invoice {inv.id}</h1>
<dl><dt>Vendor</dt><dd><a href="/erp/vendors/{vendor.id}">{escape(vendor.name)}</a></dd>
<dt>Invoice number</dt><dd>{escape(inv.invoice_number)}</dd><dt>Invoice date</dt><dd>{inv.invoice_date}</dd>
<dt>Amount</dt><dd>{_money(inv)}</dd><dt>Due date</dt><dd>{inv.due_date}</dd>
<dt>Status</dt><dd><span class="badge {inv.status.split()[0]}">{inv.status}</span></dd>
<dt>PO number</dt><dd>{escape(inv.po_number) or "-"}</dd><dt>Notes</dt><dd>{escape(inv.notes) or "-"}</dd>
<dt>Entered</dt><dd>{inv.created_at} by {escape(inv.created_by)}</dd></dl>
<div class="row" style="margin-top:18px">{actions}</div></div>"""
    flash = msg or ("Invoice saved." if created else "")
    return page("erp", f"Invoice {inv.id}", body, user=user, flash=flash)


@app.get("/erp/invoices/{iid}/edit", response_class=HTMLResponse)
def erp_edit_invoice_form(request: Request, iid: str):
    with STATE.lock:
        user, reason = _erp_user(request)
        if not user:
            return _login_redirect(request, reason)
        inv = next((i for i in STATE.world.invoices if i.id == iid), None)
        if not inv:
            return page("erp", "Not found", '<div class="card"><h1>Invoice not found</h1></div>', user=user, status=404)
        values = asdict(inv) | {"amount": f"{inv.amount:.2f}"}
        form = _invoice_form(STATE.world, values, f"/erp/invoices/{iid}/edit", "Save changes", f"Edit {iid}")
    return page("erp", f"Edit {iid}", form, user=user)


@app.post("/erp/invoices/{iid}/edit", response_class=HTMLResponse)
def erp_edit_invoice(request: Request, iid: str, vendor_id: str = Form(""), invoice_number: str = Form(""),
                     invoice_date: str = Form(""), amount: str = Form(""), currency: str = Form("INR"),
                     due_date: str = Form(""), po_number: str = Form(""), notes: str = Form("")):
    values = {k: v.strip() for k, v in dict(vendor_id=vendor_id, invoice_number=invoice_number,
                                             invoice_date=invoice_date, amount=amount, currency=currency,
                                             due_date=due_date, po_number=po_number, notes=notes).items()}
    with STATE.lock:
        user, reason = _erp_user(request)
        if not user:
            return _login_redirect(request, reason)
        inv = next((i for i in STATE.world.invoices if i.id == iid), None)
        if not inv:
            return page("erp", "Not found", '<div class="card"><h1>Invoice not found</h1></div>', user=user, status=404)
        errors = _validate_invoice(STATE.world, values, existing_id=iid)
        if errors:
            form = _invoice_form(STATE.world, values, f"/erp/invoices/{iid}/edit", "Save changes", f"Edit {iid}")
            return page("erp", f"Edit {iid}", form, user=user, error=errors, status=400)
        before = asdict(inv)
        inv.vendor_id, inv.invoice_number = values["vendor_id"], values["invoice_number"]
        inv.invoice_date, inv.amount, inv.currency = values["invoice_date"], float(values["amount"]), values["currency"]
        inv.due_date, inv.po_number, inv.notes = values["due_date"], values["po_number"], values["notes"]
        _audit(user, "invoice.update", iid, before=before)
    return RedirectResponse(f"/erp/invoices/{iid}?msg=Invoice+updated.", status_code=303)


@app.post("/erp/invoices/{iid}/schedule-payment")
def erp_schedule_payment(request: Request, iid: str):
    with STATE.lock:
        user, reason = _erp_user(request)
        if not user:
            return _login_redirect(request, reason)
        inv = next((i for i in STATE.world.invoices if i.id == iid), None)
        if not inv or inv.status != "Open":
            return RedirectResponse(f"/erp/invoices/{iid}?msg=Only+open+invoices+can+be+scheduled.", status_code=303)
        inv.status = "Payment scheduled"
        _audit(user, "invoice.schedule_payment", iid)
    return RedirectResponse(f"/erp/invoices/{iid}?msg=Payment+scheduled+for+the+next+payment+run.", status_code=303)


@app.post("/erp/invoices/{iid}/delete")
def erp_delete_invoice(request: Request, iid: str):
    with STATE.lock:
        user, reason = _erp_user(request)
        if not user:
            return _login_redirect(request, reason)
        STATE.world.invoices = [i for i in STATE.world.invoices if i.id != iid]
        _audit(user, "invoice.delete", iid)
    return RedirectResponse("/erp/invoices", status_code=303)


# --------------------------------------------------------------------------- erp: vendors

@app.get("/erp/vendors", response_class=HTMLResponse)
def erp_vendors(request: Request):
    with STATE.lock:
        user, reason = _erp_user(request)
        if not user:
            return _login_redirect(request, reason)
        if fail := _transient_get_failure():
            return fail
        rows = "".join(f'<tr><td><a href="/erp/vendors/{v.id}">{v.id}</a></td><td>{escape(v.name)}</td>'
                       f'<td>{escape(v.email)}</td><td>{v.currency}</td></tr>' for v in STATE.world.vendors)
    body = (f'<div class="card"><div class="row"><h1>Vendors</h1><a href="/erp/vendors/new" style="margin-left:auto">'
            f'+ Onboard vendor</a></div><table><thead><tr><th>ID</th><th>Name</th><th>Billing email</th>'
            f'<th>Currency</th></tr></thead><tbody>{rows}</tbody></table></div>')
    return page("erp", "Vendors", body, user=user)


@app.get("/erp/vendors/new", response_class=HTMLResponse)
def erp_new_vendor_form(request: Request):
    with STATE.lock:
        user, reason = _erp_user(request)
        if not user:
            return _login_redirect(request, reason)
    body = """<div class="card"><h1>Onboard vendor</h1><form method="post" action="/erp/vendors">
<label for="name">Legal name</label><input type="text" id="name" name="name">
<label for="email">Billing email</label><input type="text" id="email" name="email">
<label for="currency">Currency</label><select id="currency" name="currency" style="max-width:120px"><option>INR</option><option>USD</option><option>EUR</option></select>
<label for="bank_account">Bank account number</label><input type="text" id="bank_account" name="bank_account">
<label for="ifsc">IFSC / SWIFT</label><input type="text" id="ifsc" name="ifsc">
<div style="margin-top:16px"><button type="submit">Create vendor</button></div></form></div>"""
    return page("erp", "Onboard vendor", body, user=user)


@app.post("/erp/vendors")
def erp_create_vendor(request: Request, name: str = Form(""), email: str = Form(""), currency: str = Form("INR"),
                      bank_account: str = Form(""), ifsc: str = Form("")):
    with STATE.lock:
        user, reason = _erp_user(request)
        if not user:
            return _login_redirect(request, reason)
        if not name.strip():
            return page("erp", "Onboard vendor", '<div class="card"><a href="/erp/vendors/new">Back</a></div>',
                        user=user, error="Legal name is required.", status=400)
        world = STATE.world
        world.next_vendor_seq += 1
        v = Vendor(f"V-{world.next_vendor_seq}", name.strip(), email.strip(), currency, bank_account.strip(), ifsc.strip())
        world.vendors.append(v)
        _audit(user, "vendor.create", v.id, name=v.name)
    return RedirectResponse(f"/erp/vendors/{v.id}?msg=Vendor+created.", status_code=303)


@app.get("/erp/vendors/{vid}", response_class=HTMLResponse)
def erp_vendor_detail(request: Request, vid: str, msg: str = ""):
    with STATE.lock:
        user, reason = _erp_user(request)
        if not user:
            return _login_redirect(request, reason)
        v = _vendor(STATE.world, vid)
        if not v:
            return page("erp", "Not found", '<div class="card"><h1>Vendor not found</h1></div>', user=user, status=404)
        invs = [i for i in STATE.world.invoices if i.vendor_id == vid]
        rows = "".join(f'<tr><td><a href="/erp/invoices/{i.id}">{i.id}</a></td><td>{escape(i.invoice_number)}</td>'
                       f'<td>{_money(i)}</td><td>{i.due_date}</td><td>{i.status}</td></tr>' for i in invs)
    body = f"""<div class="card"><p><a href="/erp/vendors">&larr; All vendors</a></p><h1>{escape(v.name)}</h1>
<dl><dt>Vendor ID</dt><dd>{v.id}</dd><dt>Billing email</dt><dd>{escape(v.email)}</dd><dt>Currency</dt><dd>{v.currency}</dd>
<dt>Bank account</dt><dd>{escape(v.bank_account)}</dd><dt>IFSC / SWIFT</dt><dd>{escape(v.ifsc)}</dd></dl></div>
<div class="card"><h2>Invoices</h2><table><thead><tr><th>ID</th><th>Invoice #</th><th>Amount</th><th>Due</th><th>Status</th>
</tr></thead><tbody>{rows}</tbody></table></div>
<div class="card"><h2>Update bank details</h2><form method="post" action="/erp/vendors/{v.id}/bank">
<label for="bank_account">Bank account number</label><input type="text" id="bank_account" name="bank_account">
<label for="ifsc">IFSC / SWIFT</label><input type="text" id="ifsc" name="ifsc">
<div style="margin-top:14px"><button type="submit" class="danger">Save bank details</button></div></form></div>"""
    return page("erp", v.name, body, user=user, flash=msg)


@app.post("/erp/vendors/{vid}/bank")
def erp_update_bank(request: Request, vid: str, bank_account: str = Form(""), ifsc: str = Form("")):
    with STATE.lock:
        user, reason = _erp_user(request)
        if not user:
            return _login_redirect(request, reason)
        v = _vendor(STATE.world, vid)
        if v:
            _audit(user, "vendor.update_bank", vid, before=[v.bank_account, v.ifsc], after=[bank_account, ifsc])
            v.bank_account, v.ifsc = bank_account.strip(), ifsc.strip()
    return RedirectResponse(f"/erp/vendors/{vid}?msg=Bank+details+updated.", status_code=303)


# --------------------------------------------------------------------------- chat

CHANNELS = ["finance", "general"]


@app.get("/chat", include_in_schema=False)
def chat_root():
    return RedirectResponse("/chat/c/finance", status_code=303)


@app.get("/chat/c/{channel}", response_class=HTMLResponse)
def chat_channel(channel: str, msg: str = ""):
    if channel not in CHANNELS:
        return page("chat", "Not found", '<div class="card"><h1>Channel not found</h1></div>', status=404)
    with STATE.lock:
        msgs = [m for m in STATE.world.chat if m.channel == channel]
    items = "".join(f'<div class="msg"><b>{escape(m.author)}</b><span class="muted">{m.ts}</span>'
                    f'<pre class="body">{escape(m.text)}</pre></div>' for m in msgs)
    body = f"""<div class="card"><h1>#{channel}</h1>{items or '<p class="muted">No messages yet.</p>'}
<form method="post" action="/chat/c/{channel}" style="margin-top:14px"><label for="text">Message #{channel}</label>
<textarea id="text" name="text" rows="3"></textarea><div style="margin-top:8px"><button type="submit">Post message</button></div>
</form></div>"""
    return page("chat", f"#{channel}", body, user="ap.clerk", flash=msg)


@app.post("/chat/c/{channel}")
def chat_post(channel: str, text: str = Form("")):
    if channel not in CHANNELS:
        return Response("Not found", status_code=404)
    if not text.strip():
        return RedirectResponse(f"/chat/c/{channel}?msg=Message+is+empty.", status_code=303)
    with STATE.lock:
        STATE.world.chat.append(ChatMessage(channel, "ap.clerk (AI worker)", text.strip(),
                                            datetime.now().strftime("%Y-%m-%d %H:%M")))
        _audit("ap.clerk", "chat.post", channel, text=text.strip())
    return RedirectResponse(f"/chat/c/{channel}?msg=Message+posted.", status_code=303)


# --------------------------------------------------------------------------- harness-only endpoints

@app.post("/__reset")
async def reset(request: Request):
    payload = await request.json() if (await request.body()) else {}
    with STATE.lock:
        STATE.reset(chaos=payload.get("chaos"))
    return {"ok": True, "chaos": asdict(STATE.chaos)}


@app.get("/__state")
def state():
    with STATE.lock:
        w = STATE.world
        return JSONResponse({
            "vendors": [asdict(v) for v in w.vendors],
            "invoices": [asdict(i) for i in w.invoices],
            "chat": [asdict(m) for m in w.chat],
            "audit": list(w.audit),
            "emails_read": [e.id for e in w.emails if e.read],
            "chaos": asdict(STATE.chaos),
            "expected_totals": {a.invoice.invoice_number: a.invoice.total
                                for e in w.emails for a in e.attachments},
        })
