"""Best-effort, bank-agnostic statement parser.

Used as the fallback when no bank-specific parser (see registry.py)
recognizes the statement. Every Indian bank formats its PDF statements
differently -- column order, date format, whether debit/credit are separate
columns or a single signed amount, running-balance placement -- so this
makes minimal assumptions instead of hard-coding one bank's layout:

- A transaction line is any line starting with a date in one of several
  common formats, followed by narration and 1-3 trailing amount-like
  numbers.
- 3 trailing amounts -> (withdrawal, deposit, balance), the most common
  Indian-bank convention.
- 2 trailing amounts -> look for a "DR"/"CR" marker near the first amount
  to decide which column it is; if no marker is found the row is flagged
  as ambiguous rather than guessed.
- 1 trailing amount -> assumed to be a running balance only; also flagged.
- The whole document is treated as a single account (most banks issue one
  PDF per account; statements that bundle several accounts under one PDF,
  like HDFC's combined statement, need a dedicated parser - see hdfc.py).

Because this is inherently a guess, every import that uses this parser is
flagged with a warning, and the review screen lets the user edit or
correct every field (and add missing accounts/transactions by hand)
before anything is written to the ledger.
"""
import re
from datetime import datetime

from ..types import ParsedTransaction, ParsedAccount, ParsedStatement

DATE_FORMATS = [
    (re.compile(r"^(\d{2}/\d{2}/\d{4})\s+(.+)$"), "%d/%m/%Y"),
    (re.compile(r"^(\d{2}-\d{2}-\d{4})\s+(.+)$"), "%d-%m-%Y"),
    (re.compile(r"^(\d{4}-\d{2}-\d{2})\s+(.+)$"), "%Y-%m-%d"),
    (re.compile(r"^(\d{2}-[A-Za-z]{3}-\d{4})\s+(.+)$"), "%d-%b-%Y"),
    (re.compile(r"^(\d{2}\s[A-Za-z]{3}\s\d{4})\s+(.+)$"), "%d %b %Y"),
    (re.compile(r"^(\d{2}/\d{2}/\d{2})\s+(.+)$"), "%d/%m/%y"),
]

AMOUNT_RE = re.compile(r"-?[\d,]+\.\d{2}")
DR_MARK_RE = re.compile(r"\bDR\b", re.IGNORECASE)
CR_MARK_RE = re.compile(r"\bCR\b", re.IGNORECASE)
ACCOUNT_NO_RE = re.compile(
    r"(?:Account\s*(?:No\.?|Number)|A/?C\s*No\.?)\s*[:=\-—]*\s*([0-9Xx]{6,20})", re.IGNORECASE
)
OPENING_BAL_RE = re.compile(
    r"(?:Opening\s*Balance|Balance\s*B/?F|Balance\s*Forward)\s*[:=]?\s*(-?[\d,]+\.\d{2})", re.IGNORECASE
)
CLOSING_BAL_RE = re.compile(
    r"(?:Closing\s*Balance|Balance\s*C/?F)\s*[:=]?\s*(-?[\d,]+\.\d{2})", re.IGNORECASE
)

# Header/footer lines that show up across most bank statement layouts --
# never part of a transaction narration, whichever bank issued it.
NOISE_PREFIXES = (
    "STATEMENT", "PAGE ", "IFSC", "MICR", "BRANCH", "ACCOUNT NO", "A/C NO",
    "ACCOUNT NUMBER", "OPENING BALANCE", "CLOSING BALANCE", "TOTAL",
    "BALANCE B/F", "BALANCE C/F", "GSTIN", "CIN:", "REGISTERED OFFICE",
    "NOMINATION", "CUSTOMER ID", "STATEMENT PERIOD", "CURRENCY", "PAN",
)


def detect(page_texts):
    """Fallback parser: the registry only reaches this after every
    bank-specific parser has declined, so it always accepts."""
    return True


def _try_parse_date(line):
    for pattern, fmt in DATE_FORMATS:
        m = pattern.match(line)
        if m:
            try:
                return datetime.strptime(m.group(1), fmt).date(), m.group(2)
            except ValueError:
                continue
    return None, None


def _to_amount(s):
    return float(s.replace(",", ""))


def parse(page_texts):
    warnings = [
        "Statement issuer wasn't recognized by any bank-specific parser, so the "
        "generic best-effort reader was used. Please check every row below "
        "carefully -- especially the withdrawal/deposit split and balances -- "
        "and correct anything wrong (or add missing rows/accounts) before confirming."
    ]

    full_lines = []
    for page_text in page_texts:
        full_lines.extend(page_text.splitlines())

    account_number = None
    for line in full_lines[:60]:  # account number usually appears near the top
        m = ACCOUNT_NO_RE.search(line)
        if m:
            digits = re.sub(r"[Xx]", "", m.group(1))
            if digits:
                account_number = digits
                break

    account = ParsedAccount(account_number=account_number or "UNKNOWN")

    for line in full_lines:
        m = OPENING_BAL_RE.search(line)
        if m:
            account.opening_balance = _to_amount(m.group(1))
            break

    ambiguous_count = 0
    for raw in full_lines:
        line = raw.strip()
        if not line:
            continue

        txn_date, rest = _try_parse_date(line)
        if txn_date:
            amounts = AMOUNT_RE.findall(rest)
            withdrawal = deposit = balance_after = None
            narration = rest

            if len(amounts) >= 3:
                withdrawal, deposit, balance_after = (_to_amount(a) for a in amounts[-3:])
                narration = rest[:rest.find(amounts[-3])].strip()
            elif len(amounts) == 2:
                first_pos = rest.find(amounts[0])
                marker_window = rest[:first_pos + len(amounts[0]) + 8]
                withdrawal, deposit = 0.0, 0.0
                if DR_MARK_RE.search(marker_window):
                    withdrawal = _to_amount(amounts[0])
                elif CR_MARK_RE.search(marker_window):
                    deposit = _to_amount(amounts[0])
                else:
                    ambiguous_count += 1
                balance_after = _to_amount(amounts[1])
                narration = rest[:first_pos].strip()
            elif len(amounts) == 1:
                withdrawal, deposit = 0.0, 0.0
                balance_after = _to_amount(amounts[0])
                narration = rest[:rest.find(amounts[0])].strip()
                ambiguous_count += 1
            else:
                # No amounts on a date-led line - likely a wrapped reference,
                # not a real row. Fold into the previous transaction if any.
                if account.transactions:
                    account.transactions[-1].narration = (
                        account.transactions[-1].narration + " " + line
                    ).strip()
                continue

            account.transactions.append(ParsedTransaction(
                txn_date=txn_date, narration=narration or "(narration unreadable)",
                withdrawal=withdrawal, deposit=deposit, balance_after=balance_after,
            ))
            continue

        if line.upper().startswith(NOISE_PREFIXES):
            continue
        if account.transactions:
            account.transactions[-1].narration = (
                account.transactions[-1].narration + " " + line
            ).strip()

    if account.transactions:
        account.closing_balance = account.transactions[-1].balance_after
    else:
        for line in full_lines:
            m = CLOSING_BAL_RE.search(line)
            if m:
                account.closing_balance = _to_amount(m.group(1))
                break

    if ambiguous_count:
        warnings.append(
            f"{ambiguous_count} transaction(s) had an amount that couldn't be "
            "confidently classified as withdrawal vs deposit -- those default to "
            "0/0, please fill them in on the review screen."
        )
    if not account.transactions:
        warnings.append(
            "No transaction rows could be located automatically. You can still "
            "add the account and its transactions by hand below."
        )
    if account_number is None:
        warnings.append("Couldn't find an account number on this statement -- check the account details below.")

    return ParsedStatement(
        bank="Generic",
        accounts=[account],
        fixed_deposits=[],
        warnings=warnings,
    )
