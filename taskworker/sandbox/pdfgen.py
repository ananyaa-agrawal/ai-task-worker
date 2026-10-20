"""Render invoice PDFs for email attachments."""

from __future__ import annotations

from fpdf import FPDF

from .data import InvoiceDoc


def format_money(amount: float, currency: str) -> str:
    """Format like a real invoice would: Indian digit grouping for INR."""
    if currency != "INR":
        return f"{currency} {amount:,.2f}"
    whole, frac = f"{amount:.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"INR {whole}.{frac}"


def _d(d) -> str:
    return d.strftime("%d %b %Y")


def render_invoice_pdf(inv: InvoiceDoc) -> bytes:
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(True, margin=15)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 9, inv.vendor_legal_name, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(0, 5, inv.vendor_address, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)
    pdf.set_font("Helvetica", "B", 20)
    pdf.cell(0, 10, "TAX INVOICE" if inv.tax_rate else "INVOICE", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    meta = [
        ("Invoice No.", inv.invoice_number),
        ("Invoice Date", _d(inv.invoice_date)),
    ]
    if inv.due_date:
        meta.append(("Due Date", _d(inv.due_date)))
    if inv.payment_terms:
        meta.append(("Payment Terms", inv.payment_terms))
    if inv.po_number:
        meta.append(("Customer PO", inv.po_number))
    meta.append(("Bill To", "Acme Robotics Pvt Ltd, Hinjewadi Phase 2, Pune 411057"))
    for k, v in meta:
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(35, 6, k)
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(0, 6, v, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)

    pdf.set_font("Helvetica", "B", 10)
    pdf.set_fill_color(235, 235, 235)
    pdf.cell(95, 7, "Description", border=1, fill=True)
    pdf.cell(20, 7, "Qty", border=1, fill=True, align="R")
    pdf.cell(35, 7, "Unit price", border=1, fill=True, align="R")
    pdf.cell(40, 7, "Amount", border=1, fill=True, align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    for item in inv.items:
        qty = f"{item.qty:g}"
        pdf.cell(95, 7, item.description, border=1)
        pdf.cell(20, 7, qty, border=1, align="R")
        pdf.cell(35, 7, format_money(item.unit_price, inv.currency), border=1, align="R")
        pdf.cell(40, 7, format_money(item.total, inv.currency), border=1, align="R",
                 new_x="LMARGIN", new_y="NEXT")

    def total_row(label: str, value: float, bold: bool = False) -> None:
        pdf.set_font("Helvetica", "B" if bold else "", 10)
        pdf.cell(150, 7, label, align="R")
        pdf.cell(40, 7, format_money(value, inv.currency), align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.ln(2)
    total_row("Subtotal", inv.subtotal)
    if inv.tax_rate:
        total_row(f"{inv.tax_label} @ {inv.tax_rate * 100:g}%", inv.tax)
    total_row("TOTAL DUE", inv.total, bold=True)

    if inv.bank_line:
        pdf.ln(8)
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(0, 5, f"Remit to: {inv.bank_line}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    pdf.set_font("Helvetica", "I", 8)
    pdf.cell(0, 5, "This is a computer generated invoice.", new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())
