"""Net-worth math: the numbers a finance app must never get wrong."""
from app import compute_net_worth, group_by_class
from models import Asset


def _asset(category, value, **kw):
    return Asset(category=category, name=f"{category} test", current_value=value,
                 family_id=1, owner_id=1, **kw)


def test_net_worth_nets_liabilities():
    assets = [
        _asset("BANK", 100_000.0),
        _asset("MF", 250_000.0),
        _asset("LOAN", 80_000.0),
    ]
    total_assets, total_liabilities, net = compute_net_worth(assets)
    assert total_assets == 350_000.0
    assert total_liabilities == 80_000.0
    assert net == 270_000.0


def test_insurance_excluded_from_net_worth():
    assets = [_asset("BANK", 50_000.0), _asset("INSURANCE", 1_000_000.0)]
    total_assets, _, net = compute_net_worth(assets)
    assert total_assets == 50_000.0
    assert net == 50_000.0


def test_none_values_treated_as_zero():
    assets = [_asset("BANK", None), _asset("LOAN", None)]
    total_assets, total_liabilities, net = compute_net_worth(assets)
    assert (total_assets, total_liabilities, net) == (0.0, 0.0, 0.0)


def test_group_by_class_signs_liabilities_negative():
    groups = group_by_class([
        _asset("BANK", 10_000.0),
        _asset("PPF", 40_000.0),
        _asset("LOAN", 5_000.0),
        _asset("INSURANCE", 999_999.0),  # excluded entirely
    ])
    assert groups["Cash & Bank"] == 10_000.0
    assert groups["Retirement - Government-backed"] == 40_000.0
    assert groups["Liabilities"] == -5_000.0
    assert "Insurance (not counted in net worth)" not in groups


def test_asset_net_worth_contribution_property():
    assert _asset("LOAN", 1000.0).net_worth_contribution == -1000.0
    assert _asset("INSURANCE", 1000.0).net_worth_contribution == 0.0
    assert _asset("MF", 1000.0).net_worth_contribution == 1000.0
