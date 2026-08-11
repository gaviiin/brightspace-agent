"""M2.7 one-click pairing: request -> approve -> claim.

Today the user copies the pairing token off the Settings page and pastes it
into the extension popup. This router replaces that with a handshake: the
extension calls `POST /request`, the user clicks Approve on the Settings
page (which is polling `GET /pending`), and the extension claims the real
token from `GET /claim`. The manual paste path (`GET /api/settings`'s
`pairingToken` field) is untouched -- it stays as the fallback.

State: a SINGLE pending request lives on `app.state.pending_pair` (`None`,
or `{"request_id", "created_at", "approved"}`) -- there is only ever one
outstanding pairing attempt for this local, single-user app, and a fresh
`POST /request` always replaces whatever was there (see `request_pairing`).
`created_at` is `_now()` (see below), not `ingest/repo.now_iso()`'s ISO
string -- the only thing ever done with it is a numeric age comparison
against `_PENDING_TTL_SECONDS`, and a bare float is both simpler to compare
and trivial to monkeypatch a fixed value for in tests (`_now` is looked up
fresh on every call, so `monkeypatch.setattr(pair, "_now", ...)` controls it
without a real sleep -- see test_api_pair.py's `frozen_clock` fixture).
Expiry is checked lazily, in `_get_live_pending`, on every one of this
router's endpoints -- there is no background sweep.

Auth: `POST /request` and `GET /claim` are the extension's side and
carry no pairing-token `Authorization` header -- the extension doesn't have
one yet; that's the entire point of this flow. `GET /pending` and
`POST /approve` are the frontend's side and, like every other frontend
route, carry no pairing-token auth either (see `GET /api/settings`, which
already hands the frontend the real token outright).

CSRF (see main.py's `_csrf_guard`): `POST /approve` is guarded exactly like
every other frontend-facing mutation. `POST /request` is deliberately NOT
added to `_CSRF_EXEMPT_PREFIX` even though it's extension-facing and
unauthenticated like `/api/ingest/*` is -- the ingest routes are exempted
because a valid pairing-token bearer header is itself unforgeable by a
cross-origin page (it has no way to read config.toml), which defeats CSRF
without needing the header too. `POST /request` has no token to check, so
that reasoning doesn't hold: without the CSRF guard, a drive-by page could
silently fire simple (preflight-free) cross-origin POSTs at it and, via
last-writer-wins, repeatedly invalidate a real in-flight pairing attempt.
Requiring the header closes that -- and costs the extension nothing, since
as a privileged context it isn't subject to the CORS preflight the header
exists to force in the first place (see backend-client.ts's `pairRequest`,
which sets it explicitly rather than relying on an exemption). `GET /claim`
and `GET /pending` are GETs, so `main.py`'s guard (POST/PUT/DELETE only)
never touches them regardless of this decision.
"""

from __future__ import annotations

import secrets
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

router = APIRouter(prefix="/api/pair", tags=["pair"])

# A pending request older than this is treated as if it never existed --
# the popup gives up polling after 3 minutes (see the extension brief), so
# nothing legitimate should ever still be waiting past this.
_PENDING_TTL_SECONDS = 180


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


def _now() -> float:
    return time.time()


def _get_live_pending(app_state: Any) -> dict[str, Any] | None:
    """The current pending-pair dict, or `None` if there isn't one or it's
    past `_PENDING_TTL_SECONDS` old -- lazily clearing `app_state.pending_pair`
    in the latter case so a subsequent call (or `GET /pending`) doesn't have
    to redo the age check to reach the same answer."""
    pending = getattr(app_state, "pending_pair", None)
    if pending is None:
        return None
    if _now() - pending["created_at"] > _PENDING_TTL_SECONDS:
        app_state.pending_pair = None
        return None
    return pending


# --------------------------------------------------------------------------
# POST /api/pair/request -- extension bootstrap
# --------------------------------------------------------------------------


class PairRequestResponse(CamelModel):
    request_id: str


@router.post("/request", response_model=PairRequestResponse)
def request_pairing(request: Request) -> PairRequestResponse:
    request_id = secrets.token_urlsafe(16)
    # Unconditional overwrite -- a stale or still-live prior request is
    # invalidated either way (last-writer-wins), which is what keeps the
    # approve button on the Settings page unambiguous: whatever the user is
    # looking at when they click it is the ONLY thing `POST /approve` can
    # possibly mean.
    request.app.state.pending_pair = {
        "request_id": request_id,
        "created_at": _now(),
        "approved": False,
    }
    return PairRequestResponse(request_id=request_id)


# --------------------------------------------------------------------------
# GET /api/pair/pending -- frontend poll target for the approve banner
# --------------------------------------------------------------------------


class PairPendingResponse(CamelModel):
    pending: bool


@router.get("/pending", response_model=PairPendingResponse)
def get_pending(request: Request) -> PairPendingResponse:
    pending = _get_live_pending(request.app.state)
    # False once approved too, not just when absent/expired -- the banner's
    # job is to prompt a click, and once that click has happened there's
    # nothing left for it to ask about (the extension is now the one
    # polling, on `GET /claim`).
    return PairPendingResponse(pending=pending is not None and not pending["approved"])


# --------------------------------------------------------------------------
# POST /api/pair/approve -- the user's click
# --------------------------------------------------------------------------


class PairApproveResponse(CamelModel):
    approved: bool


@router.post("/approve", response_model=PairApproveResponse)
def approve_pairing(request: Request) -> PairApproveResponse:
    pending = _get_live_pending(request.app.state)
    if pending is None:
        raise HTTPException(status_code=404, detail="no pending pairing request")
    pending["approved"] = True
    return PairApproveResponse(approved=True)


# --------------------------------------------------------------------------
# GET /api/pair/claim -- the extension's poll
# --------------------------------------------------------------------------


class PairClaimResponse(CamelModel):
    status: str
    # Absent (not null) on the wire for the 'pending' outcome --
    # response_model_exclude_none below, same shape discipline
    # api/ingest.py's LtiResolutionResponse uses.
    pairing_token: str | None = None


@router.get("/claim", response_model=PairClaimResponse, response_model_exclude_none=True)
def claim_pairing(request: Request, request_id: str = Query(alias="requestId")) -> PairClaimResponse:
    pending = _get_live_pending(request.app.state)
    # Constant-time compare, and a mismatch 404s exactly like "nothing
    # pending" -- an attacker probing with a wrong id learns nothing about
    # whether a real request is in flight, let alone its id or the token.
    # Compared as UTF-8 bytes, not `str`: `secrets.compare_digest` raises
    # TypeError on a non-ASCII `str` argument, which would otherwise turn a
    # non-ASCII requestId probe into a 500 -- itself a leak (500 only when a
    # request is pending, 404 otherwise) and a break of the invariant this
    # comment claims. `real_request_id.encode()` is always plain ASCII
    # (`secrets.token_urlsafe`'s alphabet), so this only ever changes what
    # a non-ASCII `request_id` compares against, never the real id's bytes.
    if pending is None or not secrets.compare_digest(
        pending["request_id"].encode(), request_id.encode("utf-8", errors="surrogatepass")
    ):
        raise HTTPException(status_code=404, detail="unknown or expired pairing request")

    if not pending["approved"]:
        return PairClaimResponse(status="pending")

    # Single-use: cleared on the first successful claim, so a replay with
    # the same id (e.g. a second popup left polling) 404s exactly like an
    # unknown one rather than handing out the token twice.
    request.app.state.pending_pair = None
    return PairClaimResponse(status="approved", pairing_token=request.app.state.pairing_token)
