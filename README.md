# Coastline Pools Planner

Self-hosted pool-route app. Phase 0: customers, properties, pools, routes,
visits with chemistry costs and dose suggestions, jobs, analytics with income breakdowns and rate management, admin console with
dashboard and global search, company branding, branded PDF invoices with month generation and batch zip, monthly QuickBooks
Online CSV export.

## Run

    cp .env.example .env   # set APP_PASSWORD and SECRET_KEY
    docker compose up -d
    # open http://<host>:8000 — first login is owner / your APP_PASSWORD
    # manage crew under Settings → Manage crew
    # change APP_PASSWORD in .env + restart to reset the owner login (recovery)
    # on iPhone: Share → Add to Home Screen

All data lives in ./data (SQLite DB, photos, nightly backups). Back up that
folder.

## Develop

    python3.12 -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    python scripts/build_css.py
    APP_PASSWORD=dev uvicorn app.main:create_app --factory --reload
    pytest

## Visual smoke check

Proves every screen renders with the design system applied (catches stale
service-worker CSS):

    APP_URL=http://localhost:8000 APP_PASSWORD=... python scripts/visual_check.py

Requires `pip install playwright && playwright install chromium`.
