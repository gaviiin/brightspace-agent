"""Tests for `GET /api/settings`: the frontend's read model for the pairing
token, data dir, configured models, mock-LLM flag, cost cap, and whether an
Anthropic API key is configured -- WITHOUT ever serializing the key itself.
"""

from __future__ import annotations

import tomllib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("BSA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BSA_MOCK_LLM", "1")
    # Isolate from whatever's in the host environment -- these tests assert
    # on apiKeyConfigured for a specific, known state.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("BSA_ANTHROPIC_API_KEY", raising=False)
    return tmp_path


@pytest.fixture
def client(data_dir):
    from brightspace_agent.main import create_app

    return TestClient(create_app())


def test_settings_shape_and_pairing_token_matches_config_toml(client, data_dir):
    config = tomllib.loads((data_dir / "config.toml").read_text())

    resp = client.get("/api/settings")

    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {
        "pairingToken", "dataDir", "models", "mockLlm", "maxCostUsdPerRun", "apiKeyConfigured",
    }
    assert body["pairingToken"] == config["pairing_token"]
    assert body["dataDir"] == str(data_dir)
    assert set(body["models"]) == {"fast", "smart"}
    assert isinstance(body["models"]["fast"], str) and body["models"]["fast"]
    assert isinstance(body["models"]["smart"], str) and body["models"]["smart"]
    assert body["mockLlm"] is True
    assert body["maxCostUsdPerRun"] == pytest.approx(5.0)


def test_settings_api_key_not_configured_by_default(client):
    resp = client.get("/api/settings")
    assert resp.status_code == 200
    assert resp.json()["apiKeyConfigured"] is False


def test_settings_api_key_configured_true_but_key_material_never_serialized(monkeypatch, data_dir):
    from brightspace_agent.main import create_app

    secret = "sk-ant-super-secret-value-should-never-leak"  # pragma: allowlist secret
    monkeypatch.setenv("BSA_ANTHROPIC_API_KEY", secret)
    client = TestClient(create_app())

    resp = client.get("/api/settings")

    assert resp.status_code == 200
    assert resp.json()["apiKeyConfigured"] is True
    # The raw response body (not just the parsed dict) must never contain
    # the key material -- guards against it leaking under some other field
    # name too, not just the ones we already know to check.
    assert secret not in resp.text


def test_settings_does_not_require_csrf_header_or_pairing_auth(client):
    # GET, not a mutation -- no X-BSA-Request header, no Authorization
    # bearer token required (see main.py's CSRF guard: only POST/PUT/DELETE
    # under /api/ are guarded).
    resp = client.get("/api/settings")
    assert resp.status_code == 200
