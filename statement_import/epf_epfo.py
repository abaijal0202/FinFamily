"""EPFO "EPF Member Passbook" import (Employees' Provident Fund).

Downloaded from the EPFO member portal (unifiedportal-mem.epfindia.gov.in),
this single-page-per-financial-year PDF has a clean, directly-extractable
text layer (Producer: iText, not a "print to PDF" scan) — no OCR needed,
`pdftotext -layout` is enough, same as the NPS KFintech statements.

Like the NPS module, this only reads the page-1 summary rows -- the
"OB Int. Updated upto <date>" opening-balance row and the
"Closing Balance as on <date>" row, each giving three running balances
(Employee / Employer / Pension), plus the "Total Contributions" /
"Total Transfer-Ins" / "Total Withdrawals for the year" rows used only for
a sanity-check warning. It deliberately does NOT parse the per-transaction
contribution/transfer rows above them -- the app needs the current EPF
balance and when it was last updated, not a full contribution ledger.

The Pension (EPS) column is tracked for information only and NOT included
in the Asset's current_value: unlike the Employee/Employer balance (an
actual, interest-bearing, withdrawable corpus), the EPS "balance" shown
here doesn't determine your pension -- that's a separate formula based on
last-drawn salary and years of service -- so counting it as net worth would
overstate what you actually have.

One Asset (category="EPF") is kept per Member ID (folio_number), so
re-importing a newer passbook (e.g. next financial year's) refreshes the
same holding instead of creating a duplicate. A member with multiple
EPF accounts (job changes without transfer) will get one Asset per
Member ID, which is correct -- they're genuinely separate balances until
transferred.
"""
import re
import subprocess
from datetime import datetime

from models import db, Asset, StatementImport, IMPORT_STATUS_CONFIRMED
import valuation

_ESTABLISHMENT_RE = re.compile(r"Establishment ID/Name\s+(\S+)\s*/\s*(.+)")
_MEMBER_RE = re.compile(r"Member ID/Name\s+(\S+)\s*/\s*(.+)")
_UAN_RE = re.compile(r"\bUAN\s+(\d{10,12})\b")
_FY_RE = re.compile(r"Financial Year\s*-\s*(\d{4}-\d{4})")
_OB_RE = re.compile(r"OB Int\. Updated upto (\d{2}/\d{2}/\d{4})\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)")
_CLOSING_RE = re.compile(r"Closing Balance as on (\d{2}/\d{2}/\d{4})\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)")
_TOTAL_CONTRIB_RE = re.compile(r"Total Contributions for the year \[\s*(\d{4})\s*\]\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)")
_TOTAL_TRANSFER_RE = re.compile(r"Total Transfer-Ins/VDRs for the year \[\s*(\d{4})\s*\]\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)")
_TOTAL_WITHDRAWAL_RE = re.compile(r"Total Withdrawals for the year \[\s*(\d{4})\s*\]\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)")


def _num(s):
    return float(s.replace(",", ""))


def _run_pdftotext(pdf_path, password=None, first_page=None, last_page=None):
    cmd = ["pdftotext", "-layout"]
    if first_page is not None:
        cmd += ["-f", str(first_page)]
    if last_page is not None:
        cmd += ["-l", str(last_page)]
    if password:
        cmd += ["-upw", password]
    cmd += [pdf_path, "-"]
    result = subprocess.run(cmd, capture_output=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.decode("utf-8", "ignore").strip() or "pdftotext failed"
        )
    return result.stdout.decode("utf-8", "ignore")


def parse_epf_text(text):
    """Pure parsing over already-extracted text -- kept separate from
    parse_epf_pdf() so it's unit-testable with canned text fixtures.

    Returns {"establishment_id", "establishment_name", "member_id",
    "member_name", "uan", "financial_year", "ob_date",
    "ob_employee"/"ob_employer"/"ob_pension", "closing_date",
    "closing_employee"/"closing_employer"/"closing_pension",
    "contrib_employee"/"contrib_employer"/"contrib_pension",
    "transfer_employee"/"transfer_employer"/"transfer_pension",
    "withdrawal_employee"/"withdrawal_employer"/"withdrawal_pension",
    "warnings"}.
    """
    warnings = []

    establishment_id = establishment_name = None
    m = _ESTABLISHMENT_RE.search(text)
    if m:
        establishment_id, establishment_name = m.group(1), m.group(2).strip()

    member_id = member_name = None
    m = _MEMBER_RE.search(text)
    if m:
        member_id, member_name = m.group(1), m.group(2).strip()
    else:
        warnings.append("Member ID not found in this EPF passbook")

    uan = None
    m = _UAN_RE.search(text)
    if m:
        uan = m.group(1)

    financial_year = None
    m = _FY_RE.search(text)
    if m:
        financial_year = m.group(1)

    result = {
        "establishment_id": establishment_id, "establishment_name": establishment_name,
        "member_id": member_id, "member_name": member_name, "uan": uan,
        "financial_year": financial_year,
        "ob_date": None, "ob_employee": None, "ob_employer": None, "ob_pension": None,
        "closing_date": None, "closing_employee": None, "closing_employer": None, "closing_pension": None,
        "contrib_employee": None, "contrib_employer": None, "contrib_pension": None,
        "transfer_employee": None, "transfer_employer": None, "transfer_pension": None,
        "withdrawal_employee": None, "withdrawal_employer": None, "withdrawal_pension": None,
        "warnings": warnings,
    }

    m = _OB_RE.search(text)
    if m:
        result["ob_date"] = datetime.strptime(m.group(1), "%d/%m/%Y").date()
        result["ob_employee"] = _num(m.group(2))
        result["ob_employer"] = _num(m.group(3))
        result["ob_pension"] = _num(m.group(4))
    else:
        warnings.append("Opening balance row ('OB Int. Updated upto ...') not found")

    m = _CLOSING_RE.search(text)
    if m:
        result["closing_date"] = datetime.strptime(m.group(1), "%d/%m/%Y").date()
        result["closing_employee"] = _num(m.group(2))
        result["closing_employer"] = _num(m.group(3))
        result["closing_pension"] = _num(m.group(4))
    else:
        warnings.append("Closing balance row ('Closing Balance as on ...') not found")

    m = _TOTAL_CONTRIB_RE.search(text)
    if m:
        result["contrib_employee"] = _num(m.group(2))
        result["contrib_employer"] = _num(m.group(3))
        result["contrib_pension"] = _num(m.group(4))

    m = _TOTAL_TRANSFER_RE.search(text)
    if m:
        result["transfer_employee"] = _num(m.group(2))
        result["transfer_employer"] = _num(m.group(3))
        result["transfer_pension"] = _num(m.group(4))

    m = _TOTAL_WITHDRAWAL_RE.search(text)
    if m:
        result["withdrawal_employee"] = _num(m.group(2))
        result["withdrawal_employer"] = _num(m.group(3))
        result["withdrawal_pension"] = _num(m.group(4))

    # Sanity check: opening + contributions + transfers - withdrawals should
    # reconcile to the closing balance (interest credits aren't broken out
    # separately in this layout, so allow a tolerance rather than requiring
    # an exact match).
    if result["ob_employee"] is not None and result["closing_employee"] is not None:
        expected = (
            result["ob_employee"]
            + (result["contrib_employee"] or 0) + (result["transfer_employee"] or 0)
            - (result["withdrawal_employee"] or 0)
        )
        if abs(expected - result["closing_employee"]) > max(1000.0, 0.02 * result["closing_employee"]):
            warnings.append(
                "Employee balance doesn't reconcile within tolerance (opening + "
                "contributions + transfers - withdrawals vs. closing) -- likely an "
                "interest credit not broken out separately; figures are still used as printed."
            )

    return result


def is_epf_pdf(pdf_path, password=None):
    """Cheap probe: pdftotext just page 1 and look for EPF passbook branding."""
    try:
        text = _run_pdftotext(pdf_path, password=password, first_page=1, last_page=1)
    except Exception:
        return False
    blob = text.upper()
    return "PASSBOOK" in blob and "UAN" in blob and "EPF" in blob


def parse_epf_pdf(pdf_path, password=None):
    """Extract page 1 text and parse it. Returns the same dict shape as
    parse_epf_text()."""
    text = _run_pdftotext(pdf_path, password=password, first_page=1, last_page=1)
    return parse_epf_text(text)


def apply_epf(family_id, owner_id, pdf_path, password=None, original_filename="EPF.pdf",
              file_hash=None, source="upload", email_date=None):
    """Apply an EPFO EPF Member Passbook to the family's EPF Asset.

    One Asset per Member ID (matched via folio_number). current_value is
    Employee balance + Employer balance only (the Pension/EPS column is
    informational -- see module docstring). Returns (created, updated,
    skipped) -- skipped is 1 only when the PDF couldn't even be identified
    (no Member ID or closing balance found).
    """
    parsed = parse_epf_pdf(pdf_path, password=password)

    if not parsed["member_id"] or parsed["closing_employee"] is None:
        db.session.add(StatementImport(
            family_id=family_id, uploaded_by_id=owner_id, bank="EPF (EPFO)",
            original_filename=original_filename, stored_path=pdf_path, file_hash=file_hash,
            source=source, email_date=email_date,
            status=IMPORT_STATUS_CONFIRMED, confirmed_at=datetime.utcnow(),
            accounts_found=0, transactions_found=0,
            warnings="\n".join(parsed["warnings"]) or
                     "Could not read Member ID/closing balance from this EPF passbook",
        ))
        db.session.commit()
        return 0, 0, 1

    member_id = parsed["member_id"]
    asset = Asset.query.filter_by(family_id=family_id, category="EPF", folio_number=member_id).first()
    created = updated = 0
    if asset is None:
        label = parsed["establishment_name"] or member_id
        asset = Asset(
            family_id=family_id, owner_id=owner_id, category="EPF",
            name=f"EPF - {label}"[:160], institution=parsed["establishment_name"] or "EPFO",
            folio_number=member_id,
        )
        db.session.add(asset)
        db.session.flush()
        created = 1
    else:
        updated = 1

    asset.current_value = round(parsed["closing_employee"] + parsed["closing_employer"], 2)
    asset.last_valued_at = (
        datetime.combine(parsed["closing_date"], datetime.min.time())
        if parsed["closing_date"] else datetime.utcnow()
    )

    db.session.add(StatementImport(
        family_id=family_id, uploaded_by_id=owner_id, bank="EPF (EPFO)",
        original_filename=original_filename, stored_path=pdf_path, file_hash=file_hash,
        source=source, email_date=email_date,
        status=IMPORT_STATUS_CONFIRMED, confirmed_at=datetime.utcnow(),
        accounts_found=created + updated, transactions_found=0,
        warnings="\n".join(parsed["warnings"]),
    ))
    db.session.commit()
    valuation.snapshot_all_families()  # keep the net-worth trend current
    return created, updated, 0
