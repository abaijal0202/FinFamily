from datetime import date

import valuation
from models import db, Asset, NetWorthSnapshot

SAMPLE_NAV_FILE = """Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Net Asset Value;Date
Open Ended Schemes(Equity Scheme - Large Cap Fund)
Axis Mutual Fund
120503;INF846K01EW2;INF846K01EX0;Axis Bluechip Fund - Direct Plan - Growth;62.4800;17-Jul-2026
119551;INF209KB18D3;-;Aditya Birla SL Frontline Equity - Growth;512.3300;17-Jul-2026
999999;INF000000000;-;Some Suspended Fund;N.A.;17-Jul-2026
"""


def test_parse_amfi_nav_file():
    navs = valuation.parse_amfi_nav_file(SAMPLE_NAV_FILE)
    assert navs["120503"] == (62.48, "17-Jul-2026")
    assert navs["119551"] == (512.33, "17-Jul-2026")
    assert "999999" not in navs  # N.A. NAVs skipped
    assert len(navs) == 2       # header/section lines ignored


def test_refresh_mf_valuations(app, family_and_user, monkeypatch):
    family, user = family_and_user
    mf = Asset(family_id=family.id, owner_id=user.id, category="MF",
               name="Axis Bluechip", units=100.0, scheme_code="120503",
               current_value=1000.0)
    no_code = Asset(family_id=family.id, owner_id=user.id, category="MF",
                    name="Manual MF", current_value=5000.0)
    db.session.add_all([mf, no_code])
    db.session.commit()

    monkeypatch.setattr(valuation, "fetch_amfi_navs",
                        lambda app, force=False: {"120503": (62.48, "17-Jul-2026")})
    updated = valuation.refresh_mf_valuations(app)

    assert updated == 1
    assert mf.current_value == 6248.0          # 100 units x 62.48
    assert mf.last_valued_at is not None
    assert no_code.current_value == 5000.0     # untouched without scheme_code


def test_snapshot_all_families_includes_private_assets(app, family_and_user):
    family, user = family_and_user
    db.session.add_all([
        Asset(family_id=family.id, owner_id=user.id, category="BANK",
              name="Shared", current_value=10_000.0),
        Asset(family_id=family.id, owner_id=user.id, category="BANK",
              name="Private", current_value=5_000.0, is_private=True),
        Asset(family_id=family.id, owner_id=user.id, category="LOAN",
              name="Loan", current_value=3_000.0),
    ])
    db.session.commit()

    valuation.snapshot_all_families()

    snap = NetWorthSnapshot.query.filter_by(family_id=family.id,
                                            snapshot_date=date.today()).one()
    assert snap.total_assets == 15_000.0   # private asset included in family total
    assert snap.total_liabilities == 3_000.0
    assert snap.net_worth == 12_000.0

    # idempotent: second run updates, doesn't duplicate
    valuation.snapshot_all_families()
    assert NetWorthSnapshot.query.filter_by(family_id=family.id).count() == 1
