"""NSDL e-CAS (Consolidated Account Statement) import.

Unlike the CAMS/KFintech CAS handled by `cas_import.py` (clean text layer,
read directly via `casparser`), an NSDL e-CAS downloaded via "Microsoft:
Print to PDF" has the same broken/empty text layer as the HDFC bank
statements — so this module OCRs it with the same `pdf_to_page_texts`
helper, then parses the rasterized-and-OCR'd table rows with regexes tuned
to NSDL's three holdings table layouts:

  1. Equity, simple layout (one demat account, no pledge/lock-in columns):
     ISIN | Company Name | Face Value | No. of Shares | Market Price | Value
  2. Equity, CDSL layout (adds Free/Locked/Pledge-Setup and Lent/Pledgee
     balance breakdown rows under each holding — we only need the first,
     "Current Bal." line of each entry):
     ISIN | SECURITY | Current Bal. | Safekeep Bal. | Pledged Bal. | Market Price | Value
  3. Mutual Fund Folios: ISIN | Description | Folio No. | Units | Avg Cost |
     Total Cost | Current NAV | Current Value | Unrealised P/L

All three render each holding's numeric row on a single OCR text line (the
multi-line company/scheme name wraps onto following lines with no numbers),
which is what makes this reliable enough to parse without a manual-review
step — same trust level as the CAMS CAS path. `apply_nsdl_cas` applies the
result straight to EQUITY/MF assets, matching equity by ISIN and MF by
(ISIN, folio) since NSDL commonly shows several folios for the same scheme.
"""
import re
from datetime import date, datetime

from models import db, Asset, AssetCashflow, StatementImport, IMPORT_STATUS_CONFIRMED
from statement_import.ocr import pdf_to_page_texts
import valuation

# An ISIN is 12 chars: 2 letters + 9 alphanumeric + 1 numeric check digit.
# Tesseract frequently misreads a trailing "0" as the letter "O", so the
# character classes here accept "O" too and _norm_isin() fixes it up after.
_ISIN = r"[A-Z0O]{2}[A-Z0-9O]{9}[0-9O]"

# Layout 1 — plain equity row (no pledge/lock-in breakdown):
#   INE002A01018 RELIANCE INDUSTRIES LIMITED 10.00 120 1,293.90 1,55,268.00
_EQUITY_SIMPLE_RE = re.compile(
    rf"^({_ISIN})\s+(.+?)\s+([\d,]+\.\d{{2}})\s+([\d,]+,?)\s+([\d,]+\.\d{{2}})\s+([\d,]+\.\d{{2}})\s*$"
)

# Layout 2 — CDSL balance-breakdown equity row (first line only):
#   INE1C6T01020 EMMVEE PHOTOVOLTAIC 93.000 0.000 0.000 342.95 31,894.35
_EQUITY_CDSL_RE = re.compile(
    rf"^({_ISIN})\s+(.+?)\s+([\d,]+\.\d{{3}})\s+([\d,]+\.\d{{3}})\s+([\d,]+\.\d{{3}})\s+([\d,]+\.\d{{2}})\s+([\d,]+\.\d{{2}})\s*$"
)

# Mutual Fund Folios row:
#   INF209K01XX1 Aditya Birla Sun Life 1018425811 525.516 685.0410 3,60,000.00 2,117.5700 11,12,816.92 7,52,816.92
_MF_FOLIO_RE = re.compile(
    rf"^({_ISIN})\s+(.+?)\s+(\d{{4,15}})\s+([\d,]+\.\d{{3}})\s+([\d,]+\.\d{{4}})\s+"
    rf"([\d,]+\.\d{{2}})\s+([\d,]+\.\d{{4}})\s+([\d,]+\.\d{{2}})\s+([\d,]+\.\d{{2}})\s*$"
)

_STATEMENT_DATE_RE = re.compile(r"as on\s+(\d{2}-[A-Za-z]{3}-\d{4})", re.IGNORECASE)
_TICKER_RE = re.compile(r"^([A-Z0-9&]+\.NSE)")


def _clean_line(line):
    """Collapse a stray OCR space next to a comma, but only inside a
    number's final 3-digit group before a decimal point (e.g.
    "2,81 ,590.53" -> "2,81,590.53"). Deliberately narrow so it never
    merges a trailing "775," in one column with the next column's number.
    """
    return re.sub(r"(\d)\s*,\s*(\d{3})\.", r"\1,\2.", line)


def _num(s):
    return float(s.replace(",", "").strip())


def _norm_isin(s):
    return s.replace("O", "0")


def _clean_name(s):
    return re.sub(r"\s+", " ", s).strip(" -=#~")


def _looks_like_code(token):
    token = (token or "").strip()
    if not token:
        return False
    return bool(re.match(r"^[A-Z0-9/]{3,20}$", token))


def parse_page_texts(page_texts):
    """Pure parsing over already-OCR'd page text (no PDF/Tesseract calls) —
    kept separate from parse_nsdl_cas() so it can be unit-tested with
    canned OCR text fixtures. Returns
    {"equity": [...], "mf": [...], "statement_date": date|None, "warnings": [...]}.
    """
    equity_rows = []
    mf_rows = []
    warnings = []
    statement_date = None

    all_lines = []
    for page in page_texts:
        all_lines.extend(raw.rstrip() for raw in page.splitlines())

    for i, raw in enumerate(all_lines):
        if statement_date is None:
            m = _STATEMENT_DATE_RE.search(raw)
            if m:
                try:
                    statement_date = datetime.strptime(m.group(1), "%d-%b-%Y").date()
                except ValueError:
                    pass

        line = _clean_line(raw.strip())
        if not line:
            continue

        m = _MF_FOLIO_RE.match(line)
        if m:
            units = _num(m.group(4))
            if units > 0.0001:
                scheme_code = None
                nxt = all_lines[i + 1].strip() if i + 1 < len(all_lines) else ""
                first_token = nxt.split(" ", 1)[0] if nxt else ""
                if _looks_like_code(first_token):
                    scheme_code = first_token
                mf_rows.append({
                    "isin": _norm_isin(m.group(1)), "name": _clean_name(m.group(2)),
                    "folio": m.group(3), "scheme_code": scheme_code,
                    "units": units, "avg_cost": _num(m.group(5)),
                    "total_cost": _num(m.group(6)), "nav": _num(m.group(7)),
                    "value": _num(m.group(8)),
                })
            continue

        m = _EQUITY_CDSL_RE.match(line)
        if m:
            units = _num(m.group(3))
            if units > 0.0001:
                equity_rows.append({
                    "isin": _norm_isin(m.group(1)), "name": _clean_name(m.group(2)),
                    "ticker": None, "units": units,
                    "price": _num(m.group(6)), "value": _num(m.group(7)),
                })
            continue

        m = _EQUITY_SIMPLE_RE.match(line)
        if m:
            shares = _num(m.group(4))
            if shares > 0.0001:
                ticker = None
                nxt = all_lines[i + 1].strip() if i + 1 < len(all_lines) else ""
                tm = _TICKER_RE.match(nxt)
                if tm:
                    ticker = tm.group(1)
                equity_rows.append({
                    "isin": _norm_isin(m.group(1)), "name": _clean_name(m.group(2)),
                    "ticker": ticker, "units": shares,
                    "price": _num(m.group(5)), "value": _num(m.group(6)),
                })
            continue

    # The same stock can appear once per demat account (e.g. HDFC + Zerodha
    # both hold it) — a family net-worth tracker wants one row per ISIN.
    by_isin = {}
    for row in equity_rows:
        key = row["isin"]
        if key in by_isin:
            by_isin[key]["units"] += row["units"]
            by_isin[key]["value"] += row["value"]
            by_isin[key]["price"] = row["price"]
            by_isin[key]["ticker"] = by_isin[key]["ticker"] or row["ticker"]
        else:
            by_isin[key] = dict(row)

    if not by_isin and not mf_rows:
        warnings.append("No equity or mutual-fund-folio rows recognised in this CAS.")

    return {"equity": list(by_isin.values()), "mf": mf_rows,
            "statement_date": statement_date, "warnings": warnings}


def is_nsdl_cas_pdf(pdf_path, password=None):
    """Cheap probe: OCR just the first couple of pages at a low DPI and
    look for NSDL's CAS branding. Tried only after the CAMS/KFintech
    `casparser`-based probe (is_cas_pdf) has already failed for this
    password."""
    try:
        texts = pdf_to_page_texts(pdf_path, dpi=200, password=password or None,
                                   first_page=1, last_page=2)
    except Exception:
        return False
    blob = "\n".join(texts).upper()
    return "NSDL" in blob and (
        "CONSOLIDATED ACCOUNT STATEMENT" in blob or "HOLDINGS" in blob
    )


def parse_nsdl_cas(pdf_path, password=None, dpi=300):
    """OCR the full document and parse it. Returns the same dict shape as
    parse_page_texts()."""
    texts = pdf_to_page_texts(pdf_path, dpi=dpi, password=password or None)
    return parse_page_texts(texts)


def apply_nsdl_cas(family_id, owner_id, pdf_path, password, original_filename="CAS.pdf",
                    file_hash=None, source="upload", email_date=None, dpi=300):
    """OCR + parse an NSDL e-CAS and apply it to the family's EQUITY/MF
    assets. Matching: equity by ISIN, MF by (ISIN, folio) — NSDL commonly
    shows the same scheme under several folios (separate SIP batches),
    which matching on ISIN alone would incorrectly merge into one Asset.
    Returns (created, updated, skipped)."""
    parsed = parse_nsdl_cas(pdf_path, password=password, dpi=dpi)
    statement_date = parsed["statement_date"] or date.today()

    equity_assets = Asset.query.filter_by(family_id=family_id, category="EQUITY").all()
    eq_by_isin = {a.isin.strip(): a for a in equity_assets if a.isin}

    mf_assets = Asset.query.filter_by(family_id=family_id, category="MF").all()
    mf_by_key = {(a.isin.strip(), (a.folio_number or "").strip()): a
                 for a in mf_assets if a.isin}

    created = updated = skipped = 0

    for row in parsed["equity"]:
        isin = row["isin"]
        if not isin or row["units"] <= 0:
            skipped += 1
            continue
        asset = eq_by_isin.get(isin)
        if asset is None:
            asset = Asset(family_id=family_id, owner_id=owner_id, category="EQUITY",
                          name=row["name"] or isin, institution="NSDL/CDSL Demat")
            db.session.add(asset)
            db.session.flush()
            eq_by_isin[isin] = asset
            created += 1
        else:
            updated += 1
        asset.units = row["units"]
        asset.current_value = round(row["value"], 2)
        asset.isin = isin
        asset.ticker = row["ticker"] or asset.ticker
        asset.name = row["name"] or asset.name
        asset.last_valued_at = datetime.utcnow()

    for row in parsed["mf"]:
        isin, folio = row["isin"], row["folio"]
        if not isin or row["units"] <= 0:
            skipped += 1
            continue
        key = (isin, folio)
        asset = mf_by_key.get(key)
        if asset is None:
            asset = Asset(family_id=family_id, owner_id=owner_id, category="MF",
                          name=row["name"] or isin)
            db.session.add(asset)
            db.session.flush()
            mf_by_key[key] = asset
            created += 1
        else:
            updated += 1
        asset.units = row["units"]
        asset.avg_buy_price = row["avg_cost"] or asset.avg_buy_price
        asset.purchase_value = round(row["total_cost"], 2) if row["total_cost"] else asset.purchase_value
        asset.current_value = round(row["value"], 2)
        asset.isin = isin
        asset.scheme_code = row["scheme_code"] or asset.scheme_code
        asset.folio_number = folio or asset.folio_number
        asset.last_valued_at = datetime.utcnow()
        # NSDL gives only a point-in-time snapshot, not per-transaction
        # history like CAMS — record one synthetic cashflow (deduped by
        # only doing this the first time we ever see this asset) so XIRR
        # has at least an approximate starting point.
        if not asset.cashflows and row["total_cost"] and row["units"]:
            db.session.add(AssetCashflow(
                asset_id=asset.id, flow_date=statement_date,
                amount=round(row["total_cost"], 2), units=row["units"],
                nav=row["avg_cost"] or None, source="NSDL_CAS", synthetic=True,
            ))

    total = created + updated
    warnings = list(parsed["warnings"])
    db.session.add(StatementImport(
        family_id=family_id, uploaded_by_id=owner_id, bank="CAS (NSDL e-CAS)",
        original_filename=original_filename, stored_path=pdf_path, file_hash=file_hash,
        source=source, email_date=email_date,
        status=IMPORT_STATUS_CONFIRMED, confirmed_at=datetime.utcnow(),
        accounts_found=total, transactions_found=0,
        warnings="\n".join(warnings) if warnings else
                 ("" if total else "NSDL CAS parsed but no holdings were recognised"),
    ))
    db.session.commit()
    if total:
        valuation.snapshot_all_families()  # keep the net-worth trend current
    return created, updated, skipped
