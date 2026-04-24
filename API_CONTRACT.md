# Backend ↔ Frontend API contract

Both the backend agent and frontend agent must implement this contract exactly. The frontend depends on these routes and payloads; the backend must expose them.

Base URL: the backend serves everything off `http://127.0.0.1:8000`. The frontend is served as static files from `backend/static/` at `/` (so fetches can use relative paths like `/api/login`).

## Auth

### `POST /api/login`
Body:
```json
{"username": "alice", "password": "demo123"}
```
Success (200) — sets HttpOnly session cookie `session`:
```json
{"username": "alice", "dealership": "North Star Motors", "role": "manager", "sp_label": "northstar"}
```
Failure (401):
```json
{"detail": "invalid credentials"}
```

### `POST /api/logout`
Clears session cookie. Returns `{"ok": true}`.

### `GET /api/me`
Returns the current user (same shape as login success), or 401 if not authed.

## Chat

### `POST /api/chat`
Body:
```json
{"message": "what were my top selling vehicles last quarter?", "conversation_id": "optional-existing-id"}
```
Success (200):
```json
{
  "conversation_id": "genie-conv-uuid",
  "message_id": "genie-msg-uuid",
  "status": "COMPLETED",
  "answer_text": "Top 3 vehicles by units sold: ...",
  "sql": "SELECT vehicle_model, SUM(units_sold) FROM sales WHERE ...",
  "rows": [{"col": "val"}, ...],
  "columns": [{"name": "vehicle_model", "type": "STRING"}, ...]
}
```
- `rows` / `columns` may be `null` if Genie returned text only.
- Errors return 4xx with `{"detail": "..."}`.

## Flow events (transparency pane)

### `GET /api/events/stream`
Server-Sent Events stream scoped to the current session. Each event:
```
event: flow
data: {"ts": "2026-04-22T12:34:56Z", "step": "login", "status": "ok", "title": "User alice logged in", "detail": "...", "payload": {}}
```

**Step names the frontend must render in order:**

| step | when | payload keys |
|---|---|---|
| `login` | after `/api/login` success | `username`, `dealership`, `role` |
| `sp_resolve` | user → SP mapping | `sp_label`, `sp_client_id` (redacted to first 8 chars), `sp_display_name` |
| `token_exchange` | OAuth M2M call to `/oidc/v1/token` | `endpoint`, `scope`, `expires_in`, `token_preview` (first 12 chars + "…") |
| `genie_call` | Genie REST call | `endpoint`, `method`, `http_status`, `retries` |
| `genie_rate_limit` | Genie returned 429/503; backing off | `endpoint`, `method`, `http_status`, `attempt`, `max_retries`, `delay_seconds`, `retry_after_header` |
| `genie_retry` | Issuing the retry after backoff | `endpoint`, `method`, `attempt` |
| `genie_sql` | Genie-produced SQL, before execution | `sql` |
| `sql_execute` | SQL API execution as the SP | `warehouse_id`, `row_count`, `elapsed_ms` |
| `rls_applied` | Row-filter note (advisory) | `dealership`, `filter_fn` |
| `response` | final text returned to user | (no payload needed) |
| `error` | anything failing | `where`, `message` |

Status values: `ok` | `pending` | `error`.

## User store (seeded in backend/users.py)

| username | password | dealership | role | maps to SP label |
|---|---|---|---|---|
| alice | demo123 | North Star Motors | manager | northstar |
| bob | demo123 | North Star Motors | analyst | northstar |
| carol | demo123 | Sunrise Auto Group | manager | sunrise |
| dave | demo123 | Sunrise Auto Group | analyst | sunrise |

## Environment (backend loads via python-dotenv at startup)

Relevant keys (already in `.env`):
- `DBX_HOST`, `DBX_WAREHOUSE_ID`, `DBX_CATALOG`, `DBX_SCHEMA`
- `GENIE_SPACE_ID` — populated by Agent A after run
- `SP_NORTHSTAR_CLIENT_ID`, `SP_NORTHSTAR_SECRET`, `SP_NORTHSTAR_DEALERSHIP`
- `SP_SUNRISE_CLIENT_ID`, `SP_SUNRISE_SECRET`, `SP_SUNRISE_DEALERSHIP`
- `APP_SESSION_SECRET`
- `BACKEND_HOST`, `BACKEND_PORT`

## Genie REST endpoints the backend will hit (SP-auth'd)

- `POST /api/2.0/genie/spaces/{space_id}/start-conversation` body `{"content": "..."}`
- `POST /api/2.0/genie/spaces/{space_id}/conversations/{conv_id}/messages` body `{"content": "..."}`
- `GET /api/2.0/genie/spaces/{space_id}/conversations/{conv_id}/messages/{msg_id}` (poll until `status=COMPLETED`)
- `GET /api/2.0/genie/spaces/{space_id}/conversations/{conv_id}/messages/{msg_id}/query-result` for SQL result

## SQL API (for verification / flow visibility)

- `POST /api/2.0/sql/statements` with `{"warehouse_id": "...", "statement": "...", "wait_timeout": "30s"}`

Both backend and frontend may read this file — keep it in sync if anything changes.
