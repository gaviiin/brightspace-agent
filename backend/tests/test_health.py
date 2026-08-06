import stat
import tomllib

import pytest
from fastapi.testclient import TestClient

from brightspace_agent.config import Settings, ensure_data_dir


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("BSA_DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def client(data_dir):
    from brightspace_agent.main import create_app

    return TestClient(create_app())


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


def test_config_toml_created_mode_0600_and_stable_across_calls(data_dir):
    settings = Settings(data_dir=data_dir)

    first = ensure_data_dir(settings)
    config_path = data_dir / "config.toml"

    assert config_path.exists()
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert first["pairing_token"]

    second = ensure_data_dir(settings)

    assert second["pairing_token"] == first["pairing_token"]
