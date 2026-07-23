"""End-to-end check for the bug report: after adding an asset, the
dashboard's Net Worth Trend and Asset Allocation charts should render
(not sit empty until a manual refresh/app restart), and money amounts
should use Indian digit grouping."""
from models import NetWorthSnapshot


def test_add_asset_triggers_snapshot_and_dashboard_renders_charts(logged_in_client, family_and_user):
    family, user = family_and_user

    # No snapshot exists yet for a brand-new family.
    assert NetWorthSnapshot.query.filter_by(family_id=family.id).count() == 0

    resp = logged_in_client.post(
        f"/assets/add/BANK",
        data={"name": "HDFC Savings", "institution": "HDFC Bank",
              "current_value": "1234567", "owner_id": str(user.id)},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    # Adding an asset must now create today's snapshot immediately.
    assert NetWorthSnapshot.query.filter_by(family_id=family.id).count() == 1

    dash = logged_in_client.get("/dashboard")
    html = dash.get_data(as_text=True)

    # Allocation chart has data (one BANK asset) so its canvas should render.
    assert 'id="allocationChart"' in html
    # KPI values use Indian grouping (12,34,567), not Western (1,234,567).
    assert "12,34,567" in html
    assert "1,234,567" not in html


def test_edit_and_delete_asset_also_refresh_snapshot(logged_in_client, family_and_user):
    family, user = family_and_user

    logged_in_client.post(
        "/assets/add/BANK",
        data={"name": "Axis Savings", "current_value": "50000", "owner_id": str(user.id)},
        follow_redirects=True,
    )
    from models import Asset
    asset = Asset.query.filter_by(family_id=family.id, name="Axis Savings").one()

    NetWorthSnapshot.query.filter_by(family_id=family.id).delete()
    from models import db
    db.session.commit()
    assert NetWorthSnapshot.query.filter_by(family_id=family.id).count() == 0

    logged_in_client.post(
        f"/assets/{asset.id}/edit",
        data={"name": "Axis Savings", "current_value": "75000", "owner_id": str(user.id)},
        follow_redirects=True,
    )
    assert NetWorthSnapshot.query.filter_by(family_id=family.id).count() == 1
    snap = NetWorthSnapshot.query.filter_by(family_id=family.id).one()
    assert snap.total_assets == 75000.0

    NetWorthSnapshot.query.filter_by(family_id=family.id).delete()
    db.session.commit()

    logged_in_client.post(f"/assets/{asset.id}/delete", follow_redirects=True)
    assert NetWorthSnapshot.query.filter_by(family_id=family.id).count() == 1
    snap = NetWorthSnapshot.query.filter_by(family_id=family.id).one()
    assert snap.total_assets == 0.0
