"""Parser for HDFC Bank combined statements (savings accounts + FD annexure).

HDFC's "combined statement" PDF lists every savings account the customer
holds, one after another, each with its own transaction table and a
SUMMARY reconciliation block, followed (optionally) by an FD DETAILS table
and a TD-interest annexure. Statements for a single account follow the
same per-account structure minus the outer loop.

The PDF's text layer is unusable (broken CID font map), so this module
operates on OCR'd page text (see ocr.py, `--psm 6`) rather than PDF text
extraction. OCR introduces noise -- inconsistent colon/dash rendering,
stray characters, lines wrapping mid-reference -- so parsing is regex-based
and deliberately tolerant. The per-account SUMMARY block's own totals
(opening/debit/credit/closing) are treated as the source of truth for
balances; transaction-line parsing populates the ledger detail but is not
relied on for the balance itself. Validated against a real 8-page, 4-account
+ 2-FD combined statement: all four accounts reconcile exactly
(opening - debit + credit == closing) with zero parser warnings.
"""
import re
from datetime import datetime

from ..types import ParsedTransaction, ParsedAccount, ParsedFixedDeposit, ParsedStatement

DATE_RE = re.compile(r"^(\d{2}/\d{2}/\d{4})\s+(.+)$")
AMOUNT_RE = re.compile(r"[\d,]+\.\d{2}")
ACCOUNT_NUMBER_RE = re.compile(r"Account\s*Number\s*[\s:=\-—]*([0-9]{6,20})")
ACCOUNT_TYPE_RE = re.compile(r"Account\s*Type\s*:?\s*(.+)")
OPENING_BAL_RE = re.compile(r"Opening\s*Balance\s*:?\s*([\d,]+\.\d{2})")
SUMMARY_ROW_RE = re.compile(
    r"([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})"
)
FD_ROW1_RE = re.compile(
    r"^(\d{10,20})\s+([A-Z]{3})\s+([\d,]+\.\d{2})\s+(\d{2}/\d{2}/\d{4})\s+"
    r"([\d.]+)\s+([\d,]+\.\d{2})(?:\s+(YES))?"
)
FD_ROW2_RE = re.compile(r"^([\d,]+\.\d{2})\s+(\d{2}/\d{2}/\d{4})\s+([\d,]+\.\d{2})")

STOP_LINE_PREFIXES = (
    "SUMMARY", "Opening Balance", "Debit Count", "Total Withdrawal",
    "Total Sweep", "**", "***", "#", "Statement From", "Account Number",
    "Account Branch", "Account Type", "Nomination", "Expected AMB",
    "Savings Account Details", "Currency", "Customer ID", "Digitally",
    "Page ", "We understand", "Joint Holders", "RTGS/NEFT", "Validity",
    "Date:", "Reason:", "Location:",
)


def detect(page_texts):
    joined = "\n".join(page_texts[:2]).upper()
    return "HDFC BANK" in joined or "HDFC0" in joined or "WE UNDERSTAND YOUR WORLD" in joined


def _to_amount(s):
    return float(s.replace(",", ""))


def _to_date(s):
    return datetime.strptime(s, "%d/%m/%Y").date()


def parse(page_texts):
    warnings = []
    accounts = {}
    order = []
    locked_accounts = set()
    fixed_deposits = []
    current_account = None
    in_fd_table = False

    full_lines = []
    for page_text in page_texts:
        full_lines.extend(page_text.splitlines())

    i = 0
    n = len(full_lines)
    while i < n:
        raw = full_lines[i]
        line = raw.strip()
        i += 1
        if not line:
            continue

        if line.startswith("FD DETAILS"):
            in_fd_table = True
            current_account = None
            continue
        if line.startswith("Details of TD Interest") or line.startswith("Disclaimer"):
            in_fd_table = False
            current_account = None
            continue

        acct_match = ACCOUNT_NUMBER_RE.search(line)
        if acct_match:
            in_fd_table = False
            acc_no = acct_match.group(1)
            if acc_no not in accounts:
                accounts[acc_no] = ParsedAccount(account_number=acc_no)
                order.append(acc_no)
            current_account = accounts[acc_no]
            continue

        if in_fd_table:
            m1 = FD_ROW1_RE.match(line)
            if m1:
                fd_number, ccy, principal, open_date, rate, maturity_amt, _nom = m1.groups()
                current_amt, maturity_date = None, None
                if i < n:
                    m2 = FD_ROW2_RE.match(full_lines[i].strip())
                    if m2:
                        current_amt = _to_amount(m2.group(1))
                        maturity_date = _to_date(m2.group(2))
                        i += 1
                if maturity_date is None:
                    warnings.append(f"FD {fd_number}: could not read current amount / maturity date row")
                fixed_deposits.append(ParsedFixedDeposit(
                    fd_number=fd_number,
                    principal=_to_amount(principal),
                    open_date=_to_date(open_date),
                    rate=float(rate),
                    current_amount=current_amt if current_amt is not None else _to_amount(principal),
                    maturity_date=maturity_date,
                    maturity_amount=_to_amount(maturity_amt),
                ))
            continue

        if current_account is None:
            continue

        type_match = ACCOUNT_TYPE_RE.search(line)
        if type_match and not current_account.account_type:
            current_account.account_type = type_match.group(1).strip(" ;")
            continue

        open_match = OPENING_BAL_RE.search(line)
        if open_match and current_account.opening_balance is None:
            current_account.opening_balance = _to_amount(open_match.group(1))
            continue

        if line.startswith("SUMMARY"):
            for j in range(i, min(i + 4, n)):
                m = SUMMARY_ROW_RE.search(full_lines[j])
                if m:
                    ob, dr, cr, cb = (_to_amount(x) for x in m.groups())
                    if current_account.opening_balance is None:
                        current_account.opening_balance = ob
                    current_account.debit_total = dr
                    current_account.credit_total = cr
                    current_account.closing_balance = cb
                    break
            else:
                warnings.append(f"Account {current_account.account_number}: SUMMARY block found but totals row unreadable")
            locked_accounts.add(current_account.account_number)
            continue

        date_match = DATE_RE.match(line)
        if date_match:
            rest = date_match.group(2)
            amounts = AMOUNT_RE.findall(rest)
            if len(amounts) >= 3:
                txn_date = _to_date(date_match.group(1))
                withdrawal, deposit, balance_after = (_to_amount(a) for a in amounts[-3:])
                first_amount_pos = rest.find(amounts[-3])
                narration = rest[:first_amount_pos].strip()
                current_account.transactions.append(ParsedTransaction(
                    txn_date=txn_date, narration=narration,
                    withdrawal=withdrawal, deposit=deposit, balance_after=balance_after,
                ))
            elif current_account.transactions and current_account.account_number not in locked_accounts:
                current_account.transactions[-1].narration = (
                    current_account.transactions[-1].narration + " " + line
                ).strip()
            else:
                warnings.append(f"Account {current_account.account_number}: could not read amounts on line: {raw!r}")
                current_account.transactions.append(ParsedTransaction(
                    txn_date=_to_date(date_match.group(1)), narration=rest.strip(),
                    withdrawal=0.0, deposit=0.0, balance_after=None,
                ))
            continue

        if (current_account.transactions
                and current_account.account_number not in locked_accounts
                and not line.startswith(STOP_LINE_PREFIXES)):
            current_account.transactions[-1].narration = (
                current_account.transactions[-1].narration + " " + line
            ).strip()

    return ParsedStatement(
        bank="HDFC",
        accounts=[accounts[a] for a in order],
        fixed_deposits=fixed_deposits,
        warnings=warnings,
    )
