# FinFamily — Personal & Family Finance Tracker (India)

A working Phase-1 implementation of the FinFamily Business Requirements Document: a
family-level net worth and financial-planning web application covering every Indian
asset class named in the BRD.

## What's implemented

| BRD Section | Feature | Where |
|---|---|---|
| 6.1 / 7.1 | Family workspace, invite members, roles (Owner/Contributor/Viewer), managed profiles for dependents, per-asset Private/Shared visibility | `/family` |
| 6.2 / 7.2–7.7 | Manual tracking of Bank, FD, Mutual Funds, Direct Equity/F&O, PPF, NPS (with E/C/G/A split), EPF, Gold, Real Estate, Loans, Insurance | `/assets` |
| 6.3 / 10.1 | Consolidated + member-wise net worth, asset-allocation donut chart, net-worth trend line, liquidity view | `/dashboard` |
| 6.5 / 10.2 | Goal-based planning: link assets to a goal, progress bar, shortfall, suggested top-up SIP | `/goals` |
| 6.7 / 10.5 | Tax-reference view: unrealized gains, estimated interest income (informational only) | `/tax-reference` |
| 6.10 / NFR-SEC-06 | Default-private asset visibility, per-asset sharing toggle | throughout |

**Not implemented in this build** (out of scope for a demo/MVP without the paid
registrations and licensing the BRD itself calls out — RBI Account Aggregator FIU
status, CAS/CRA statement parsing, and broker API integration): live data pulls
from banks/AMCs/brokers/CRA. All holdings are entered manually, exactly as the BRD's
own "Fallback" column specifies for Phase 1. The data model (`models.py`) is
structured so those integrations can be added later without a schema rework —
each asset already carries the fields (folio/PRAN-style institution reference,
NAV-style current value, cost basis) those feeds would populate.

## Tech stack

- **Backend:** Python 3 / Flask, Flask-Login (auth), Flask-SQLAlchemy (ORM)
- **Database:** SQLite by default (file-based, zero setup); swap to PostgreSQL by
  setting `DATABASE_URL` — see `.env.example`
- **Frontend:** Server-rendered Jinja2 templates, Chart.js for the dashboard charts,
  no build step required
- **WSGI server for production:** Gunicorn (see `DEPLOY_GCP.md`)

## Run locally

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # edit SECRET_KEY at minimum
export $(cat .env | xargs)    # or use python-dotenv, already a dependency
python3 app.py
```

Visit `http://localhost:5000`, register the first user (becomes the family's
primary user / Owner), and start adding accounts under **Assets & Liabilities**.

## Deploying

See `DEPLOY_GCP.md` for a full step-by-step guide to deploying this on a Google
Cloud Platform Compute Engine VM (Ubuntu) with Gunicorn, Nginx, systemd and HTTPS.

## Project structure

```
finfamily/
├── app.py                # routes / application factory
├── models.py             # SQLAlchemy models (Family, User, Asset, Goal, NetWorthSnapshot)
├── config.py             # env-driven configuration
├── wsgi.py               # gunicorn entrypoint
├── requirements.txt
├── .env.example
├── templates/            # Jinja2 templates
└── static/css, static/js
```
