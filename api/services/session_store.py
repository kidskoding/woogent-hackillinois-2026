"""
Checkout session store.

Persists sessions to MySQL so they survive API restarts (e.g. uvicorn --reload).
Falls back to in-memory for routes that don't have a DB session.
"""
from __future__ import annotations

import time
import uuid
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from db.woo_queries import session_create, session_get, session_update
from models.ucp import CheckoutSession


_sessions: dict[str, dict] = {}  # in-memory fallback: id → {"session": CheckoutSession, "created_at": float}

SESSION_TTL_SECONDS = 3600

async def create_session(session: CheckoutSession, db: Optional[AsyncSession] = None) -> CheckoutSession:
    session.id = str(uuid.uuid4())
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    session.created_at = ts
    session.updated_at = ts
    if db:
        await session_create(db, session.id, session.model_dump_json())
    _sessions[session.id] = {"session": session, "created_at": time.time()}
    return session


async def get_session(session_id: str, db: Optional[AsyncSession] = None) -> Optional[CheckoutSession]:
    # Try DB first (survives restarts)
    if db:
        data = await session_get(db, session_id)
        if data:
            try:
                return CheckoutSession.model_validate_json(data)
            except Exception:
                pass
    # Fallback to in-memory
    entry = _sessions.get(session_id)
    if entry is None:
        return None
    if time.time() - entry["created_at"] > SESSION_TTL_SECONDS:
        del _sessions[session_id]
        return None
    return entry["session"]


async def update_session(session: CheckoutSession, db: Optional[AsyncSession] = None) -> CheckoutSession:
    session.updated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if db:
        await session_update(db, session.id, session.model_dump_json())
    if session.id in _sessions:
        _sessions[session.id]["session"] = session
    return session


def delete_session(session_id: str) -> None:
    _sessions.pop(session_id, None)
