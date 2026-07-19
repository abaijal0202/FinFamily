"""HDFC parser against synthetic OCR-style text (no real statement needed)."""
from datetime import date

from statement_import.parsers import hdfc
from statement_import.registry import detect_bank

PAGE = """HDFC BANK Ltd. We understand your world
Account Number : 12345678901234
Account Type : SAVINGS
Opening Balance : 10,000.00
01/04/2026 UPI-SWIGGY-ORDER99 500.00 0.00 9,500.00
02/04/2026 SALARY JUL ACME CORP 0.00 50,000.00 59,500.00
SUMMARY
10,000.00 500.00 50,000.00 59,500.00
FD DETAILS
50300012345678 INR 100,000.00 01/01/2026 7.10 107,100.00 YES
101,750.00 01/01/2027 107,100.00
Disclaimer
"""


def test_detect():
    assert hdfc.detect([PAGE]) is True
    assert detect_bank([PAGE]) == "HDFC"
    assert detect_bank(["Some Other Bank Statement"]) == "Generic"


def test_parse_account_summary_is_authoritative():
    parsed = hdfc.parse([PAGE])
    assert len(parsed.accounts) == 1
    acc = parsed.accounts[0]
    assert acc.account_number == "12345678901234"
    assert acc.opening_balance == 10_000.00
    assert acc.debit_total == 500.00
    assert acc.credit_total == 50_000.00
    assert acc.closing_balance == 59_500.00
    # reconciliation the docstring promises: open - debit + credit == close
    assert acc.opening_balance - acc.debit_total + acc.credit_total == acc.closing_balance


def test_parse_transactions():
    acc = hdfc.parse([PAGE]).accounts[0]
    assert len(acc.transactions) == 2
    t1, t2 = acc.transactions
    assert t1.txn_date == date(2026, 4, 1)
    assert t1.withdrawal == 500.00 and t1.deposit == 0.00
    assert "SWIGGY" in t1.narration
    assert t2.deposit == 50_000.00 and t2.balance_after == 59_500.00


def test_parse_fd():
    parsed = hdfc.parse([PAGE])
    assert len(parsed.fixed_deposits) == 1
    fd = parsed.fixed_deposits[0]
    assert fd.principal == 100_000.00
    assert fd.rate == 7.10
    assert fd.current_amount == 101_750.00
    assert fd.maturity_date == date(2027, 1, 1)
    assert parsed.warnings == []
