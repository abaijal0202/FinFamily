from datetime import datetime

import gmail_ingest
from models import db, StatementImport, ScanSender, ProcessedEmail, \
    IMPORT_STATUS_PENDING, IMPORT_STATUS_CONFIRMED, IMPORT_STATUS_DISCARDED


def _login_client(app, email="tester@example.com", password="pw"):
    c = app.test_client()
    c.post("/login", data={"email": email, "password": password})
    return c


def _make_import(family_id, user_id, status, **kw):
    imp = StatementImport(family_id=family_id, uploaded_by_id=user_id, bank="HDFC",
                          original_filename="s.pdf", stored_path="", status=status, **kw)
    db.session.add(imp)
    db.session.commit()
    return imp


# ------------------------------------------------- delete discarded imports

def test_delete_only_discarded(app, family_and_user):
    family, user = family_and_user
    c = _login_client(app)

    pending = _make_import(family.id, user.id, IMPORT_STATUS_PENDING)
    confirmed = _make_import(family.id, user.id, IMPORT_STATUS_CONFIRMED)
    discarded = _make_import(family.id, user.id, IMPORT_STATUS_DISCARDED)

    # pending and confirmed are refused
    for imp in (pending, confirmed):
        r = c.post(f"/import/{imp.id}/delete", follow_redirects=True)
        assert b"Only discarded imports can be deleted" in r.data
    assert StatementImport.query.count() == 3

    # discarded is removed
    r = c.post(f"/import/{discarded.id}/delete", follow_redirects=True)
    assert b"removed from history" in r.data
    assert StatementImport.query.count() == 2
    assert db.session.get(StatementImport, discarded.id) is None


# ------------------------------------------------- source + email date

def test_import_history_shows_source_and_email_date(app, family_and_user):
    family, user = family_and_user
    c = _login_client(app)
    _make_import(family.id, user.id, IMPORT_STATUS_CONFIRMED,
                 source="gmail", email_date=datetime(2026, 7, 12, 9, 30))
    _make_import(family.id, user.id, IMPORT_STATUS_CONFIRMED, source="upload")

    r = c.get("/import")
    body = r.data.decode()
    assert "Gmail" in body and "email: 12 Jul 2026" in body
    assert "Upload" in body


# ------------------------------------------------- scan sender config

def test_scan_sender_add_and_config_merge(app, family_and_user):
    family, user = family_and_user
    c = _login_client(app)

    r = c.post("/import/settings/senders/add",
               data={"email": "Estatements@MyBank.co.in", "attachment_password": "PAN123",
                     "notes": "bank pdf pw"},
               follow_redirects=True)
    assert r.status_code == 200
    s = ScanSender.query.filter_by(family_id=family.id).one()
    assert s.email == "estatements@mybank.co.in"  # normalized lowercase
    assert s.attachment_password == "PAN123"

    cfg = gmail_ingest._config(app, family_id=family.id)
    assert "estatements@mybank.co.in" in cfg["senders"]
    assert ("estatements@mybank.co.in", "PAN123") in cfg["sender_passwords"]
    # matching works for mail from that sender
    assert gmail_ingest._matches("Alerts <estatements@mybank.co.in>", "Your e-statement", cfg)

    # settings page renders with masked password
    r = c.get("/import/settings")
    assert b"estatements@mybank.co.in" in r.data and b"PAN123" not in r.data

    # delete
    r = c.post(f"/import/settings/senders/{s.id}/delete", follow_redirects=True)
    assert ScanSender.query.count() == 0


def test_scan_history_clear(app, family_and_user):
    family, user = family_and_user
    c = _login_client(app)
    db.session.add(ProcessedEmail(family_id=family.id, message_id="<m1@x>", sender="a@b",
                                  subject="s", result="error: bad password"))
    db.session.commit()

    r = c.get("/import/settings")
    assert b"error: bad password" in r.data  # visible for diagnosis

    r = c.post("/import/settings/scan-history/clear", follow_redirects=True)
    assert b"Scan history cleared (1" in r.data
    assert ProcessedEmail.query.count() == 0
