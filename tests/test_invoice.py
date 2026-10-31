import csv
import json
import shutil
from decimal import Decimal
from io import BytesIO, StringIO
from pathlib import Path

import pytest
from click.testing import CliRunner
from pypdf import PdfWriter
from reportlab.pdfgen import canvas

from excalibur.cli import cli
from excalibur.invoices import (
    InvoiceParseError,
    invoice_filename,
    invoice_verification,
    parse_invoice_bytes,
    parse_invoice_text,
    rename_invoice_pdf,
    to_csv,
)
from excalibur.www.app import create_app
from excalibur import configuration as conf

_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_PDF = _ROOT / "PS MB 31308266 Oct-07-2026.pdf"
APPAREL_PDF = _ROOT / "GP MB 30039178 Feb-11-2026.pdf"
KEN_NO_PST_PDF = _ROOT / "KEN MB 31316217 Oct-09-2026.pdf"
KEN_REFUND_PDF = _ROOT / "KEN MB 31368179 Oct-16-2026.pdf"
KEN_NEGATIVE_TAX_PDF = _ROOT / "KEN MB 31278204 Oct-02-2026.pdf"
MM_PDF = _ROOT / "MM MB 31274871.pdf"
RD_PDFS = sorted(_ROOT.glob("RD MB *.pdf"))
MM_PDFS = sorted(_ROOT.glob("MM MB *.pdf"))

REFUND_TEXT = """
Invoice #:        31368179 RI
Invoice Date:     10/16/2026
1- Amount Due 10/16/2026 96.00-
G/L Acct  Description             QTY Shipped          Net        Tax        Total
7001-001  FOOD                              1-       96.00-                 96.00-
"""

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
    assert record["supplier"] == "Martin Brower of Canada"
    assert record["invoice_date"] == "10/05/2026"
    assert record["pst"] == "6.00"
    assert record["gst_hst_tps"] == "175.07"
    assert record["amount_due_date"] == "10/12/2026"
    assert record["total_order"] == "19181.40"
    assert record["warnings"] == []
    assert record["accounts"]["7001-001"] == {
        "description": "FOOD",
        "net": "15642.81",
        "tax": "1.00",
    }
    assert record["accounts"]["7030-001"]["net"] == "2187.41"
    assert record["accounts"]["7030-001"]["tax"] == "2.00"
    assert record["accounts"]["7420-001"]["net"] == "85.72"
    assert record["accounts"]["7420-001"]["tax"] == "0.50"
    assert record["accounts"]["7426-001"]["net"] == "87.51"
    assert record["accounts"]["7426-001"]["tax"] == "0.20"
    assert record["accounts"]["7428-001"] == {
        "description": "GIVE AWAY PROMO",
        "net": "996.88",
        "tax": "0.00",
    }


def test_example_text_uses_the_net_column():
    _assert_example(parse_invoice_text(EXAMPLE_TEXT))


def test_invoice_verification_uses_exported_line_taxes():
    if not SAMPLE_PDF.is_file() or not APPAREL_PDF.is_file():
        pytest.skip("sample invoice PDFs are not in the repo")
    sample = invoice_verification(parse_invoice_bytes(SAMPLE_PDF.read_bytes()))
    assert sample["gst"] == Decimal("305.53")
    assert sample["gst_pst"] == Decimal("31.16")
    assert sample["total"] == Decimal("32044.02")
    assert sample["difference"] == Decimal("0.00")
    assert sample["ok"] is True
    apparel = invoice_verification(parse_invoice_bytes(APPAREL_PDF.read_bytes()))
    assert apparel["gst"] == Decimal("140.70")
    assert apparel["gst_pst"] == Decimal("90.05")
    assert apparel["total"] == Decimal("18959.00")
    assert apparel["difference"] == Decimal("0.00")
    assert apparel["ok"] is True


def test_supplier_comes_from_remit_to():
    text = "CoOp Manitoba Remit To: EXAMPLE SUPPLIER LTD\n" + EXAMPLE_TEXT
    record = parse_invoice_text(text)
    assert record["supplier"] == "EXAMPLE SUPPLIER LTD"


@pytest.mark.parametrize("store_number", ["6210", "3983"])
def test_chason_stores_use_the_hyphenated_supplier_name(store_number):
    text = f"Store #: {store_number}\nRemit To: MARTIN-BROWER OF CANADA CO\n" + EXAMPLE_TEXT
    record = parse_invoice_text(text)
    assert record["supplier"] == "Martin-Brower of Canada"
    rows = list(csv.DictReader(StringIO(to_csv(record))))
    assert rows[0]["Supplier"] == "Martin-Brower of Canada"


def test_other_stores_keep_the_unhyphenated_supplier_name():
    text = "Store #: 26060\nRemit To: MARTIN-BROWER OF CANADA CO\n" + EXAMPLE_TEXT
    record = parse_invoice_text(text)
    assert record["supplier"] == "Martin Brower of Canada"
    rows = list(csv.DictReader(StringIO(to_csv(record))))
    assert rows[0]["Supplier"] == "Martin Brower of Canada"


def test_store_code_comes_from_ship_to():
    text = (
        "Ship To: MCDONALD'S 26060                    Bill To:     "
        "GUEST FIRST HOSPITALITY INC\n" + EXAMPLE_TEXT
    )
    record = parse_invoice_text(text)
    assert record["store_number"] == "26060"
    assert record["store_code"] == "KEN MB"


def test_invoice_filename_uses_store_bill_and_date():
    text = "Store #: 26060\n" + EXAMPLE_TEXT.replace("31278945", "31146799").replace(
        "10/05/2026", "09/08/2026"
    )
    record = parse_invoice_text(text)
    assert invoice_filename(record) == "KEN MB 31146799 Sep-08-2026.pdf"


def test_rename_invoices_cli_appends_the_date(tmp_path):
    if not MM_PDF.is_file():
        pytest.skip("MM invoice PDF is not in the repo")
    copied = tmp_path / MM_PDF.name
    shutil.copy(MM_PDF, copied)
    result = CliRunner().invoke(cli, ["rename-invoices", str(copied)])
    assert result.exit_code == 0, result.output
    renamed = tmp_path / "MM MB 31274871 Oct-02-2026.pdf"
    assert renamed.is_file()
    assert not copied.exists()
    assert "MM MB 31274871 Oct-02-2026.pdf" in result.output


def test_rename_invoice_pdf_is_a_no_op_when_already_named(tmp_path):
    if not MM_PDF.is_file():
        pytest.skip("MM invoice PDF is not in the repo")
    destination = tmp_path / "MM MB 31274871 Oct-02-2026.pdf"
    shutil.copy(MM_PDF, destination)
    result = rename_invoice_pdf(destination)
    assert result == destination
    assert destination.is_file()


@pytest.mark.parametrize("store_number", ["14203", "15611", "26020", "29033"])
def test_team_ouzy_stores_use_their_promotion_and_uniform_accounts(store_number):
    text = (
        f"Store #: {store_number}\n"
        + EXAMPLE_TEXT
        + "7440-001  APPAREL ACCESSORY                   1       224.60      26.94       251.54\n"
    )
    rows = list(csv.DictReader(StringIO(to_csv(parse_invoice_text(text)))))
    by_amount = {row["Line Amount"]: row for row in rows}
    promo = by_amount["996.88"]
    assert promo["Account"] == "5345 Promotion - Martin Brower"
    assert promo["Line Description"] == "Monopoly Cost"
    assert promo["Line Tax Code"] == "Out of Scope"
    uniforms = by_amount["224.60"]
    assert uniforms["Account"] == "5420 Uniforms"
    assert uniforms["Line Description"] == "Uniform Purchase"
    assert uniforms["Line Tax Code"] == "GST/PST MB"
    assert by_amount["15642.81"]["Account"] == "5020 Food Purchases"


def test_pst_sk_writes_gst_pst_sk_on_provincial_lines():
    text = (
        EXAMPLE_TEXT.replace("PST - MB", "PST - SK")
        + "7440-001  APPAREL ACCESSORY                   1       224.60      26.94       251.54\n"
    )
    record = parse_invoice_text(text)
    assert record["pst"] == "6.00"
    assert record["pst_province"] == "SK"
    rows = list(csv.DictReader(StringIO(to_csv(record))))
    by_amount = {row["Line Amount"]: row for row in rows}
    assert by_amount["85.72"]["Line Tax Code"] == "GST/PST SK"
    assert by_amount["224.60"]["Line Tax Code"] == "GST/PST SK"
    assert by_amount["15642.81"]["Line Tax Code"] == "GST"
    assert by_amount["996.88"]["Line Tax Code"] == "Out of Scope"
    check = invoice_verification(record)
    assert check["gst_pst"] == Decimal("27.44")
    assert check["gst"] == Decimal("3.20")


def test_pst_mb_keeps_gst_pst_mb():
    record = parse_invoice_text(EXAMPLE_TEXT)
    assert record["pst_province"] == "MB"
    rows = list(csv.DictReader(StringIO(to_csv(record))))
    operating = [row for row in rows if row["Line Amount"] == "85.72"]
    assert operating[0]["Line Tax Code"] == "GST/PST MB"


def test_invoice_without_a_pst_line_stays_on_gst_pst_mb():
    text = EXAMPLE_TEXT.replace(
        "PST - MB                       7.000%      6.00\n",
        "",
    )
    record = parse_invoice_text(text)
    assert record["pst"] == "0.00"
    assert record["pst_province"] is None
    rows = list(csv.DictReader(StringIO(to_csv(record))))
    operating = [row for row in rows if row["Line Amount"] == "85.72"]
    assert operating[0]["Line Tax Code"] == "GST/PST MB"


def _combined_tax_codes(path):
    record = parse_invoice_bytes(path.read_bytes())
    rows = list(csv.DictReader(StringIO(to_csv(record))))
    combined = {
        row["Line Tax Code"]
        for row in rows
        if row["Line Tax Code"].startswith("GST/PST")
    }
    return record, combined


@pytest.mark.skipif(not RD_PDFS, reason="Saskatchewan invoices are not in the repo")
def test_rd_invoices_use_gst_pst_sk():
    assert RD_PDFS
    for path in RD_PDFS:
        record, combined = _combined_tax_codes(path)
        assert record["pst_province"] == "SK", path.name
        assert combined == {"GST/PST SK"}, path.name
        check = invoice_verification(record)
        assert check["ok"] is True, path.name
        assert check["gst_pst"] > 0, path.name


@pytest.mark.skipif(not MM_PDFS, reason="Manitoba invoices are not in the repo")
def test_mm_invoices_use_gst_pst_mb():
    assert MM_PDFS
    for path in MM_PDFS:
        record, combined = _combined_tax_codes(path)
        assert record["pst_province"] is None, path.name
        assert combined == {"GST/PST MB"}, path.name
        check = invoice_verification(record)
        assert check["ok"] is True, path.name
        assert check["gst_pst"] > 0, path.name


def test_rc_foods_store_keeps_the_original_promotion_and_uniform_accounts():
    text = (
        "Store #: 26060\n"
        + EXAMPLE_TEXT
        + "7440-001  APPAREL ACCESSORY                   1       224.60      26.94       251.54\n"
    )
    rows = list(csv.DictReader(StringIO(to_csv(parse_invoice_text(text)))))
    by_amount = {row["Line Amount"]: row for row in rows}
    assert by_amount["996.88"]["Account"] == "5380 Promotion - Martin Brower"
    assert by_amount["224.60"]["Account"] == "5430 Uniforms"


def test_csv_is_a_quickbooks_bill_import():
    text = to_csv(parse_invoice_text(EXAMPLE_TEXT))
    rows = list(csv.DictReader(StringIO(text)))
    assert list(rows[0]) == [
        "Supplier",
        "Bill Date",
        "Due Date",
        "Account",
        "Line Description",
        "Line Amount",
        "Line Tax Code",
        "Line Tax Amount",
        "Bill No",
    ]
    assert len(rows) == 5
    food = rows[0]
    assert food["Supplier"] == "Martin Brower of Canada"
    assert food["Bill Date"] == "10/05/2026"
    assert food["Due Date"] == "10/12/2026"
    assert "Terms" not in food
    assert food["Bill No"] == "31278945"
    assert [
        (
            row["Account"],
            row["Line Description"],
            row["Line Tax Code"],
            row["Line Amount"],
            row["Line Tax Amount"],
        )
        for row in rows
    ] == [
        ("5020 Food Purchases", "Food Purchases", "GST", "15642.81", "1.00"),
        ("5040 Paper Purchases", "Paper Purchases", "GST", "2187.41", "2.00"),
        (
            "5440 Operating Supplies",
            "Ops Supplies Purchase",
            "GST/PST MB",
            "85.72",
            "0.50",
        ),
        (
            "4080 Cost of Non-Product Sales",
            "Non-Product Purchase",
            "GST",
            "87.51",
            "0.20",
        ),
        (
            "5380 Promotion - Martin Brower",
            "Monopoly Cost",
            "Out of Scope",
            "996.88",
            "0.00",
        ),
    ]
    assert "19181.40" not in text
    assert "10/02/2026" not in text
    assert "175.07" not in text
    assert "6.00" not in text


def test_example_pdf_round_trip():
    _assert_example(parse_invoice_bytes(_example_pdf()))


def test_missing_standard_account_is_omitted():
    text = EXAMPLE_TEXT.replace(
        "7426-001  TOY PROMOTION                      2        87.51       0.20        87.71\n",
        "",
    )
    record = parse_invoice_text(text)
    assert "7426-001" not in record["accounts"]
    assert list(record["accounts"]) == [
        "7001-001",
        "7030-001",
        "7420-001",
        "7428-001",
    ]
    rows = list(csv.DictReader(StringIO(to_csv(record))))
    assert [row["Account"] for row in rows] == [
        "5020 Food Purchases",
        "5040 Paper Purchases",
        "5440 Operating Supplies",
        "5380 Promotion - Martin Brower",
    ]
    assert all(row["Line Amount"] for row in rows)
    assert record["warnings"]
    assert "was not found" not in record["warnings"][0]


def test_unrecognized_pdf_explains_what_is_missing():
    with pytest.raises(InvoiceParseError, match="Invoice #"):
        parse_invoice_bytes(_blank_pdf())


def test_missing_gst_and_pst_totals_are_zero():
    text = EXAMPLE_TEXT.replace(
        "GST / HST / TPS                5.000%    175.07\n"
        "PST - MB                       7.000%      6.00\n",
        "",
    )
    record = parse_invoice_text(text)
    assert record["gst_hst_tps"] == "0.00"
    assert record["pst"] == "0.00"
    assert record["invoice_number"] == "31278945"


def test_missing_pst_alone_is_zero():
    text = EXAMPLE_TEXT.replace(
        "PST - MB                       7.000%      6.00\n",
        "",
    )
    record = parse_invoice_text(text)
    assert record["pst"] == "0.00"
    assert record["gst_hst_tps"] == "175.07"


def test_refund_invoice_keeps_negative_amounts():
    record = parse_invoice_text(REFUND_TEXT)
    assert record["invoice_number"] == "31368179"
    assert record["gst_hst_tps"] == "0.00"
    assert record["pst"] == "0.00"
    assert record["total_order"] == "-96.00"
    assert record["accounts"]["7001-001"] == {
        "description": "FOOD",
        "net": "-96.00",
        "tax": "0.00",
    }
    assert record["warnings"] == []
    rows = list(csv.DictReader(StringIO(to_csv(record))))
    assert len(rows) == 1
    assert rows[0]["Line Amount"] == "-96.00"
    assert rows[0]["Line Tax Amount"] == "0.00"
    assert rows[0]["Line Tax Code"] == "GST"
    assert rows[0]["Bill No"] == "31368179"


@pytest.mark.skipif(
    not SAMPLE_PDF.is_file(), reason="sample invoice PDF is not in the repo"
)
def test_supplier_invoice_pdf():
    record = parse_invoice_bytes(SAMPLE_PDF.read_bytes())
    assert record["invoice_number"] == "31308266"
    assert record["supplier"] == "Martin Brower of Canada"
    assert record["invoice_date"] == "10/07/2026"
    assert record["pst"] == "18.18"
    assert record["gst_hst_tps"] == "318.51"
    assert record["amount_due_date"] == "10/14/2026"
    assert record["total_order"] == "32044.02"
    assert record["warnings"] == []
    assert record["accounts"]["7001-001"]["net"] == "25921.00"
    assert record["accounts"]["7001-001"]["tax"] == "29.19"
    assert record["accounts"]["7030-001"]["net"] == "3649.29"
    assert record["accounts"]["7030-001"]["tax"] == "227.74"
    assert record["accounts"]["7420-001"]["net"] == "259.70"
    assert record["accounts"]["7420-001"]["tax"] == "31.16"
    assert record["accounts"]["7426-001"]["net"] == "971.93"
    assert record["accounts"]["7426-001"]["tax"] == "48.60"
    assert record["accounts"]["7428-001"]["net"] == "905.41"
    assert record["accounts"]["7428-001"]["tax"] == "0.00"


@pytest.mark.skipif(
    not KEN_NO_PST_PDF.is_file(), reason="KEN invoice PDF is not in the repo"
)
def test_invoice_without_pst_total_is_still_parsed():
    record = parse_invoice_bytes(KEN_NO_PST_PDF.read_bytes())
    assert record["invoice_number"] == "31316217"
    assert record["pst"] == "0.00"
    assert record["gst_hst_tps"] == "186.78"
    assert record["total_order"] == "27630.97"
    assert record["warnings"] == []
    assert record["accounts"]["7001-001"]["net"] == "24201.09"
    rows = list(csv.DictReader(StringIO(to_csv(record))))
    assert rows
    assert all(row["Line Amount"] for row in rows)


@pytest.mark.skipif(
    not KEN_REFUND_PDF.is_file(), reason="KEN refund PDF is not in the repo"
)
def test_refund_pdf_exports_a_negative_food_line():
    record = parse_invoice_bytes(KEN_REFUND_PDF.read_bytes())
    assert record["invoice_number"] == "31368179"
    assert record["gst_hst_tps"] == "0.00"
    assert record["pst"] == "0.00"
    assert record["total_order"] == "-96.00"
    assert record["accounts"]["7001-001"] == {
        "description": "FOOD",
        "net": "-96.00",
        "tax": "0.00",
    }
    assert record["warnings"] == []
    rows = list(csv.DictReader(StringIO(to_csv(record))))
    assert [row["Line Amount"] for row in rows] == ["-96.00"]
    assert rows[0]["Line Tax Amount"] == "0.00"


@pytest.mark.skipif(
    not KEN_NEGATIVE_TAX_PDF.is_file(),
    reason="KEN invoice PDF is not in the repo",
)
def test_negative_line_tax_is_written_as_zero():
    record = parse_invoice_bytes(KEN_NEGATIVE_TAX_PDF.read_bytes())
    assert record["invoice_number"] == "31278204"
    assert Decimal(record["accounts"]["7420-001"]["tax"]) < 0
    assert record["accounts"]["7420-001"]["net"] == "-375.36"
    rows = list(csv.DictReader(StringIO(to_csv(record))))
    ops = [row for row in rows if row["Account"] == "5440 Operating Supplies"]
    assert ops[0]["Line Amount"] == "-375.36"
    assert ops[0]["Line Tax Amount"] == "0.00"
    assert Decimal(ops[0]["Line Tax Amount"]) >= 0
    assert all(Decimal(row["Line Tax Amount"]) >= 0 for row in rows)


@pytest.mark.skipif(
    not APPAREL_PDF.is_file(), reason="apparel invoice PDF is not in the repo"
)
def test_extra_gl_account_is_added_after_the_standard_rows():
    record = parse_invoice_bytes(APPAREL_PDF.read_bytes())
    assert record["invoice_number"] == "30039178"
    assert record["supplier"] == "Martin Brower of Canada"
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
        "7440-001",
    ]
    assert "7428-001" not in record["accounts"]
    assert record["accounts"]["7001-001"]["net"] == "15055.46"
    assert record["accounts"]["7001-001"]["tax"] == "0.11"
    assert record["accounts"]["7030-001"]["net"] == "2493.26"
    assert record["accounts"]["7030-001"]["tax"] == "124.68"
    assert record["accounts"]["7420-001"]["net"] == "636.55"
    assert record["accounts"]["7420-001"]["tax"] == "63.11"
    assert record["accounts"]["7426-001"]["net"] == "318.38"
    assert record["accounts"]["7426-001"]["tax"] == "15.91"
    assert record["accounts"]["7440-001"] == {
        "description": "APPAREL ACCESSORY",
        "net": "224.60",
        "tax": "26.94",
    }
    exported = list(csv.DictReader(StringIO(to_csv(record))))
    assert [row["Account"] for row in exported] == [
        "5020 Food Purchases",
        "5040 Paper Purchases",
        "5440 Operating Supplies",
        "4080 Cost of Non-Product Sales",
        "5430 Uniforms",
    ]
    uniforms = exported[-1]
    assert uniforms["Line Description"] == "Uniform Purchase"
    assert uniforms["Line Tax Code"] == "GST/PST MB"
    assert uniforms["Line Amount"] == "224.60"
    assert uniforms["Line Tax Amount"] == "26.94"


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
    assert text.splitlines()[0].startswith("Supplier,Bill Date,Due Date,Account,")
    assert "31308266" in text
    assert "10/07/2026" in text
    assert "Martin Brower of Canada" in text
    assert "5020 Food Purchases" in text
    assert "25921.00" in text
    assert "29.19" in text
    assert "32044.02" not in text
    assert "318.51" not in text
    assert "18.18" not in text


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
    assert "GST / HST / TPS" not in body
    assert ">PST</th>" not in body
    assert "25,921.00 (tax 29.19)" in body
    assert "3,649.29 (tax 227.74)" in body
    assert "259.70 (tax 31.16)" in body
    assert "971.93 (tax 48.60)" in body
    assert "905.41 (tax 0.00)" in body
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
    assert "7428-001 GIVE AWAY PROMO" not in body
    assert "7440-001 APPAREL ACCESSORY" in body
    assert "224.60 (tax 26.94)" in body
    assert "15,055.46 (tax 0.11)" in body
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
    assert b"Bill Date" in as_csv.data
    assert b"10/05/2026" in as_csv.data
    assert b"10/02/2026" not in as_csv.data
    assert b"15642.81" in as_csv.data
    assert b"5020 Food Purchases" in as_csv.data
    assert b"1.00" in as_csv.data
    assert b"19181.40" not in as_csv.data
    assert b"175.07" not in as_csv.data
    assert b"31278945" in as_csv.data


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
    assert b"Upload or drop PDFs" in bulk.data

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
    assert "10 line items" in body
    assert "Download CSV" in body
    assert "Supplier,Bill Date,Due Date,Account" in body
    assert "Verification" in body
    assert "GST 305.53" in body
    assert "GST/PST 31.16" in body
    assert "GST 140.70" in body
    assert "GST/PST 90.05" in body
    assert "Total − lines 0.00" in body

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
    rows = list(csv.DictReader(StringIO(text)))
    assert len(rows) == 10
    first = [row for row in rows if row["Bill No"] == "31308266"]
    second = [row for row in rows if row["Bill No"] == "30039178"]
    assert len(first) == 5
    assert len(second) == 5
    assert first[0]["Supplier"] == "Martin Brower of Canada"
    assert first[0]["Bill Date"] == "10/07/2026"
    assert first[0]["Due Date"] == "10/14/2026"
    assert "Terms" not in first[0]
    assert [row["Account"] for row in first] == [
        "5020 Food Purchases",
        "5040 Paper Purchases",
        "5440 Operating Supplies",
        "4080 Cost of Non-Product Sales",
        "5380 Promotion - Martin Brower",
    ]
    assert [row["Line Tax Code"] for row in first] == [
        "GST",
        "GST",
        "GST/PST MB",
        "GST",
        "Out of Scope",
    ]
    assert [row["Line Amount"] for row in first] == [
        "25921.00",
        "3649.29",
        "259.70",
        "971.93",
        "905.41",
    ]
    assert [row["Line Tax Amount"] for row in first] == [
        "29.19",
        "227.74",
        "31.16",
        "48.60",
        "0.00",
    ]
    assert first[4]["Line Description"] == "Monopoly Cost"
    assert [row["Account"] for row in second] == [
        "5020 Food Purchases",
        "5040 Paper Purchases",
        "5440 Operating Supplies",
        "4080 Cost of Non-Product Sales",
        "5430 Uniforms",
    ]
    assert [row["Line Amount"] for row in second] == [
        "15055.46",
        "2493.26",
        "636.55",
        "318.38",
        "224.60",
    ]
    assert [row["Line Tax Amount"] for row in second] == [
        "0.11",
        "124.68",
        "63.11",
        "15.91",
        "26.94",
    ]
    assert second[2]["Line Description"] == "Ops Supplies Purchase"
    assert second[2]["Line Tax Code"] == "GST/PST MB"
    assert second[-1]["Line Description"] == "Uniform Purchase"
    assert second[-1]["Line Tax Code"] == "GST/PST MB"
    assert "Filename" not in rows[0]


@pytest.mark.skipif(
    not RD_PDFS or not MM_PDFS,
    reason="Saskatchewan and Manitoba invoices are not in the repo",
)
def test_bulk_csv_keeps_each_invoice_pst_province(client):
    saskatchewan = RD_PDFS[0]
    manitoba = MM_PDFS[0]
    uploads = [
        (BytesIO(saskatchewan.read_bytes()), saskatchewan.name),
        (BytesIO(manitoba.read_bytes()), manitoba.name),
    ]
    downloaded = client.post(
        "/bulk?format=csv",
        data={"files": uploads},
        content_type="multipart/form-data",
    )
    assert downloaded.status_code == 200
    rows = list(csv.DictReader(StringIO(downloaded.get_data(as_text=True))))
    sk_bill = parse_invoice_bytes(saskatchewan.read_bytes())["invoice_number"]
    mb_bill = parse_invoice_bytes(manitoba.read_bytes())["invoice_number"]
    sk_codes = {
        row["Line Tax Code"] for row in rows if row["Bill No"] == sk_bill
    }
    mb_codes = {
        row["Line Tax Code"] for row in rows if row["Bill No"] == mb_bill
    }
    assert "GST/PST SK" in sk_codes
    assert "GST/PST MB" not in sk_codes
    assert "GST/PST MB" in mb_codes
    assert "GST/PST SK" not in mb_codes


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


def test_bulk_leaves_a_negative_bill_out_of_the_csv(client):
    if not SAMPLE_PDF.is_file() or not KEN_REFUND_PDF.is_file():
        pytest.skip("sample invoice PDFs are not in the repo")
    uploads = [
        (BytesIO(SAMPLE_PDF.read_bytes()), SAMPLE_PDF.name),
        (BytesIO(KEN_REFUND_PDF.read_bytes()), KEN_REFUND_PDF.name),
    ]
    page = client.post(
        "/bulk",
        data={"files": uploads},
        content_type="multipart/form-data",
    )
    body = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "left out of the CSV" in body
    assert KEN_REFUND_PDF.name in body
    assert "Bill total is negative" in body
    assert "31308266" in body
    downloaded = client.post(
        "/bulk?format=csv",
        data={
            "files": [
                (BytesIO(SAMPLE_PDF.read_bytes()), SAMPLE_PDF.name),
                (BytesIO(KEN_REFUND_PDF.read_bytes()), KEN_REFUND_PDF.name),
            ]
        },
        content_type="multipart/form-data",
    )
    text = downloaded.get_data(as_text=True)
    assert downloaded.status_code == 200
    assert "31308266" in text
    assert "31368179" not in text
