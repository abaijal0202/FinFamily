"""Parser for SBI (State Bank of India) "Account Statement" PDFs — the
combined YONO/internet-banking export that lists a savings account's ledger
alongside a separate Fixed Deposits (TDR/STDR) table.

Like HDFC, SBI's "Print to PDF" statement has a broken/unusable text layer,
so this module operates on OCR'd page text (see ocr.py). Unlike HDFC,
Tesseract's default `--psm 6` ("uniform block of text") drops the masked
savings-account-number line entirely (it renders as a separate visual
"chip" the segmenter misses) and mangles the transaction-ledger rows at
300dpi. `--psm 4` ("single column of variable-sized text") at 400dpi reads
both cleanly, so this module declares PREFERRED_OCR and
statement_import.parse_pdf() re-runs OCR at those settings once SBI has
been detected on the initial (cheaper, default-settings) pass.

Layout:
  - "FIXED DEPOSITS" page: one line per TDR/STDR --
    TERM DEPOSIT <masked acct> <open date> <principal> <P/S> <SINGLE/JOINT>
    <ROI%> <lien amt> <interest accrued> <maturity amount> <maturity date> <Yes/No>
  - "TRANSACTION DETAILS" / "SAVING ACCOUNT" page: masked account number,
    then an account-details key/value block, then "TRANSACTION OVERVIEW"
    bounded by "Your Opening Balance on <date>: = <amt>" and
    "Your Closing Balance on <date>: = <amt>". Each ledger line is
    `<date> <narration...> <amounts...>`, ending in the running balance —
    the two "credit"/"debit" amount columns before it are unreliable (OCR
    frequently drops the leading zero or a decimal point), so this parser
    ignores them and infers withdrawal/deposit from the balance delta
    against the previous row instead, which is exact.
"""
import re
from datetime import datetime

from ..types import ParsedTransaction, ParsedAccount, ParsedFixedDeposit, ParsedStatement

# Bumps OCR quality for this bank specifically -- see module docstring.
PREFERRED_OCR = {"dpi": 400, "psm": 4}

_MASKED_ACCOUNT_RE = re.compile(r"^[Xx]{4,}[A-Za-z0-9]{2,8}$")
_DATE2_RE = re.compile(r"^(\d{2}-\d{2}-\d{2})\s+(.*)$")
_AMOUNT_RE = re.compile(r"[\d,]+\.\d{2}")
_OPENING_RE = re.compile(r"Your Opening Balance on (\d{2}-\d{2}-\d{2}):?\s*=?\s*([\d,]+\.\d{2})")
_CLOSING_RE = re.compile(r"Your Closing Balance on (\d{2}-\d{2}-\d{2}):?\s*=?\s*([\d,]+\.\d{2})")
_FD_ROW_RE = re.compile(
    r"^TERM DEPOSIT\s+([Xx]{2,}\d{2,8})\s+(\d{2}-\d{2}-\d{2})\s+([\d,]+\.\d{2})\s+([PS])\s+"
    r"(SINGLE|JOINT)\s+([\d.]+)\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+"
    r"(\d{2}-\d{2}-\d{2})\s+(Yes|No)\s*$"
)

# Footer/disclaimer lines inside the TRANSACTION OVERVIEW block that must
# never be folded into the previous row's narration.
STOP_PREFIXES = (
    "*All dates", ">", "Visit https", "Nomination", "Make claims",
    "Nominate them", "customer needs", "TDS Certificates", "Contents of this",
)


def detect(page_texts):
    joined = "\n".join(page_texts[:2]).upper()
    return "SBI" in joined and (
        "SBI.CO.IN" in joined or "SBIN0" in joined or "STATE BANK OF INDIA" in joined
    )


def _to_amount(s):
    return float(s.replace(",", ""))


def _to_date2(s):
    """SBI statement dates are DD-MM-YY (2-digit year)."""
    return datetime.strptime(s, "%d-%m-%y").date()


def _parse_savings_section(lines, i, n, warnings):
    """Parse one 'SAVING ACCOUNT' block starting at index i (the line
    reading exactly 'SAVING ACCOUNT'). Returns (ParsedAccount, next_index)."""
    account_number = None
    j = i + 1
    if j < n and _MASKED_ACCOUNT_RE.match(lines[j].strip()):
        digits = re.sub(r"[^0-9]", "", lines[j].strip())
        account_number = digits or lines[j].strip()
        j += 1

    account = ParsedAccount(account_number=account_number or "UNKNOWN", account_type="Savings")
    opening_seen = False
    closed = False

    while j < n:
        line = lines[j].strip()
        j += 1
        if not line:
            continue

        m = _OPENING_RE.search(line)
        if m:
            account.opening_balance = _to_amount(m.group(2))
            opening_seen = True
            continue

        m = _CLOSING_RE.search(line)
        if m:
            account.closing_balance = _to_amount(m.group(2))
            closed = True
            break

        if not opening_seen:
            continue  # still inside the account-details key/value block

        date_match = _DATE2_RE.match(line)
        if date_match:
            rest = date_match.group(2)
            amounts = _AMOUNT_RE.findall(rest)
            if amounts:
                balance_after = _to_amount(amounts[-1])
                narration = rest[:rest.find(amounts[0])].strip(" -")
                prev_balance = (account.transactions[-1].balance_after
                                if account.transactions else account.opening_balance) or 0.0
                delta = round(balance_after - prev_balance, 2)
                withdrawal = round(-delta, 2) if delta < 0 else 0.0
                deposit = delta if delta > 0 else 0.0
                account.transactions.append(ParsedTransaction(
                    txn_date=_to_date2(date_match.group(1)),
                    narration=narration or "(narration unreadable)",
                    withdrawal=withdrawal, deposit=deposit, balance_after=balance_after,
                ))
            else:
                warnings.append(f"Account {account.account_number}: could not read an amount on line: {line!r}")
            continue

        if line.startswith(STOP_PREFIXES):
            continue

        # Wrapped narration continuation (reference text wraps to the next
        # line, e.g. "...POOJA" / "MEHRA*BATC").
        if account.transactions:
            account.transactions[-1].narration = (
                account.transactions[-1].narration + " " + line
            ).strip()

    if not opening_seen:
        warnings.append(f"Account {account.account_number}: could not find an opening balance line")
    if not closed:
        warnings.append(f"Account {account.account_number}: could not find a closing balance line")
        if account.transactions:
            account.closing_balance = account.transactions[-1].balance_after

    return account, j


def parse(page_texts):
    warnings = []
    accounts = []
    fixed_deposits = []

    full_lines = []
    for page_text in page_texts:
        full_lines.extend(page_text.splitlines())
    n = len(full_lines)

    i = 0
    while i < n:
        line = full_lines[i].strip()

        if line.upper() == "SAVING ACCOUNT":
            account, i = _parse_savings_section(full_lines, i, n, warnings)
            accounts.append(account)
            continue

        m = _FD_ROW_RE.match(line)
        if m:
            (fd_no, open_date, principal, _holding, _mode, roi, _lien,
             _interest_accrued, maturity_amt, maturity_date, _nominee) = m.groups()
            fixed_deposits.append(ParsedFixedDeposit(
                fd_number=fd_no,
                principal=_to_amount(principal),
                open_date=_to_date2(open_date),
                rate=float(roi),
                current_amount=_to_amount(principal),
                maturity_date=_to_date2(maturity_date),
                maturity_amount=_to_amount(maturity_amt),
            ))
            i += 1
            continue

        i += 1

    if not accounts and not fixed_deposits:
        warnings.append(
            "No savings account transactions or fixed deposits were recognised in this SBI statement."
        )

    return ParsedStatement(bank="SBI", accounts=accounts, fixed_deposits=fixed_deposits, warnings=warnings)
