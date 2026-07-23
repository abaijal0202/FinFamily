"""CAMS / KFintech Consolidated Account Statement (CAS) import.

Uses the open-source `casparser` package to read the password-protected CAS
PDF directly from its text layer (no OCR needed — CAS PDFs have a clean text
layer, unlike the HDFC bank statements). Each folio's closing units and
valuation are applied to the matching MF Asset, creating it if new.

Matching order per scheme: ISIN -> AMFI scheme code -> (folio + name).
CAS is authoritative for MF holdings, so this updates directly (with an
audit StatementImport row) instead of a review screen.
"""
from datetime import datetime, date

from models import db, Asset, AssetCashflow, StatementImport, IMPORT_STATUS_CONFIRMED
import valuation


def _to_date(value):
    """casparser dates arrive as date objects or strings in a few formats."""
    if isinstance(value, date):
        return value
    if isinstance(value, datetime):
        return value.date()
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(str(value).strip(), fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def _record_cashflows(asset, scheme, period_from):
    """Store this scheme's transactions as XIRR cashflows (deduped), plus a
    synthetic opening-balance flow when the CAS period starts with units
    already held (statement doesn't reach back to inception)."""
    added = 0
    txns = scheme.get("transactions") or []

    existing = {(f.flow_date, round(f.amount, 2), round(f.units or 0.0, 4))
                for f in asset.cashflows}

    parsed_txns = []
    for t in txns:
        flow_date = _to_date(t.get("date"))
        try:
            amount = float(t.get("amount") or 0)
        except (TypeError, ValueError):
            amount = 0.0
        if not flow_date or abs(amount) < 0.01:
            continue  # dividend declarations / zero rows aren't cash flows
        units = None
        try:
            units = float(t["units"]) if t.get("units") is not None else None
        except (TypeError, ValueError):
            pass
        nav = None
        try:
            nav = float(t["nav"]) if t.get("nav") is not None else None
        except (TypeError, ValueError):
            pass
        parsed_txns.append((flow_date, amount, units, nav))

    # Synthetic opening flow: only if the asset has no history at all yet,
    # this CAS period starts with units held, and we can date + price it.
    try:
        open_units = float(scheme.get("open") or 0)
    except (TypeError, ValueError):
        open_units = 0.0
    if open_units > 0.0001 and not asset.cashflows:
        start = _to_date(period_from)
        ref_nav = next((nav for _, _, _, nav in parsed_txns if nav), None)
        if ref_nav is None:
            val_info = scheme.get("valuation") or {}
            try:
                ref_nav = float(val_info.get("nav") or 0) or None
            except (TypeError, ValueError):
                ref_nav = None
        if start and ref_nav:
            amount = round(open_units * ref_nav, 2)
            key = (start, amount, round(open_units, 4))
            if key not in existing:
                db.session.add(AssetCashflow(
                    asset_id=asset.id, flow_date=start, amount=amount,
                    units=open_units, nav=ref_nav, synthetic=True,
                ))
                existing.add(key)
                added += 1

    for flow_date, amount, units, nav in parsed_txns:
        key = (flow_date, round(amount, 2), round(units or 0.0, 4))
        if key in existing:
            continue
        db.session.add(AssetCashflow(
            asset_id=asset.id, flow_date=flow_date, amount=amount,
            units=units, nav=nav, synthetic=False,
        ))
        existing.add(key)
        added += 1
    return added


def is_cas_pdf(pdf_path, password):
    """Cheap probe: can casparser open this at all?"""
    try:
        parse_cas(pdf_path, password)
        return True
    except Exception:
        return False


def parse_cas(pdf_path, password):
    """Returns casparser's dict: {"statement_period": ..., "folios": [...]}."""
    from casparser import read_cas_pdf
    return read_cas_pdf(pdf_path, password)


def _iter_schemes(cas_data):
    for folio in cas_data.get("folios", []):
        folio_no = str(folio.get("folio") or "").strip()
        amc = folio.get("amc") or ""
        for scheme in folio.get("schemes", []):
            yield folio_no, amc, scheme


def apply_cas(family_id, owner_id, pdf_path, password, original_filename="CAS.pdf",
              file_hash=None, source="upload", email_date=None):
    """Apply a CAS PDF to the family's MF assets. Returns (created, updated, skipped)."""
    cas_data = parse_cas(pdf_path, password)
    period_from = (cas_data.get("statement_period") or {}).get("from")

    mf_assets = Asset.query.filter_by(family_id=family_id, category="MF").all()
    by_isin = {a.isin.strip(): a for a in mf_assets if a.isin}
    by_code = {a.scheme_code.strip(): a for a in mf_assets if a.scheme_code}
    by_folio_name = {(a.folio_number or "", a.name.lower()): a for a in mf_assets}

    created = updated = skipped = 0
    for folio_no, amc, scheme in _iter_schemes(cas_data):
        name = (scheme.get("scheme") or "").strip()
        isin = (str(scheme.get("isin")) or "").strip()
        amfi_code = str(scheme.get("amfi") or "").strip()
        try:
            close_units = float(scheme.get("close") or 0)
        except (TypeError, ValueError):
            close_units = 0.0
        val_info = scheme.get("valuation") or {}
        try:
            value = float(val_info.get("value") or 0)
        except (TypeError, ValueError):
            value = 0.0

        if not name or close_units <= 0:
            skipped += 1  # closed/zero-balance folios don't belong in net worth
            continue

        asset = (by_isin.get(isin) or by_code.get(amfi_code)
                 or by_folio_name.get((folio_no, name.lower())))
        if asset is None:
            asset = Asset(
                family_id=family_id, owner_id=owner_id, category="MF",
                name=name, institution=amc,
            )
            db.session.add(asset)
            db.session.flush()  # asset.id needed for cashflow rows
            created += 1
        else:
            updated += 1

        asset.units = close_units
        asset.current_value = round(value, 2) if value else asset.current_value
        asset.isin = isin or asset.isin
        asset.scheme_code = amfi_code or asset.scheme_code
        asset.folio_number = folio_no or asset.folio_number
        asset.last_valued_at = datetime.utcnow()
        _record_cashflows(asset, scheme, period_from)  # XIRR history (deduped)
        # keep lookup dicts current so a second folio of the same scheme matches
        if isin:
            by_isin[isin] = asset
        if amfi_code:
            by_code[amfi_code] = asset

    # Audit trail (NFR-SEC-07): record that this CAS was applied.
    db.session.add(StatementImport(
        family_id=family_id, uploaded_by_id=owner_id, bank="CAS (CAMS/KFintech)",
        original_filename=original_filename, stored_path=pdf_path, file_hash=file_hash,
        source=source, email_date=email_date,
        status=IMPORT_STATUS_CONFIRMED, confirmed_at=datetime.utcnow(),
        accounts_found=created + updated, transactions_found=0,
        warnings="" if (created + updated) else "CAS parsed but no active folios found",
    ))
    db.session.commit()
    if created + updated:
        valuation.snapshot_all_families()  # keep the net-worth trend current
    return created, updated, skipped
