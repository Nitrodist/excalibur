"""Extract the summary fields from a Martin-Brower invoice PDF.

The G/L figures are the Net column and the Tax column on that same row. On
these invoices the net amounts plus PST and GST equal Total Order. CSV
downloads are a QuickBooks Online bill import: one row per G/L line that
is actually on the invoice.
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
    ("7440-001", "APPAREL ACCESSORY"),
)

# Printed after "Remit To:" on this supplier's invoices. Example PDFs that
# omit the line still export under this name.
DEFAULT_SUPPLIER = "Martin Brower of Canada"

# Martin Brower G/L code -> BDPQ Automation account.
SUPPLIER_TO_BDPQ = {
    "MARTIN-BROWER OF CANADA CO": "Martin Brower of Canada",
}
# Martin Brower G/L code -> BDPQ Automation account.
MB_TO_BDPQ = {
    "7001-001": "5020 Food Purchases",
    "7030-001": "5040 Paper Purchases",
    "7420-001": "5440 Operating Supplies",
    "7426-001": "4080 Cost of Non-Product Sales",
    "7428-001": "5380 Promotion - Martin Brower",
    "7440-001": "5430 Uniforms",
}
# RC Foods uses MB_TO_BDPQ. Team Ouzzy uses different account numbers for
# the same Martin Brower promotion and uniform lines.
TEAM_OUZZY_STORES = frozenset({"14203", "15611", "26020", "29033"})
TEAM_OUZZY_ACCOUNTS = {
    "7428-001": "5345 Promotion - Martin Brower",
    "7440-001": "5420 Uniforms",
}

# Chason uses MB_TO_BDPQ. Chason uses different account numbers for
# the same Martin Brower promotion and uniform lines.
CHASON_STORES = frozenset({"6210", "3983"})
CHASON_ACCOUNTS = {
    "7428-001": "5350 Promotion - Martin Brower",
    "7440-001": "5420 Uniforms",
}
# BDPQ Automation account -> QuickBooks line description.
BDPQ_LINE_DESCRIPTION = {
    "5020 Food Purchases": "Food Purchases",
    "5040 Paper Purchases": "Paper Purchases",
    "5440 Operating Supplies": "Ops Supplies Purchase",
    "4080 Cost of Non-Product Sales": "Non-Product Purchase",
    "5380 Promotion - Martin Brower": "Monopoly Cost",
    "5430 Uniforms": "Uniform Purchase",
}
# Short store code -> McDonald's store number, as printed after Ship To.
STORE_CODES = {
    "22ND MB": "1597",
    "AN MB": "3983",
    "CC MB": "40485",
    "CL MB": "14203",
    "CM MB": "40345",
    "GP MB": "1187",
    "KEN MB": "26060",
    "MM MB": "29036",
    "MN MB": "40806",
    "PS MB": "7094",
    "RD MB": "10822",
    "SEA MB": "40700",
    "SHER MB": "26056",
    "STJ MB": "26020",
    "STJWM MB": "29033",
    "WR MB": "7504",
    "WW MB": "15611",
}
STORE_NUMBERS = {number: code for code, number in STORE_CODES.items()}
_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
# BDPQ Automation account -> QuickBooks tax code. Unmapped accounts use GST.
# "GST/PST MB" is Manitoba. A "PST - XX" line on the invoice replaces MB
# with that province, so "PST - SK" is written as "GST/PST SK".
BDPQ_TAX_CODE = {
    "5020 Food Purchases": "GST",
    "5040 Paper Purchases": "GST",
    "5440 Operating Supplies": "GST/PST MB",
    "4080 Cost of Non-Product Sales": "GST",
    "5380 Promotion - Martin Brower": "Out of Scope",
    "5430 Uniforms": "GST/PST MB",
}
_GST_PST_TAX_CODE = "GST/PST MB"
DEFAULT_TAX_CODE = "GST"
# Headings from the QuickBooks Online bill-import sample, plus the line tax
# amount taken from each G/L row. Asterisks in that sample mark mandatory
# fields; they are not part of the column names.
QBO_COLUMNS = (
    "Supplier",
    "Bill Date",
    "Due Date",
    "Account",
    "Line Description",
    "Line Amount",
    "Line Tax Code",
    "Line Tax Amount",
    "Bill No",
)

_MONEY = r"(-?\d[\d,]*\.\d{2}-?)"
# The Tax column can be printed as ".11" with no leading zero.
_GL_MONEY = r"-?(?:\d[\d,]*\.\d{2}|\.\d{2})-?"
_INVOICE_NUMBER = re.compile(r"Invoice\s*#:\s*(\d+)")
_INVOICE_DATE = re.compile(r"Invoice Date:\s*(\d{1,2}/\d{1,2}/\d{4})")
_SUPPLIER = re.compile(r"Remit To:\s*(.+)")
_STORE_NUMBER = re.compile(r"Store\s*#:\s*(\d+)")
_SHIP_TO_STORE = re.compile(r"Ship To:.*?(\d+)\s+Bill To:")
_GST = re.compile(rf"GST\s*/\s*HST\s*/\s*TPS\s+[\d.]+\s*%\s+({_MONEY})")
_PST = re.compile(
    rf"PST(?:\s*-\s*(?P<province>[A-Z]{{2}}))?\s+[\d.]+\s*%\s+(?P<amount>{_MONEY})"
)
_AMOUNT_DUE = re.compile(rf"Amount Due\s+(\d{{1,2}}/\d{{1,2}}/\d{{4}})\s+({_MONEY})")
_GL_LINE = re.compile(
    rf"^(?P<code>\d{{4}}-\d{{3}})\s+"
    rf"(?P<description>[A-Za-z0-9][A-Za-z0-9 &'/-]*?)\s+"
    rf"(?:(?P<qty>\d+-?)\s+)?"
    rf"(?P<amounts>{_GL_MONEY}(?:\s+{_GL_MONEY})*)",
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
    supplier_match = _SUPPLIER.search(text)
    store_match = _STORE_NUMBER.search(text) or _SHIP_TO_STORE.search(text)
    gst_match = _GST.search(text)
    pst_match = _PST.search(text)
    due_match = _AMOUNT_DUE.search(text)
    accounts, warnings = _accounts(_gl_section(text))

    missing = []
    if not invoice_match:
        missing.append("Invoice #")
    if not invoice_date_match:
        missing.append("Invoice Date")
    if not due_match:
        missing.append("Amount Due date and Total Order")
    if missing:
        raise InvoiceParseError(
            "This PDF does not look like a Martin-Brower invoice. Missing "
            + ", ".join(missing)
            + "."
        )

    supplier = DEFAULT_SUPPLIER
    if supplier_match:
        supplier = supplier_match.group(1).strip() or DEFAULT_SUPPLIER
        if SUPPLIER_TO_BDPQ.get(supplier):
            supplier = SUPPLIER_TO_BDPQ.get(supplier)

    store_number = store_match.group(1) if store_match else None
    pst, pst_province = _pst_total(pst_match)
    record = {
        "invoice_number": invoice_match.group(1),
        "supplier": supplier,
        "store_number": store_number,
        "store_code": STORE_NUMBERS.get(store_number) if store_number else None,
        "invoice_date": _normalize_date(invoice_date_match.group(1)),
        "pst": pst,
        "pst_province": pst_province,
        "gst_hst_tps": _optional_tax_total(gst_match),
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


def invoice_filename(record):
    """'{store} {bill no} {Mon-DD-YYYY}.pdf' for a parsed invoice."""
    store_code = record.get("store_code")
    if not store_code:
        number = record.get("store_number") or "unknown"
        raise InvoiceParseError(f"Store number {number} is not in the store map.")
    return (
        f"{store_code} {record['invoice_number']} "
        f"{_filename_date(record['invoice_date'])}.pdf"
    )


def rename_invoice_pdf(path, dry_run=False):
    """Rename one invoice PDF in place. Returns the destination path."""
    from pathlib import Path

    source = Path(path)
    record = parse_invoice(str(source))
    destination = source.with_name(invoice_filename(record))
    if destination.resolve() == source.resolve():
        return destination
    if destination.exists():
        raise InvoiceParseError(
            f"{source.name} would overwrite existing file {destination.name}."
        )
    if not dry_run:
        source.rename(destination)
    return destination


def rename_invoice_paths(paths, dry_run=False):
    """Rename each PDF. Returns (renamed, failures) path/message pairs."""
    from pathlib import Path

    renamed = []
    failures = []
    for path in paths:
        source = Path(path)
        try:
            destination = rename_invoice_pdf(source, dry_run=dry_run)
            renamed.append((source, destination))
        except Exception as exc:
            failures.append((source, str(exc)))
    return renamed, failures


def _filename_date(invoice_date):
    parsed = datetime.strptime(invoice_date, "%m/%d/%Y")
    return f"{_MONTHS[parsed.month - 1]}-{parsed.day:02d}-{parsed.year}"


def to_csv(record):
    """QuickBooks Online bill import for one invoice, one row per G/L account."""
    return _quickbooks_csv([record])


def to_batch_csv(entries):
    """One header and one QuickBooks bill line per G/L account on each invoice.

    Known accounts keep their order. Extra accounts follow them. An account
    that is not on an invoice is left out.
    """
    records = [entry["record"] for entry in entries]
    columns, descriptions = _batch_account_columns(records)
    return _quickbooks_csv(records, columns, descriptions)


def _quickbooks_csv(records, columns=None, descriptions=None):
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(QBO_COLUMNS)
    for record in records:
        writer.writerows(_quickbooks_rows(record, columns, descriptions))
    return buffer.getvalue()


def _quickbooks_rows(record, columns=None, descriptions=None):
    """Repeat bill fields on every line, as the QuickBooks sample does.

    Line Amount is the Net column and Line Tax Amount is the Tax column.
    Only accounts that appear on the invoice are written.
    """
    if columns is None:
        columns = list(record["accounts"])
    supplier = record.get("supplier") or DEFAULT_SUPPLIER
    rows = []
    for code in columns:
        account = record["accounts"].get(code) or {}
        net = account.get("net")
        if net in (None, ""):
            continue
        description = account.get("description") or (descriptions or {}).get(code, "")
        account_name, line_description, tax_code = _bdpq_fields(
            code,
            description,
            record.get("store_number"),
            record.get("pst_province"),
        )
        rows.append(
            [
                supplier,
                record["invoice_date"],
                record["amount_due_date"],
                account_name,
                line_description,
                net,
                tax_code,
                _qbo_line_tax_amount(account.get("tax")),
                record["invoice_number"],
            ]
        )
    return rows


def qbo_import_error(record):
    """QuickBooks Online rejects a bill whose total is negative."""
    if Decimal(record["total_order"]) < 0:
        return "Bill total is negative; QuickBooks Online cannot import it."
    return None


def _qbo_line_tax_amount(raw):
    """QuickBooks requires a Line Tax Amount of zero or more."""
    tax = Decimal(raw or "0")
    if tax < 0:
        return format_money(Decimal("0.00"))
    return format_money(tax)


def _bdpq_fields(code, description, store_number=None, pst_province=None):
    """Map a Martin Brower G/L line onto the BDPQ Automation bill fields.

    Line description and tax code follow the Martin Brower account. The
    QuickBooks account name follows the store's client. Combined GST/PST
    uses the province printed on the invoice's PST line.
    """
    default = MB_TO_BDPQ.get(code)
    account_name = _client_account(code, store_number) or default
    if not account_name:
        label = f"{code} {description}".strip()
        return label, description, DEFAULT_TAX_CODE
    lookup = default or account_name
    return (
        account_name,
        BDPQ_LINE_DESCRIPTION.get(lookup, description),
        _line_tax_code(BDPQ_TAX_CODE.get(lookup, DEFAULT_TAX_CODE), pst_province),
    )


def _line_tax_code(tax_code, pst_province):
    """Use the invoice PST province for lines that carry provincial tax."""
    if tax_code == _GST_PST_TAX_CODE and pst_province:
        return f"GST/PST {pst_province}"
    return tax_code


def _client_account(code, store_number):
    if store_number in TEAM_OUZZY_STORES:
        return TEAM_OUZZY_ACCOUNTS.get(code)
    if store_number in CHASON_STORES:
        return CHASON_ACCOUNTS.get(code)
    return None


def _batch_account_columns(records):
    descriptions = {code: description for code, description in KNOWN_ACCOUNTS}
    extras = []
    seen = set(descriptions)
    for record in records:
        for code, account in record["accounts"].items():
            if account.get("description"):
                descriptions[code] = account["description"]
            if code not in seen:
                seen.add(code)
                extras.append(code)
    extras.sort()
    columns = [code for code, _description in KNOWN_ACCOUNTS] + extras
    return columns, descriptions


def display_rows(record):
    """Label/value pairs for the web page, with money grouped in thousands."""
    rows = [
        ("Invoice Number", record["invoice_number"]),
        ("Invoice Date", record["invoice_date"]),
        ("Amount Due", record["amount_due_date"]),
    ]
    for code, account in record["accounts"].items():
        rows.append((f"{code} {account['description']}", _display_account(account)))
    rows.append(("Total Order", _display_money(record["total_order"])))
    return rows


def invoice_verification(record):
    """Compare the tax-inclusive total owing to the exported bill lines.

    GST and GST/PST are the Line Tax Amounts grouped by the mapped tax code.
    The remainder is Total Order minus those line amounts and line taxes.
    """
    gst = Decimal("0.00")
    gst_pst = Decimal("0.00")
    line_items = Decimal("0.00")
    for code, account in record["accounts"].items():
        net = account.get("net")
        if net in (None, ""):
            continue
        tax_amount = Decimal(account.get("tax") or "0")
        _, _, tax_code = _bdpq_fields(
            code,
            account.get("description") or "",
            record.get("store_number"),
            record.get("pst_province"),
        )
        line_items += Decimal(net) + tax_amount
        if tax_code.startswith("GST/PST "):
            gst_pst += tax_amount
        elif tax_code == "GST":
            gst += tax_amount
    total = Decimal(record["total_order"])
    difference = (total - line_items).quantize(Decimal("0.01"))
    return {
        "gst": gst.quantize(Decimal("0.01")),
        "gst_pst": gst_pst.quantize(Decimal("0.01")),
        "line_items": line_items.quantize(Decimal("0.01")),
        "total": total,
        "difference": difference,
        "ok": difference == Decimal("0.00"),
    }


def display_verification(record):
    """Formatted GST, GST/PST, and remainder for the bulk results table."""
    check = invoice_verification(record)
    return {
        "gst": _display_money(check["gst"]),
        "gst_pst": _display_money(check["gst_pst"]),
        "difference": _display_money(check["difference"]),
        "ok": check["ok"],
    }


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


def _optional_tax_total(match):
    """GST and PST totals are omitted when no line incurred that tax."""
    if not match:
        return format_money(Decimal("0.00"))
    return format_money(parse_money(match.group(1)))


def _pst_total(match):
    """PST amount, and the province from a line such as 'PST - SK'."""
    if not match:
        return format_money(Decimal("0.00")), None
    return format_money(parse_money(match.group("amount"))), match.group("province")


def _normalize_date(raw):
    parsed = datetime.strptime(raw, "%m/%d/%Y")
    return parsed.strftime("%m/%d/%Y")


def _display_money(value):
    return f"{Decimal(value):,.2f}"


def _display_account(account):
    net = account.get("net")
    if not net:
        return "—"
    tax = account.get("tax") or "0.00"
    return f"{_display_money(net)} (tax {_display_money(tax)})"


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
        net, tax = _net_and_tax(match.group("amounts"))
        found[match.group("code")] = (description, net, tax)

    accounts = {}
    remaining = dict(found)
    for code, _canonical in KNOWN_ACCOUNTS:
        if code not in remaining:
            continue
        description, net, tax = remaining.pop(code)
        accounts[code] = {
            "description": description,
            "net": format_money(net),
            "tax": format_money(tax),
        }
    for code in sorted(remaining):
        description, net, tax = remaining[code]
        accounts[code] = {
            "description": description,
            "net": format_money(net),
            "tax": format_money(tax),
        }
    return accounts, []


def _net_and_tax(raw):
    """Read Net and Tax from the money amounts on a G/L row.

    A full row is Net, Tax, Total. When the Tax column is blank the row has
    Net and Total only, and the tax amount is zero.
    """
    tokens = re.findall(_GL_MONEY, raw)
    net = parse_money(tokens[0])
    if len(tokens) >= 3:
        tax = parse_money(tokens[1])
    else:
        tax = Decimal("0.00")
    return net, tax


def _reconciliation_warning(record):
    nets = []
    for account in record["accounts"].values():
        if account["net"] is None:
            continue
        nets.append(Decimal(account["net"]))
    if not nets:
        return None
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
