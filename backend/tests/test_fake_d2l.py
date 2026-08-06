"""Tests for the fake D2L tenant (fake_d2l.py) used by `make e2e` and this
suite alike: the D2L surface shapes the extension actually depends on, plus
both fault-injection modes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import fake_d2l
from fake_d2l import ORG_UNIT_ID, make_fake_d2l

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "d2l"


@pytest.fixture
def client():
    app = make_fake_d2l(FIXTURES_DIR)
    return TestClient(app)


# --------------------------------------------------------------------------
# versions / whoami
# --------------------------------------------------------------------------


def test_versions_returns_lp_and_le():
    client_ = TestClient(make_fake_d2l(FIXTURES_DIR))
    resp = client_.get("/d2l/api/versions/")
    assert resp.status_code == 200
    by_product = {v["ProductCode"]: v for v in resp.json()}
    assert by_product.keys() == {"lp", "le"}
    assert by_product["lp"]["LatestVersion"] == fake_d2l.LP_VERSION
    assert by_product["le"]["LatestVersion"] == fake_d2l.LE_VERSION


def test_whoami_returns_identifier(client):
    resp = client.get(f"/d2l/api/lp/{fake_d2l.LP_VERSION}/users/whoami")
    assert resp.status_code == 200
    assert resp.json()["Identifier"] == "999"


# --------------------------------------------------------------------------
# enrollments: bookmark paging + course-type shape
# --------------------------------------------------------------------------


def test_enrollments_paginate_via_bookmark_and_cover_both_items(client):
    page1 = client.get(f"/d2l/api/lp/{fake_d2l.LP_VERSION}/enrollments/myenrollments/")
    assert page1.status_code == 200
    body1 = page1.json()
    assert body1["PagingInfo"]["HasMoreItems"] is True
    assert len(body1["Items"]) == 1
    bookmark = body1["PagingInfo"]["Bookmark"]

    page2 = client.get(
        f"/d2l/api/lp/{fake_d2l.LP_VERSION}/enrollments/myenrollments/", params={"bookmark": bookmark}
    )
    assert page2.status_code == 200
    body2 = page2.json()
    assert body2["PagingInfo"]["HasMoreItems"] is False
    assert len(body2["Items"]) == 1

    all_ids = {body1["Items"][0]["OrgUnit"]["Id"], body2["Items"][0]["OrgUnit"]["Id"]}
    assert all_ids == {ORG_UNIT_ID, 9999}

    # The course-type filter the extension applies client-side (see
    # d2l-client.ts's isCourseEnrollment) must have something real to work
    # with: one item's Type looks like a course, the other doesn't.
    types = {item["OrgUnit"]["Id"]: item["OrgUnit"]["Type"]["Code"] for item in [body1["Items"][0], body2["Items"][0]]}
    assert types[ORG_UNIT_ID] == "Course Offering"
    assert types[9999] == "Department"


# --------------------------------------------------------------------------
# content/toc
# --------------------------------------------------------------------------


def test_content_toc_returns_the_fixture_verbatim(client):
    resp = client.get(f"/d2l/api/le/{fake_d2l.LE_VERSION}/{ORG_UNIT_ID}/content/toc")
    assert resp.status_code == 200
    expected = json.loads((FIXTURES_DIR / "toc_sample.json").read_text())
    assert resp.json() == expected


def test_content_toc_unknown_org_unit_404s(client):
    resp = client.get(f"/d2l/api/le/{fake_d2l.LE_VERSION}/1/content/toc")
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# content/topics/{id}/file
# --------------------------------------------------------------------------


def test_file_endpoint_serves_a_real_pdf_for_a_pdf_topic(client):
    resp = client.get(f"/d2l/api/le/{fake_d2l.LE_VERSION}/{ORG_UNIT_ID}/content/topics/1001/file")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/pdf")
    assert resp.content.startswith(b"%PDF")


def test_file_endpoint_serves_a_real_pptx_for_a_pptx_topic(client):
    resp = client.get(f"/d2l/api/le/{fake_d2l.LE_VERSION}/{ORG_UNIT_ID}/content/topics/1004/file")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    )
    # A pptx is a zip container; a real one starts with the zip local-file
    # header magic bytes.
    assert resp.content[:2] == b"PK"


def test_file_endpoint_unknown_topic_404s(client):
    resp = client.get(f"/d2l/api/le/{fake_d2l.LE_VERSION}/{ORG_UNIT_ID}/content/topics/424242/file")
    assert resp.status_code == 404


def test_file_endpoint_serves_vtt_and_html_for_those_extensions(tmp_path):
    toc = {
        "Modules": [
            {
                "ModuleId": 1,
                "Title": "M1",
                "Topics": [
                    {
                        "TopicId": 2001,
                        "Title": "Lecture Captions",
                        "TypeIdentifier": "File",
                        "Url": "/d2l/le/content/1/topics/2001/download/captions.vtt",
                        "LastModifiedDate": None,
                        "SizeBytes": None,
                    },
                    {
                        "TopicId": 2002,
                        "Title": "Reading Page",
                        "TypeIdentifier": "File",
                        "Url": "/d2l/le/content/1/topics/2002/download/page.html",
                        "LastModifiedDate": None,
                        "SizeBytes": None,
                    },
                ],
            }
        ]
    }
    (tmp_path / "toc_sample.json").write_text(json.dumps(toc))
    app = make_fake_d2l(tmp_path)
    client_ = TestClient(app)

    vtt_resp = client_.get(f"/d2l/api/le/{fake_d2l.LE_VERSION}/{ORG_UNIT_ID}/content/topics/2001/file")
    assert vtt_resp.status_code == 200
    assert vtt_resp.headers["content-type"].startswith("text/vtt")
    assert vtt_resp.content.startswith(b"WEBVTT")

    html_resp = client_.get(f"/d2l/api/le/{fake_d2l.LE_VERSION}/{ORG_UNIT_ID}/content/topics/2002/file")
    assert html_resp.status_code == 200
    assert html_resp.headers["content-type"].startswith("text/html")
    assert b"<html>" in html_resp.content


# --------------------------------------------------------------------------
# news / dropbox extras
# --------------------------------------------------------------------------


def test_news_and_dropbox_serve_the_real_valence_shape_not_the_backend_contract(client):
    """Regression guard for the extras wire-shape mismatch: this fixture
    used to serve `{id, title, html}` / `{id, name, instructionsText}` --
    the BACKEND's contract -- which made the extension look correct while
    forwarding raw Valence objects that the backend would reject outright.
    D2L sends PascalCase with the body nested; the reshaping is
    d2l-client.ts's job (toNewsExtras/toDropboxExtras)."""
    news_resp = client.get(f"/d2l/api/le/{fake_d2l.LE_VERSION}/{ORG_UNIT_ID}/news/")
    assert news_resp.status_code == 200
    assert news_resp.json() == fake_d2l.NEWS
    for item in news_resp.json():
        assert {"Id", "Title", "Body"} <= set(item)
        assert set(item["Body"]) >= {"Text", "Html"}
        # The backend-contract keys must NOT be present -- that was the bug.
        assert not {"id", "title", "html"} & set(item)

    dropbox_resp = client.get(f"/d2l/api/le/{fake_d2l.LE_VERSION}/{ORG_UNIT_ID}/dropbox/folders/")
    assert dropbox_resp.status_code == 200
    assert dropbox_resp.json() == fake_d2l.DROPBOX
    for item in dropbox_resp.json():
        assert {"Id", "Name", "CustomInstructions"} <= set(item)
        assert set(item["CustomInstructions"]) >= {"Text", "Html"}
        assert not {"id", "name", "instructionsText"} & set(item)


# --------------------------------------------------------------------------
# Fault injection: rate limiting
# --------------------------------------------------------------------------


def test_rate_limit_429_every_nth_request_returns_429_with_headers():
    app = make_fake_d2l(FIXTURES_DIR, rate_limit_429_every=3)
    client_ = TestClient(app)

    r1 = client_.get("/d2l/api/versions/")
    r2 = client_.get("/d2l/api/versions/")
    r3 = client_.get("/d2l/api/versions/")

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r3.status_code == 429
    assert r3.headers["retry-after"] == "1"
    assert "x-rate-limit-remaining" in {k.lower() for k in r3.headers}

    # Rate limiting is periodic, not sticky -- the next request succeeds.
    r4 = client_.get("/d2l/api/versions/")
    assert r4.status_code == 200


# --------------------------------------------------------------------------
# Fault injection: session expiry + /_control/reset
# --------------------------------------------------------------------------


def test_expire_session_after_n_requests_then_401s_until_reset():
    app = make_fake_d2l(FIXTURES_DIR, expire_session_after=2)
    client_ = TestClient(app)

    assert client_.get("/d2l/api/versions/").status_code == 200
    assert client_.get("/d2l/api/versions/").status_code == 200
    # 3rd request trips it, and it stays tripped (sticky) for anything after.
    assert client_.get("/d2l/api/versions/").status_code == 401
    assert client_.get(f"/d2l/api/lp/{fake_d2l.LP_VERSION}/users/whoami").status_code == 401

    reset_resp = client_.post("/_control/reset")
    assert reset_resp.status_code == 200

    assert client_.get("/d2l/api/versions/").status_code == 200


def test_control_state_reports_counters():
    app = make_fake_d2l(FIXTURES_DIR)
    client_ = TestClient(app)
    client_.get("/d2l/api/versions/")
    client_.get("/d2l/api/versions/")

    state = client_.get("/_control/state").json()
    assert state["requestCount"] == 2
    assert state["sessionExpired"] is False
