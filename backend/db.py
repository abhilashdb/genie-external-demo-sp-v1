"""SQLite-backed store for per-user Genie conversation pointers.

We only persist metadata — the actual transcript lives in Genie and is pulled
via `GET /api/2.0/genie/spaces/{space}/conversations/{conv_id}/messages` when
the user opens an old thread. That way a conversation in the sidebar is just a
title + a genie_conv_id; the source of truth is Databricks.

Scoped by `local_user` (alice/bob/...) because multiple local users share a
single SP, so Genie's own listing would merge their threads together.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_DB_PATH = Path(__file__).resolve().parent / "data" / "conversations.db"
_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    local_user      TEXT    NOT NULL,
    sp_label        TEXT    NOT NULL,
    genie_conv_id   TEXT    NOT NULL UNIQUE,
    title           TEXT    NOT NULL,
    created_at      TEXT    NOT NULL,
    last_active_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conversations_user
    ON conversations(local_user, last_active_at DESC);
"""


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _lock, _connect() as conn:
        conn.executescript(_SCHEMA)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _title_from(message: str, limit: int = 60) -> str:
    t = (message or "").strip().splitlines()[0] if message else ""
    if len(t) > limit:
        t = t[: limit - 1].rstrip() + "…"
    return t or "(untitled)"


def upsert_conversation(
    *,
    local_user: str,
    sp_label: str,
    genie_conv_id: str,
    first_message: Optional[str],
) -> None:
    """Insert on first turn; bump last_active_at on follow-ups."""
    now = _now_iso()
    with _lock, _connect() as conn:
        existing = conn.execute(
            "SELECT id FROM conversations WHERE genie_conv_id = ?",
            (genie_conv_id,),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE conversations SET last_active_at = ? WHERE genie_conv_id = ?",
                (now, genie_conv_id),
            )
            return
        conn.execute(
            """
            INSERT INTO conversations
                (local_user, sp_label, genie_conv_id, title, created_at, last_active_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                local_user,
                sp_label,
                genie_conv_id,
                _title_from(first_message or ""),
                now,
                now,
            ),
        )


def list_for_user(local_user: str) -> List[Dict[str, Any]]:
    with _lock, _connect() as conn:
        rows = conn.execute(
            """
            SELECT genie_conv_id, title, created_at, last_active_at
            FROM conversations
            WHERE local_user = ?
            ORDER BY last_active_at DESC
            """,
            (local_user,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_for_user(local_user: str, genie_conv_id: str) -> Optional[Dict[str, Any]]:
    with _lock, _connect() as conn:
        row = conn.execute(
            """
            SELECT genie_conv_id, title, sp_label, created_at, last_active_at
            FROM conversations
            WHERE local_user = ? AND genie_conv_id = ?
            """,
            (local_user, genie_conv_id),
        ).fetchone()
    return dict(row) if row else None
