"""Reupload-a-duplicate-file flow: instead of silently blocking a re-upload
of an already-imported PDF, the app shows what a fresh re-parse finds vs.
what was recorded before, and lets the user explicitly override to
re-import (e.g. after a parser fix picks up data that was missed the first
time -- see the SBI FD-5653 / opening-balance production bug)."""
import io

import import_service
from models import db, StatementImport, ImportedAccount, IMPORT_STATUS_CONFIRMED
from statement_import.types import ParsedStatement, ParsedFixedDeposit, ParsedAccount, ParsedTransaction


def _make_existing_import(family, user, accounts):
    stmt = StatementImport(family_id=family.id, uploaded_by_id=user.id, bank="SBI",
                           original_filename="sbi.pdf", stored_path="/tmp/sbi.pdf",
                           file_hash="deadbeef", status=IMPORT_STATUS_CONFIRMED)
    db.session.add(stmt)
    db.session.flush()
    for kw in accounts:
        db.session.add(ImportedAccount(statement_import_id=stmt.id, **kw))
    db.session.commit()
    return stmt


def test_diff_marks_a_newly_found_fd_as_added(app, family_and_user):
    family, user = family_and_user
    existing = _make_existing_import(family, user, [
        dict(account_kind="FD", account_number="XXXXXXX0127", account_number_last4="0127",
             opening_balance=500000.0, closing_balance=500000.0, interest_rate=7.0),
    ])
    parsed = ParsedStatement(bank="SBI", accounts=[], fixed_deposits=[
        ParsedFixedDeposit(fd_number="XXXXXXX0127", principal=500000.0, open_date=None,
                           rate=7.0, current_amount=500000.0, maturity_date=None, maturity_amount=613486.0),
        ParsedFixedDeposit(fd_number="XXXXXXX5653", principal=500000.0, open_date=None,
                           rate=7.0, current_amount=500000.0, maturity_date=None, maturity_amount=574441.0),
    ], warnings=[])

    diff = import_service.diff_bank_accounts(existing, parsed)
    by_key = {(r["kind"], r["last4"]): r for r in diff}

    assert by_key[("FD", "0127")]["status"] == "unchanged"
    assert by_key[("FD", "5653")]["status"] == "added"
    assert by_key[("FD", "5653")]["old"] is None
    assert by_key[("FD", "5653")]["new"]["opening_balance"] == 500000.0


def test_diff_marks_a_missing_account_as_removed(app, family_and_user):
    family, user = family_and_user
    existing = _make_existing_import(family, user, [
        dict(account_kind="FD", account_number="XXXXXXX0127", account_number_last4="0127",
             opening_balance=500000.0, closing_balance=500000.0, interest_rate=7.0),
    ])
    parsed = ParsedStatement(bank="SBI", accounts=[], fixed_deposits=[], warnings=[])

    diff = import_service.diff_bank_accounts(existing, parsed)
    row = diff[0]
    assert row["status"] == "removed"
    assert row["new"] is None
    assert row["old"]["opening_balance"] == 500000.0


def test_diff_marks_changed_balance_and_transaction_count(app, family_and_user):
    family, user = family_and_user
    existing = _make_existing_import(family, user, [
        dict(account_kind="BANK", account_number="UNKNOWN", account_number_last4="NOWN",
             opening_balance=None, closing_balance=6996.0),
    ])
    parsed = ParsedStatement(bank="SBI", accounts=[
        ParsedAccount(account_number="XXXXXXKX2973", account_type="Savings",
                      opening_balance=155957.0, closing_balance=6996.0,
                      transactions=[ParsedTransaction(txn_date=None, narration="x",
                                                      withdrawal=50000.0, deposit=0.0,
                                                      balance_after=105957.0)]),
    ], fixed_deposits=[], warnings=[])

    diff = import_service.diff_bank_accounts(existing, parsed)
    # different last4 ("NOWN" vs "2973") means these don't even match up --
    # the old garbled account shows removed, the new correctly-read one added
    by_key = {(r["kind"], r["last4"]): r for r in diff}
    assert by_key[("BANK", "NOWN")]["status"] == "removed"
    assert by_key[("BANK", "2973")]["status"] == "added"
    assert by_key[("BANK", "2973")]["new"]["transaction_count"] == 1


def test_diff_returns_empty_list_for_identical_reparse(app, family_and_user):
    family, user = family_and_user
    existing = _make_existing_import(family, user, [
        dict(account_kind="FD", account_number="XXXXXXX0127", account_number_last4="0127",
             opening_balance=500000.0, closing_balance=500000.0, interest_rate=7.0),
    ])
    parsed = ParsedStatement(bank="SBI", accounts=[], fixed_deposits=[
        ParsedFixedDeposit(fd_number="XXXXXXX0127", principal=500000.0, open_date=None,
                           rate=7.0, current_amount=500000.0, maturity_date=None, maturity_amount=613486.0),
    ], warnings=[])
    diff = import_service.diff_bank_accounts(existing, parsed)
    assert all(r["status"] == "unchanged" for r in diff)


def test_route_pdf_duplicate_still_blocks_by_default(app, family_and_user, tmp_path, monkeypatch):
    family, user = family_and_user
    path = tmp_path / "dup.pdf"
    path.write_bytes(b"%PDF-1.4 data")
    from import_service import file_sha256
    db.session.add(StatementImport(family_id=family.id, uploaded_by_id=user.id, bank="SBI",
                                   original_filename="dup.pdf", stored_path=str(path),
                                   file_hash=file_sha256(str(path)), status=IMPORT_STATUS_CONFIRMED))
    db.session.commit()

    monkeypatch.setattr(import_service, "parse_pdf",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should not parse")))
    res = import_service.route_pdf(app, family.id, user.id, str(path), "dup.pdf")
    assert res["kind"] == "duplicate"
    assert res["existing_import"] is not None


def test_route_pdf_allow_duplicate_bypasses_guard_and_records_supersedes(
    app, family_and_user, tmp_path, monkeypatch
):
    family, user = family_and_user
    path = tmp_path / "dup.pdf"
    path.write_bytes(b"%PDF-1.4 data")
    from import_service import file_sha256
    original = StatementImport(family_id=family.id, uploaded_by_id=user.id, bank="SBI",
                               original_filename="dup.pdf", stored_path=str(path),
                               file_hash=file_sha256(str(path)), status=IMPORT_STATUS_CONFIRMED)
    db.session.add(original)
    db.session.commit()

    monkeypatch.setattr(import_service, "parse_pdf",
                        lambda path, dpi=300, password=None: ("SBI", ParsedStatement(
                            bank="SBI", accounts=[], fixed_deposits=[
                                ParsedFixedDeposit(fd_number="XXXXXXX5653", principal=500000.0,
                                                   open_date=None, rate=7.0, current_amount=500000.0,
                                                   maturity_date=None, maturity_amount=574441.0),
                            ], warnings=[])))

    res = import_service.route_pdf(app, family.id, user.id, str(path), "dup.pdf",
                                   allow_duplicate=True, supersedes_import_id=original.id)
    assert res["kind"] == "bank"
    assert res["stmt"].supersedes_import_id == original.id
    assert res["stmt"].id != original.id


def test_reupload_shows_diff_page_then_override_creates_new_import(
    logged_in_client, family_and_user, monkeypatch
):
    """Full app-level flow: upload once, upload the identical bytes again --
    should render the duplicate/diff preview instead of a flat error page --
    then confirm the override and get a new pending import linked to the
    original via supersedes_import_id."""
    family, user = family_and_user
    pdf_bytes = b"%PDF-1.4 same bytes every time"

    call_count = {"n": 0}

    def fake_parse_pdf(path, dpi=300, password=None):
        call_count["n"] += 1
        fds = [ParsedFixedDeposit(fd_number="XXXXXXX0127", principal=500000.0, open_date=None,
                                  rate=7.0, current_amount=500000.0, maturity_date=None,
                                  maturity_amount=613486.0)]
        if call_count["n"] > 1:  # "parser fix" finds a second FD from the second pass onward
            fds.append(ParsedFixedDeposit(fd_number="XXXXXXX5653", principal=500000.0, open_date=None,
                                          rate=7.0, current_amount=500000.0, maturity_date=None,
                                          maturity_amount=574441.0))
        return "SBI", ParsedStatement(bank="SBI", accounts=[], fixed_deposits=fds, warnings=[])

    monkeypatch.setattr(import_service, "parse_pdf", fake_parse_pdf)

    first = logged_in_client.post(
        "/import/upload",
        data={"statement": (io.BytesIO(pdf_bytes), "sbi.pdf")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert first.status_code == 200
    original = StatementImport.query.filter_by(family_id=family.id).one()
    assert original.bank == "SBI"

    second = logged_in_client.post(
        "/import/upload",
        data={"statement": (io.BytesIO(pdf_bytes), "sbi.pdf")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert second.status_code == 200
    assert b"Already Imported" in second.data or b"already imported" in second.data.lower()
    assert b"5653" in second.data  # the newly-found FD shows up in the diff

    override = logged_in_client.post(
        "/import/upload/override",
        data={
            "stored_path": original.stored_path,
            "filename": "sbi.pdf",
            "supersedes_import_id": str(original.id),
        },
        follow_redirects=True,
    )
    assert override.status_code == 200

    imports = StatementImport.query.filter_by(family_id=family.id).order_by(StatementImport.id).all()
    assert len(imports) == 2
    assert imports[1].supersedes_import_id == original.id
    assert imports[1].id != original.id
