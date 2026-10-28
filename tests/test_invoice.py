import csv
import json
from io import BytesIO, StringIO
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
APPAREL_PDF = Path(__file__).resolve().parents[1] / "GP MB 30039178 Feb-11-2026.pdf"

# Figures from a different invoice, used to lock which column is read.
# The Net column plus PST and GST equals Total Order. The last money
# amount on each G/L row is the tax-included total and must be ignored.
EXAMPLE_TEXT = """
Invoice #:        31278945 RI
Invoice Date:     10/05/2026
Order Date:       10/02/2026
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
    document.drawString(72, 724, "Invoice Date:")
    document.drawString(220, 724, "10/05/2026")
    document.drawString(72, 708, "Order Date:")
    document.drawString(220, 708, "10/02/2026")
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
    assert record["invoice_date"] == "10/05/2026"
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


def test_missing_standard_account_stays_in_the_output():
    text = EXAMPLE_TEXT.replace(
        "7426-001  TOY PROMOTION                      2        87.51       0.20        87.71\n",
        "",
    )
    record = parse_invoice_text(text)
    assert list(record["accounts"])[:5] == [code for code, _description in (
        ("7001-001", "FOOD"),
        ("7030-001", "PAPER"),
        ("7420-001", "OPERATING SUPPLIES"),
        ("7426-001", "TOY PROMOTION"),
        ("7428-001", "GIVE AWAY PROMO"),
    )]
    assert record["accounts"]["7426-001"]["net"] is None
    assert record["warnings"]
    assert "was not found" not in record["warnings"][0]


def test_unrecognized_pdf_explains_what_is_missing():
    with pytest.raises(InvoiceParseError, match="Invoice #"):
        parse_invoice_bytes(_blank_pdf())


@pytest.mark.skipif(not SAMPLE_PDF.is_file(), reason="sample invoice PDF is not in the repo")
def test_supplier_invoice_pdf():
    record = parse_invoice_bytes(SAMPLE_PDF.read_bytes())
    assert record["invoice_number"] == "31308266"
    assert record["invoice_date"] == "10/07/2026"
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


@pytest.mark.skipif(not APPAREL_PDF.is_file(), reason="apparel invoice PDF is not in the repo")
def test_extra_gl_account_is_added_after_the_standard_rows():
    record = parse_invoice_bytes(APPAREL_PDF.read_bytes())
    assert record["invoice_number"] == "30039178"
    assert record["invoice_date"] == "02/11/2026"
    assert record["pst"] == "47.01"
    assert record["gst_hst_tps"] == "183.74"
    assert record["amount_due_date"] == "02/18/2026"
    assert record["total_order"] == "18959.00"
    assert record["warnings"] == []
    assert list(record["accounts"]) == [
        "7001-001",
        "7030-001",
        "7420-001",
        "7426-001",
        "7428-001",
        "7440-001",
    ]
    assert record["accounts"]["7001-001"]["net"] == "15055.46"
    assert record["accounts"]["7030-001"]["net"] == "2493.26"
    assert record["accounts"]["7420-001"]["net"] == "636.55"
    assert record["accounts"]["7426-001"]["net"] == "318.38"
    assert record["accounts"]["7428-001"] == {
        "description": "GIVE AWAY PROMO",
        "net": None,
    }
    assert record["accounts"]["7440-001"] == {
        "description": "APPAREL ACCESSORY",
        "net": "224.60",
    }


def test_cli_prints_json_for_the_supplier_invoice():
    if not SAMPLE_PDF.is_file():
        pytest.skip("sample invoice PDF is not in the repo")
    result = CliRunner().invoke(cli, ["invoice", str(SAMPLE_PDF)])
    assert result.exit_code == 0, result.output
    record = json.loads(result.output)
    assert record["invoice_number"] == "31308266"
    assert record["invoice_date"] == "10/07/2026"
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
    assert "Invoice Date" in text.splitlines()[0]
    assert "31308266" in text
    assert "10/07/2026" in text
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
    assert "Invoice Date" in body
    assert "10/07/2026" in body
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


def test_web_page_lists_standard_accounts_and_extra_ones(client):
    if not APPAREL_PDF.is_file():
        pytest.skip("apparel invoice PDF is not in the repo")
    response = client.post(
        "/invoice",
        data={"file": (BytesIO(APPAREL_PDF.read_bytes()), APPAREL_PDF.name)},
        content_type="multipart/form-data",
    )
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "7001-001 FOOD" in body
    assert "7030-001 PAPER" in body
    assert "7420-001 OPERATING SUPPLIES" in body
    assert "7426-001 TOY PROMOTION" in body
    assert "7428-001 GIVE AWAY PROMO" in body
    assert "7440-001 APPAREL ACCESSORY" in body
    giveaway = body.index("7428-001 GIVE AWAY PROMO")
    apparel = body.index("7440-001 APPAREL ACCESSORY")
    assert giveaway < apparel
    assert "—" in body
    assert "224.60" in body
    assert "was not found" not in body


def test_web_json_and_csv(client):
    pdf = _example_pdf()
    as_json = client.post(
        "/invoice?format=json",
        data={"file": (BytesIO(pdf), "example.pdf")},
        content_type="multipart/form-data",
    )
    assert as_json.status_code == 200
    assert as_json.get_json()["invoice_number"] == "31278945"
    assert as_json.get_json()["invoice_date"] == "10/05/2026"
    assert as_json.get_json()["accounts"]["7001-001"]["net"] == "15642.81"

    as_csv = client.post(
        "/invoice?format=csv",
        data={"file": (BytesIO(pdf), "example.pdf")},
        content_type="multipart/form-data",
    )
    assert as_csv.status_code == 200
    assert "text/csv" in as_csv.content_type
    assert b"10/05/2026" in as_csv.data
    assert b"10/02/2026" not in as_csv.data
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


def test_bulk_page_is_separate_from_the_single_invoice_form(client):
    bulk = client.get("/bulk")
    assert bulk.status_code == 200
    assert b"Bulk invoices" in bulk.data
    assert b'name="files"' in bulk.data
    assert b"multiple" in bulk.data

    single = client.get("/invoice")
    assert b'name="files"' not in single.data
    assert b"multiple" not in single.data
    assert b"Upload two or more" not in single.data


def test_bulk_requires_two_pdfs(client):
    response = client.post(
        "/bulk",
        data={"files": (BytesIO(_blank_pdf()), "only.pdf")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 400
    assert b"at least two" in response.data


def test_bulk_csv_has_a_header_and_one_row_per_invoice(client):
    if not SAMPLE_PDF.is_file() or not APPAREL_PDF.is_file():
        pytest.skip("sample invoice PDFs are not in the repo")
    uploads = [
        (BytesIO(SAMPLE_PDF.read_bytes()), SAMPLE_PDF.name),
        (BytesIO(APPAREL_PDF.read_bytes()), APPAREL_PDF.name),
    ]
    page = client.post(
        "/bulk",
        data={"files": uploads},
        content_type="multipart/form-data",
    )
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "31308266" in body
    assert "30039178" in body
    assert "3 rows" in body
    assert "Download CSV" in body

    uploaded_again = [
        (BytesIO(SAMPLE_PDF.read_bytes()), SAMPLE_PDF.name),
        (BytesIO(APPAREL_PDF.read_bytes()), APPAREL_PDF.name),
    ]
    downloaded = client.post(
        "/bulk?format=csv",
        data={"files": uploaded_again},
        content_type="multipart/form-data",
    )
    assert downloaded.status_code == 200
    assert "text/csv" in downloaded.content_type
    text = downloaded.get_data(as_text=True)
    assert len(text.splitlines()) == 3
    rows = list(csv.DictReader(StringIO(text)))
    assert [row["Invoice Number"] for row in rows] == ["31308266", "30039178"]
    assert rows[0]["Filename"] == SAMPLE_PDF.name
    assert rows[0]["7428-001 GIVE AWAY PROMO"] == "905.41"
    assert rows[0]["7440-001 APPAREL ACCESSORY"] == ""
    assert rows[1]["7428-001 GIVE AWAY PROMO"] == ""
    assert rows[1]["7440-001 APPAREL ACCESSORY"] == "224.60"
    assert rows[1]["7001-001 FOOD"] == "15055.46"


def test_bulk_keeps_successful_invoices_when_one_pdf_fails(client):
    if not SAMPLE_PDF.is_file():
        pytest.skip("sample invoice PDF is not in the repo")
    response = client.post(
        "/bulk",
        data={
            "files": [
                (BytesIO(SAMPLE_PDF.read_bytes()), SAMPLE_PDF.name),
                (BytesIO(_blank_pdf()), "blank.pdf"),
            ]
        },
        content_type="multipart/form-data",
    )
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "31308266" in body
    assert "blank.pdf" in body
    assert "left out of the CSV" in body
    assert len(body.split("31308266")) >= 2
