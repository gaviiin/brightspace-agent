"""Shared FastAPI dependencies: pairing-token auth and app-state accessors.

`create_app()` (main.py) stashes the pairing token, SQLAlchemy session
factory, and blob store on `app.state` once at startup; these dependencies
just read them back out per-request.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from brightspace_agent.ingest.store import BlobStore


def require_pairing_token(request: Request) -> None:
    """Raise 401 unless `Authorization: Bearer <token>` matches the stored
    pairing token (constant-time compare)."""
    auth = request.headers.get("Authorization", "")
    prefix = "Bearer "
    token = auth[len(prefix) :] if auth.startswith(prefix) else ""
    pairing_token: str = request.app.state.pairing_token
    if not token or not secrets.compare_digest(token, pairing_token):
        raise HTTPException(status_code=401, detail="invalid pairing token")


def get_session(request: Request) -> Iterator[Session]:
    """Yield a SQLAlchemy session for the request's lifetime, closed after."""
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        yield session


def get_blob_store(request: Request) -> BlobStore:
    return request.app.state.blob_store
