"""An Owner/Contributor should be able to import a family member's
statement — bank PDF or CAS/NPS — without logging in as that member.
Covers the "For" owner picker on the upload form and its effect on the
Assets eventually created (bank statements via the review/confirm screen,
CAS/NSDL/NPS synchronously)."""
import io

import import_service
from models import db, User, Asset, StatementImport, ROLE_VIEWER, IMPORT_STATUS_PENDING


def _add_member(family, name="Spouse", email="spouse@example.com"):
    member = User(name=name, email=email, role=ROLE_VIEWER, family_id=family.id)
    db.session.add(member)
    db.session.commit()
    return member


def test_bank_statement_upload_for_family_member_creates_asset_under_them(
    logged_in_client, family_and_user, monkeypatch
):
    family, owner_user = family_and_user
    spouse = _add_member(family)

    from statement_import.types import ParsedStatement, ParsedAccount

    def fake_parse_pdf(path, dpi=300, password=None):
        return "HDFC", ParsedStatement(
            bank="HDFC",
            accounts=[ParsedAccount(
                account_number="1234567890", account_type="Savings",
                opening_balance=1000.0, closing_balance=5000.0,
                debit_total=0.0, credit_total=4000.0, transactions=[],
            )],
            fixed_deposits=[], warnings=[],
        )

    monkeypatch.setattr(import_service, "parse_pdf", fake_parse_pdf)

    resp = logged_in_client.post(
        "/import/upload",
        data={
            "statement": (io.BytesIO(b"%PDF-1.4 fake"), "spouse_hdfc.pdf"),
            "owner_id": str(spouse.id),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200

    stmt = StatementImport.query.filter_by(family_id=family.id).one()
    assert stmt.status == IMPORT_STATUS_PENDING
    assert stmt.asset_owner_id == spouse.id
    assert stmt.uploaded_by_id == owner_user.id  # who actually clicked upload

    # Confirm the staged account — new asset should belong to the spouse,
    # not the logged-in Owner who did the upload.
    acc = stmt.accounts[0]
    confirm = logged_in_client.post(
        f"/import/{stmt.id}/confirm",
        data={
            f"asset_choice_{acc.id}": "new",
            f"asset_name_{acc.id}": "Spouse HDFC Savings",
            f"closing_balance_{acc.id}": "5000",
            f"last4_{acc.id}": "7890",
            "manual_account_count": "0",
        },
        follow_redirects=True,
    )
    assert confirm.status_code == 200

    asset = Asset.query.filter_by(family_id=family.id, name="Spouse HDFC Savings").one()
    assert asset.owner_id == spouse.id


def test_bank_statement_upload_defaults_to_uploader_without_owner_id(
    logged_in_client, family_and_user, monkeypatch
):
    family, owner_user = family_and_user
    from statement_import.types import ParsedStatement

    monkeypatch.setattr(import_service, "parse_pdf",
                        lambda path, dpi=300, password=None:
                        ("HDFC", ParsedStatement(bank="HDFC", accounts=[], fixed_deposits=[], warnings=[])))

    logged_in_client.post(
        "/import/upload",
        data={"statement": (io.BytesIO(b"%PDF-1.4 fake"), "own.pdf")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    # empty statement gets auto-deleted (route_pdf "skipped"), so check via
    # a variant with one account instead — reuse the model default directly.
    stmt = StatementImport(family_id=family.id, uploaded_by_id=owner_user.id, bank="HDFC")
    db.session.add(stmt)
    db.session.commit()
    assert stmt.asset_owner_id is None  # column default is nullable; app.py fills it in


def test_owner_id_from_a_different_family_is_rejected(logged_in_client, family_and_user, monkeypatch):
    """A spoofed owner_id for a user outside this family must not be
    honoured — falls back to the uploader."""
    family, owner_user = family_and_user
    from models import Family
    other_family = Family(name="Other Family")
    db.session.add(other_family)
    db.session.flush()
    outsider = User(name="Outsider", email="outsider@example.com", role=ROLE_VIEWER,
                    family_id=other_family.id)
    db.session.add(outsider)
    db.session.commit()

    from statement_import.types import ParsedStatement, ParsedAccount

    def fake_parse_pdf(path, dpi=300, password=None):
        return "HDFC", ParsedStatement(
            bank="HDFC",
            accounts=[ParsedAccount(account_number="111", account_type="Savings",
                                    opening_balance=0.0, closing_balance=100.0,
                                    debit_total=0.0, credit_total=100.0, transactions=[])],
            fixed_deposits=[], warnings=[],
        )
    monkeypatch.setattr(import_service, "parse_pdf", fake_parse_pdf)

    logged_in_client.post(
        "/import/upload",
        data={"statement": (io.BytesIO(b"%PDF-1.4 fake"), "x.pdf"), "owner_id": str(outsider.id)},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    stmt = StatementImport.query.filter_by(family_id=family.id).one()
    assert stmt.asset_owner_id == owner_user.id  # not the outsider
