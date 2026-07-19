# FinFamily — Personal & Family Finance Tracker (India)

A family-level net worth and financial-planning web application covering Indian
asset classes: Bank, FD, Mutual Funds, Equity, PPF, NPS, EPF, Gold, Real Estate,
Loans, Insurance. Designed for local, weekly use — start it, it refreshes itself,
review, close it.

## What's implemented

| Feature | Where |
|---|---|
| Family workspace, roles (Owner/Contributor/Viewer), managed profiles, per-asset Private/Shared visibility | `/family` |
| Manual tracking of every asset class, with liabilities netted into net worth | `/assets` |
| Consolidated + member-wise net worth, allocation chart, trend line, liquidity view | `/dashboard` |
| Goal-based planning: linked assets, progress, shortfall, suggested top-up SIP | `/goals` |
| Tax-reference view: unrealized gains, estimated interest income | `/tax-reference` |
| **Bank statement PDF import** (OCR, HDFC parser + generic fallback, review-before-confirm) | `/import` |
| **Gmail auto-ingestion**: scans your inbox (read-only IMAP) for bank e-statements and CAS emails | `/import` → Check Gmail |
| **CAS import** (CAMS/KFintech consolidated MF statement): folios applied to MF assets automatically | via Gmail check |
| **NAV auto-valuation**: MF values refreshed from AMFI's daily NAV file on every app start (units × NAV) | startup + Refresh button |

## Run locally (Windows — the intended setup)

1. Install [Python 3.11+](https://www.python.org/downloads/) (tick "Add to PATH").
2. Double-click **`start_finfamily.bat`**. First run creates the venv, installs
   dependencies, sets up the database, and opens your browser at
   `http://127.0.0.1:8000`. Close the window to stop.
3. Edit `.env` (created from `.env.example` on first run):
   - `SECRET_KEY` — any long random string
   - `GMAIL_ADDRESS` + `GMAIL_APP_PASSWORD` — for statement auto-ingestion
     (Google Account → Security → 2-Step Verification → App passwords)
   - `CAS_PASSWORD` — the password on your CAMS/KFintech CAS PDFs (usually PAN)

**For bank-statement PDF import only** (CAS + NAV refresh work without these):

- [Tesseract OCR for Windows](https://github.com/UB-Mannheim/tesseract/wiki)
- [Poppler for Windows](https://github.com/oschwartz10612/poppler-windows/releases)
  — unzip and add its `Library\bin` to PATH

The start script warns if either is missing.

### Linux / manual run

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # edit at minimum SECRET_KEY
export FLASK_APP=app.py
flask db upgrade                # creates/migrates the database
python3 serve.py
```

## Weekly workflow

1. Double-click `start_finfamily.bat`.
2. On startup the app automatically: pulls the latest AMFI NAV file (cached 6h),
   revalues every MF holding with a scheme code + units, and records today's
   net-worth snapshot.
3. On the Import page, click **Check Gmail now**: new CAS emails update your
   mutual funds directly; new bank e-statements are OCR'd and staged for review.
4. Review anything pending, check the dashboard, close the window.

## Database migrations

Schema changes are managed with Flask-Migrate (Alembic). The start script runs
`flask db upgrade` automatically. After changing `models.py`:

```bash
flask db migrate -m "describe the change"
flask db upgrade
```

## Tests

```bash
pip install pytest
pytest tests/
```

Covers net-worth math, transaction categorization, the HDFC parser (against
synthetic OCR text), AMFI NAV parsing, and CAS-to-asset matching. GitHub Actions
runs the suite on every push (`.github/workflows/ci.yml`).

## Tech stack

- **Backend:** Python 3 / Flask, Flask-Login, Flask-SQLAlchemy, Flask-Migrate
- **Database:** SQLite (file-based, zero setup); PostgreSQL via `DATABASE_URL`
- **Server:** waitress (cross-platform; gunicorn optional for Linux)
- **Frontend:** server-rendered Jinja2 + Chart.js, no build step
- **Statement parsing:** Tesseract OCR + pdf2image (bank PDFs), casparser (CAS)
- **Valuation:** AMFI NAVAll.txt (free, public); optional yfinance for equity tickers

## Project structure

```
FinFamily/
├── app.py                  # routes / application factory
├── models.py               # SQLAlchemy models
├── config.py               # env-driven configuration
├── valuation.py            # AMFI NAV refresh + net-worth snapshots
├── gmail_ingest.py         # IMAP statement fetcher
├── import_service.py       # shared bank-PDF ingestion (web upload + Gmail)
├── serve.py                # local server entrypoint (waitress)
├── start_finfamily.bat     # Windows one-click start
├── wsgi.py                 # gunicorn entrypoint (Linux deploys)
├── migrations/             # Alembic database migrations
├── statement_import/       # OCR + bank parser plugins + CAS import
├── tests/                  # pytest suite
├── templates/  static/     # UI
└── docs/                   # BRD, deployment guides, automation roadmap
```
