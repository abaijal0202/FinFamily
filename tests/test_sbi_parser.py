"""SBI parser against real (lightly trimmed) Tesseract OCR output --
`--psm 4` at 400dpi, the PREFERRED_OCR settings this parser declares -- from
an actual SBI combined statement (savings account + 5 term deposits)."""
from datetime import date

from statement_import.parsers import sbi
from statement_import.registry import detect_bank

FD_PAGE = """Welcome Mrs. POOJA MEHRA
S [3 | Customer XXXXXXX8787
Visit https://sbi.co.in Customer Care Number: 1800 1234 Customer Care Email: customercare@sbi.co.in

FIXED DEPOSITS

TDR AND STDR ACCOUNTS CURRENCY: INR
Account Type Account Number Account Open Principal Amount Holding Mode Of Operation ROI (%) Lien/Hold Amount Interest Accrued Maturity Amount Maturity Date Nominee

TERM DEPOSIT XXXXXXX0127 05-03-25 500000.00 P SINGLE 7.00 0.00 44089.82 613486.00 15-02-28 Yes
TERM DEPOSIT XXXXXXX5653 05-03-25 500000.00 P SINGLE 7.00 0.00 44089.82 574441.00 05-03-27 Yes
TERM DEPOSIT XXXXXXX0424 21-02-26 1250000.00 P SINGLE 6.45 0.00 28393.41 1351200.00 11-05-27 Yes
TERM DEPOSIT XXXXXXX6650 27-05-26 1050000.00 P SINGLE 6.45 0.00 6441.23 1135008.00 14-08-27 Yes
TERM DEPOSIT XXXXXXX2893 09-06-26 1000000.00 P SINGLE 6.45 0.00 3855.97 1080960.00 27-08-27 Yes
*All dates are in DD-MM-YY
**ROI: Rate Of Interest
"""

SAVINGS_PAGE = """Welcome Mrs. POOJA MEHRA
S B} | Customer XXXXXXX8787
Visit https://sbi.co.in Customer Care Number: 1800 1234 Customer Care Email: customercare@sbi.co.in

TRANSACTION DETAILS

SAVING ACCOUNT
XXXXXXKX2973

Name of the Account Holder Mrs. POOJA MEHRA
Mr. ASHISH BAIJAL
Secondary Account Holders Name
Branch Name PBB HIRANANDANI, MUMBAI
Branch Code 04234
MICR Code 400002145
IFSC Code SBIN0004234
Nominee Registered Yes
Available Balance 6996.00
Multi-Option Deposit Balance 0.00

TRANSACTION OVERVIEW

Your Opening Balance on 01-06-26: = 155957.00
04-06-26 NEFT UTR NO: SBIN426155854683 - 0 50000.00 105957.00
05-06-26 NEFT UTR NO: SBIN526156842637 - 0 100000.00 5957.00
08-06-26 NEFT*IBKLONEFT01*0608i29913481721*POOJA 250000.00 0 255957.00
MEHRA*BATC
09-06-26 NEFT*IBKLONEFT01*0609i29915311361*POOJA 250000.00 0 505957.00
MEHRA*BATC
09-06-26 NEFT*IBKLONEFT01*0609i29915314381*POOJA 250000.00 0 755957.00
MEHRA*BATC
09-06-26 NEFT*IBKLONEFT01*0609i29915379361*POOJA 250000.00 0 1005957.00
MEHRA*BATC
09-06-26 FIRDFTFRREF:235870087933192953K580035 - 0 1000000.00 5957.00
25-06-26 INTEREST CREDIT - 1039.00 0 6996.00
Your Closing Balance on 30-06-26: = 6996.00

*All dates are in DD-MM-YY format

> Contents of this statement will be considered correct if no error is reported within 30 days of receipt of the statement.

Visit https://sbi.co.in Customer Care Number: 1800 1234 Customer Care Email: customercare@sbi.co.in
"""


def test_detect():
    assert sbi.detect([FD_PAGE]) is True
    assert sbi.detect([SAVINGS_PAGE]) is True
    assert detect_bank([SAVINGS_PAGE]) == "SBI"
    assert detect_bank(["Some Other Bank Statement"]) == "Generic"


def test_preferred_ocr_declared():
    """This bank needs --psm 4 at 400dpi -- statement_import.parse_pdf()
    re-OCRs once it identifies SBI on the initial pass."""
    assert sbi.PREFERRED_OCR == {"dpi": 400, "psm": 4}


def test_parse_fixed_deposits():
    parsed = sbi.parse([FD_PAGE])
    assert len(parsed.fixed_deposits) == 5

    fd = parsed.fixed_deposits[0]
    assert fd.fd_number == "XXXXXXX0127"
    assert fd.principal == 500_000.00
    assert fd.open_date == date(2025, 3, 5)
    assert fd.rate == 7.00
    assert fd.current_amount == 500_000.00  # matches SBI's own reported FD total, not principal+accrued
    assert fd.maturity_amount == 613_486.00
    assert fd.maturity_date == date(2028, 2, 15)

    # matches the account summary's "Total INR: Rs. 4306996.00" (FDs 4,300,000 + savings 6,996)
    assert sum(f.principal for f in parsed.fixed_deposits) == 4_300_000.00


def test_parse_savings_account_number_from_masked_line():
    parsed = sbi.parse([SAVINGS_PAGE])
    assert len(parsed.accounts) == 1
    acc = parsed.accounts[0]
    # "XXXXXXKX2973" (OCR noise: stray K) -> digits only
    assert acc.account_number == "2973"
    assert acc.account_type == "Savings"


def test_parse_savings_opening_and_closing_balance():
    acc = sbi.parse([SAVINGS_PAGE]).accounts[0]
    assert acc.opening_balance == 155_957.00
    assert acc.closing_balance == 6_996.00


def test_parse_savings_transactions_via_balance_delta():
    """The two amount columns before the running balance are unreliable
    from OCR (a bare '0' with no decimal, or a dropped digit) -- withdrawal
    vs deposit is inferred from the balance delta instead, which is exact."""
    acc = sbi.parse([SAVINGS_PAGE]).accounts[0]
    assert len(acc.transactions) == 8

    t = acc.transactions
    assert t[0].txn_date == date(2026, 6, 4)
    assert t[0].withdrawal == 50_000.00 and t[0].deposit == 0.0
    assert t[0].balance_after == 105_957.00

    assert t[1].withdrawal == 100_000.00 and t[1].balance_after == 5_957.00

    # wrapped narration ("...POOJA" + "MEHRA*BATC" on the next line) folds
    # into one transaction, and is correctly read as a deposit
    assert t[2].deposit == 250_000.00 and t[2].balance_after == 255_957.00
    assert "POOJA" in t[2].narration and "MEHRA" in t[2].narration

    assert t[3].deposit == 250_000.00 and t[3].balance_after == 505_957.00
    assert t[4].deposit == 250_000.00 and t[4].balance_after == 755_957.00
    assert t[5].deposit == 250_000.00 and t[5].balance_after == 1_005_957.00

    assert t[6].withdrawal == 1_000_000.00 and t[6].balance_after == 5_957.00
    assert "FIRDFTFRREF" in t[6].narration

    assert t[7].deposit == 1_039.00 and t[7].balance_after == 6_996.00
    assert "INTEREST CREDIT" in t[7].narration

    # full reconciliation: opening - total debit + total credit == closing
    acc_obj = sbi.parse([SAVINGS_PAGE]).accounts[0]
    total_debit = sum(x.withdrawal for x in acc_obj.transactions)
    total_credit = sum(x.deposit for x in acc_obj.transactions)
    assert acc_obj.opening_balance - total_debit + total_credit == acc_obj.closing_balance
    assert sbi.parse([SAVINGS_PAGE]).warnings == []


def test_parse_combined_statement_both_sections():
    parsed = sbi.parse([FD_PAGE, SAVINGS_PAGE])
    assert len(parsed.accounts) == 1
    assert len(parsed.fixed_deposits) == 5
    assert parsed.bank == "SBI"
    assert parsed.warnings == []


def test_parse_pdf_re_ocrs_at_preferred_settings_for_sbi(monkeypatch):
    """statement_import.parse_pdf() should OCR once at the default settings
    to detect the bank, then re-OCR once more at SBI's PREFERRED_OCR before
    handing off to sbi.parse() -- a bank without PREFERRED_OCR (HDFC) must
    only be OCR'd once."""
    import statement_import

    calls = []

    def fake_ocr(pdf_path, dpi=300, psm=6, password=None, first_page=None, last_page=None):
        calls.append({"dpi": dpi, "psm": psm})
        return [SAVINGS_PAGE, FD_PAGE]

    monkeypatch.setattr(statement_import, "pdf_to_page_texts", fake_ocr)

    bank, parsed = statement_import.parse_pdf("dummy.pdf")
    assert bank == "SBI"
    assert len(parsed.accounts) == 1 and len(parsed.fixed_deposits) == 5
    assert calls == [
        {"dpi": 300, "psm": 6},   # initial detection pass, default settings
        {"dpi": 400, "psm": 4},   # SBI's PREFERRED_OCR re-pass
    ]


def test_parse_pdf_does_not_re_ocr_for_banks_without_preferred_settings(monkeypatch):
    import statement_import

    calls = []

    def fake_ocr(pdf_path, dpi=300, psm=6, password=None, first_page=None, last_page=None):
        calls.append({"dpi": dpi, "psm": psm})
        return ["HDFC BANK Ltd. We understand your world"]

    monkeypatch.setattr(statement_import, "pdf_to_page_texts", fake_ocr)

    bank, _ = statement_import.parse_pdf("dummy.pdf")
    assert bank == "HDFC"
    assert len(calls) == 1  # no second OCR pass
