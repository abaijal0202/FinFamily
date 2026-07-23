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

That said, real-world Tesseract builds vary (a real production run OCR'd
the same document differently than the one this module was tuned against:
the masked account number and the "Your Opening Balance on ..." line both
failed to match, which — because transaction parsing used to be gated on
that opening-balance line matching — silently dropped every transaction
with only a vague warning, and a fixed deposit row silently vanished with
no warning at all). This module now trades a little precision for a lot of
resilience:
  - every line is whitespace-normalized (runs of spaces collapsed) and has
    unicode dash variants (–—−) folded to a plain hyphen before any regex
    runs against it, since OCR font substitution is a common source of
    near-miss failures;
  - the masked account number is searched for in a small window of lines
    after the "SAVING ACCOUNT" header, not just the immediate next line;
  - the opening/closing balance lines are matched case-insensitively and
    tolerate wrapping onto the next OCR line;
  - transaction rows are recognised by their own date prefix, not gated on
    the opening-balance line having matched first — so a missed opening
    balance no longer takes the whole ledger down with it (it only means
    the very first transaction's withdrawal/deposit split can't be
    inferred, which is flagged with its own warning);
  - a "TERM DEPOSIT..." line that fails to fully parse now raises a
    warning instead of being silently skipped.

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
_OPENING_RE = re.compile(r"Your Opening Balance on (\d{2}-\d{2}-\d{2}):?\s*=?\s*([\d,]+\.\d{2})", re.IGNORECASE)
_CLOSING_RE = re.compile(r"Your Closing Balance on (\d{2}-\d{2}-\d{2}):?\s*=?\s*([\d,]+\.\d{2})", re.IGNORECASE)
_FD_ROW_RE = re.compile(
    r"^TERM DEPOSIT\s+([Xx]{2,}[A-Za-z0-9]{2,8})\s+(\d{2}-\d{2}-\d{2})\s+([\d,]+\.\d{2})\s+([PS])\s+"
    r"(SINGLE|JOINT)\s+([\d.]+)\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+"
    r"(\d{2}-\d{2}-\d{2})\s+(Yes|No)\s*$",
    re.IGNORECASE,
)
_FD_CANDIDATE_RE = re.compile(r"^TERM DEPOSIT\b", re.IGNORECASE)
_DASH_RE = re.compile(u"[‐‑‒–—―−]")  # unicode dash/minus variants -> '-'

# Footer/disclaimer lines inside the TRANSACTION OVERVIEW block that must
# never be folded into the previous row's narration.
STOP_PREFIXES = (
    "*All dates", ">", "Visit https", "Nomination", "Make claims",
    "Nominate them", "customer needs", "TDS Certificates", "Contents of this",
)


def _normalize_line(raw):
    """Collapse OCR whitespace noise and fold unicode dash variants to '-'
    so the regexes below don't have to account for font-substitution noise
    directly. Keeps the line otherwise intact (case, digits, punctuation)."""
    line = _DASH_RE.sub("-", raw)
    return re.sub(r"[ \t]+", " ", line).strip()


def _find_masked_account(lines, start, n, window=6):
    """Scan up to `window` lines from `start` for the masked account-number
    line. OCR sometimes shifts it by a line or garbles whitespace/case
    inside it, so this doesn't assume it's exactly the very next line."""
    end = min(start + window, n)
    for j in range(start, end):
        candidate = re.sub(r"\s+", "", lines[j])
        if candidate and _MASKED_ACCOUNT_RE.match(candidate):
            digits = re.sub(r"[^0-9]", "", candidate)
            return digits or candidate, j + 1
    return None, start


def _search_with_lookahead(regex, line, lookahead, label):
    """Try to match `regex` against `line` alone, then -- only if `line`
    already looks like the start of that balance line (contains `label`,
    e.g. "Opening Balance") -- against `line` joined with `lookahead`, to
    handle it wrapping onto a second OCR row. The `label` guard matters: a
    transaction line immediately followed by the closing-balance line must
    NOT get swallowed by this just because the *next* line happens to
    contain "Closing Balance" -- re.search on the joined string would
    otherwise match regardless of what's in `line` itself.
    Returns (match_or_None, consumed_lookahead_bool)."""
    m = regex.search(line)
    if m:
        return m, False
    if lookahead and label.lower() in line.lower():
        m = regex.search(line + " " + lookahead)
        if m:
            return m, True
    return None, False


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
    reading exactly 'SAVING ACCOUNT'). Returns (ParsedAccount, next_index).

    Transaction rows are recognised purely by their own DD-MM-YY date
    prefix -- parsing is *not* gated on the opening-balance line matching
    first. That used to be a single point of failure: if OCR mangled just
    that one line, every transaction silently vanished with only a vague
    warning. Now a missed opening balance only affects the first
    transaction's withdrawal/deposit inference (flagged on its own below),
    not the rest of the ledger.
    """
    account_number, j = _find_masked_account(lines, i + 1, n)
    if account_number is None:
        warnings.append(
            "Could not find the masked savings account number after the "
            "'SAVING ACCOUNT' header -- please fill in the account number "
            "and last 4 digits manually on the review screen."
        )

    account = ParsedAccount(account_number=account_number or "UNKNOWN", account_type="Savings")
    closed = False

    while j < n:
        line = lines[j]
        lookahead = lines[j + 1] if j + 1 < n else None
        j += 1
        if not line:
            continue

        m, used_next = _search_with_lookahead(_OPENING_RE, line, lookahead, "Opening Balance")
        if m:
            account.opening_balance = _to_amount(m.group(2))
            if used_next:
                j += 1
            continue

        m, used_next = _search_with_lookahead(_CLOSING_RE, line, lookahead, "Closing Balance")
        if m:
            account.closing_balance = _to_amount(m.group(2))
            closed = True
            if used_next:
                j += 1
            break

        date_match = _DATE2_RE.match(line)
        if date_match:
            rest = date_match.group(2)
            amounts = _AMOUNT_RE.findall(rest)
            if amounts:
                balance_after = _to_amount(amounts[-1])
                narration = rest[:rest.find(amounts[0])].strip(" -")
                if account.transactions:
                    prev_balance = account.transactions[-1].balance_after
                elif account.opening_balance is not None:
                    prev_balance = account.opening_balance
                else:
                    prev_balance = balance_after  # can't infer a sign yet -- flag it
                    warnings.append(
                        f"Account {account.account_number}: the opening balance was "
                        f"never read, so the first transaction (on {date_match.group(1)}) "
                        f"couldn't be classified as a withdrawal or deposit -- please "
                        f"check/edit that row manually."
                    )
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
        # line, e.g. "...POOJA" / "MEHRA*BATC"). Harmless no-op before the
        # first transaction exists (e.g. still inside the account-details
        # key/value block).
        if account.transactions:
            account.transactions[-1].narration = (
                account.transactions[-1].narration + " " + line
            ).strip()

    if account.opening_balance is None:
        warnings.append(
            f"Account {account.account_number}: could not find an opening balance line -- "
            f"the first transaction's withdrawal/deposit split may be wrong; please verify."
        )
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
        full_lines.extend(_normalize_line(l) for l in page_text.splitlines())
    n = len(full_lines)

    i = 0
    while i < n:
        line = full_lines[i]

        if line.upper() == "SAVING ACCOUNT":
            account, i = _parse_savings_section(full_lines, i, n, warnings)
            accounts.append(account)
            continue

        if _FD_CANDIDATE_RE.match(line):
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
            else:
                warnings.append(
                    f"Found a fixed deposit line that could not be fully read -- it was "
                    f"skipped rather than guessed at. Please add it manually on the review "
                    f"screen if it's missing: {line!r}"
                )
            i += 1
            continue

        i += 1

    if not accounts and not fixed_deposits:
        warnings.append(
            "No savings account transactions or fixed deposits were recognised in this SBI statement."
        )

    return ParsedStatement(bank="SBI", accounts=accounts, fixed_deposits=fixed_deposits, warnings=warnings)
