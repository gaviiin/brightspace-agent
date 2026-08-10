#!/usr/bin/env python3
"""Offline end-to-end harness for `make e2e` (and the seeding half of
`make e2e-ui`).

Runs entirely offline: a temp `BSA_DATA_DIR`, `BSA_MOCK_LLM=1`, a fake D2L
tenant (tests/fake_d2l.py) and the real backend app, both served in-process
via uvicorn. A `D2LSyncDriver` replays the extension's exact sync-engine.ts
call sequence over httpx (versions -> whoami -> enrollments -> handshake ->
toc -> file(+X-D2L-Updated) x N -> complete) against the real ingest API,
then drives the real pipeline run to completion and checks the resulting
graph's invariants.

Usage:
    uv run python ../scripts/e2e.py                       # full E2E (make e2e)
    uv run python ../scripts/e2e.py --seed-only            # seed once, exit
    uv run python ../scripts/e2e.py --seed-only --keep-running
        # seed once, then keep the backend (127.0.0.1:8730, matching the
        # frontend dev proxy) + fake D2L (127.0.0.1:9799) servers running
        # until killed -- what `make e2e-ui` uses to give Playwright a real
        # backend with a seeded course to click through.

Exits non-zero (with a readable failure message, and a unified diff for
any "these two things should be identical" assertion) on any failure.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import difflib
import json
import os
import re
import signal
import sqlite3
import sys
import tempfile
import traceback
import urllib.parse
from pathlib import Path
from typing import Any

import httpx
import uvicorn

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_TESTS_DIR = REPO_ROOT / "backend" / "tests"
FIXTURE_DIR = BACKEND_TESTS_DIR / "fixtures" / "d2l"

# fake_d2l.py lives in backend/tests/ (see its own docstring: importable by
# pytest and this script, no production code imports it).
sys.path.insert(0, str(BACKEND_TESTS_DIR))
import fake_d2l  # noqa: E402  (import after sys.path insert, by necessity)
from fake_d2l import make_fake_d2l  # noqa: E402

FAKE_D2L_PORT = 9799
SEED_BACKEND_PORT = 8730  # matches frontend/vite.config.ts's dev-server proxy target

_COURSE_TYPE_RE = re.compile(r"course", re.IGNORECASE)


class E2EAssertionError(Exception):
    """A failed E2E invariant. Caught at the top level and reported without
    a raw Python traceback -- the message is meant to be read directly."""


def _assert(condition: bool, message: str, detail: Any = None) -> None:
    if condition:
        return
    if detail is None:
        raise E2EAssertionError(message)
    raise E2EAssertionError(f"{message}\n  detail: {_pretty(detail)}")


def _pretty(value: Any) -> str:
    try:
        return json.dumps(value, indent=2, sort_keys=True, default=str)
    except TypeError:
        return repr(value)


def _assert_json_equal(a: Any, b: Any, message: str) -> None:
    if a == b:
        return
    diff = "\n".join(
        difflib.unified_diff(
            _pretty(a).splitlines(), _pretty(b).splitlines(), fromfile="run-1", tofile="run-2", lineterm=""
        )
    )
    raise E2EAssertionError(f"{message}\n{diff}")


# --------------------------------------------------------------------------
# D2LSyncDriver -- a Python replica of extension/src/lib/sync-engine.ts's
# call sequence, driven over httpx against the fake D2L tenant + the real
# ingest API. Retries a 429 the same way RateLimitedFetcher does (see
# extension/src/lib/d2l-client.ts): Retry-After if present, else a short
# backoff, up to a few attempts; 401/403 raises immediately (no retry).
# --------------------------------------------------------------------------


class SessionExpired(Exception):
    pass


class D2LSyncDriver:
    def __init__(self, d2l: httpx.AsyncClient, backend: httpx.AsyncClient, pairing_token: str, origin: str) -> None:
        self._d2l = d2l
        self._backend = backend
        self._auth = {"Authorization": f"Bearer {pairing_token}"}
        self._origin = origin

    async def _d2l_get(self, path: str, **kwargs: Any) -> httpx.Response:
        max_attempts = 5
        for attempt in range(max_attempts):
            resp = await self._d2l.get(path, **kwargs)
            if resp.status_code in (401, 403):
                raise SessionExpired(f"D2L session expired (HTTP {resp.status_code}) at {path}")
            if resp.status_code == 429:
                if attempt == max_attempts - 1:
                    raise E2EAssertionError(f"D2L rate limit exceeded after {max_attempts} attempts at {path}")
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else 0.25 * (2**attempt)
                await asyncio.sleep(delay)
                continue
            return resp
        raise AssertionError("unreachable")  # pragma: no cover

    # -- discover: versions -> whoami -> enrollments -> backend.handshake --

    async def discover(self) -> list[dict]:
        lp, le = await self._versions()

        whoami_resp = await self._d2l_get(f"/d2l/api/lp/{lp}/users/whoami")
        whoami_resp.raise_for_status()
        whoami = whoami_resp.json()

        enrollments = await self._all_enrollments(lp)
        course_enrollments = [item for item in enrollments if _is_course_enrollment(item)]

        handshake_resp = await self._backend.post(
            "/api/ingest/handshake",
            headers=self._auth,
            json={
                "tenantOrigin": self._origin,
                "apiVersions": {"lp": lp, "le": le},
                "whoami": whoami,
                "enrollments": [
                    {
                        "orgUnitId": item["OrgUnit"]["Id"],
                        "name": item["OrgUnit"]["Name"],
                        "code": item["OrgUnit"]["Code"],
                    }
                    for item in course_enrollments
                ],
            },
        )
        handshake_resp.raise_for_status()
        return handshake_resp.json()["knownCourses"]

    async def _versions(self) -> tuple[str, str]:
        resp = await self._d2l_get("/d2l/api/versions/")
        resp.raise_for_status()
        versions = resp.json()
        lp = next(v["LatestVersion"] for v in versions if v["ProductCode"] == "lp")
        le = next(v["LatestVersion"] for v in versions if v["ProductCode"] == "le")
        return lp, le

    async def _all_enrollments(self, lp: str) -> list[dict]:
        items: list[dict] = []
        bookmark: str | None = None
        while True:
            params = {"bookmark": bookmark} if bookmark else None
            resp = await self._d2l_get(f"/d2l/api/lp/{lp}/enrollments/myenrollments/", params=params)
            resp.raise_for_status()
            page = resp.json()
            items.extend(page["Items"])
            if not page["PagingInfo"]["HasMoreItems"]:
                break
            bookmark = page["PagingInfo"]["Bookmark"]
        return items

    # -- syncCourse: discoverVersions -> [toc, news, dropbox] -> backend.toc
    #    -> drainQueue -> backend.complete --

    async def sync_course(self, org_unit_id: int) -> dict:
        _lp, le = await self._versions()

        toc_resp = await self._d2l_get(f"/d2l/api/le/{le}/{org_unit_id}/content/toc")
        toc_resp.raise_for_status()
        toc = toc_resp.json()

        news_resp = await self._d2l_get(f"/d2l/api/le/{le}/{org_unit_id}/news/")
        news_resp.raise_for_status()
        dropbox_resp = await self._d2l_get(f"/d2l/api/le/{le}/{org_unit_id}/dropbox/folders/")
        dropbox_resp.raise_for_status()

        toc_backend_resp = await self._backend.post(
            "/api/ingest/toc",
            headers=self._auth,
            json={
                "orgUnitId": org_unit_id,
                "toc": toc,
                # Reshaped exactly as d2l-client.ts does: the fake tenant
                # serves real PascalCase Valence objects, and the backend's
                # extras contract is camelCase (see _to_news_extras).
                "extras": {
                    "news": _to_news_extras(news_resp.json()),
                    "dropbox": _to_dropbox_extras(dropbox_resp.json()),
                },
            },
        )
        toc_backend_resp.raise_for_status()
        toc_body = toc_backend_resp.json()
        sync_run_id = toc_body["syncRunId"]
        needed = toc_body["needed"]

        errors: list[dict] = []
        for item in needed:
            try:
                file_resp = await self._d2l_get(
                    f"/d2l/api/le/{le}/{org_unit_id}/content/topics/{item['d2lTopicId']}/file"
                )
                file_resp.raise_for_status()
            except (SessionExpired, httpx.HTTPStatusError) as exc:
                errors.append({"d2lTopicId": item["d2lTopicId"], "message": str(exc)})
                continue

            headers = {
                **self._auth,
                "X-Source-Url": item["url"],
                "X-Title": urllib.parse.quote(item["title"], safe=""),
                "Content-Type": file_resp.headers.get("content-type", "application/octet-stream"),
            }
            if item.get("lastModified"):
                headers["X-D2L-Updated"] = item["lastModified"]

            upload_resp = await self._backend.post(
                f"/api/ingest/file?syncRunId={sync_run_id}&d2lTopicId={item['d2lTopicId']}",
                headers=headers,
                content=file_resp.content,
            )
            if upload_resp.status_code != 200:
                errors.append({"d2lTopicId": item["d2lTopicId"], "message": f"upload HTTP {upload_resp.status_code}"})

        complete_resp = await self._backend.post(
            "/api/ingest/complete", headers=self._auth, json={"syncRunId": sync_run_id, "errors": errors}
        )
        complete_resp.raise_for_status()

        return {"syncRunId": sync_run_id, "needed": needed, "errors": errors, "complete": complete_resp.json()}


def _rich_text(value: Any, field: str) -> str | None:
    """`Body`/`CustomInstructions` are `{Text, Html}` objects in Valence,
    and either half may be missing."""
    return value.get(field) if isinstance(value, dict) else None


def _to_news_extras(items: Any) -> list[dict]:
    """Mirrors d2l-client.ts's toNewsExtras: D2L's PascalCase news items ->
    the backend's `extras.news` contract. An item with no numeric `Id` is
    dropped (there is no `d2l:news:{id}` to key it on)."""
    extras: list[dict] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("Id"), int):
            continue
        extras.append(
            {
                "id": item["Id"],
                "title": item.get("Title") or "",
                "html": _rich_text(item.get("Body"), "Html") or "",
            }
        )
    return extras


def _to_dropbox_extras(items: Any) -> list[dict]:
    """Mirrors d2l-client.ts's toDropboxExtras."""
    extras: list[dict] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("Id"), int):
            continue
        extras.append(
            {
                "id": item["Id"],
                "name": item.get("Name") or "",
                "instructionsText": _rich_text(item.get("CustomInstructions"), "Text"),
            }
        )
    return extras


def _is_course_enrollment(item: dict) -> bool:
    """Mirrors d2l-client.ts's isCourseEnrollment: Type.Code or Type.Name
    matches /course/i."""
    type_info = item["OrgUnit"]["Type"]
    return bool(_COURSE_TYPE_RE.search(type_info.get("Code") or "") or _COURSE_TYPE_RE.search(type_info.get("Name") or ""))


def _find_course(known_courses: list[dict], org_unit_id: int) -> dict:
    for course in known_courses:
        if course["orgUnitId"] == org_unit_id:
            return course
    raise E2EAssertionError(f"handshake did not return a course for orgUnitId={org_unit_id}")


# --------------------------------------------------------------------------
# In-process server plumbing
# --------------------------------------------------------------------------


async def _serve(app: Any, host: str, port: int) -> tuple[uvicorn.Server, asyncio.Task, int]:
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():
            # startup failed (e.g. port already in use) -- surface it now
            # rather than spinning forever.
            task.result()
        await asyncio.sleep(0.01)
    actual_port = server.servers[0].sockets[0].getsockname()[1]
    return server, task, actual_port


async def _shutdown(server: uvicorn.Server, task: asyncio.Task) -> None:
    server.should_exit = True
    with contextlib.suppress(Exception):
        await asyncio.wait_for(task, timeout=5)


@contextlib.asynccontextmanager
async def _clients(fake_port: int, backend_port: int):
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{fake_port}", timeout=15.0) as d2l:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{backend_port}", timeout=30.0) as backend:
            yield d2l, backend


def _build_backend_app(data_dir: Path) -> Any:
    os.environ["BSA_DATA_DIR"] = str(data_dir)
    os.environ["BSA_MOCK_LLM"] = "1"
    from brightspace_agent.main import create_app  # local import: env must be set first

    return create_app()


async def _wait_forever() -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)
    await stop_event.wait()


# --------------------------------------------------------------------------
# Backend HTTP helpers
# --------------------------------------------------------------------------


async def _get_graph(backend: httpx.AsyncClient, course_id: int) -> dict:
    resp = await backend.get(f"/api/courses/{course_id}/graph")
    resp.raise_for_status()
    return resp.json()


async def _get_course(backend: httpx.AsyncClient, course_id: int) -> dict:
    resp = await backend.get(f"/api/courses/{course_id}")
    resp.raise_for_status()
    return resp.json()


async def _start_pipeline(backend: httpx.AsyncClient, course_id: int) -> int:
    resp = await backend.post(f"/api/courses/{course_id}/pipeline/run", headers={"X-BSA-Request": "1"})
    resp.raise_for_status()
    return resp.json()["runToken"]


async def _wait_pipeline_idle(backend: httpx.AsyncClient, course_id: int, *, timeout: float = 60.0) -> dict:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        resp = await backend.get(f"/api/courses/{course_id}/pipeline/status")
        resp.raise_for_status()
        status = resp.json()
        if not status["active"] and status["stages"]:
            return status
        if loop.time() > deadline:
            raise E2EAssertionError(f"pipeline did not finish within {timeout}s", status)
        await asyncio.sleep(0.05)


async def _run_pipeline_to_completion(backend: httpx.AsyncClient, course_id: int) -> dict:
    await _start_pipeline(backend, course_id)
    status = await _wait_pipeline_idle(backend, course_id)
    failed = [s for s in status["stages"] if s["status"] not in ("complete", "aborted")]
    _assert(not failed, "a pipeline stage did not finish 'complete'", failed)
    return status


async def _run_media_to_completion(backend: httpx.AsyncClient, course_id: int, *, timeout: float = 60.0) -> dict:
    """Detect + process this course's recordings, offline. `BSA_MOCK_LLM=1`
    forces MockMediaFetcher/MockTranscriber (see make_media_fetcher), so no
    yt-dlp subprocess and no ASR engine are involved -- the fake tenant's
    third announcement carries a `mock-captions` Zoom link, which that mock
    answers with a caption track. Runs BEFORE the pipeline so the transcript
    material it produces is summarized/classified by the ordinary run,
    which is the whole point of ingesting a transcript as just a material.
    """
    detect_resp = await backend.post(
        f"/api/courses/{course_id}/media/detect", headers={"X-BSA-Request": "1"}
    )
    detect_resp.raise_for_status()
    detected = detect_resp.json()
    _assert(detected["found"] >= 1, "media detect found no recording links in the synced course", detected)

    process_resp = await backend.post(
        f"/api/courses/{course_id}/media/process", headers={"X-BSA-Request": "1"}
    )
    process_resp.raise_for_status()

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        resp = await backend.get(f"/api/courses/{course_id}/media")
        resp.raise_for_status()
        body = resp.json()
        if not body["active"]:
            break
        if loop.time() > deadline:
            raise E2EAssertionError(f"media job did not finish within {timeout}s", body)
        await asyncio.sleep(0.05)

    unfinished = [s for s in body["sources"] if s["status"] != "done"]
    _assert(not unfinished, "a media source did not finish 'done'", unfinished)
    _assert(
        all(s["transcriptMaterialId"] is not None for s in body["sources"]),
        "a done media source has no transcript material",
        body["sources"],
    )
    return body


def _count_llm_cache_rows(data_dir: Path) -> int:
    conn = sqlite3.connect(data_dir / "brightspace.db")
    try:
        return conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0]
    finally:
        conn.close()


_SNAKE_KEY_RE = re.compile(r"[a-z0-9]_[a-z0-9]")


def _find_snake_case_keys(obj: Any, path: str = "$") -> list[str]:
    found: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str) and _SNAKE_KEY_RE.search(key):
                found.append(f"{path}.{key}")
            found.extend(_find_snake_case_keys(value, f"{path}.{key}"))
    elif isinstance(obj, list):
        for index, item in enumerate(obj):
            found.extend(_find_snake_case_keys(item, f"{path}[{index}]"))
    return found


def _check_transcript_reached_the_graph(graph: dict, media: dict) -> None:
    """The media half of the graph invariants: every transcript the media
    job produced must be a material in the graph AND attached to something.
    `_check_graph_invariants`'s "no material has zero attachments" rule
    would already catch the second half, but only implicitly -- this names
    the transcript, so a regression reads as "the recording never made it"
    rather than "some material lost its attachments"."""
    transcript_ids = {s["transcriptMaterialId"] for s in media["sources"]}
    material_ids = {m["id"] for m in graph["materials"]}
    attached_ids = {a["materialId"] for a in graph["attachments"]}

    _assert(transcript_ids <= material_ids, "a transcript material is missing from the graph", sorted(transcript_ids))
    _assert(transcript_ids <= attached_ids, "a transcript material has no attachments", sorted(transcript_ids))

    by_id = {m["id"]: m for m in graph["materials"]}
    for transcript_id in transcript_ids:
        material = by_id[transcript_id]
        _assert(material["kind"] == "transcript", "transcript material has the wrong kind", material)


def _check_graph_invariants(graph: dict, course: dict) -> None:
    real_topics = [t for t in graph["topics"] if t["id"] != 0]
    _assert(len(real_topics) >= 3, f"expected >= 3 non-Unsorted topics, got {len(real_topics)}", real_topics)

    material_ids = {m["id"] for m in graph["materials"]}
    attached_ids = {a["materialId"] for a in graph["attachments"]}
    missing = material_ids - attached_ids
    _assert(not missing, "some material has zero attachments", sorted(missing))

    _assert(
        course["materialCounts"]["failed"] == 0,
        "some material ended up status='failed'",
        course["materialCounts"],
    )

    snake_keys = _find_snake_case_keys(graph)
    _assert(not snake_keys, "graph response has snake_case keys (expected camelCase throughout)", snake_keys)

    _assert(
        graph["meta"]["taxonomyVersion"] == 1,
        f"expected meta.taxonomyVersion == 1, got {graph['meta']['taxonomyVersion']}",
    )


# --------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------


async def _discover_and_sync(driver: D2LSyncDriver) -> dict:
    known_courses = await driver.discover()
    course = _find_course(known_courses, fake_d2l.ORG_UNIT_ID)
    result = await driver.sync_course(fake_d2l.ORG_UNIT_ID)
    _assert(result["errors"] == [], "sync reported per-file errors", result["errors"])
    _assert(result["complete"]["status"] == "complete", "sync did not finish 'complete'", result["complete"])
    return course


async def run_seed(*, keep_running: bool) -> None:
    with tempfile.TemporaryDirectory(prefix="bsa-e2e-seed-") as tmp:
        data_dir = Path(tmp)
        fake_server, fake_task, fake_port = await _serve(make_fake_d2l(FIXTURE_DIR), "127.0.0.1", FAKE_D2L_PORT)

        backend_app = _build_backend_app(data_dir)
        pairing_token = backend_app.state.pairing_token
        backend_port_request = SEED_BACKEND_PORT if keep_running else 0
        backend_server, backend_task, backend_port = await _serve(backend_app, "127.0.0.1", backend_port_request)

        try:
            async with _clients(fake_port, backend_port) as (d2l, backend):
                driver = D2LSyncDriver(d2l, backend, pairing_token, origin=f"http://127.0.0.1:{fake_port}")
                course = await _discover_and_sync(driver)
                await _run_pipeline_to_completion(backend, course["courseId"])
                print(f"[e2e] seeded course id={course['courseId']} orgUnitId={fake_d2l.ORG_UNIT_ID}")

            if keep_running:
                print(f"[e2e] backend listening on http://127.0.0.1:{backend_port}")
                print(f"[e2e] fake D2L listening on http://127.0.0.1:{fake_port}")
                print(f"[e2e] data dir: {data_dir}")
                print("[e2e] seed-only: waiting for SIGINT/SIGTERM to shut down…", flush=True)
                await _wait_forever()
        finally:
            await _shutdown(backend_server, backend_task)
            await _shutdown(fake_server, fake_task)


async def run_main_scenario() -> None:
    print("[e2e] === main scenario ===")
    with tempfile.TemporaryDirectory(prefix="bsa-e2e-main-") as tmp:
        data_dir = Path(tmp)
        fake_server, fake_task, fake_port = await _serve(make_fake_d2l(FIXTURE_DIR), "127.0.0.1", FAKE_D2L_PORT)

        backend_app = _build_backend_app(data_dir)
        pairing_token = backend_app.state.pairing_token
        backend_server, backend_task, backend_port = await _serve(backend_app, "127.0.0.1", 0)

        try:
            async with _clients(fake_port, backend_port) as (d2l, backend):
                driver = D2LSyncDriver(d2l, backend, pairing_token, origin=f"http://127.0.0.1:{fake_port}")

                course = await _discover_and_sync(driver)
                course_id = course["courseId"]
                media = await _run_media_to_completion(backend, course_id)
                await _run_pipeline_to_completion(backend, course_id)

                graph1 = await _get_graph(backend, course_id)
                course_detail1 = await _get_course(backend, course_id)
                _check_graph_invariants(graph1, course_detail1)
                _check_transcript_reached_the_graph(graph1, media)
                print(f"[e2e] media OK: {len(media['sources'])} recording(s) transcribed and in the graph")
                print(
                    f"[e2e] first run OK: {len(graph1['topics'])} topics, "
                    f"{len(graph1['materials'])} materials, taxonomyVersion={graph1['meta']['taxonomyVersion']}"
                )

                llm_cache_before = _count_llm_cache_rows(data_dir)

                result2 = await driver.sync_course(fake_d2l.ORG_UNIT_ID)
                # A topic whose D2L LastModifiedDate is null can never be
                # diffed as "unchanged" (see ingest/diff.py's compute_needed:
                # a null stored d2l_updated_at always means needed, by
                # design -- there's nothing to compare against). toc_sample's
                # topic 1006 is exactly that case on purpose (it's what
                # test_ingest_api.py's own diff tests exercise); anything
                # else reappearing here would mean incremental sync is
                # broken.
                unexpectedly_needed = [item for item in result2["needed"] if item.get("lastModified") is not None]
                _assert(
                    not unexpectedly_needed,
                    "second /toc reported needed items with a non-null lastModified "
                    "(incremental sync should have diffed these as unchanged)",
                    unexpectedly_needed,
                )

                await _run_pipeline_to_completion(backend, course_id)

                llm_cache_after = _count_llm_cache_rows(data_dir)
                _assert(
                    llm_cache_after == llm_cache_before,
                    "pipeline re-run was not a no-op: llm_cache grew",
                    {"before": llm_cache_before, "after": llm_cache_after},
                )

                course_detail2 = await _get_course(backend, course_id)
                _assert(
                    course_detail2["taxonomyVersion"] == course_detail1["taxonomyVersion"],
                    "taxonomy version changed across a no-op re-run",
                    {"first": course_detail1["taxonomyVersion"], "second": course_detail2["taxonomyVersion"]},
                )

                graph2 = await _get_graph(backend, course_id)
                _assert_json_equal(graph1, graph2, "graph changed across a no-op re-run")
                print("[e2e] second run OK: needed empty, pipeline no-op (0 new llm_cache rows), graph identical")
        finally:
            await _shutdown(backend_server, backend_task)
            await _shutdown(fake_server, fake_task)


async def run_rate_limit_scenario() -> None:
    print("[e2e] === 429 rate-limit scenario (rate_limit_429_every=5) ===")
    with tempfile.TemporaryDirectory(prefix="bsa-e2e-429-") as tmp:
        data_dir = Path(tmp)
        fake_app = make_fake_d2l(FIXTURE_DIR, rate_limit_429_every=5)
        fake_server, fake_task, fake_port = await _serve(fake_app, "127.0.0.1", FAKE_D2L_PORT)

        backend_app = _build_backend_app(data_dir)
        pairing_token = backend_app.state.pairing_token
        backend_server, backend_task, backend_port = await _serve(backend_app, "127.0.0.1", 0)

        try:
            async with _clients(fake_port, backend_port) as (d2l, backend):
                driver = D2LSyncDriver(d2l, backend, pairing_token, origin=f"http://127.0.0.1:{fake_port}")
                course = await _discover_and_sync(driver)
                print(f"[e2e] sync under rate limiting completed OK (course id={course['courseId']})")
        finally:
            await _shutdown(backend_server, backend_task)
            await _shutdown(fake_server, fake_task)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--seed-only",
        action="store_true",
        help="Only seed one course (sync + pipeline run to completion); skip the "
        "second-run/no-op assertions and the rate-limit scenario.",
    )
    parser.add_argument(
        "--keep-running",
        action="store_true",
        help="After seeding, keep the backend (127.0.0.1:8730) and fake D2L "
        "(127.0.0.1:9799) servers running until killed. Requires --seed-only.",
    )
    args = parser.parse_args(argv)
    if args.keep_running and not args.seed_only:
        parser.error("--keep-running requires --seed-only")
    return args


async def _amain(args: argparse.Namespace) -> None:
    if args.seed_only:
        await run_seed(keep_running=args.keep_running)
        return
    await run_main_scenario()
    await run_rate_limit_scenario()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        asyncio.run(_amain(args))
    except E2EAssertionError as exc:
        print(f"\n[e2e] FAILED: {exc}\n", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n[e2e] interrupted\n", file=sys.stderr)
        return 130
    except Exception:  # noqa: BLE001 -- top-level catch-all so any failure exits non-zero with a readable trace
        traceback.print_exc()
        print("\n[e2e] FAILED (unexpected exception -- see traceback above)\n", file=sys.stderr)
        return 1

    print("\n[e2e] OK\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
