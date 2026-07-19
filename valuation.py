"""On-demand valuation refresh (runs at app startup and via the Refresh button).

- Mutual funds: AMFI publishes NAVs for every Indian scheme in one public
  text file (https://www.amfiindia.com/spages/NAVAll.txt). We cache it in
  instance/ and revalue every MF asset that has scheme_code + units.
- Equity: optional. If the `yfinance` package is installed and an asset has
  ticker + units, it's revalued from the latest close. Skipped silently
  otherwise (uncomment yfinance in requirements.txt to enable).
- After revaluing, a family-wide NetWorthSnapshot is recorded for today,
  replacing the old write-on-every-dashboard-view behaviour.

Weekly-use design: everything is a plain synchronous function; serve.py
runs `run_full_refresh` in a background thread at startup so the UI is
usable immediately, and the routes can also call it on demand.
"""
import os
import threading
import urllib.request
from datetime import datetime, date, timedelta

from models import db, Asset, Family, NetWorthSnapshot

AMFI_NAV_URL = "https://www.amfiindia.com/spages/NAVAll.txt"
NAV_CACHE_MAX_AGE_HOURS = 6

# Simple status board the UI can show; guarded by _lock.
_lock = threading.Lock()
last_run = {"at": None, "mf_updated": 0, "equity_updated": 0, "errors": []}


def _nav_cache_path(app):
    return os.path.join(app.instance_path, "NAVAll.txt")


def fetch_amfi_navs(app, force=False):
    """Return {scheme_code: (nav, nav_date_str)} from AMFI, using a cached
    copy of NAVAll.txt if it is fresh enough."""
    cache = _nav_cache_path(app)
    os.makedirs(app.instance_path, exist_ok=True)

    needs_download = force or not os.path.exists(cache)
    if not needs_download:
        age = datetime.now() - datetime.fromtimestamp(os.path.getmtime(cache))
        needs_download = age > timedelta(hours=NAV_CACHE_MAX_AGE_HOURS)

    if needs_download:
        req = urllib.request.Request(AMFI_NAV_URL, headers={"User-Agent": "FinFamily/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()
        if len(data) < 100_000:  # sanity: real file is ~2 MB
            raise ValueError("AMFI NAV download looks truncated; keeping previous cache.")
        with open(cache, "wb") as f:
            f.write(data)

    return parse_amfi_nav_file(open(cache, encoding="utf-8", errors="replace").read())


def parse_amfi_nav_file(text):
    """Parse AMFI NAVAll.txt content.

    Data lines are semicolon-separated:
    Scheme Code;ISIN Div Payout/Growth;ISIN Div Reinvestment;Scheme Name;NAV;Date
    Everything else (AMC headers, category headers, blanks) has no ';'-fields.
    """
    navs = {}
    for line in text.splitlines():
        parts = line.split(";")
        if len(parts) != 6:
            continue
        code, _isin1, _isin2, _name, nav, nav_date = [p.strip() for p in parts]
        if not code.isdigit():
            continue
        try:
            navs[code] = (float(nav), nav_date)
        except ValueError:
            continue  # NAV can be "N.A."
    return navs


def refresh_mf_valuations(app):
    """current_value = units x latest NAV for every MF asset with scheme_code."""
    navs = fetch_amfi_navs(app)
    updated = 0
    assets = Asset.query.filter(Asset.category == "MF", Asset.scheme_code.isnot(None),
                                Asset.units.isnot(None)).all()
    for a in assets:
        entry = navs.get(a.scheme_code.strip())
        if not entry:
            continue
        nav, _nav_date = entry
        a.current_value = round((a.units or 0.0) * nav, 2)
        a.last_valued_at = datetime.utcnow()
        updated += 1
    db.session.commit()
    return updated


def refresh_equity_valuations(app):
    """Optional: revalue EQUITY assets with ticker + units via yfinance."""
    try:
        import yfinance  # noqa: soft dependency
    except ImportError:
        return 0
    updated = 0
    assets = Asset.query.filter(Asset.category == "EQUITY", Asset.ticker.isnot(None),
                                Asset.units.isnot(None)).all()
    for a in assets:
        try:
            price = yfinance.Ticker(a.ticker.strip()).fast_info["last_price"]
        except Exception:
            continue
        if price:
            a.current_value = round((a.units or 0.0) * float(price), 2)
            a.last_valued_at = datetime.utcnow()
            updated += 1
    db.session.commit()
    return updated


def snapshot_all_families():
    """Record/refresh today's family-wide net-worth snapshot for every family.

    Unlike the old per-dashboard-view write, this includes ALL assets
    (private ones too — a snapshot is a family total, not a viewer's view).
    """
    today = date.today()
    for family in Family.query.all():
        assets = Asset.query.filter_by(family_id=family.id).all()
        total_assets = sum(a.current_value or 0.0 for a in assets
                           if not a.is_liability and a.category != "INSURANCE")
        total_liabilities = sum(a.current_value or 0.0 for a in assets if a.is_liability)
        snap = NetWorthSnapshot.query.filter_by(family_id=family.id, snapshot_date=today).first()
        if snap:
            snap.total_assets, snap.total_liabilities = total_assets, total_liabilities
            snap.net_worth = total_assets - total_liabilities
        else:
            db.session.add(NetWorthSnapshot(
                family_id=family.id, snapshot_date=today,
                total_assets=total_assets, total_liabilities=total_liabilities,
                net_worth=total_assets - total_liabilities,
            ))
    db.session.commit()


def run_full_refresh(app):
    """NAV + equity refresh, then snapshot. Safe to call from a thread."""
    with app.app_context():
        errors = []
        mf = eq = 0
        try:
            mf = refresh_mf_valuations(app)
        except Exception as exc:
            errors.append(f"MF NAV refresh failed: {exc}")
        try:
            eq = refresh_equity_valuations(app)
        except Exception as exc:
            errors.append(f"Equity refresh failed: {exc}")
        try:
            snapshot_all_families()
        except Exception as exc:
            errors.append(f"Snapshot failed: {exc}")
        try:
            # One-off/idempotent maintenance: hash pre-guard imports and
            # auto-discard pending duplicates of the same PDF.
            from import_service import backfill_hashes_and_discard_duplicates
            backfill_hashes_and_discard_duplicates()
        except Exception as exc:
            errors.append(f"Duplicate-import cleanup failed: {exc}")
        with _lock:
            last_run.update({"at": datetime.now(), "mf_updated": mf,
                             "equity_updated": eq, "errors": errors})
        return mf, eq, errors


def start_background_refresh(app):
    threading.Thread(target=run_full_refresh, args=(app,), daemon=True).start()
