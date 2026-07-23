"""KFintech CRA National Pension System (NPS) statement import.

KFintech's NPS PDFs (both the full "Transaction Statement" covering the
account's whole history, and the shorter "Holding Statement" covering just
a recent period) share the exact same page-1 layout: a "Subscriber Details"
block with the PRAN, and an "Investment Details as on <date>" summary table
followed by a per-scheme (E/C/G = Equity / Corporate Debt / Government
Securities) valuation table. Unlike the HDFC bank statements and NSDL CAS,
this PDF has a clean, directly-extractable text layer (produced by
Microsoft Reporting Services) — no OCR needed, `pdftotext -layout` is
enough.

This module only reads that page-1 summary: current total valuation,
per-scheme split, and the valuation date. It deliberately does NOT parse
the multi-page "Contribution / Redemption Details" or "Transaction Details"
tables — the app just needs the current NPS holding value and when it was
last updated, not a full contribution-level XIRR history. Parsing is
bounded to end right before those sections so a positive/parenthesised
number in that later data can never be mistaken for page-1 summary data.

One Asset (category="NPS") is kept per PRAN, matched via folio_number, so
re-importing a newer statement refreshes the same holding instead of
creating a duplicate.
"""
import re
import subprocess
from datetime import date, datetime

from models import db, Asset, StatementImport, IMPORT_STATUS_CONFIRMED
import valuation

_PRAN_RE = re.compile(r"\bPRAN\s+(\d{9,12})\b")
_AS_ON_RE = re.compile(r"Investment Details as on\s+(\d{2}-\d{2}-\d{4})")
_STATEMENT_DATE_RE = re.compile(r"Statement Date\s+([A-Za-z]{3}\s+\d{1,2},\s*\d{4})")

# The "Investment Details" summary row, e.g.:
#   134    1195529.97    0.00    2732.52    2576856.68    1381326.71
# No of Contributions | Total Contribution | Total Withdrawal |
# Deductions due to Charges | Current Valuation | Notional Gain/Loss
_SUMMARY_ROW_RE = re.compile(
    r"^\s*(\d+)\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+"
    r"([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s*$"
)

# A per-scheme valuation row, e.g. "12209.0293   71.3748   871417.02   10.57%"
# (the trailing XIRR% is only present on one row — the overall portfolio
# XIRR, positioned there by the PDF's layout, not a per-scheme figure).
_SCHEME_VALUE_ROW_RE = re.compile(
    r"^\s*([\d,]+\.\d{4})\s+([\d,]+\.\d{4})\s+([\d,]+\.\d{2})(?:\s+([\d.]+)%)?\s*$"
)
_SCHEME_LABEL_RE = re.compile(r"SCHEME\s+([ECGA])\s*-\s*TIER", re.IGNORECASE)

# Sections that start after the page-1 summary — used to bound parsing so
# the later contribution/transaction tables can't be mistaken for it.
_SECTION_BOUNDARY_RE = re.compile(
    r"Contribution\s*/\s*Redemption Details|Transaction Details"
)


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


def parse_nps_text(text):
    """Pure parsing over already-extracted page-1(-2) text — kept separate
    from parse_nps_pdf() so it's unit-testable with canned text fixtures.

    Returns {"pran", "as_on_date", "current_valuation", "total_contribution",
    "schemes" ({"E"/"C"/"G"/"A": {"units","nav","value"}}), "xirr_pct",
    "warnings"}.
    """
    warnings = []
    all_lines = [ln.rstrip() for ln in text.splitlines()]

    # Bound parsing to before the contribution/transaction history sections.
    lines = all_lines
    for i, line in enumerate(all_lines):
        if _SECTION_BOUNDARY_RE.search(line):
            lines = all_lines[:i]
            break

    pran = None
    m = _PRAN_RE.search(text)
    if m:
        pran = m.group(1)
    else:
        warnings.append("PRAN not found in statement")

    as_on_date = None
    m = _AS_ON_RE.search(text)
    if m:
        try:
            as_on_date = datetime.strptime(m.group(1), "%d-%m-%Y").date()
        except ValueError:
            pass
    if as_on_date is None:
        m = _STATEMENT_DATE_RE.search(text)
        if m:
            try:
                as_on_date = datetime.strptime(
                    re.sub(r"\s+", " ", m.group(1)), "%b %d, %Y"
                ).date()
            except ValueError:
                pass
    if as_on_date is None:
        warnings.append("valuation/statement date not found")

    current_valuation = None
    total_contribution = None
    for line in lines:
        m = _SUMMARY_ROW_RE.match(line)
        if m:
            total_contribution = _num(m.group(2))
            current_valuation = _num(m.group(5))
            break
    if current_valuation is None:
        warnings.append("Investment Details summary row not found")

    schemes = {}
    overall_xirr = None
    for i, line in enumerate(lines):
        m = _SCHEME_VALUE_ROW_RE.match(line)
        if not m:
            continue
        units, nav, value = _num(m.group(1)), _num(m.group(2)), _num(m.group(3))
        xirr = float(m.group(4)) if m.group(4) else None
        label = None
        for j in (i + 1, i - 1, i + 2):
            if 0 <= j < len(lines):
                lm = _SCHEME_LABEL_RE.search(lines[j])
                if lm:
                    label = lm.group(1).upper()
                    break
        if label:
            schemes[label] = {"units": units, "nav": nav, "value": value}
            if xirr is not None:
                overall_xirr = xirr

    if not schemes:
        warnings.append("no per-scheme (E/C/G/A) valuation rows found")

    return {
        "pran": pran,
        "as_on_date": as_on_date,
        "current_valuation": current_valuation,
        "total_contribution": total_contribution,
        "schemes": schemes,
        "xirr_pct": overall_xirr,
        "warnings": warnings,
    }


def is_nps_pdf(pdf_path, password=None):
    """Cheap probe: pdftotext just page 1 and look for NPS/PRAN branding."""
    try:
        text = _run_pdftotext(pdf_path, password=password, first_page=1, last_page=1)
    except Exception:
        return False
    blob = text.upper()
    return "NATIONAL PENSION SYSTEM" in blob and "PRAN" in blob


def parse_nps_pdf(pdf_path, password=None):
    """Extract page 1-2 text and parse it. Returns the same dict shape as
    parse_nps_text()."""
    text = _run_pdftotext(pdf_path, password=password, first_page=1, last_page=2)
    return parse_nps_text(text)


def apply_nps(family_id, owner_id, pdf_path, password=None, original_filename="NPS.pdf",
              file_hash=None, source="upload", email_date=None):
    """Apply a KFintech CRA NPS statement to the family's NPS Asset.

    One Asset per PRAN (matched via folio_number). Sets current_value,
    last_valued_at (the statement's "as on" valuation date), and the
    nps_equity_pct/nps_corp_debt_pct/nps_gov_sec_pct/nps_alt_pct split
    computed from each scheme's *current* value (not the target allocation
    printed elsewhere in the PDF, which drifts from actual holdings between
    rebalances). Returns (created, updated, skipped) — skipped is 1 only
    when the PDF couldn't even be identified (no PRAN/valuation found).
    """
    parsed = parse_nps_pdf(pdf_path, password=password)

    if not parsed["pran"] or parsed["current_valuation"] is None:
        db.session.add(StatementImport(
            family_id=family_id, uploaded_by_id=owner_id, bank="NPS (KFintech CRA)",
            original_filename=original_filename, stored_path=pdf_path, file_hash=file_hash,
            source=source, email_date=email_date,
            status=IMPORT_STATUS_CONFIRMED, confirmed_at=datetime.utcnow(),
            accounts_found=0, transactions_found=0,
            warnings="\n".join(parsed["warnings"]) or
                     "Could not read PRAN/valuation from this NPS statement",
        ))
        db.session.commit()
        return 0, 0, 1

    pran = parsed["pran"]
    asset = Asset.query.filter_by(family_id=family_id, category="NPS", folio_number=pran).first()
    created = updated = 0
    if asset is None:
        asset = Asset(
            family_id=family_id, owner_id=owner_id, category="NPS",
            name=f"NPS Tier I - {pran}", institution="NPS Trust (KFintech CRA)",
            folio_number=pran,
        )
        db.session.add(asset)
        db.session.flush()
        created = 1
    else:
        updated = 1

    asset.current_value = round(parsed["current_valuation"], 2)
    asset.last_valued_at = (
        datetime.combine(parsed["as_on_date"], datetime.min.time())
        if parsed["as_on_date"] else datetime.utcnow()
    )

    total_value = sum(s["value"] for s in parsed["schemes"].values())
    if total_value:
        asset.nps_equity_pct = round(100 * parsed["schemes"].get("E", {}).get("value", 0) / total_value, 2)
        asset.nps_corp_debt_pct = round(100 * parsed["schemes"].get("C", {}).get("value", 0) / total_value, 2)
        asset.nps_gov_sec_pct = round(100 * parsed["schemes"].get("G", {}).get("value", 0) / total_value, 2)
        asset.nps_alt_pct = round(100 * parsed["schemes"].get("A", {}).get("value", 0) / total_value, 2)

    db.session.add(StatementImport(
        family_id=family_id, uploaded_by_id=owner_id, bank="NPS (KFintech CRA)",
        original_filename=original_filename, stored_path=pdf_path, file_hash=file_hash,
        source=source, email_date=email_date,
        status=IMPORT_STATUS_CONFIRMED, confirmed_at=datetime.utcnow(),
        accounts_found=created + updated, transactions_found=0,
        warnings="\n".join(parsed["warnings"]),
    ))
    db.session.commit()
    valuation.snapshot_all_families()  # keep the net-worth trend current
    return created, updated, 0
