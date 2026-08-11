"""Tests for M2.7 one-click pairing: `POST /api/pair/request` (extension
bootstrap) -> `POST /api/pair/approve` (frontend, user click) -> `GET
/api/pair/claim` (extension polls), plus `GET /api/pair/pending` (the
frontend's poll target for the approve banner).

Against a real (BSA_MOCK_LLM=1) FastAPI app -- no network, no subprocess.
"""

from __future__ import annotations

import tomllib

import pytest
from fastapi.testclient import TestClient

import brightspace_agent.api.pair as pair_module

CSRF_HEADERS = {"X-BSA-Request": "1"}

# Loopback Host -- see test_health.py's LOOPBACK_BASE_URL docstring for why
# TestClient's default "testserver" Host is rejected by TrustedHostMiddleware.
LOOPBACK_BASE_URL = "http://127.0.0.1:8730"


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("BSA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BSA_MOCK_LLM", "1")
    return tmp_path


@pytest.fixture
def client(data_dir):
    from brightspace_agent.main import create_app

    return TestClient(create_app(), base_url=LOOPBACK_BASE_URL)


@pytest.fixture
def frozen_clock(monkeypatch):
    """Injectable clock for pair.py's lazy 180s expiry -- see its module
    docstring. `tick(seconds)` advances it without a real sleep."""
    state = {"now": 1_000_000.0}
    monkeypatch.setattr(pair_module, "_now", lambda: state["now"])

    def tick(seconds: float) -> None:
        state["now"] += seconds

    return tick


def request_pair(client) -> str:
    resp = client.post("/api/pair/request", headers=CSRF_HEADERS)
    assert resp.status_code == 200
    return resp.json()["requestId"]


# --------------------------------------------------------------------------
# POST /api/pair/request
# --------------------------------------------------------------------------


def test_request_returns_a_request_id(client):
    resp = client.post("/api/pair/request", headers=CSRF_HEADERS)

    assert resp.status_code == 200
    assert set(resp.json()) == {"requestId"}
    request_id = resp.json()["requestId"]
    assert isinstance(request_id, str) and len(request_id) > 10


def test_request_does_not_require_pairing_token(client):
    # No Authorization header at all -- this is the bootstrap, before the
    # extension has a token to send.
    resp = client.post("/api/pair/request", headers=CSRF_HEADERS)
    assert resp.status_code == 200


def test_request_without_csrf_header_403s(client):
    # Unlike /api/ingest/*, POST /api/pair/request is NOT exempted from the
    # CSRF guard -- it has no pairing token to defeat CSRF the way ingest
    # routes do, so a drive-by cross-origin page must still be unable to
    # invalidate a real pending request. See pair.py's module docstring.
    resp = client.post("/api/pair/request")
    assert resp.status_code == 403


def test_two_requests_generate_different_ids(client):
    first = request_pair(client)
    second = request_pair(client)
    assert first != second


# --------------------------------------------------------------------------
# GET /api/pair/pending
# --------------------------------------------------------------------------


def test_pending_false_with_no_request_made(client):
    resp = client.get("/api/pair/pending")
    assert resp.status_code == 200
    assert resp.json() == {"pending": False}


def test_pending_true_after_a_request(client):
    request_pair(client)

    resp = client.get("/api/pair/pending")

    assert resp.status_code == 200
    assert resp.json() == {"pending": True}


def test_pending_requires_no_csrf_header_or_auth(client):
    request_pair(client)
    # Plain GET, no headers at all -- same exposure as other frontend GETs.
    resp = client.get("/api/pair/pending")
    assert resp.status_code == 200


def test_pending_false_once_approved(client):
    # The banner's job is done once the user has clicked Approve -- the
    # approve endpoint's own 200 is what confirms the click, so `pending`
    # dropping to False here is what makes the banner disappear.
    request_pair(client)
    client.post("/api/pair/approve", headers=CSRF_HEADERS)

    resp = client.get("/api/pair/pending")

    assert resp.json() == {"pending": False}


# --------------------------------------------------------------------------
# POST /api/pair/approve
# --------------------------------------------------------------------------


def test_approve_with_nothing_pending_404s(client):
    resp = client.post("/api/pair/approve", headers=CSRF_HEADERS)
    assert resp.status_code == 404


def test_approve_without_csrf_header_403s(client):
    request_pair(client)
    resp = client.post("/api/pair/approve")
    assert resp.status_code == 403


def test_approve_succeeds_when_a_request_is_pending(client):
    request_pair(client)

    resp = client.post("/api/pair/approve", headers=CSRF_HEADERS)

    assert resp.status_code == 200
    assert resp.json() == {"approved": True}


# --------------------------------------------------------------------------
# GET /api/pair/claim
# --------------------------------------------------------------------------


def test_claim_reports_pending_before_approval(client):
    request_id = request_pair(client)

    resp = client.get(f"/api/pair/claim?requestId={request_id}")

    assert resp.status_code == 200
    assert resp.json() == {"status": "pending"}


def test_claim_unknown_request_id_404s(client):
    request_pair(client)

    resp = client.get("/api/pair/claim?requestId=not-a-real-id")

    assert resp.status_code == 404


def test_claim_with_no_request_ever_made_404s(client):
    resp = client.get("/api/pair/claim?requestId=whatever")
    assert resp.status_code == 404


# --- Fix-wave item 2: a non-ASCII requestId must 404 like any other -------
# mismatch, not 500. `secrets.compare_digest` raises TypeError on non-ASCII
# str inputs, so this must never reach it with the raw request_id.


def test_claim_non_ascii_request_id_404s_with_nothing_pending(client):
    resp = client.get("/api/pair/claim", params={"requestId": "é"})
    assert resp.status_code == 404


def test_claim_non_ascii_request_id_404s_with_a_request_pending(client):
    """The exact reproduction from the final review: `?requestId=%C3%A9`
    against a pending request must 404 exactly like a mismatched ASCII id
    does -- not 500. Before the fix this was the ONE case (pending +
    non-ASCII) that hit `secrets.compare_digest` and raised TypeError."""
    request_pair(client)

    resp = client.get("/api/pair/claim", params={"requestId": "é"})

    assert resp.status_code == 404
    assert "pairingToken" not in resp.text


# --- Security invariant 1: token released ONLY to the approved requestId ---


def test_claim_wrong_id_404s_even_after_approval_never_leaks_the_token(client):
    request_id = request_pair(client)
    client.post("/api/pair/approve", headers=CSRF_HEADERS)

    resp = client.get("/api/pair/claim?requestId=some-other-id")

    assert resp.status_code == 404
    assert "pairingToken" not in resp.text

    # The real id still claims successfully -- proves approval wasn't
    # consumed or otherwise disturbed by the wrong-id attempt above.
    ok = client.get(f"/api/pair/claim?requestId={request_id}")
    assert ok.status_code == 200
    assert ok.json()["status"] == "approved"


def test_claim_returns_the_pairing_token_matching_config_toml(client, data_dir):
    config = tomllib.loads((data_dir / "config.toml").read_text())
    request_id = request_pair(client)
    client.post("/api/pair/approve", headers=CSRF_HEADERS)

    resp = client.get(f"/api/pair/claim?requestId={request_id}")

    assert resp.status_code == 200
    assert resp.json() == {"status": "approved", "pairingToken": config["pairing_token"]}


# --- Security invariant: single-use claim -----------------------------------


def test_claim_is_single_use(client):
    request_id = request_pair(client)
    client.post("/api/pair/approve", headers=CSRF_HEADERS)

    first = client.get(f"/api/pair/claim?requestId={request_id}")
    assert first.status_code == 200
    assert first.json()["status"] == "approved"

    second = client.get(f"/api/pair/claim?requestId={request_id}")
    assert second.status_code == 404


# --- Security invariant 2: a second request before approval invalidates ----
# --- the first id (last-writer-wins) ----------------------------------------


def test_second_request_before_approval_invalidates_the_first_id(client):
    first_id = request_pair(client)
    second_id = request_pair(client)
    assert first_id != second_id

    client.post("/api/pair/approve", headers=CSRF_HEADERS)

    stale = client.get(f"/api/pair/claim?requestId={first_id}")
    assert stale.status_code == 404

    current = client.get(f"/api/pair/claim?requestId={second_id}")
    assert current.status_code == 200
    assert current.json()["status"] == "approved"


# --- Security invariant 3: claim after expiry 404s ---------------------------


def test_claim_after_expiry_404s(client, frozen_clock):
    request_id = request_pair(client)
    client.post("/api/pair/approve", headers=CSRF_HEADERS)

    frozen_clock(181)

    resp = client.get(f"/api/pair/claim?requestId={request_id}")

    assert resp.status_code == 404


def test_claim_just_under_the_expiry_window_still_succeeds(client, frozen_clock):
    request_id = request_pair(client)
    client.post("/api/pair/approve", headers=CSRF_HEADERS)

    frozen_clock(179)

    resp = client.get(f"/api/pair/claim?requestId={request_id}")

    assert resp.status_code == 200


# --- Security invariant 4 (extended): approve with nothing pending 404s, ---
# --- including when the only prior request has since expired --------------


def test_approve_after_expiry_404s(client, frozen_clock):
    request_pair(client)

    frozen_clock(181)

    resp = client.post("/api/pair/approve", headers=CSRF_HEADERS)

    assert resp.status_code == 404


def test_pending_false_after_expiry(client, frozen_clock):
    request_pair(client)
    frozen_clock(181)

    resp = client.get("/api/pair/pending")

    assert resp.json() == {"pending": False}
