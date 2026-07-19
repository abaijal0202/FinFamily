import os
import tempfile
from datetime import datetime

from import_service import (
    file_sha256, find_existing_import, backfill_hashes_and_discard_duplicates,
)
from models import db, StatementImport, IMPORT_STATUS_PENDING, IMPORT_STATUS_CONFIRMED, \
    IMPORT_STATUS_DISCARDED


def _make_import(family_id, user_id, file_hash=None, status=IMPORT_STATUS_PENDING,
                 stored_path="", uploaded_at=None):
    imp = StatementImport(
        family_id=family_id, uploaded_by_id=user_id, bank="HDFC",
        original_filename="stmt.pdf", stored_path=stored_path,
        file_hash=file_hash, status=status,
        uploaded_at=uploaded_at or datetime.utcnow(),
    )
    db.session.add(imp)
    db.session.commit()
    return imp


def test_file_sha256_stable(tmp_path):
    p = tmp_path / "a.pdf"
    p.write_bytes(b"%PDF-1.4 same content")
    q = tmp_path / "b.pdf"
    q.write_bytes(b"%PDF-1.4 same content")
    assert file_sha256(str(p)) == file_sha256(str(q))
    q.write_bytes(b"%PDF-1.4 different")
    assert file_sha256(str(p)) != file_sha256(str(q))


def test_find_existing_import_ignores_discarded(app, family_and_user):
    family, user = family_and_user
    _make_import(family.id, user.id, file_hash="abc", status=IMPORT_STATUS_DISCARDED)
    assert find_existing_import(family.id, "abc") is None

    kept = _make_import(family.id, user.id, file_hash="abc", status=IMPORT_STATUS_CONFIRMED)
    assert find_existing_import(family.id, "abc").id == kept.id
    assert find_existing_import(family.id, None) is None
    assert find_existing_import(family.id, "other") is None


def test_backfill_discards_pending_duplicates_keeps_first(app, family_and_user, tmp_path):
    family, user = family_and_user
    pdf = tmp_path / "s.pdf"
    pdf.write_bytes(b"%PDF-1.4 hello")
    h = file_sha256(str(pdf))

    t0 = datetime(2026, 7, 1, 10, 0)
    first = _make_import(family.id, user.id, file_hash=None, stored_path=str(pdf),
                         status=IMPORT_STATUS_CONFIRMED, uploaded_at=t0)
    dup1 = _make_import(family.id, user.id, file_hash=h, uploaded_at=datetime(2026, 7, 2))
    dup2 = _make_import(family.id, user.id, file_hash=h, uploaded_at=datetime(2026, 7, 3))

    hashed, discarded = backfill_hashes_and_discard_duplicates()

    assert hashed == 1                     # first import got its hash backfilled
    assert first.file_hash == h
    assert discarded == 2                  # both later pending duplicates discarded
    assert dup1.status == IMPORT_STATUS_DISCARDED
    assert dup2.status == IMPORT_STATUS_DISCARDED
    assert first.status == IMPORT_STATUS_CONFIRMED  # keeper untouched
    assert "duplicate" in dup1.warnings.lower()


def test_backfill_never_discards_confirmed(app, family_and_user):
    family, user = family_and_user
    _make_import(family.id, user.id, file_hash="xyz", status=IMPORT_STATUS_CONFIRMED,
                 uploaded_at=datetime(2026, 7, 1))
    later_confirmed = _make_import(family.id, user.id, file_hash="xyz",
                                   status=IMPORT_STATUS_CONFIRMED,
                                   uploaded_at=datetime(2026, 7, 2))
    _, discarded = backfill_hashes_and_discard_duplicates()
    assert discarded == 0
    assert later_confirmed.status == IMPORT_STATUS_CONFIRMED
