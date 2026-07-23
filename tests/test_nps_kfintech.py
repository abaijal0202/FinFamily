from datetime import date

from statement_import import nps_kfintech
from models import db, Asset, StatementImport

# Real page-1 pdftotext -layout output from two actual KFintech CRA NPS
# statements for the same PRAN: the short "Holding Statement" (4 pages,
# covers just the most recent quarter) and the full "Transaction Statement"
# (32 pages, covers the account's whole history). Both share the exact same
# page-1 "Investment Details" layout, which is all this parser reads.
HOLDING_STATEMENT_P1 = """
                                        CENTRAL RECORDKEEPING AGENCY

                                                       NATIONAL PENSION SYSTEM

                                                          Transaction Statement - Tier I
                                Transaction Statement for the Period of Apr 01, 2026 to Jul 23, 2026
                                                                Subscriber Details

                                                             PRAN        110062728758
                                                             Statement Date Jul 23, 2026
    ASHISH BAIJAL                                            PRAN Generated Date         Jan 11,2013

                                                       Scheme Deails                                                                          Percentage

         Scheme 1                              NPS TRUST- A/C - UTI PENSION FUND SCHEME E - TIER I POP                                           35.00

         Scheme 2                              NPS TRUST- A/C - UTI PENSION FUND SCHEME C - TIER I POP                                           20.00

         Scheme 3                              NPS TRUST- A/C - UTI PENSION FUND SCHEME G - TIER I POP                                           45.00

                                                    Investment Details as on 22-07-2026
                          Total Contribution      Total Withdrawal           Deductions due to              Current Valuation            Notional Gain/Loss
 No of Contributions             (₹)                     (₹)                   Charges (₹)                         (₹)                           (₹)

                  134            1195529.97                      0.00                       2732.52                   2576856.68                    1381326.71

               Scheme Name                               Total Units                 Latest NAV                     Value at NAV                   XIRR
NPS TRUST- A/C - UTI PENSION FUND
                                                               12209.0293                         71.3748                       871417.02         10.57%
SCHEME E - TIER I POP
NPS TRUST- A/C - UTI PENSION FUND
                                                               12775.4246                         41.2639                       527163.84
SCHEME C - TIER I POP
NPS TRUST- A/C - UTI PENSION FUND
                                                               31261.9614                         37.6904                  1178275.82
SCHEME G - TIER I POP
                       Total                                                                                               2576856.68

                                                         Changes made during selected period

                                                                                                                                Page 1 of 4

 KFintech, Tower – B, Plot No 31 & 32,Selenuim Building, Financial district, Nanakramguda, Gachibowli, Hyderabad-500032, Telangana, India.
                                                              Contribution / Redemption Details
                                                                         Employer/ Aggregator     Employee/ Subscriber
    Date                 Particulars                 Uploaded By                                                             Total Contribution (₹)
01-Apr-2026 Opening balance                                                                0.00                      0.00                1195529.97
   Total                                                                                                                                 1195529.97

                                                                     Transaction Details
                Transaction
    DATE                                  Particulars                Name of Fund Manager                   Units           NAV          Amount
02-Apr-2026    Billing                 CRA Billing for   NPS TRUST- A/C - UTI PENSION FUND SCHEME
                                       Q4, 2025-2026     E - TIER I POP                                       (0.4426)       66.1318          (29.27)
"""

# Full 32-page Transaction Statement, same PRAN, an earlier valuation date —
# used to confirm a later re-import updates the same Asset instead of
# creating a duplicate.
FULL_STATEMENT_P1 = """
                                        CENTRAL RECORDKEEPING AGENCY

                                                       NATIONAL PENSION SYSTEM

                                                          Transaction Statement - Tier I
                                Transaction Statement for the Period of Jan 11, 2013 to Jul 21, 2026
                                                                Subscriber Details

                                                             PRAN        110062728758
                                                             Statement Date Jul 21, 2026
    ASHISH BAIJAL                                            PRAN Generated Date         Jan 11,2013

                                                    Investment Details as on 20-07-2026
                          Total Contribution      Total Withdrawal           Deductions due to              Current Valuation           Notional Gain/Loss
 No of Contributions             (₹)                     (₹)                   Charges (₹)                         (₹)                          (₹)

                  134            1195529.97                      0.00                       2732.52                   2588029.04                  1392499.07

               Scheme Name                               Total Units                 Latest NAV                     Value at NAV                 XIRR
NPS TRUST- A/C - UTI PENSION FUND
                                                               12209.0293                         72.1006                       880278.33       10.64%
SCHEME E - TIER I POP
NPS TRUST- A/C - UTI PENSION FUND
                                                               12775.4246                         41.2368                       526817.62
SCHEME C - TIER I POP
NPS TRUST- A/C - UTI PENSION FUND
                                                               31261.9614                         37.7754                  1180933.09
SCHEME G - TIER I POP
                       Total                                                                                               2588029.04

                                                                            Page 1 of 32

 KFintech, Tower – B, Plot No 31 & 32,Selenuim Building, Financial district, Nanakramguda, Gachibowli, Hyderabad-500032, Telangana, India.
                                                        Contribution / Redemption Details
    Date              Particulars              Uploaded By                                                                   Total Contribution (₹)
11-Jan-2013 Opening balance                                                            0.00                         0.00                       500.00
"""


def test_parse_nps_text_holding_statement():
    parsed = nps_kfintech.parse_nps_text(HOLDING_STATEMENT_P1)

    assert parsed["pran"] == "110062728758"
    assert parsed["as_on_date"] == date(2026, 7, 22)
    assert parsed["current_valuation"] == 2576856.68
    assert parsed["total_contribution"] == 1195529.97
    assert parsed["xirr_pct"] == 10.57

    schemes = parsed["schemes"]
    assert set(schemes) == {"E", "C", "G"}
    assert schemes["E"] == {"units": 12209.0293, "nav": 71.3748, "value": 871417.02}
    assert schemes["C"]["value"] == 527163.84
    assert schemes["G"]["value"] == 1178275.82
    assert parsed["warnings"] == []


def test_parse_nps_text_ignores_transaction_details_numbers():
    """The (0.4426) 66.1318 (29.27) row inside 'Transaction Details' must
    never be mistaken for a page-1 scheme valuation row — parsing is bounded
    to end at the first 'Contribution / Redemption Details' / 'Transaction
    Details' section marker."""
    parsed = nps_kfintech.parse_nps_text(HOLDING_STATEMENT_P1)
    assert len(parsed["schemes"]) == 3  # not 4+ from stray Transaction Details rows


def test_parse_nps_text_falls_back_to_statement_date():
    text_without_as_on = HOLDING_STATEMENT_P1.replace(
        "Investment Details as on 22-07-2026", "Investment Details"
    )
    parsed = nps_kfintech.parse_nps_text(text_without_as_on)
    assert parsed["as_on_date"] == date(2026, 7, 23)  # from "Statement Date Jul 23, 2026"


def test_parse_nps_text_missing_data_produces_warnings():
    parsed = nps_kfintech.parse_nps_text("some unrelated PDF text\nwith no NPS data at all")
    assert parsed["pran"] is None
    assert parsed["current_valuation"] is None
    assert parsed["warnings"]  # non-empty


def test_is_nps_pdf_probes_first_page(monkeypatch):
    monkeypatch.setattr(nps_kfintech, "_run_pdftotext",
                        lambda path, password=None, first_page=None, last_page=None:
                        "NATIONAL PENSION SYSTEM\nPRAN 110062728758")
    assert nps_kfintech.is_nps_pdf("x.pdf") is True


def test_is_nps_pdf_false_for_non_nps_text(monkeypatch):
    monkeypatch.setattr(nps_kfintech, "_run_pdftotext",
                        lambda path, password=None, first_page=None, last_page=None:
                        "HDFC BANK statement of account")
    assert nps_kfintech.is_nps_pdf("x.pdf") is False


def test_apply_nps_creates_asset_with_allocation_split(app, family_and_user, monkeypatch):
    family, user = family_and_user
    monkeypatch.setattr(nps_kfintech, "parse_nps_pdf",
                        lambda path, password=None: nps_kfintech.parse_nps_text(HOLDING_STATEMENT_P1))

    created, updated, skipped = nps_kfintech.apply_nps(
        family.id, user.id, "nps.pdf", original_filename="110062728758.pdf")

    assert (created, updated, skipped) == (1, 0, 0)

    asset = Asset.query.filter_by(family_id=family.id, category="NPS").one()
    assert asset.folio_number == "110062728758"
    assert asset.current_value == 2576856.68
    assert asset.last_valued_at.date() == date(2026, 7, 22)

    # value-weighted split from the 3 scheme values, not the printed target
    # allocation (35/20/45) — reflects actual current holdings.
    total = 871417.02 + 527163.84 + 1178275.82
    assert asset.nps_equity_pct == round(100 * 871417.02 / total, 2)
    assert asset.nps_corp_debt_pct == round(100 * 527163.84 / total, 2)
    assert asset.nps_gov_sec_pct == round(100 * 1178275.82 / total, 2)
    assert asset.nps_alt_pct == 0.0

    stmt = StatementImport.query.filter_by(family_id=family.id).one()
    assert stmt.bank == "NPS (KFintech CRA)"
    assert stmt.status == "confirmed"
    assert stmt.accounts_found == 1


def test_apply_nps_reimport_updates_same_asset_by_pran(app, family_and_user, monkeypatch):
    family, user = family_and_user

    monkeypatch.setattr(nps_kfintech, "parse_nps_pdf",
                        lambda path, password=None: nps_kfintech.parse_nps_text(FULL_STATEMENT_P1))
    created1, updated1, _ = nps_kfintech.apply_nps(family.id, user.id, "full.pdf")
    assert (created1, updated1) == (1, 0)

    monkeypatch.setattr(nps_kfintech, "parse_nps_pdf",
                        lambda path, password=None: nps_kfintech.parse_nps_text(HOLDING_STATEMENT_P1))
    created2, updated2, _ = nps_kfintech.apply_nps(family.id, user.id, "holding.pdf")
    assert (created2, updated2) == (0, 1)

    # still exactly one NPS asset, refreshed to the newer statement's value
    assets = Asset.query.filter_by(family_id=family.id, category="NPS").all()
    assert len(assets) == 1
    assert assets[0].current_value == 2576856.68
    assert assets[0].last_valued_at.date() == date(2026, 7, 22)
    assert StatementImport.query.filter_by(family_id=family.id).count() == 2


def test_apply_nps_unrecognisable_pdf_is_skipped_not_crashed(app, family_and_user, monkeypatch):
    family, user = family_and_user
    monkeypatch.setattr(nps_kfintech, "parse_nps_pdf",
                        lambda path, password=None: nps_kfintech.parse_nps_text("not an nps pdf"))

    created, updated, skipped = nps_kfintech.apply_nps(family.id, user.id, "junk.pdf")
    assert (created, updated, skipped) == (0, 0, 1)
    assert Asset.query.filter_by(family_id=family.id, category="NPS").count() == 0
