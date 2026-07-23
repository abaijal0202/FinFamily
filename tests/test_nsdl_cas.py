from datetime import date

from statement_import import nsdl_cas
from models import db, Asset, AssetCashflow, StatementImport

# Real (lightly trimmed) Tesseract OCR output from an actual NSDL e-CAS, used
# to make sure the regexes survive the OCR noise this document type
# actually produces: multi-line wrapped names, the CDSL 3-subrow balance
# breakdown, and stray whitespace NSDL/Tesseract sometimes inserts inside a
# grouped number's last 3-digit block (e.g. "2,81 ,590.53").
EQUITY_PAGE = """
Consolidated Account Statement (C) NSDL
Summary Holdings Transactions
ISIN Company Name Face Value No. of Market Value in Rs
Stock Symbol in Rs Shares Price in Rs
INE090A01021 ICICI BANK LIMITED 2.00 550 1,375.20 7,56,360.00
ICICIBANK.NSE
INE467B01029 TATA CONSULTANCY SERVICES 1.00 56 2,031.50 1,13,764.00
TCS.NSE LIMITED
ISIN SECURITY Current Bal. Safekeep Bal. Pledged Bal. Market Price Value
Free Bal. Locked In Bal. Earmarked Bal. / Face Value in Rs
Lent Bal. Pledge Setup Bal. Pledgee Bal. in Rs
INE1C6T01020 EMMVEE PHOTOVOLTAIC 93.000 0.000 0.000 342.95 31,894.35
POWER LIMITED #EQUITY 93.000 0.000 0.000
SHARES 0.000 0.000 0.000
INE382Z01011 GARDEN REACH 0.000 0.000 0.000 2,726.30 0.00
SHIPBUILDERS & 0.000 0.000 0.000
ENGINEERS LIMITED#EQUITY 0.000 0.000 0.000
Holdings as on 30-Jun-2026
"""

MF_PAGE = """
Consolidated Account Statement (C) NSDL
Holdings
ISIN ISIN Description Folio No. No. of Average Total Cost Current NAV Current Value Unrealised
UCC Units Cost Per Units
INF179K01YV8 HDFC Large Cap 16828108 31.382 414.2502 13,000.00 1,211.2500 38,011.45 25,011.45
MFHDFC0020 Fund - Direct Plan -
Growth Option
INF179K01YV8 HDFC Large Cap 17677778 318.821 919.0110 2,93,000.00 1,211.2500 3,86,171.94 93,171.94
MFHDFC0020 Fund - Direct Plan -
Growth Option
INF109KO1GN9 ___ ICICI Prudential All 2730825 13,636.190 18.4441 2,51,506.77 39.0943 5,33,097.30 2,81 ,590.53
MF/003/93/6/30 Seasons Bond Fund
- Growth
Sub Total 90,78,049.51
"""


def test_parse_page_texts_equity_both_layouts():
    parsed = nsdl_cas.parse_page_texts([EQUITY_PAGE])
    by_isin = {r["isin"]: r for r in parsed["equity"]}

    assert by_isin["INE090A01021"]["units"] == 550
    assert by_isin["INE090A01021"]["value"] == 756360.00
    assert by_isin["INE090A01021"]["ticker"] == "ICICIBANK.NSE"
    assert "ICICI BANK LIMITED" in by_isin["INE090A01021"]["name"]

    assert by_isin["INE1C6T01020"]["units"] == 93.0
    assert by_isin["INE1C6T01020"]["value"] == 31894.35

    # zero-balance row (GARDEN REACH, current bal 0.000) is dropped
    assert "INE382Z01011" not in by_isin

    assert parsed["statement_date"] == date(2026, 6, 30)


def test_parse_page_texts_mf_folios_with_ocr_artifacts():
    parsed = nsdl_cas.parse_page_texts([MF_PAGE])
    mf = {r["folio"]: r for r in parsed["mf"]}

    assert set(mf) == {"16828108", "17677778", "2730825"}

    row = mf["16828108"]
    assert row["isin"] == "INF179K01YV8"
    assert row["scheme_code"] == "MFHDFC0020"
    assert row["units"] == 31.382
    assert row["avg_cost"] == 414.2502
    assert row["total_cost"] == 13000.00
    assert row["nav"] == 1211.25
    assert row["value"] == 38011.45

    # the ISIN's trailing "9" OCR'd as clean, but the folio number's final
    # value has a stray space-before-comma artifact ("2,81 ,590.53") that
    # must still parse to the correct amount.
    gn9 = mf["2730825"]
    assert gn9["isin"] == "INF109K01GN9"  # normalised, no stray "O"
    assert gn9["units"] == 13636.190


def test_apply_nsdl_cas_creates_equity_and_mf_assets(app, family_and_user, monkeypatch):
    family, user = family_and_user
    monkeypatch.setattr(nsdl_cas, "parse_nsdl_cas",
                        lambda path, password=None, dpi=300:
                        nsdl_cas.parse_page_texts([EQUITY_PAGE, MF_PAGE]))

    created, updated, skipped = nsdl_cas.apply_nsdl_cas(
        family.id, user.id, "cas.pdf", "1234", original_filename="cas.PDF")

    # 2 equity (ICICI Bank, TCS, Emmvee = 3 actually) + 3 MF folios created
    assert created == 6
    assert updated == 0

    equity = {a.isin: a for a in Asset.query.filter_by(family_id=family.id, category="EQUITY").all()}
    assert equity["INE090A01021"].units == 550
    assert equity["INE090A01021"].current_value == 756360.00
    assert equity["INE090A01021"].ticker == "ICICIBANK.NSE"

    mf = {(a.isin, a.folio_number): a for a in Asset.query.filter_by(family_id=family.id, category="MF").all()}
    hdfc = mf[("INF179K01YV8", "16828108")]
    assert hdfc.units == 31.382
    assert hdfc.avg_buy_price == 414.2502
    assert hdfc.purchase_value == 13000.00
    assert hdfc.scheme_code == "MFHDFC0020"

    # a second folio of the exact same ISIN must stay a *separate* asset
    hdfc2 = mf[("INF179K01YV8", "17677778")]
    assert hdfc2.id != hdfc.id
    assert hdfc2.units == 318.821

    # audit trail row
    stmt = StatementImport.query.filter_by(family_id=family.id).one()
    assert stmt.bank == "CAS (NSDL e-CAS)"
    assert stmt.status == "confirmed"
    assert stmt.accounts_found == 6

    # approximate-XIRR seed: one synthetic cashflow per MF asset
    assert AssetCashflow.query.filter_by(asset_id=hdfc.id, synthetic=True).count() == 1

    # re-applying updates in place rather than duplicating
    monkeypatch.setattr(nsdl_cas, "parse_nsdl_cas",
                        lambda path, password=None, dpi=300:
                        nsdl_cas.parse_page_texts([EQUITY_PAGE, MF_PAGE]))
    created2, updated2, _ = nsdl_cas.apply_nsdl_cas(family.id, user.id, "cas.pdf", "1234")
    assert created2 == 0 and updated2 == 6
    assert Asset.query.filter_by(family_id=family.id, category="MF").count() == 3
    assert AssetCashflow.query.filter_by(asset_id=hdfc.id).count() == 1  # not duplicated


def test_is_nsdl_cas_pdf_probes_first_two_pages(monkeypatch):
    monkeypatch.setattr(nsdl_cas, "pdf_to_page_texts",
                        lambda path, dpi, password, first_page, last_page:
                        ["Consolidated Account Statement (C) NSDL", "Holdings"])
    assert nsdl_cas.is_nsdl_cas_pdf("x.pdf", "pw") is True


def test_is_nsdl_cas_pdf_false_for_non_nsdl_text(monkeypatch):
    monkeypatch.setattr(nsdl_cas, "pdf_to_page_texts",
                        lambda path, dpi, password, first_page, last_page:
                        ["HDFC BANK statement of account"])
    assert nsdl_cas.is_nsdl_cas_pdf("x.pdf", "pw") is False
