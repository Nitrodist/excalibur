"""Extract the summary fields from a Martin-Brower invoice PDF.

The G/L figures are the Net column (before tax). On these invoices the net
amounts plus PST and GST equal Total Order.
"""

import os
import re
import tempfile
from decimal import Decimal
from datetime import datetime

import playa

# Printed on the G/L summary of this supplier's invoices, in statement order.
KNOWN_ACCOUNTS = (
    ("7001-001", "FOOD"),
    ("7030-001", "PAPER"),
    ("7420-001", "OPERATING SUPPLIES"),
    ("7426-001", "TOY PROMOTION"),
    ("7428-001", "GIVE AWAY PROMO"),
)

_MONEY = r"(-?\d[\d,]*\.\d{2}-?)"
_INVOICE_NUMBER = re.compile(r"Invoice\s*#:\s*(\d+)")
_INVOICE_DATE = re.compile(r"Invoice Date:\s*(\d{1,2}/\d{1,2}/\d{4})")
_GST = re.compile(rf"GST\s*/\s*HST\s*/\s*TPS\s+[\d.]+\s*%\s+({_MONEY})")
_PST = re.compile(rf"PST(?:\s*-\s*[A-Z]{{2}})?\s+[\d.]+\s*%\s+({_MONEY})")
_AMOUNT_DUE = re.compile(
    rf"Amount Due\s+(\d{{1,2}}/\d{{1,2}}/\d{{4}})\s+({_MONEY})"
)
_GL_LINE = re.compile(
    rf"^(?P<code>\d{{4}}-\d{{3}})\s+"
    rf"(?P<description>[A-Za-z0-9][A-Za-z0-9 &'/-]*?)\s+"
    rf"(?:(?P<qty>\d+)\s+)?(?P<net>{_MONEY})",
    re.MULTILINE,
)


class InvoiceParseError(Exception):
    """The PDF opened, but it does not have the expected invoice fields."""


def parse_invoice(path):
    """Parse a supplier invoice PDF and return a JSON-ready dict."""
    try:
        text = layout_text(path)
    except Exception as exc:
        raise InvoiceParseError(f"Could not read PDF: {exc}") from exc
    return parse_invoice_text(text)


def parse_invoice_bytes(data):
    """Parse an invoice from PDF bytes."""
    if not data.startswith(b"%PDF"):
        raise InvoiceParseError("File is not a PDF.")
    fd, name = tempfile.mkstemp(suffix=".pdf")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        return parse_invoice(name)
    finally:
        os.unlink(name)


def parse_invoice_text(text):
    """Parse invoice fields from layout-preserved text."""
    invoice_match = _INVOICE_NUMBER.search(text)
    invoice_date_match = _INVOICE_DATE.search(text)
    gst_match = _GST.search(text)
    pst_match = _PST.search(text)
    due_match = _AMOUNT_DUE.search(text)
    accounts, warnings = _accounts(_gl_section(text))

    missing = []
    if not invoice_match:
        missing.append("Invoice #")
    if not invoice_date_match:
        missing.append("Invoice Date")
    if not gst_match:
        missing.append("GST / HST / TPS")
    if not pst_match:
        missing.append("PST")
    if not due_match:
        missing.append("Amount Due date and Total Order")
    if missing:
        raise InvoiceParseError(
            "This PDF does not look like a Martin-Brower invoice. Missing "
            + ", ".join(missing)
            + "."
        )

    record = {
        "invoice_number": invoice_match.group(1),
        "invoice_date": _normalize_date(invoice_date_match.group(1)),
        "pst": format_money(parse_money(pst_match.group(1))),
        "gst_hst_tps": format_money(parse_money(gst_match.group(1))),
        "amount_due_date": _normalize_date(due_match.group(1)),
        "accounts": accounts,
        "total_order": format_money(parse_money(due_match.group(2))),
        "warnings": warnings,
    }
    warning = _reconciliation_warning(record)
    if warning:
        record["warnings"].append(warning)
    return record


def to_json(record):
    import json

    return json.dumps(record, indent=2) + "\n"


def to_csv(record):
    import csv
    import io

    headers = [
        "Invoice Number",
        "Invoice Date",
        "PST",
        "GST / HST / TPS",
        "Amount Due",
    ]
    values = [
        record["invoice_number"],
        record["invoice_date"],
        record["pst"],
        record["gst_hst_tps"],
        record["amount_due_date"],
    ]
    for code, account in record["accounts"].items():
        headers.append(f"{code} {account['description']}")
        values.append(account["net"] or "")
    headers.append("Total Order")
    values.append(record["total_order"])

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(headers)
    writer.writerow(values)
    return buffer.getvalue()


def display_rows(record):
    """Label/value pairs for the web page, with money grouped in thousands."""
    rows = [
        ("Invoice Number", record["invoice_number"]),
        ("Invoice Date", record["invoice_date"]),
        ("PST", _display_money(record["pst"])),
        ("GST / HST / TPS", _display_money(record["gst_hst_tps"])),
        ("Amount Due", record["amount_due_date"]),
    ]
    for code, account in record["accounts"].items():
        net = account["net"]
        rows.append(
            (
                f"{code} {account['description']}",
                _display_money(net) if net else "—",
            )
        )
    rows.append(("Total Order", _display_money(record["total_order"])))
    return rows


def layout_text(path):
    """Rebuild reading order from glyph positions.

    pypdf's plain text extraction interleaves the two columns of these
    invoices. Positions keep the G/L summary on one line per account.
    """
    lines = []
    with playa.open(path) as document:
        for page in document.pages:
            lines.extend(_page_lines(page))
    return "\n".join(lines)


def format_money(value):
    return f"{value.quantize(Decimal('0.01')):.2f}"


def parse_money(raw):
    negative = raw.startswith("-") or raw.endswith("-")
    cleaned = raw.strip("-").replace(",", "")
    value = Decimal(cleaned)
    if negative:
        value = -value
    return value.quantize(Decimal("0.01"))


def _normalize_date(raw):
    parsed = datetime.strptime(raw, "%m/%d/%Y")
    return parsed.strftime("%m/%d/%Y")


def _display_money(value):
    return f"{Decimal(value):,.2f}"


def _gl_section(text):
    marker = "G/L Acct"
    if marker not in text:
        return text
    section = text.split(marker, 1)[1]
    for stop in ("Y - HST", "* - GST", "H - HAZARDOUS"):
        if stop in section:
            section = section.split(stop, 1)[0]
    return section


def _accounts(section):
    found = {}
    for match in _GL_LINE.finditer(section):
        description = re.sub(r"\s+", " ", match.group("description")).strip()
        found[match.group("code")] = (description, parse_money(match.group("net")))

    accounts = {}
    warnings = []
    for code, canonical in KNOWN_ACCOUNTS:
        if code in found:
            description, net = found.pop(code)
            accounts[code] = {
                "description": description,
                "net": format_money(net),
            }
        else:
            accounts[code] = {"description": canonical, "net": None}
            warnings.append(f"{code} {canonical} was not found")
    for code in sorted(found):
        description, net = found[code]
        accounts[code] = {"description": description, "net": format_money(net)}
    return accounts, warnings


def _reconciliation_warning(record):
    nets = []
    for account in record["accounts"].values():
        if account["net"] is None:
            return None
        nets.append(Decimal(account["net"]))
    expected = sum(nets, Decimal("0"))
    expected += Decimal(record["pst"]) + Decimal(record["gst_hst_tps"])
    expected = expected.quantize(Decimal("0.01"))
    total = Decimal(record["total_order"])
    if expected != total:
        return (
            "Net amounts plus PST and GST equal "
            f"{expected:,.2f}, which does not match Total Order {total:,.2f}."
        )
    return None


def _page_lines(page, y_tol=2.0, space_gap=2.2):
    glyphs = []
    for glyph in page.glyphs:
        text = glyph.text
        if not text:
            continue
        x0, y0, x1, _y1 = glyph.bbox
        glyphs.append((y0, x0, x1, text))
    glyphs.sort(key=lambda item: (item[0], item[1]))

    grouped = []
    for y0, x0, x1, text in glyphs:
        if grouped and abs(y0 - grouped[-1][0]) <= y_tol:
            grouped[-1][1].append((x0, x1, text))
        else:
            grouped.append([y0, [(x0, x1, text)]])

    lines = []
    for _y0, parts in grouped:
        parts.sort()
        pieces = []
        prev_x1 = None
        for x0, x1, text in parts:
            if prev_x1 is not None and x0 - prev_x1 > space_gap:
                pieces.append(" ")
            pieces.append(text)
            prev_x1 = x1 if prev_x1 is None else max(prev_x1, x1)
        line = "".join(pieces).strip()
        if line:
            lines.append(line)
    return lines
