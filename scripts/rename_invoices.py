#!/usr/bin/env python3
"""Rename Martin-Brower invoice PDFs to '{store} {bill} {Mon-DD-YYYY}.pdf'.

Usage:
    python scripts/rename_invoices.py invoice.pdf [invoice.pdf ...]
    python scripts/rename_invoices.py -n *.pdf
    excalibur rename-invoices invoice.pdf
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from excalibur.cli import cli

if __name__ == "__main__":
    cli(args=["rename-invoices", *sys.argv[1:]], prog_name="rename_invoices.py")
