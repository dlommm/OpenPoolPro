# Coastline Pools Planner

Self-hosted pool-route app. Phase 0: customers, properties, pools, routes,
visits with chemistry costs, jobs, monthly QuickBooks Online CSV export.

## Run

    cp .env.example .env   # set APP_PASSWORD and SECRET_KEY
    docker compose up -d
    # open http://<host>:8000 — on iPhone: Share → Add to Home Screen

All data lives in ./data (SQLite DB, photos, nightly backups). Back up that
folder.

## Develop

    python3.12 -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    python scripts/build_css.py
    APP_PASSWORD=dev uvicorn app.main:create_app --factory --reload
    pytest
