from datetime import date

from statement_import import cas_import
from analytics import compute_asset_xirr
from models import db, Asset, AssetCashflow, StatementImport

CAS_FIXTURE = {
    "statement_period": {"from": "01-Apr-2026", "to": "17-Jul-2026"},
    "folios": [
        {
            "folio": "12345678/90",
            "amc": "Axis Mutual Fund",
            "schemes": [
                {
                    "scheme": "Axis Bluechip Fund - Direct Growth",
                    "isin": "INF846K01EW2", "amfi": "120503",
                    "open": 0.0, "close": 150.5,
                    "valuation": {"date": "2026-07-17", "nav": 62.48, "value": 9403.24},
                    "transactions": [
                        {"date": "2026-04-05", "description": "SIP Purchase", "amount": 5000.0,
                         "units": 84.03, "nav": 59.50, "type": "PURCHASE_SIP"},
                        {"date": "2026-05-05", "description": "SIP Purchase", "amount": 4000.0,
                         "units": 66.47, "nav": 60.18, "type": "PURCHASE_SIP"},
                        {"date": "2026-06-01", "description": "*** Stamp Duty ***", "amount": 0.0,
                         "units": None, "nav": None, "type": "STAMP_DUTY_TAX"},
                    ],
                },
                {
                    "scheme": "Axis Closed Fund - Regular",
                    "isin": "INF846K01XX1", "amfi": "111111",
                    "open": 200.0, "close": 0.0,  # fully redeemed
                    "valuation": {"date": "2026-07-17", "nav": 10.0, "value": 0.0},
                    "transactions": [],
                },
            ],
        }
    ],
}


def test_apply_cas_creates_and_updates(app, family_and_user, monkeypatch):
    family, user = family_and_user
    monkeypatch.setattr(cas_import, "parse_cas", lambda path, pw: CAS_FIXTURE)

    created, updated, skipped = cas_import.apply_cas(family.id, user.id, "x.pdf", "PAN")
    assert (created, updated, skipped) == (1, 0, 1)  # zero-unit folio skipped

    asset = Asset.query.filter_by(family_id=family.id, category="MF").one()
    assert asset.units == 150.5
    assert asset.current_value == 9403.24
    assert asset.scheme_code == "120503"
    assert asset.isin == "INF846K01EW2"
    assert asset.folio_number == "12345678/90"
    assert asset.institution == "Axis Mutual Fund"

    # audit row recorded
    assert StatementImport.query.filter_by(family_id=family.id).count() == 1

    # cashflows captured for XIRR: 2 SIPs stored, zero-amount stamp duty skipped
    flows = AssetCashflow.query.filter_by(asset_id=asset.id).all()
    assert len(flows) == 2
    assert {f.flow_date for f in flows} == {date(2026, 4, 5), date(2026, 5, 5)}
    assert all(not f.synthetic for f in flows)  # open units were 0 -> no synthetic flow

    # XIRR computable from flows + current value (9000 invested -> 9403 in ~3 months)
    pct, approx = compute_asset_xirr(asset, today=date(2026, 7, 17))
    assert pct is not None and pct > 0 and not approx

    # re-applying the same CAS updates in place — no duplicate asset, no duplicate flows
    created2, updated2, _ = cas_import.apply_cas(family.id, user.id, "x.pdf", "PAN")
    assert (created2, updated2) == (0, 1)
    assert Asset.query.filter_by(family_id=family.id, category="MF").count() == 1
    assert AssetCashflow.query.filter_by(asset_id=asset.id).count() == 2


def test_apply_cas_synthetic_opening_flow(app, family_and_user, monkeypatch):
    family, user = family_and_user
    fixture = {
        "statement_period": {"from": "01-Apr-2026", "to": "17-Jul-2026"},
        "folios": [{
            "folio": "555/1", "amc": "HDFC MF",
            "schemes": [{
                "scheme": "HDFC Flexi Cap - Direct Growth",
                "isin": "INF179K01YV8", "amfi": "118955",
                "open": 100.0, "close": 110.0,  # held before the statement period
                "valuation": {"date": "2026-07-17", "nav": 105.0, "value": 11550.0},
                "transactions": [
                    {"date": "2026-05-10", "description": "Purchase", "amount": 1000.0,
                     "units": 10.0, "nav": 100.0, "type": "PURCHASE"},
                ],
            }],
        }],
    }
    monkeypatch.setattr(cas_import, "parse_cas", lambda path, pw: fixture)
    cas_import.apply_cas(family.id, user.id, "y.pdf", "PAN")

    asset = Asset.query.filter_by(family_id=family.id, scheme_code="118955").one()
    flows = AssetCashflow.query.filter_by(asset_id=asset.id).order_by(AssetCashflow.flow_date).all()
    assert len(flows) == 2
    synthetic = flows[0]
    assert synthetic.synthetic and synthetic.flow_date == date(2026, 4, 1)
    assert synthetic.amount == 100.0 * 100.0  # open units x first txn nav

    # XIRR flagged as approximate because history starts from a synthetic flow
    pct, approx = compute_asset_xirr(asset, today=date(2026, 7, 17))
    assert pct is not None and approx


def test_apply_cas_matches_existing_by_isin(app, family_and_user, monkeypatch):
    family, user = family_and_user
    existing = Asset(family_id=family.id, owner_id=user.id, category="MF",
                     name="My Axis Fund (manual)", isin="INF846K01EW2",
                     current_value=1.0)
    db.session.add(existing)
    db.session.commit()

    monkeypatch.setattr(cas_import, "parse_cas", lambda path, pw: CAS_FIXTURE)
    created, updated, _ = cas_import.apply_cas(family.id, user.id, "x.pdf", "PAN")

    assert (created, updated) == (0, 1)
    assert existing.units == 150.5
    assert existing.current_value == 9403.24
    # user's own name is preserved; codes are backfilled
    assert existing.name == "My Axis Fund (manual)"
    assert existing.scheme_code == "120503"
