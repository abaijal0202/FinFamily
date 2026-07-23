import tempfile

import import_service
from statement_import import cas_import
from models import Asset, StatementImport, IMPORT_STATUS_CONFIRMED


def _pdf(tmp_path, name, content=b"%PDF-1.4 data"):
    p = tmp_path / name
    p.write_bytes(content)
    return str(p)


def test_route_pdf_password_reaches_ocr(app, family_and_user, tmp_path, monkeypatch):
    family, user = family_and_user
    seen = {}

    def fake_parse(path, dpi=300, password=None):
        seen["password"] = password
        from statement_import.types import ParsedStatement
        return "HDFC", ParsedStatement(bank="HDFC", accounts=[], fixed_deposits=[], warnings=[])

    monkeypatch.setattr(import_service, "parse_pdf", fake_parse)
    monkeypatch.setattr(cas_import, "parse_cas",
                        lambda p, pw: (_ for _ in ()).throw(Exception("not a cas")))

    res = import_service.route_pdf(app, family.id, user.id, _pdf(tmp_path, "s.pdf"),
                                   "s.pdf", passwords=["SECRET"])
    assert seen["password"] == "SECRET"
    assert res["kind"] == "skipped"  # empty statement -> nothing staged


def test_route_pdf_cas_with_password(app, family_and_user, tmp_path, monkeypatch):
    family, user = family_and_user
    CAS = {"statement_period": {"from": "01-Apr-2026", "to": "17-Jul-2026"},
           "folios": [{"folio": "1/1", "amc": "Axis MF", "schemes": [
               {"scheme": "Axis Bluechip", "isin": "INF1", "amfi": "120503",
                "open": 0.0, "close": 100.0,
                "valuation": {"nav": 60.0, "value": 6000.0}, "transactions": []}]}]}

    def fake_parse_cas(path, pw):
        if pw != "MYPAN":
            raise Exception("bad password")
        return CAS

    monkeypatch.setattr(cas_import, "parse_cas", fake_parse_cas)

    res = import_service.route_pdf(app, family.id, user.id, _pdf(tmp_path, "cas.pdf"),
                                   "cas.pdf", passwords=["wrong", "MYPAN"])
    assert res["kind"] == "cas"
    assert res["counts"][0] == 1  # one new folio created
    asset = Asset.query.filter_by(family_id=family.id, scheme_code="120503").one()
    assert asset.units == 100.0
    # recorded as an upload-sourced confirmed import
    imp = StatementImport.query.filter_by(family_id=family.id).one()
    assert imp.status == IMPORT_STATUS_CONFIRMED and imp.source == "upload"


def test_route_pdf_falls_through_to_nsdl_cas(app, family_and_user, tmp_path, monkeypatch):
    family, user = family_and_user

    # CAMS/KFintech probe fails for every password tried...
    monkeypatch.setattr(cas_import, "parse_cas",
                        lambda p, pw: (_ for _ in ()).throw(Exception("not a cams cas")))

    from statement_import import nsdl_cas

    monkeypatch.setattr(nsdl_cas, "is_nsdl_cas_pdf", lambda path, password=None: True)

    def fake_apply(family_id, user_id, path, pw, original_filename, file_hash=None,
                   source="upload", email_date=None, dpi=300):
        return (2, 1, 0)

    monkeypatch.setattr(nsdl_cas, "apply_nsdl_cas", fake_apply)

    res = import_service.route_pdf(app, family.id, user.id, _pdf(tmp_path, "nsdl_cas.pdf"),
                                   "nsdl_cas.pdf")
    assert res["kind"] == "cas"
    assert res["counts"] == (2, 1, 0)
    assert "NSDL" in res["message"]


def test_route_pdf_falls_through_to_nps(app, family_and_user, tmp_path, monkeypatch):
    family, user = family_and_user

    # CAMS/KFintech and NSDL CAS probes both fail...
    monkeypatch.setattr(cas_import, "parse_cas",
                        lambda p, pw: (_ for _ in ()).throw(Exception("not a cams cas")))
    from statement_import import nsdl_cas
    monkeypatch.setattr(nsdl_cas, "is_nsdl_cas_pdf", lambda path, password=None: False)

    from statement_import import nps_kfintech
    monkeypatch.setattr(nps_kfintech, "is_nps_pdf", lambda path, password=None: True)

    def fake_apply(family_id, user_id, path, password=None, original_filename="NPS.pdf",
                   file_hash=None, source="upload", email_date=None):
        return (1, 0, 0)

    monkeypatch.setattr(nps_kfintech, "apply_nps", fake_apply)

    res = import_service.route_pdf(app, family.id, user.id, _pdf(tmp_path, "nps.pdf"), "nps.pdf")
    assert res["kind"] == "nps"
    assert res["counts"] == (1, 0, 0)
    assert "NPS" in res["message"]


def test_route_pdf_asset_owner_id_used_for_cas_not_uploader(app, family_and_user, tmp_path, monkeypatch):
    """An Owner uploading a family member's CAS should create the MF asset
    under that member, not under whoever clicked upload."""
    family, uploader = family_and_user
    from models import db, User, ROLE_VIEWER
    member = User(name="Spouse", email="spouse@example.com", role=ROLE_VIEWER, family_id=family.id)
    db.session.add(member)
    db.session.commit()

    seen_owner = {}

    def fake_apply_cas(family_id, owner_id, *a, **kw):
        seen_owner["owner_id"] = owner_id
        return (1, 0, 0)

    monkeypatch.setattr(cas_import, "parse_cas", lambda p, pw: {})  # unused, apply_cas is faked
    monkeypatch.setattr(cas_import, "apply_cas", fake_apply_cas)
    monkeypatch.setattr(cas_import, "is_cas_pdf", lambda path, pw: True)

    res = import_service.route_pdf(app, family.id, uploader.id, _pdf(tmp_path, "spouse_cas.pdf"),
                                   "spouse_cas.pdf", asset_owner_id=member.id)
    assert res["kind"] == "cas"
    assert seen_owner["owner_id"] == member.id  # not uploader.id


def test_route_pdf_asset_owner_id_defaults_to_uploader(app, family_and_user, tmp_path, monkeypatch):
    family, uploader = family_and_user
    seen_owner = {}

    def fake_apply_cas(family_id, owner_id, *a, **kw):
        seen_owner["owner_id"] = owner_id
        return (1, 0, 0)

    monkeypatch.setattr(cas_import, "apply_cas", fake_apply_cas)
    monkeypatch.setattr(cas_import, "is_cas_pdf", lambda path, pw: True)

    import_service.route_pdf(app, family.id, uploader.id, _pdf(tmp_path, "cas.pdf"), "cas.pdf")
    assert seen_owner["owner_id"] == uploader.id


def test_route_pdf_duplicate_short_circuits(app, family_and_user, tmp_path, monkeypatch):
    family, user = family_and_user
    path = _pdf(tmp_path, "dup.pdf")
    from import_service import file_sha256
    from models import db
    db.session.add(StatementImport(family_id=family.id, uploaded_by_id=user.id, bank="HDFC",
                                   original_filename="dup.pdf", stored_path=path,
                                   file_hash=file_sha256(path), status=IMPORT_STATUS_CONFIRMED))
    db.session.commit()

    # parse should never be called for a duplicate
    monkeypatch.setattr(cas_import, "parse_cas",
                        lambda p, pw: (_ for _ in ()).throw(AssertionError("should not parse")))
    res = import_service.route_pdf(app, family.id, user.id, path, "dup.pdf")
    assert res["kind"] == "duplicate"
