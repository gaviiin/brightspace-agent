"""Tests for the SPA fallback in main.py: a client-side route with no file
extension (e.g. `/courses/3`, reachable by React Router but not backed by
any real file) serves `frontend/dist/index.html` so a hard refresh or a
shared deep link into the SPA works -- StaticFiles(html=True) alone only
auto-serves index.html for a path that's an actual *directory* on disk (see
main.py's `_spa_or_static_response` docstring), not an arbitrary client-side
route. Actual static files (assets) stay static, and /api/* is unaffected
either way (a genuinely unknown API path is still a clean 404, not the SPA
shell).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("BSA_DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data"


@pytest.fixture
def dist_dir(tmp_path, monkeypatch):
    """A minimal fake `frontend/dist/` -- tests don't depend on a real
    `pnpm build` having been run."""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html><body>SPA shell</body></html>")
    (dist / "assets" / "index-abc123.js").write_text("console.log('hi');")
    monkeypatch.setattr("brightspace_agent.main.FRONTEND_DIST", dist)
    return dist


@pytest.fixture
def client(data_dir, dist_dir):
    from brightspace_agent.main import create_app

    # Loopback Host, not TestClient's default "testserver" -- see
    # test_health.py's LOOPBACK_BASE_URL for why.
    with TestClient(create_app(), base_url="http://127.0.0.1:8730") as test_client:
        yield test_client


def test_unknown_client_side_route_serves_index_html(client):
    response = client.get("/courses/3")

    assert response.status_code == 200
    assert "SPA shell" in response.text
    assert response.headers["content-type"].startswith("text/html")


def test_root_serves_index_html(client):
    response = client.get("/")

    assert response.status_code == 200
    assert "SPA shell" in response.text


def test_real_asset_file_is_still_served_as_a_static_file(client):
    response = client.get("/assets/index-abc123.js")

    assert response.status_code == 200
    assert "console.log" in response.text


def test_asset_looking_path_that_does_not_exist_404s_rather_than_serving_html(client):
    response = client.get("/assets/does-not-exist.js")

    assert response.status_code == 404
    assert "SPA shell" not in response.text


def test_unknown_api_path_is_a_clean_404_not_the_spa_shell(client):
    response = client.get("/api/totally-unknown-endpoint")

    assert response.status_code == 404
    assert "SPA shell" not in response.text


def test_known_api_route_is_unaffected(client):
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
