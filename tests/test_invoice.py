import json
from io import BytesIO
from pathlib import Path

import pytest
from click.testing import CliRunner
from pypdf import PdfWriter
from reportlab.pdfgen import canvas

from excalibur.cli import cli
from excalibur.invoices import InvoiceParseError, parse_invoice_bytes, parse_invoice_text
from excalibur.www.app import create_app
from excalibur import configuration as conf

SAMPLE_PDF = Path(__file__).resolve().parents[1] / "PS MB 31308266 Oct-07-2026.pdf"

# Figures from a different invoice, used to lock which column is read.
# The Net column plus PST and GST equals Total Order. The last money
# amount on each G/L row is the tax-included total and must be ignored.
EXAMPLE_TEXT = """
Invoice #:        31278945 RI
GST / HST / TPS                5.000%    175.07
PST - MB                       7.000%      6.00
510                                   181.07 Amount Due 10/12/2026    19,181.40
G/L Acct  Description             QTY Shipped          Net        Tax        Total
7001-001  FOOD                            100    15,642.81       1.00    15,643.81
7030-001  PAPER                             10     2,187.41       2.00     2,189.41
7420-001  OPERATING SUPPLIES                 1        85.72       0.50        86.22
7426-001  TOY PROMOTION                      2        87.51       0.20        87.71
7428-001  GIVE AWAY PROMO                             996.88                 996.88
"""


@pytest.fixture
def client():
    application = create_app(conf)
    application.config["TESTING"] = True
    return application.test_client()


def _blank_pdf():
    buffer = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.write(buffer)
    return buffer.getvalue()


def _example_pdf():
    """Place the example summary in columns, the way the supplier PDF does."""
    buffer = BytesIO()
    document = canvas.Canvas(buffer, pagesize=(612, 792))
    document.setFont("Courier", 10)
    document.drawString(72, 740, "Invoice #:")
    document.drawString(220, 740, "31278945 RI")
    document.drawString(72, 700, "GST / HST / TPS")
    document.drawString(280, 700, "5.000%")
    document.drawString(360, 700, "175.07")
    document.drawString(72, 686, "PST - MB")
    document.drawString(280, 686, "7.000%")
    document.drawString(360, 686, "6.00")
    document.drawString(
        72,
        650,
        "510    181.07 Amount Due 10/12/2026    19,181.40",
    )
    document.drawString(72, 620, "G/L Acct  Description")
    rows = [
        (600, "7001-001", "FOOD", "100", "15,642.81", "1.00", "15,643.81"),
        (586, "7030-001", "PAPER", "10", "2,187.41", "2.00", "2,189.41"),
        (572, "7420-001", "OPERATING SUPPLIES", "1", "85.72", "0.50", "86.22"),
        (558, "7426-001", "TOY PROMOTION", "2", "87.51", "0.20", "87.71"),
        (544, "7428-001", "GIVE AWAY PROMO", None, "996.88", None, "996.88"),
    ]
    for y, code, description, qty, net, tax, total in rows:
        document.drawString(72, y, code)
        document.drawString(150, y, description)
        if qty:
            document.drawString(320, y, qty)
        document.drawString(370, y, net)
        if tax:
            document.drawString(450, y, tax)
        document.drawString(510, y, total)
    document.save()
    return buffer.getvalue()


def _assert_example(record):
    assert record["invoice_number"] == "31278945"
    assert record["pst"] == "6.00"
    assert record["gst_hst_tps"] == "175.07"
    assert record["amount_due_date"] == "10/12/2026"
    assert record["total_order"] == "19181.40"
    assert record["warnings"] == []
    assert record["accounts"]["7001-001"] == {
        "description": "FOOD",
        "net": "15642.81",
    }
    assert record["accounts"]["7030-001"]["net"] == "2187.41"
    assert record["accounts"]["7420-001"]["net"] == "85.72"
    assert record["accounts"]["7426-001"]["net"] == "87.51"
    assert record["accounts"]["7428-001"] == {
        "description": "GIVE AWAY PROMO",
        "net": "996.88",
    }


def test_example_text_uses_the_net_column():
    _assert_example(parse_invoice_text(EXAMPLE_TEXT))


def test_example_pdf_round_trip():
    _assert_example(parse_invoice_bytes(_example_pdf()))


def test_missing_account_is_reported():
    text = EXAMPLE_TEXT.replace("7426-001  TOY PROMOTION                      2        87.51       0.20        87.71\n", "")
    record = parse_invoice_text(text)
    assert record["accounts"]["7426-001"]["net"] is None
    assert "7426-001 TOY PROMOTION was not found" in record["warnings"]


def test_unrecognized_pdf_explains_what_is_missing():
    with pytest.raises(InvoiceParseError, match="Invoice #"):
        parse_invoice_bytes(_blank_pdf())


@pytest.mark.skipif(not SAMPLE_PDF.is_file(), reason="sample invoice PDF is not in the repo")
def test_supplier_invoice_pdf():
    record = parse_invoice_bytes(SAMPLE_PDF.read_bytes())
    assert record["invoice_number"] == "31308266"
    assert record["pst"] == "18.18"
    assert record["gst_hst_tps"] == "318.51"
    assert record["amount_due_date"] == "10/14/2026"
    assert record["total_order"] == "32044.02"
    assert record["warnings"] == []
    assert record["accounts"]["7001-001"]["net"] == "25921.00"
    assert record["accounts"]["7030-001"]["net"] == "3649.29"
    assert record["accounts"]["7420-001"]["net"] == "259.70"
    assert record["accounts"]["7426-001"]["net"] == "971.93"
    assert record["accounts"]["7428-001"]["net"] == "905.41"


def test_cli_prints_json_for_the_supplier_invoice():
    if not SAMPLE_PDF.is_file():
        pytest.skip("sample invoice PDF is not in the repo")
    result = CliRunner().invoke(cli, ["invoice", str(SAMPLE_PDF)])
    assert result.exit_code == 0, result.output
    record = json.loads(result.output)
    assert record["invoice_number"] == "31308266"
    assert record["accounts"]["7001-001"]["net"] == "25921.00"


def test_cli_writes_csv(tmp_path):
    if not SAMPLE_PDF.is_file():
        pytest.skip("sample invoice PDF is not in the repo")
    destination = tmp_path / "invoice.csv"
    result = CliRunner().invoke(
        cli, ["invoice", str(SAMPLE_PDF), "--format", "csv", "-o", str(destination)]
    )
    assert result.exit_code == 0, result.output
    text = destination.read_text(encoding="utf-8")
    assert "Invoice Number" in text.splitlines()[0]
    assert "31308266" in text
    assert "25921.00" in text
    assert "32044.02" in text


def test_web_page_shows_supplier_invoice(client):
    if not SAMPLE_PDF.is_file():
        pytest.skip("sample invoice PDF is not in the repo")
    response = client.post(
        "/invoice",
        data={"file": (BytesIO(SAMPLE_PDF.read_bytes()), SAMPLE_PDF.name)},
        content_type="multipart/form-data",
    )
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "31308266" in body
    assert "10/14/2026" in body
    assert "18.18" in body
    assert "318.51" in body
    assert "25,921.00" in body
    assert "3,649.29" in body
    assert "259.70" in body
    assert "971.93" in body
    assert "905.41" in body
    assert "32,044.02" in body
    assert "31278945" not in body


def test_web_json_and_csv(client):
    pdf = _example_pdf()
    as_json = client.post(
        "/invoice?format=json",
        data={"file": (BytesIO(pdf), "example.pdf")},
        content_type="multipart/form-data",
    )
    assert as_json.status_code == 200
    assert as_json.get_json()["invoice_number"] == "31278945"
    assert as_json.get_json()["accounts"]["7001-001"]["net"] == "15642.81"

    as_csv = client.post(
        "/invoice?format=csv",
        data={"file": (BytesIO(pdf), "example.pdf")},
        content_type="multipart/form-data",
    )
    assert as_csv.status_code == 200
    assert "text/csv" in as_csv.content_type
    assert b"15642.81" in as_csv.data
    assert b"19181.40" in as_csv.data


def test_web_rejects_a_blank_pdf(client):
    response = client.post(
        "/invoice",
        data={"file": (BytesIO(_blank_pdf()), "blank.pdf")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 422
    assert b"Martin-Brower" in response.data
