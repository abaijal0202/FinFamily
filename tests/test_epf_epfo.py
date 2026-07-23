from datetime import date

from statement_import import epf_epfo
from models import db, Asset, StatementImport

# Real page-1 pdftotext -layout output from an actual EPFO "EPF Member
# Passbook" (single page per financial year, clean text layer -- no OCR
# needed). The Devanagari/Hindi labels on the left are the bilingual field
# names EPFO prints; only the English half after "|" matters for parsing.
PASSBOOK_P1 = """
                                                                                  lnL; iklcqd / Member Passbook
------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
LFkkiuk vkbZMh@uke                 | Establishment ID/Name                                    THVSH1721427000 / NUSUMMIT TECHNOLOGIES PRIVATE LIMITED
lnL; vkbZMh@uke                    | Member ID/Name                                           THVSH17214270000011797 / ASHISH BAIJAL
tUe frfFk                          | Date of Birth                                            02-02-1981
;w , u                             | UAN                                                      100100913113


                                       bZih,Q iklcqd [ foÙkh; o"kZ - 2026-2027 ] / EPF Passbook [ Financial Year - 2026-2027 ]
                                                                                                                                                             deZpkjh 'ks"k /         fu;ksäk 'ks"k /            isa'ku 'ks"k /
fooj.k / Particulars                                                                                                                                          Employee                 Employer                    Pension
                                                                                                                                                                 Balance                 Balance                   Balance
OB Int. Updated upto 01/04/2026                                                                                                                                  4,22,985                3,99,160                  22,500

                        VªkalD'ku / Transaction                                                                       osru / Wages                                         va'knku / Contribution
  osru ekg /
                                        çdkj                        fooj.k / Particulars
 Wage Month                                                                                                                                                 deZpkjh /               fu;ksäk /                 isa'ku /
                         fnuk¡d / Date / Type                                                             bZ ih ,Q / EPF bZ ih ,l / EPS                     Employee                Employer                 Pension
       Mar-2026 01-04-2026                     CR          Cont. for Due-Month 042026                                97,239                  15,000                 11,669                  10,419                   1,250
                                                           TRANSFER IN - SAME OFFICE
                                                           (Old Member Id-
        Apr-2026 01-06-2026                    CR                                                                             0                      0           4,49,336                4,21,102                           0
                                                           :KDMAL02129650000017681
                                                           )
                                                           TRANSFER IN - INTEREST
                                                           AMOUNT ONLY (Old Member
        Feb-2027 01-03-2027                    CR          Id-                                                                0                      0                6,178                   5,790                         0
                                                           :KDMAL02129650000017681
                                                           )
                                                                                            Total Contributions for the year [ 2026 ]                               11,669                  10,419                   1,250
                                                                                     Total Transfer-Ins/VDRs for the year [ 2026 ]                               4,55,514                4,26,892                           0
                                                                                            Total Withdrawals for the year [ 2026 ]                                          0                      0                       0
Interest details N/A                                                                                                                                                         0                      0                       0
Closing Balance as on 31/03/2027                                                                                                                                 8,90,168                8,36,471                  23,750


                          ----------------------fooj.k dh lekfIr/End Of Statement----------------------                                                                eqfnzr/Printed On : 23-07-2026 21:01:35
izfr[;ku & mij nh xbZ tkudkjh dsUnzh; loZj ij nh xbZ tkudkjh ds vk/kkj ij gSA ;g tkudkjh dkuwuh iz;kstu ds fy, mi;ksx ugha dh tk ldrh gSaA
Disclaimer - Information shown above is based on available data on central server.This information may not be use for legal purpose.
------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
* Please never respond to any call for sharing any personal details like Aadhar, PAN, Bank details, OTP or request for any payment.
* EPFO never calls members/ pensioners to deposit any amount.
* Please do not make any payment based on any such call.
------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------




THVSH17214270000011797                                                                                                                                                                                     Page 1 of 1
"""


def test_parse_epf_text_identifiers():
    parsed = epf_epfo.parse_epf_text(PASSBOOK_P1)
    assert parsed["establishment_id"] == "THVSH1721427000"
    assert parsed["establishment_name"] == "NUSUMMIT TECHNOLOGIES PRIVATE LIMITED"
    assert parsed["member_id"] == "THVSH17214270000011797"
    assert parsed["member_name"] == "ASHISH BAIJAL"
    assert parsed["uan"] == "100100913113"
    assert parsed["financial_year"] == "2026-2027"


def test_parse_epf_text_opening_and_closing_balances():
    parsed = epf_epfo.parse_epf_text(PASSBOOK_P1)
    assert parsed["ob_date"] == date(2026, 4, 1)
    assert parsed["ob_employee"] == 422985.0
    assert parsed["ob_employer"] == 399160.0
    assert parsed["ob_pension"] == 22500.0

    assert parsed["closing_date"] == date(2027, 3, 31)
    assert parsed["closing_employee"] == 890168.0
    assert parsed["closing_employer"] == 836471.0
    assert parsed["closing_pension"] == 23750.0


def test_parse_epf_text_year_totals():
    parsed = epf_epfo.parse_epf_text(PASSBOOK_P1)
    assert parsed["contrib_employee"] == 11669.0
    assert parsed["contrib_employer"] == 10419.0
    assert parsed["contrib_pension"] == 1250.0

    assert parsed["transfer_employee"] == 455514.0
    assert parsed["transfer_employer"] == 426892.0
    assert parsed["transfer_pension"] == 0.0

    assert parsed["withdrawal_employee"] == 0.0
    assert parsed["withdrawal_employer"] == 0.0
    assert parsed["withdrawal_pension"] == 0.0


def test_parse_epf_text_reconciles_within_tolerance():
    """opening + contributions + transfers - withdrawals for Employee:
    422985 + 11669 + 455514 - 0 = 890168, an exact match to the closing
    balance here (this member's transfer-in already included the year's
    interest) -- no reconciliation warning should fire."""
    parsed = epf_epfo.parse_epf_text(PASSBOOK_P1)
    assert not any("reconcile" in w for w in parsed["warnings"])


def test_parse_epf_text_missing_data_produces_warnings():
    parsed = epf_epfo.parse_epf_text("some unrelated PDF text\nwith no EPF data at all")
    assert parsed["member_id"] is None
    assert parsed["closing_employee"] is None
    assert parsed["warnings"]  # non-empty


def test_is_epf_pdf_probes_first_page(monkeypatch):
    monkeypatch.setattr(epf_epfo, "_run_pdftotext",
                        lambda path, password=None, first_page=None, last_page=None:
                        "EPF Member Passbook\nUAN 100100913113")
    assert epf_epfo.is_epf_pdf("x.pdf") is True


def test_is_epf_pdf_false_for_non_epf_text(monkeypatch):
    monkeypatch.setattr(epf_epfo, "_run_pdftotext",
                        lambda path, password=None, first_page=None, last_page=None:
                        "HDFC BANK statement of account")
    assert epf_epfo.is_epf_pdf("x.pdf") is False


def test_apply_epf_creates_asset_excluding_pension_from_value(app, family_and_user, monkeypatch):
    family, user = family_and_user
    monkeypatch.setattr(epf_epfo, "parse_epf_pdf",
                        lambda path, password=None: epf_epfo.parse_epf_text(PASSBOOK_P1))

    created, updated, skipped = epf_epfo.apply_epf(
        family.id, user.id, "epf.pdf", original_filename="THVSH17214270000011797_2026.pdf")

    assert (created, updated, skipped) == (1, 0, 0)

    asset = Asset.query.filter_by(family_id=family.id, category="EPF").one()
    assert asset.folio_number == "THVSH17214270000011797"
    assert asset.institution == "NUSUMMIT TECHNOLOGIES PRIVATE LIMITED"
    # current_value = Employee + Employer only -- Pension/EPS is informational,
    # not a withdrawable corpus, so it's deliberately excluded
    assert asset.current_value == 890168.0 + 836471.0
    assert asset.last_valued_at.date() == date(2027, 3, 31)

    stmt = StatementImport.query.filter_by(family_id=family.id).one()
    assert stmt.bank == "EPF (EPFO)"
    assert stmt.status == "confirmed"
    assert stmt.accounts_found == 1


def test_apply_epf_reimport_updates_same_asset_by_member_id(app, family_and_user, monkeypatch):
    family, user = family_and_user

    monkeypatch.setattr(epf_epfo, "parse_epf_pdf",
                        lambda path, password=None: epf_epfo.parse_epf_text(PASSBOOK_P1))
    created1, updated1, _ = epf_epfo.apply_epf(family.id, user.id, "epf1.pdf")
    assert (created1, updated1) == (1, 0)

    # a later financial year's passbook, same member, higher closing balance
    next_year = PASSBOOK_P1.replace("8,90,168", "9,50,000").replace("Closing Balance as on 31/03/2027",
                                                                     "Closing Balance as on 31/03/2028")
    monkeypatch.setattr(epf_epfo, "parse_epf_pdf",
                        lambda path, password=None: epf_epfo.parse_epf_text(next_year))
    created2, updated2, _ = epf_epfo.apply_epf(family.id, user.id, "epf2.pdf")
    assert (created2, updated2) == (0, 1)

    assets = Asset.query.filter_by(family_id=family.id, category="EPF").all()
    assert len(assets) == 1
    assert assets[0].current_value == 950000.0 + 836471.0
    assert assets[0].last_valued_at.date() == date(2028, 3, 31)
    assert StatementImport.query.filter_by(family_id=family.id).count() == 2


def test_apply_epf_unrecognisable_pdf_is_skipped_not_crashed(app, family_and_user, monkeypatch):
    family, user = family_and_user
    monkeypatch.setattr(epf_epfo, "parse_epf_pdf",
                        lambda path, password=None: epf_epfo.parse_epf_text("not an epf pdf"))

    created, updated, skipped = epf_epfo.apply_epf(family.id, user.id, "junk.pdf")
    assert (created, updated, skipped) == (0, 0, 1)
    assert Asset.query.filter_by(family_id=family.id, category="EPF").count() == 0
