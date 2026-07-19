from datetime import date, datetime, timedelta

from analytics import (
    xirr, compute_alerts, monthly_cashflow, category_spend, category_comparison,
    last_n_months,
)
from models import Asset


# --------------------------------------------------------------------- XIRR

def test_xirr_simple_doubling_in_one_year():
    flows = [(date(2025, 1, 1), -1000.0), (date(2026, 1, 1), 2000.0)]
    r = xirr(flows)
    assert r is not None
    assert abs(r - 1.0) < 0.01  # ~100% p.a.


def test_xirr_flat_investment():
    flows = [(date(2025, 1, 1), -1000.0), (date(2026, 1, 1), 1000.0)]
    r = xirr(flows)
    assert abs(r) < 0.01  # ~0%


def test_xirr_sip_like_flows_reasonable_range():
    # 12 monthly SIPs of 10k, final value 130k -> modest positive return
    flows = [(date(2025, m, 1), -10_000.0) for m in range(1, 13)]
    flows.append((date(2026, 1, 1), 130_000.0))
    r = xirr(flows)
    assert r is not None and 0.05 < r < 0.35


def test_xirr_rejects_degenerate_input():
    assert xirr([]) is None
    assert xirr([(date(2025, 1, 1), -100.0)]) is None
    assert xirr([(date(2025, 1, 1), -100.0), (date(2025, 6, 1), -200.0)]) is None  # no inflow
    assert xirr([(date(2025, 1, 1), -100.0), (date(2025, 1, 1), 150.0)]) is None  # zero span


# ------------------------------------------------------------------- Alerts

def _asset(category, **kw):
    return Asset(category=category, name=f"{category} test", family_id=1, owner_id=1, **kw)


def test_fd_maturity_alerts():
    today = date(2026, 7, 19)
    assets = [
        _asset("FD", maturity_date=today + timedelta(days=10)),   # due soon
        _asset("FD", maturity_date=today + timedelta(days=45)),   # info
        _asset("FD", maturity_date=today - timedelta(days=3)),    # matured
        _asset("FD", maturity_date=today + timedelta(days=200)),  # no alert
    ]
    alerts = compute_alerts(assets, today=today)
    kinds = [a["kind"] for a in alerts]
    assert kinds.count("fd") == 3
    assert sum(1 for a in alerts if a["severity"] == "due") == 2  # 10-day + matured


def test_insurance_premium_alert():
    today = date(2026, 7, 19)
    alerts = compute_alerts([_asset("INSURANCE", maturity_date=today + timedelta(days=5))],
                            today=today)
    assert len(alerts) == 1 and alerts[0]["severity"] == "due"


def test_mf_valuation_health_alerts():
    today = date(2026, 7, 19)
    stale = _asset("MF", units=100.0, scheme_code="120503",
                   last_valued_at=datetime.utcnow() - timedelta(days=30))
    no_code = _asset("MF", units=50.0)
    fresh = _asset("MF", units=10.0, scheme_code="120503", last_valued_at=datetime.utcnow())
    alerts = compute_alerts([stale, no_code, fresh], today=today)
    assert len([a for a in alerts if a["kind"] == "mf"]) == 2


def test_alerts_sorted_due_first():
    today = date(2026, 7, 19)
    assets = [
        _asset("MF", units=1.0),                                   # info
        _asset("FD", maturity_date=today + timedelta(days=2)),     # due
    ]
    alerts = compute_alerts(assets, today=today)
    assert alerts[0]["severity"] == "due"


# ---------------------------------------------------------------- Cash flow

class _Txn:
    def __init__(self, d, deposit=0.0, withdrawal=0.0, category="Uncategorized"):
        self.txn_date, self.deposit, self.withdrawal, self.category = d, deposit, withdrawal, category


def test_last_n_months():
    months = last_n_months(3, today=date(2026, 2, 15))
    assert months == [(2025, 12), (2026, 1), (2026, 2)]


def test_monthly_cashflow_aggregates_and_zero_fills():
    today = date(2026, 7, 19)
    txns = [
        _Txn(date(2026, 7, 1), deposit=50_000.0, category="Salary"),
        _Txn(date(2026, 7, 5), withdrawal=12_000.0, category="Bills & Utilities"),
        _Txn(date(2026, 6, 20), withdrawal=8_000.0, category="Food & Groceries"),
        _Txn(date(2026, 7, 8), deposit=99_999.0, category="Internal Transfer"),  # excluded
    ]
    rows = monthly_cashflow(txns, months=12, today=today)
    assert len(rows) == 12
    jul = rows[-1]
    assert jul["label"] == "Jul 2026"
    assert jul["income"] == 50_000.0 and jul["expense"] == 12_000.0 and jul["net"] == 38_000.0
    assert rows[-2]["expense"] == 8_000.0
    assert rows[0]["income"] == 0.0  # zero-filled old month


def test_category_spend_and_comparison():
    today = date(2026, 7, 19)
    txns = [
        _Txn(date(2026, 7, 2), withdrawal=5_000.0, category="Food & Groceries"),
        _Txn(date(2026, 7, 3), withdrawal=2_000.0, category="Food & Groceries"),
        _Txn(date(2026, 6, 2), withdrawal=4_000.0, category="Food & Groceries"),
        _Txn(date(2026, 6, 9), withdrawal=1_500.0, category="Shopping / POS"),
    ]
    spend = category_spend(txns, 2026, 7)
    assert spend[0] == ("Food & Groceries", 7_000.0)

    rows, this_label, prev_label = category_comparison(txns, today=today)
    assert (this_label, prev_label) == ("Jul 2026", "Jun 2026")
    food = next(r for r in rows if r["category"] == "Food & Groceries")
    assert food["this_month"] == 7_000.0 and food["prev_month"] == 4_000.0 and food["delta"] == 3_000.0
    pos = next(r for r in rows if r["category"] == "Shopping / POS")
    assert pos["this_month"] == 0.0 and pos["delta"] == -1_500.0
