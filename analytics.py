"""Pure-computation helpers: XIRR, dashboard alerts, cash-flow aggregation.

No database access here — everything takes plain objects/lists so it's
trivially unit-testable. Routes fetch data, these functions compute.
"""
from datetime import date, datetime, timedelta
from collections import defaultdict

# ---------------------------------------------------------------------------
# XIRR
# ---------------------------------------------------------------------------

def xirr(cashflows):
    """Annualized internal rate of return for irregular cash flows.

    cashflows: list of (date, amount) with the investor's sign convention —
    negative = money paid in (purchase/SIP), positive = money received
    (redemption) or the current value of the holding as a final inflow.

    Returns the rate as a decimal (0.12 = 12% p.a.), or None when it can't
    be computed (all same sign, zero span, or no bracketable root).
    Solved by bisection on NPV over (-99%, +1000%) — slower than Newton but
    can't diverge.
    """
    flows = [(d, float(a)) for d, a in cashflows if a]
    if len(flows) < 2:
        return None
    flows.sort(key=lambda f: f[0])
    if not (any(a < 0 for _, a in flows) and any(a > 0 for _, a in flows)):
        return None
    t0 = flows[0][0]
    span_days = (flows[-1][0] - t0).days
    if span_days < 1:
        return None

    def npv(rate):
        total = 0.0
        for d, a in flows:
            years = (d - t0).days / 365.25
            total += a / ((1.0 + rate) ** years)
        return total

    lo, hi = -0.99, 10.0
    f_lo, f_hi = npv(lo), npv(hi)
    if f_lo * f_hi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2.0
        f_mid = npv(mid)
        if abs(f_mid) < 1e-9:
            return mid
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2.0


def compute_asset_xirr(asset, today=None):
    """XIRR % for an MF asset from its stored cashflows + current value.

    Returns (xirr_pct, approximate) or (None, False) when not computable.
    `approximate` is True when the history starts from a synthetic opening-
    balance flow (CAS didn't cover the fund since inception).
    """
    today = today or date.today()
    flows = getattr(asset, "cashflows", None) or []
    if not flows or not asset.current_value:
        return None, False
    series = [(f.flow_date, -float(f.amount)) for f in flows if f.amount]
    if not series:
        return None, False
    series.append((today, float(asset.current_value)))
    rate = xirr(series)
    if rate is None:
        return None, False
    # Guard against nonsense from tiny time spans
    earliest = min(d for d, _ in series)
    if (today - earliest).days < 30:
        return None, False
    approximate = any(f.synthetic for f in flows)
    return round(rate * 100.0, 1), approximate


# ---------------------------------------------------------------------------
# Dashboard alerts (BRD 6.8, scoped to data the app actually has)
# ---------------------------------------------------------------------------

def compute_alerts(assets, today=None):
    """Returns a list of {kind, severity, title, detail} dicts.

    severity: 'due' (act now) or 'info' (worth knowing). Rules only use
    fields the app reliably captures — no speculative alerts.
    """
    today = today or date.today()
    alerts = []

    for a in assets:
        # FD maturities within 60 days, or already matured
        if a.category == "FD" and a.maturity_date:
            days = (a.maturity_date - today).days
            if days < 0:
                alerts.append(dict(kind="fd", severity="due", title=f"{a.name} has matured",
                                   detail=f"Matured {-days} day(s) ago — redeem or renew, then update the asset."))
            elif days <= 60:
                alerts.append(dict(kind="fd", severity="due" if days <= 14 else "info",
                                   title=f"{a.name} matures in {days} day(s)",
                                   detail=f"Maturity date {a.maturity_date.strftime('%d %b %Y')}."))

        # Insurance premium due (maturity_date is reused as next-premium-due)
        if a.category == "INSURANCE" and a.maturity_date:
            days = (a.maturity_date - today).days
            if 0 <= days <= 45:
                alerts.append(dict(kind="insurance", severity="due" if days <= 10 else "info",
                                   title=f"{a.name}: premium due in {days} day(s)",
                                   detail=f"Due {a.maturity_date.strftime('%d %b %Y')}. Update the due date after paying."))
            elif days < 0:
                alerts.append(dict(kind="insurance", severity="due",
                                   title=f"{a.name}: premium overdue",
                                   detail=f"Was due {a.maturity_date.strftime('%d %b %Y')} — check policy status."))

        # PPF 15-year maturity window
        if a.category == "PPF" and a.opened_on:
            maturity = date(a.opened_on.year + 15, a.opened_on.month, a.opened_on.day)
            days = (maturity - today).days
            if 0 <= days <= 180:
                alerts.append(dict(kind="ppf", severity="info",
                                   title=f"{a.name} completes 15 years in {days} day(s)",
                                   detail="Plan withdrawal or a 5-year extension (FR-PPF-04)."))

        # Manually-tracked balances gone stale (PPF/EPF have no auto feed)
        if a.category in ("PPF", "EPF") and a.updated_at:
            staleness = (datetime.utcnow() - a.updated_at).days
            if staleness > 120:
                alerts.append(dict(kind="stale", severity="info",
                                   title=f"{a.name} balance not updated in {staleness} days",
                                   detail="Manual asset — check the latest balance and update it."))

        # MF auto-valuation health
        if a.category == "MF" and (a.units or 0) > 0:
            if not a.scheme_code:
                alerts.append(dict(kind="mf", severity="info",
                                   title=f"{a.name} has units but no AMFI scheme code",
                                   detail="Add the scheme code so NAV refresh can value it automatically."))
            elif a.last_valued_at and (datetime.utcnow() - a.last_valued_at).days > 7:
                alerts.append(dict(kind="mf", severity="info",
                                   title=f"{a.name} NAV is {(datetime.utcnow() - a.last_valued_at).days} days old",
                                   detail="Refresh valuations, or check the scheme code if this persists."))

    order = {"due": 0, "info": 1}
    alerts.sort(key=lambda al: order[al["severity"]])
    return alerts


# ---------------------------------------------------------------------------
# Cash flow (BRD 10.3) — from the confirmed transaction ledger
# ---------------------------------------------------------------------------

EXCLUDED_FROM_CASHFLOW = {"Internal Transfer"}


def _month_key(d):
    return (d.year, d.month)


def _month_label(year, month):
    return date(year, month, 1).strftime("%b %Y")


def last_n_months(n, today=None):
    """[(year, month)] oldest -> newest, ending at the current month."""
    today = today or date.today()
    out = []
    y, m = today.year, today.month
    for _ in range(n):
        out.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(out))


def monthly_cashflow(transactions, months=12, today=None):
    """Aggregate income vs spend per month.

    Returns rows [{label, income, expense, net}] oldest -> newest, always
    exactly `months` rows (zero-filled), internal transfers excluded.
    """
    buckets = defaultdict(lambda: {"income": 0.0, "expense": 0.0})
    for t in transactions:
        if t.category in EXCLUDED_FROM_CASHFLOW:
            continue
        b = buckets[_month_key(t.txn_date)]
        b["income"] += t.deposit or 0.0
        b["expense"] += t.withdrawal or 0.0

    rows = []
    for (y, m) in last_n_months(months, today):
        b = buckets.get((y, m), {"income": 0.0, "expense": 0.0})
        rows.append({
            "label": _month_label(y, m),
            "income": round(b["income"], 2),
            "expense": round(b["expense"], 2),
            "net": round(b["income"] - b["expense"], 2),
        })
    return rows


def category_spend(transactions, year, month):
    """Spend (withdrawals) per category for one month, sorted desc."""
    totals = defaultdict(float)
    for t in transactions:
        if t.category in EXCLUDED_FROM_CASHFLOW:
            continue
        if _month_key(t.txn_date) == (year, month) and (t.withdrawal or 0) > 0:
            totals[t.category or "Uncategorized"] += t.withdrawal
    return sorted(((c, round(v, 2)) for c, v in totals.items()), key=lambda x: -x[1])


def category_comparison(transactions, today=None):
    """This month's spend per category with month-on-month delta.

    Returns (rows, this_label, prev_label) where rows =
    [{category, this_month, prev_month, delta}] sorted by this month desc.
    """
    today = today or date.today()
    (py, pm), (cy, cm) = last_n_months(2, today)
    this_spend = dict(category_spend(transactions, cy, cm))
    prev_spend = dict(category_spend(transactions, py, pm))
    rows = []
    for cat in sorted(set(this_spend) | set(prev_spend), key=lambda c: -this_spend.get(c, 0.0)):
        cur, prev = this_spend.get(cat, 0.0), prev_spend.get(cat, 0.0)
        rows.append({"category": cat, "this_month": cur, "prev_month": prev,
                     "delta": round(cur - prev, 2)})
    return rows, _month_label(cy, cm), _month_label(py, pm)
