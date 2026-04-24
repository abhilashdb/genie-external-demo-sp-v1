# Genie Space SP Demo — Dealership Analytics

Local demo of **local login → user→SP mapping → OAuth M2M token exchange → Genie (MCP) conversational analytics → RLS-filtered results**, with a live transparency pane showing every step.

## Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│  Browser (http://127.0.0.1:8000)                                   │
│  ┌─────────────────┐       ┌────────────────────────────────────┐  │
│  │ Login / Chat UI │──────►│ Data Flow pane (SSE subscriber)    │  │
│  └─────────┬───────┘       └────────────────────────────────────┘  │
└────────────┼───────────────────────────────────────────────────────┘
             │ cookie session
             ▼
┌────────────────────────────────────────────────────────────────────┐
│  FastAPI backend  (uvicorn :8000)                                  │
│   • in-memory user store (alice/bob → northstar, carol/dave → sunrise)
│   • session cookie (itsdangerous-signed)                           │
│   • user → SP mapping                                              │
│   • OAuth M2M token cache  (SP client_id/secret → bearer token)    │
│   • Genie REST client (MCP-equivalent conversation API)            │
│   • SQL API client (optional verification)                         │
│   • SSE flow event bus → transparency pane                         │
└────────────┬───────────────────────────────────────────────────────┘
             │ Bearer <SP token>
             ▼
┌────────────────────────────────────────────────────────────────────┐
│  Databricks workspace                                              │
│   • /oidc/v1/token  (M2M token exchange)                           │
│   • /api/2.0/genie/spaces/…  (conversational analytics)            │
│   • /api/2.0/sql/statements  (SQL API)                             │
│   • UC catalog abhilash_r.genie_demo                               │
│     ├─ dealerships, vehicles, sales, service_tickets               │
│     └─ dealership_rls() row filter → SP-specific dealership view   │
└────────────────────────────────────────────────────────────────────┘
```

## Prereqs

- Python 3.10+
- Databricks CLI authenticated as a workspace admin (`databricks auth login --profile e2-demo`)
- Two OAuth service principals already created in the workspace, with client secrets captured
- `.env` populated (see `.env.example`)

## One-time setup

```bash
cd /Users/abhilash.r/dbx-demo/genie-space-app-sp

# 1. Python env
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. Copy & fill env
cp .env.example .env
# → paste SP client_ids / secrets, verify workspace host + warehouse ID

# 3. Provision schema, tables, RLS, grants
.venv/bin/python scripts/setup_databricks.py
# → creates abhilash_r.genie_demo with 2 dealerships / 20 vehicles / 40 sales / 15 service_tickets
# → creates UC row-filter fn dealership_rls() and applies to all tables
# → grants both SPs USE CATALOG / USE SCHEMA / SELECT / EXECUTE
# → verifies each SP sees only their dealership's rows

# 4. Create the Genie space (UI — see below)
```

### Creating the Genie space

The `/api/2.0/genie/spaces` create endpoint requires an undocumented proto
blob, so this step is manual:

1. Open `<your-workspace>/genie`
2. **Create** → **New Genie Space**
3. Title: `Dealership Analytics`
4. Warehouse: the one matching `DBX_WAREHOUSE_ID` in `.env`
5. Add tables from `abhilash_r.genie_demo`:
   - `dealerships`, `vehicles`, `sales`, `service_tickets`
6. Save. The URL becomes `/genie/rooms/<UUID>` — copy that UUID.
7. Apply SP grants and write the ID into `.env`:
   ```bash
   GENIE_SPACE_ID=<uuid> .venv/bin/python scripts/create_genie_space.py
   ```

## Running

```bash
.venv/bin/python -m backend.run
# or: .venv/bin/uvicorn backend.main:app --reload --port 8000
```

Open http://127.0.0.1:8000 in your browser.

## Test users

| Username | Password | Dealership | Role | SP |
|---|---|---|---|---|
| alice | demo123 | North Star Motors | manager | northstar |
| bob | demo123 | North Star Motors | analyst | northstar |
| carol | demo123 | Sunrise Auto Group | manager | sunrise |
| dave | demo123 | Sunrise Auto Group | analyst | sunrise |

**RLS check:** Log in as `alice` and ask "how many sales records do I have?" → 20.
Log in as `carol` and ask the same → 20 — but different rows. Neither can see the
other's data, because the Genie SQL runs under the SP's token and UC applies
`dealership_rls()` based on `current_user()`.

## What the transparency pane shows

Each message round-trip emits a series of SSE events rendered in reverse-chronological order:

1. **login** — user authenticated locally
2. **sp_resolve** — local user mapped to service principal (client_id redacted to 8 chars)
3. **token_exchange** — OAuth M2M call to `/oidc/v1/token`, token preview + expires_in
4. **genie_call** — each underlying REST hop (start-conversation / messages / query-result)
5. **genie_sql** — SQL produced by Genie
6. **sql_execute** — (optional) SQL API execution as the SP for the verification ping
7. **rls_applied** — advisory note about which row-filter applies
8. **response** — final answer back to the user

Each card has a color-coded pill, timestamp, title, detail, and a collapsible JSON payload.

## File tree

```
genie-space-app-sp/
├── .env / .env.example / .gitignore
├── README.md
├── API_CONTRACT.md              # backend ↔ frontend route contract
├── requirements.txt
├── scripts/
│   ├── setup_databricks.py      # schema, data, RLS, grants
│   ├── create_genie_space.py    # post-UI grants + .env population
│   └── teardown_databricks.py   # DROP SCHEMA CASCADE
└── backend/
    ├── __init__.py
    ├── config.py                # .env loader
    ├── users.py                 # in-memory user store
    ├── sp_mapping.py            # user.sp_label → (client_id, secret, dealership, app_id)
    ├── databricks_auth.py       # M2M token exchange + cache
    ├── genie_client.py          # Genie REST client
    ├── sql_client.py            # SQL API client
    ├── flow_events.py           # SSE event bus (per-session queue)
    ├── main.py                  # FastAPI app
    ├── run.py                   # launcher
    └── static/
        ├── index.html
        ├── app.js
        └── styles.css
```

## Teardown

```bash
.venv/bin/python scripts/teardown_databricks.py
# drops abhilash_r.genie_demo CASCADE (safe if schema doesn't exist)
```

The Genie space itself must be deleted from the UI.

## Known gotchas

- **`current_user()` returns the SP's applicationId (UUID)**, not display name. The row filter function matches on UUIDs pulled from env vars.
- **Genie REST shapes vary slightly across workspace versions.** `genie_client.py` handles two common variants for `start-conversation` and query-result responses. If your workspace returns different keys, the two spots to tweak are `extract_sql_and_text` and `_parse_query_result`.
- **Service principal secrets are shown only once.** If you lose one, delete the SP's OAuth secret and regenerate (`databricks service-principal-secrets-proxy create <app-id>`).
- **Token cache is in-process** — restarting the backend re-exchanges. Fine for a demo; for production use a shared cache.
