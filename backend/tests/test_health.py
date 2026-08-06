import stat
import tomllib

import pytest
from fastapi.testclient import TestClient

from brightspace_agent.config import Settings, ensure_data_dir


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("BSA_DATA_DIR", str(tmp_path))
    return tmp_path


# TestClient's default base_url is "http://testserver", i.e. a Host header
# TrustedHostMiddleware (see main.create_app) rejects -- deliberately, since
# that's the same rejection a DNS-rebound attacker page gets. Tests talk to
# the app over a loopback host instead of the middleware being widened for
# them; test_rejects_a_non_loopback_host_header below pins that behavior.
LOOPBACK_BASE_URL = "http://127.0.0.1:8730"


@pytest.fixture
def client(data_dir):
    from brightspace_agent.main import create_app

    return TestClient(create_app(), base_url=LOOPBACK_BASE_URL)


def test_health_ok_and_unpaired_without_token(client):
    response = client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["paired"] is False


def test_health_paired_with_correct_bearer_token(client, data_dir):
    config = tomllib.loads((data_dir / "config.toml").read_text())
    token = config["pairing_token"]

    response = client.get(
        "/api/health", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    assert response.json()["paired"] is True


def test_rejects_a_non_loopback_host_header(client):
    """Anti-DNS-rebinding: an attacker can point evil.example at 127.0.0.1,
    which makes their page same-origin with this server -- CORS never
    engages and the page can set X-BSA-Request itself, so both of the other
    guards are moot. The Host header is the one thing the browser sends
    truthfully, so a non-loopback Host is refused before routing."""
    for host in ("evil.example", "attacker.test:8730", "brightspace-agent.evil.example"):
        response = client.get("/api/health", headers={"Host": host})
        assert response.status_code == 400, host

    # Both loopback spellings still work, on any port.
    assert client.get("/api/health", headers={"Host": "127.0.0.1:8730"}).status_code == 200
    assert client.get("/api/health", headers={"Host": "localhost:5173"}).status_code == 200


def test_config_toml_created_mode_0600_and_stable_across_calls(data_dir):
    settings = Settings(data_dir=data_dir)

    first = ensure_data_dir(settings)
    config_path = data_dir / "config.toml"

    assert config_path.exists()
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert first["pairing_token"]

    second = ensure_data_dir(settings)

    assert second["pairing_token"] == first["pairing_token"]
