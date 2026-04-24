# Genie Space SP Demo — Work Summary

Local demo at `/Users/abhilash.r/dbx-demo/genie-space-app-sp/` showing
**local login → SP token exchange → Genie MCP → UC row-level security**
with a live transparency pane + Architecture tab.

---

## Stack

- **Backend**: FastAPI (port **8001**) + vanilla JS frontend (no build step, served same-origin)
- **Workspace**: set via `DBX_HOST` in `.env`
- **SQL warehouse**: set via `DBX_WAREHOUSE_ID` in `.env`
- **Genie space**: set via `GENIE_SPACE_ID` in `.env` (title: *Dealership Analytics*)
- **Service principals** (2 dealerships):
  - `northstar` → sees `dealership_id = NS001` rows
  - `sunrise` → sees `dealership_id = SR001` rows

---

## Databricks data layer (already provisioned ✅)

Catalog `abhilash_r`, schema `genie_demo`, 4 Delta tables:
- `dealerships` (2 rows)
- `vehicles` (20 rows)
- `sales` (40 rows — 20 per dealership)
- `service_tickets` (15 rows)

RLS function `abhilash_r.genie_demo.dealership_rls(dealership_id STRING)` matches
`current_user()` against each SP's **applicationId** (UUID, not display name) and
returns only matching dealership rows. Applied to all 4 tables via
`ALTER TABLE … SET ROW FILTER`.

Verified: each SP sees exactly 20 of 40 sales rows.

---

## Backend (`backend/*.py`)

| File | Purpose |
|---|---|
| `main.py` | FastAPI app, routes, session middleware, SSE stream |
| `config.py` | `.env` loader, `Settings` dataclass |
| `users.py` | In-memory user store (alice/bob → northstar, carol/dave → sunrise, pw `demo123`) |
| `sp_mapping.py` | `sp_label → (client_id, secret, dealership, app_id)` |
| `databricks_auth.py` | OAuth M2M exchange (`/oidc/v1/token`), token cache (60s safety margin) |
| `genie_client.py` | Genie REST client — polling, result parser, **retry on 429/503** with exp backoff + jitter (honors `Retry-After`, max 5 retries) |
| `sql_client.py` | SQL API wrapper (verification pings) |
| `flow_events.py` | Per-session `asyncio.Queue` SSE bus, 1h idle GC |
| `dev_flags.py` | Per-session counter for simulated 429s |
| `run.py` | Launcher (`python -m backend.run`) |

### Endpoints

| Route | Purpose |
|---|---|
| `POST /api/login` / `POST /api/logout` / `GET /api/me` | Auth |
| `POST /api/chat` | user → SP → Genie → answer + SQL + rows |
| `GET /api/events/stream` | SSE flow events |
| `POST /api/dev/simulate-rate-limit` | Arm N synthetic 429s (orange pill) |
| `POST /api/dev/stress-genie` | Fire N concurrent real Genie POSTs (to trigger actual QPM 429s) |

### Flow event steps (rendered in transparency pane)

`login` • `sp_resolve` • `token_exchange` • `genie_call` • `genie_rate_limit` • `genie_retry` • `genie_sql` • `sql_execute` • `rls_applied` • `response` • `error`

---

## Frontend (`backend/static/`)

Single-page app, three files, no npm:
- `index.html` — login card, chat layout, arch tab, topbar controls
- `app.js` — tab switching, SSE subscription, Mermaid rendering, API calls
- `styles.css` — handwritten, Databricks-ish styling

### UI features

- **Login view**: seed-user table (alice/bob/carol/dave)
- **Chat view**: assistant bubbles render answer text + SQL `<details>` + result table (50-row cap)
- **Data Flow pane** (right column): color-coded step pills with collapsible JSON payloads
- **Architecture tab** (available from login too): Mermaid sequence diagram (full-width) + components flowchart + notes
- **Topbar dev controls**:
  - 🟠 **Simulate 429** — arm N fake 429s (no Genie calls)
  - 🔴 **Real 429 stress** — fire N concurrent real POSTs to trigger actual QPM

---

## Run (fresh terminal after laptop restart)

```bash
cd /Users/abhilash.r/dbx-demo/genie-space-app-sp

# If anything in .venv broke on restart:
# python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

.venv/bin/uvicorn backend.main:app --host 127.0.0.1 --port 8001 --reload
```

Open **http://127.0.0.1:8001** → log in as any of `alice` / `bob` / `carol` / `dave` (pw `demo123`).

---

## Demo script (for presenting)

1. **Architecture tab from login page** — walk the sequence diagram and
   components, explain the 5 hops (local → OIDC → Genie → UC → RLS).
2. **Log in as alice** → ask *"Show my top 3 sales by sale_price"* →
   point out the SQL, the result table, and the green `genie_call → 200`
   in the flow pane.
3. **Log out, log in as carol** → same question → different rows, same SQL.
   *"Identical SQL, different rows — that's UC enforcing RLS."*
4. **Arm Simulate 429 = 3** → ask anything → narrate the three orange
   retry cards with 1s → 2s → 4s backoffs.
5. **Fire Real 429 stress = 10** → if e2-demo's QPM allows, all succeed
   (no 429s); if not, real 429s appear. Emphasize: *"Same retry path, we
   don't care if the 429 was synthetic or real."*

---

## Key gotchas learned

1. **Genie `/api/2.0/genie/spaces` create requires an undocumented proto blob** — manual UI creation + post-create grant script (`scripts/create_genie_space.py`).
2. **`current_user()` inside UC for SPs returns the applicationId (UUID)**, not display name — the row filter matches on UUIDs from env vars.
3. **Genie query-result format**: `statement_response.result.data_typed_array[].values[].{str}` (PROTOBUF_ARRAY), not `data_array`. `_parse_query_result` in `genie_client.py` handles both shapes.
4. **`uvicorn --reload` does NOT pick up newly created Python modules** (e.g. when `backend/dev_flags.py` was added). A full Ctrl-C + restart is required.
5. **Genie QPM is per-space and shared across all SPs** — ~5/min default on standard plans, higher on e2-demo. Polling (GET) is free; POST start-conversation/messages counts.
6. **Databricks shades (relocates) Kafka client classes** under `kafkashaded.*` (general gotcha from memory; not directly used here).

---

## File tree

```
genie-space-app-sp/
├── .env                          # gitignored, SP creds + workspace cfg
├── .env.example
├── .gitignore
├── README.md
├── API_CONTRACT.md               # source of truth for backend↔frontend
├── SUMMARY.md                    # this file
├── requirements.txt
├── scripts/
│   ├── setup_databricks.py       # schema + tables + RLS + grants (done)
│   ├── create_genie_space.py     # post-UI-create SP grants
│   ├── teardown_databricks.py    # DROP SCHEMA CASCADE
│   └── debug_genie_result.py     # raw Genie response inspector
└── backend/
    ├── __init__.py / config.py / users.py / sp_mapping.py
    ├── databricks_auth.py / genie_client.py / sql_client.py
    ├── flow_events.py / dev_flags.py / main.py / run.py
    └── static/
        ├── index.html / app.js / styles.css
```

---

## Current state at save time

- ✅ Databricks data + RLS provisioned and verified
- ✅ Genie space created + grants applied
- ✅ Backend runs, all 7 routes working
- ✅ Frontend: login, chat, transparency pane, Architecture tab all functional
- ✅ Result table rendering (Genie `data_typed_array` parser fixed)
- ✅ Rate-limit retry logic with SSE visibility
- ✅ Simulate 429 toggle (fake) + Real 429 stress (live Genie hits)
- ⚠️ Uvicorn needs a fresh restart after laptop reboot (no stale background processes by then — ignore earlier notes about port 8001 being busy)

## Resume checklist

- [ ] `source .env` isn't needed; backend loads via python-dotenv
- [ ] Start uvicorn (see Run section above)
- [ ] Hard-refresh browser (⌘⇧R)
- [ ] Verify `/api/dev/simulate-rate-limit` and `/api/dev/stress-genie` exist (both in topbar)
- [ ] If any module changes, full Ctrl-C + restart uvicorn (reload doesn't always catch new files)
