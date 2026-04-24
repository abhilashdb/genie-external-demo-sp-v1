"""FastAPI app for the Genie Space SP demo.

Endpoints (see API_CONTRACT.md):
  POST /api/login        — authenticate + issue session cookie
  POST /api/logout       — clear cookie
  GET  /api/me           — return current user or 401
  POST /api/chat         — user -> SP -> Genie conversation -> result
  GET  /api/events/stream — SSE transparency stream for the current session

Static frontend is served from backend/static/ at `/`.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, Field

from . import databricks_auth, db, dev_flags, flow_events, sql_client
from .config import settings
from .genie_client import GenieClient, GenieError, extract_sql_and_text, normalize_history
from .users import User, authenticate, get_user

log = logging.getLogger("genie_sp_demo")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# --------------------------------------------------------------------
# App + static mount
# --------------------------------------------------------------------

app = FastAPI(title="Genie SP Demo", version="0.1.0")

_STATIC_DIR = Path(__file__).resolve().parent / "static"
if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

# --------------------------------------------------------------------
# Session cookie helpers (itsdangerous signer, 1h max-age)
# --------------------------------------------------------------------

_SESSION_COOKIE = "session"
_SESSION_MAX_AGE = 60 * 60  # 1 hour
_signer = URLSafeTimedSerializer(
    settings.app_session_secret, salt="genie-sp-demo-session-v1"
)


def _issue_session_cookie(
    response: Response, username: str, session_id: str
) -> None:
    token = _signer.dumps({"username": username, "session_id": session_id})
    response.set_cookie(
        key=_SESSION_COOKIE,
        value=token,
        max_age=_SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=False,  # local HTTP
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(_SESSION_COOKIE, path="/")


def _read_session(request: Request) -> Optional[Dict[str, str]]:
    raw = request.cookies.get(_SESSION_COOKIE)
    if not raw:
        return None
    try:
        data = _signer.loads(raw, max_age=_SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict):
        return None
    if "username" not in data or "session_id" not in data:
        return None
    return data


class _AuthCtx:
    def __init__(self, user: User, session_id: str):
        self.user = user
        self.session_id = session_id


def get_current_ctx(request: Request) -> _AuthCtx:
    sess = _read_session(request)
    if sess is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    user = get_user(sess["username"])
    if user is None:
        raise HTTPException(status_code=401, detail="unknown user")
    return _AuthCtx(user=user, session_id=sess["session_id"])


def _user_public(user: User) -> Dict[str, str]:
    return {
        "username": user.username,
        "dealership": user.dealership,
        "role": user.role,
        "sp_label": user.sp_label,
    }


# --------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1)
    conversation_id: Optional[str] = None


class SimulateRateLimitRequest(BaseModel):
    count: int = Field(..., ge=0, le=10)
    status: int = Field(default=429)


class StressGenieRequest(BaseModel):
    count: int = Field(..., ge=1, le=15)
    question: str = Field(default="count sales")


# --------------------------------------------------------------------
# Root + health
# --------------------------------------------------------------------


@app.get("/")
async def root() -> FileResponse:
    index = _STATIC_DIR / "index.html"
    if not index.exists():
        return JSONResponse(
            {"ok": True, "detail": "backend running; frontend not yet built"},
            status_code=200,
        )
    return FileResponse(str(index))


@app.get("/api/health")
async def health() -> Dict[str, Any]:
    return {
        "ok": True,
        "genie_space_configured": bool(settings.genie_space_id),
        "dbx_host": settings.dbx_host,
    }


# --------------------------------------------------------------------
# Auth routes
# --------------------------------------------------------------------


@app.post("/api/login")
async def login(body: LoginRequest) -> Response:
    user = authenticate(body.username, body.password)
    if user is None:
        raise HTTPException(status_code=401, detail="invalid credentials")

    session_id = uuid.uuid4().hex
    payload = _user_public(user)
    response = JSONResponse(payload)
    _issue_session_cookie(response, username=user.username, session_id=session_id)

    # Publish transparency events (best-effort).
    await flow_events.publish(
        session_id,
        step="login",
        status="ok",
        title=f"User {user.username} logged in",
        detail=f"Role: {user.role} @ {user.dealership}",
        payload={
            "username": user.username,
            "dealership": user.dealership,
            "role": user.role,
        },
    )
    redacted_id = (
        (
            settings.sp_northstar_client_id
            if user.sp_label == "northstar"
            else settings.sp_sunrise_client_id
        )[:8]
        + "…"
    )
    await flow_events.publish(
        session_id,
        step="sp_resolve",
        status="ok",
        title=f"Resolved service principal for {user.dealership}",
        detail=f"sp_label={user.sp_label}",
        payload={
            "sp_label": user.sp_label,
            "sp_client_id": redacted_id,
            "sp_display_name": user.dealership,
        },
    )
    return response


@app.post("/api/logout")
async def logout(request: Request) -> Response:
    sess = _read_session(request)
    if sess and sess.get("session_id"):
        dev_flags.clear(sess["session_id"])
    response = JSONResponse({"ok": True})
    _clear_session_cookie(response)
    return response


@app.get("/api/me")
async def me(ctx: _AuthCtx = Depends(get_current_ctx)) -> Dict[str, str]:
    return _user_public(ctx.user)


# --------------------------------------------------------------------
# Dev / simulation endpoints
# --------------------------------------------------------------------


@app.post("/api/dev/simulate-rate-limit")
async def simulate_rate_limit(
    body: SimulateRateLimitRequest,
    ctx: _AuthCtx = Depends(get_current_ctx),
) -> Dict[str, Any]:
    """Arm N synthetic rate-limit responses for this session.

    The next N Genie HTTP attempts will return a synthetic `body.status`
    response (default 429), exercising the real retry+backoff code path.
    The transparency pane shows each retry just like a real rate limit.
    """
    new_count = dev_flags.arm_rate_limit(ctx.session_id, body.count, body.status)
    await flow_events.publish(
        ctx.session_id,
        step="genie_rate_limit",
        status="pending",
        title=f"Dev toggle armed: next {new_count} Genie attempts will be simulated {body.status}",
        detail="Ask a question to trigger the retry flow.",
        payload={"armed": new_count, "status": body.status, "simulated_setup": True},
    )
    return {"armed": new_count, "status": body.status}


@app.get("/api/dev/simulate-rate-limit")
async def simulate_rate_limit_peek(
    ctx: _AuthCtx = Depends(get_current_ctx),
) -> Dict[str, int]:
    return {"remaining": dev_flags.peek_rate_limit(ctx.session_id)}


@app.post("/api/dev/stress-genie")
async def stress_genie(
    body: StressGenieRequest,
    ctx: _AuthCtx = Depends(get_current_ctx),
) -> Dict[str, Any]:
    """Fire N concurrent REAL Genie start-conversation POSTs to exhaust QPM.

    This is NOT a simulation — each call hits Databricks for real with the
    session's SP token. The concurrency is intentional so we blow past the
    5 QPM bucket and surface genuine 429s via the same retry/backoff code
    path used by regular chat.
    """
    import asyncio  # local import to avoid polluting module namespace

    from .genie_client import GenieClient, GenieError

    if not settings.genie_space_id:
        raise HTTPException(status_code=503, detail="GENIE_SPACE_ID not set")

    try:
        sp_token = await databricks_auth.get_sp_token(ctx.user.sp_label, ctx.session_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"token_exchange failed: {e}")

    await flow_events.publish(
        ctx.session_id,
        step="genie_rate_limit",
        status="pending",
        title=f"Stress test: firing {body.count} concurrent Genie POSTs",
        detail="If real QPM is exceeded, the retry loop will surface 429s from Databricks.",
        payload={"count": body.count, "stress_test": True},
    )

    async def fire(i: int) -> Dict[str, Any]:
        client = GenieClient(
            space_id=settings.genie_space_id,
            sp_token=sp_token,
            session_id=ctx.session_id,
        )
        try:
            conv_id, msg_id = await client.start_conversation(
                f"{body.question} (stress #{i + 1})"
            )
            return {"i": i + 1, "ok": True, "conv_id": conv_id, "msg_id": msg_id}
        except GenieError as e:
            return {"i": i + 1, "ok": False, "error": str(e)[:300]}
        except Exception as e:  # noqa: BLE001
            return {"i": i + 1, "ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
        finally:
            await client.close()

    results = await asyncio.gather(*[fire(i) for i in range(body.count)])
    ok_count = sum(1 for r in results if r.get("ok"))
    fail_count = body.count - ok_count

    await flow_events.publish(
        ctx.session_id,
        step="response",
        status="ok" if fail_count == 0 else "error",
        title=f"Stress test done: {ok_count} ok / {fail_count} failed",
        detail="Check the genie_call / genie_rate_limit events above for the retry pattern.",
        payload={"ok": ok_count, "failed": fail_count, "results": results},
    )
    return {"count": body.count, "ok": ok_count, "failed": fail_count, "results": results}


# --------------------------------------------------------------------
# SSE stream
# --------------------------------------------------------------------


@app.get("/api/events/stream")
async def events_stream(ctx: _AuthCtx = Depends(get_current_ctx)) -> StreamingResponse:
    generator = flow_events.subscribe(ctx.session_id)
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# --------------------------------------------------------------------
# Chat
# --------------------------------------------------------------------


@app.post("/api/chat")
async def chat(
    body: ChatRequest, ctx: _AuthCtx = Depends(get_current_ctx)
) -> Dict[str, Any]:
    user = ctx.user
    session_id = ctx.session_id

    if not settings.genie_space_id:
        raise HTTPException(
            status_code=503,
            detail="GENIE_SPACE_ID is not configured; cannot route to Genie.",
        )

    # 1) Exchange credentials for the SP's bearer token.
    try:
        sp_token = await databricks_auth.get_sp_token(user.sp_label, session_id)
    except Exception as e:
        await flow_events.publish(
            session_id,
            step="error",
            status="error",
            title="Failed to obtain SP token",
            detail=str(e),
            payload={"where": "token_exchange", "message": str(e)},
        )
        raise HTTPException(status_code=502, detail=f"token_exchange failed: {e}")

    # 2) Genie conversation (start or continue).
    client = GenieClient(
        space_id=settings.genie_space_id,
        sp_token=sp_token,
        session_id=session_id,
    )
    try:
        if body.conversation_id:
            conv_id = body.conversation_id
            msg_id = await client.send_message(conv_id, body.message)
        else:
            conv_id, msg_id = await client.start_conversation(body.message)

        # Persist pointer so the user can revisit this thread later.
        db.upsert_conversation(
            local_user=user.username,
            sp_label=user.sp_label,
            genie_conv_id=conv_id,
            first_message=body.message,
        )

        # 3) Poll to completion.
        message_json = await client.poll_message(conv_id, msg_id)
        status = (message_json.get("status") or "COMPLETED").upper()

        sql, answer_text = extract_sql_and_text(message_json)
        rows = None
        columns = None

        if sql:
            await client.publish_sql(sql)
            # Fetch the tabular result.
            try:
                qr = await client.get_query_result(conv_id, msg_id)
                columns = qr.get("columns") or None
                rows = qr.get("rows") or None
            except GenieError as e:
                await flow_events.publish(
                    session_id,
                    step="error",
                    status="error",
                    title="Failed to fetch Genie query result",
                    detail=str(e),
                    payload={"where": "genie_query_result", "message": str(e)},
                )

        # 4) Advisory RLS note.
        await flow_events.publish(
            session_id,
            step="rls_applied",
            status="ok",
            title=f"Row-level filter applied for {user.dealership}",
            detail="Advisory: the SP identity enforces RLS at the table/view level.",
            payload={
                "dealership": user.dealership,
                "filter_fn": "sales.dealership = current_user_dealership()",
            },
        )

        # 5) Final response event.
        preview = (answer_text or "")[:180]
        await flow_events.publish(
            session_id,
            step="response",
            status="ok",
            title="Response ready",
            detail=preview + ("…" if answer_text and len(answer_text) > 180 else ""),
            payload={},
        )

        return {
            "conversation_id": conv_id,
            "message_id": msg_id,
            "status": status,
            "answer_text": answer_text,
            "sql": sql,
            "rows": rows,
            "columns": columns,
        }
    except GenieError as e:
        await flow_events.publish(
            session_id,
            step="error",
            status="error",
            title="Genie call failed",
            detail=str(e),
            payload={"where": "genie", "message": str(e)},
        )
        raise HTTPException(status_code=502, detail=f"genie: {e}")
    except HTTPException:
        raise
    except Exception as e:
        await flow_events.publish(
            session_id,
            step="error",
            status="error",
            title="Unexpected backend error",
            detail=str(e),
            payload={"where": "chat", "message": str(e)},
        )
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await client.close()


# --------------------------------------------------------------------
# Conversation history
# --------------------------------------------------------------------


@app.get("/api/conversations")
async def list_conversations(
    ctx: _AuthCtx = Depends(get_current_ctx),
) -> Dict[str, Any]:
    """Return this user's conversations (most recent first)."""
    return {"conversations": db.list_for_user(ctx.user.username)}


@app.get("/api/conversations/{conv_id}/messages")
async def conversation_messages(
    conv_id: str, ctx: _AuthCtx = Depends(get_current_ctx)
) -> Dict[str, Any]:
    """Fetch transcript for an old conversation from Genie.

    Enforces ownership via the local DB — a user can only pull a conv_id
    that was persisted under their username. We don't cache the transcript;
    Genie is the source of truth.
    """
    owned = db.get_for_user(ctx.user.username, conv_id)
    if not owned:
        raise HTTPException(status_code=404, detail="conversation not found")

    if not settings.genie_space_id:
        raise HTTPException(status_code=503, detail="GENIE_SPACE_ID not set")

    try:
        sp_token = await databricks_auth.get_sp_token(ctx.user.sp_label, ctx.session_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"token_exchange failed: {e}")

    client = GenieClient(
        space_id=settings.genie_space_id,
        sp_token=sp_token,
        session_id=ctx.session_id,
    )
    try:
        raw = await client.list_messages(conv_id)
        messages = normalize_history(raw)

        # Fetch tabular results for every assistant message that has SQL.
        # All GETs (free against QPM) — parallelized to keep history load snappy.
        import asyncio as _asyncio

        async def _fetch(msg_id: str) -> Dict[str, Any]:
            try:
                qr = await client.get_query_result(conv_id, msg_id)
                return {"ok": True, **qr}
            except GenieError as e:
                return {"ok": False, "error": str(e)}

        targets = [m for m in messages if m.get("sql") and m.get("message_id")]
        results = await _asyncio.gather(*[_fetch(m["message_id"]) for m in targets])
        for m, qr in zip(targets, results):
            if qr.get("ok"):
                m["columns"] = qr.get("columns")
                m["rows"] = qr.get("rows")
            else:
                # Genie expires stored query results after a retention window;
                # flag it so the UI can show "results expired" instead of silent empty.
                m["result_expired"] = True
    except GenieError as e:
        raise HTTPException(status_code=502, detail=f"genie: {e}")
    finally:
        await client.close()

    return {
        "conversation_id": conv_id,
        "title": owned["title"],
        "messages": messages,
    }


# --------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------


@app.on_event("startup")
async def _on_startup() -> None:
    db.init()
    sps = []
    if settings.sp_northstar_client_id:
        sps.append(f"northstar ({settings.sp_northstar_dealership})")
    if settings.sp_sunrise_client_id:
        sps.append(f"sunrise ({settings.sp_sunrise_dealership})")
    log.info("Configured SPs: %s", ", ".join(sps) if sps else "(none)")
    log.info(
        "GENIE_SPACE_ID %s",
        "set" if settings.genie_space_id else "NOT SET (chat will 503 until populated)",
    )
    log.info("DBX host: %s  warehouse: %s", settings.dbx_host, settings.dbx_warehouse_id)


@app.on_event("shutdown")
async def _on_shutdown() -> None:
    await databricks_auth.shutdown()
    await sql_client.shutdown()
