"""FastAPI app factory and CLI entry point."""

import logging
import secrets
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response

from brightspace_agent import __version__
from brightspace_agent.agents.llm import make_backend
from brightspace_agent.agents.web import make_web_backend
from brightspace_agent.api.courses import router as courses_router
from brightspace_agent.api.enrichment import router as enrichment_router
from brightspace_agent.api.events import router as events_router
from brightspace_agent.api.graph import router as graph_router
from brightspace_agent.api.ingest import router as ingest_router
from brightspace_agent.api.materials import router as materials_router
from brightspace_agent.api.media import router as media_router
from brightspace_agent.api.pipeline import router as pipeline_router
from brightspace_agent.api.settings import router as settings_router
from brightspace_agent.api.taxonomy import router as taxonomy_router
from brightspace_agent.config import Settings, ensure_data_dir
from brightspace_agent.db.session import init_db
from brightspace_agent.ingest.store import BlobStore
from brightspace_agent.media.fetch import make_media_fetcher
from brightspace_agent.media.transcribe import make_transcriber
from brightspace_agent.pipeline.runner import EventBus, PipelineRunner

# backend/src/brightspace_agent/main.py -> repo root
REPO_ROOT = Path(__file__).resolve().parents[3]
FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"

# CSRF rule for browser-facing mutating endpoints (see the Task 9 brief):
# POST/PUT/DELETE under /api/ -- except /api/ingest/*, which authenticates
# via the pairing token instead -- require this header. A custom header
# forces the browser to send a CORS preflight first, which the CORS
# middleware's allow_origins already restricts to the frontend's own
# origin; a drive-by page on another origin can request the mutation, but
# the browser will never attach the header for it, so it 403s before doing
# anything (in particular, before spending on an LLM call).
_CSRF_HEADER = "X-BSA-Request"
_CSRF_EXEMPT_PREFIX = "/api/ingest/"
_CSRF_GUARDED_METHODS = {"POST", "PUT", "DELETE"}

# Anti-DNS-rebinding. Both guards above assume an attacker page is
# cross-origin: CORS restricts who may read a response, and the CSRF header
# survives because a browser won't attach it cross-origin without a
# preflight the CORS policy denies. DNS rebinding breaks that assumption
# outright -- the attacker points evil.example at 127.0.0.1, so the page IS
# same-origin as this server, CORS never engages, and the page may freely
# set X-BSA-Request: 1. It could then read GET /api/settings (which returns
# the pairing token, i.e. full ingest-API access) and start real runs.
#
# The one thing a rebound request cannot forge is the Host header: the
# browser sends the attacker's own hostname. Rejecting any Host that isn't
# loopback closes the hole for every route at once, before routing.
# Starlette compares the hostname only (the port is stripped), so any port
# this server is configured on works.
_ALLOWED_HOSTS = ["127.0.0.1", "localhost"]


def create_app() -> FastAPI:
    settings = Settings()
    config = ensure_data_dir(settings)
    pairing_token = config["pairing_token"]

    engine, session_factory = init_db(settings.db_path)
    blob_store = BlobStore(settings.blobs_dir, settings.text_dir)
    backend = make_backend(settings)
    web_backend = make_web_backend(settings)
    media_fetcher = make_media_fetcher(settings)
    transcriber = make_transcriber(settings)
    event_bus = EventBus()
    runner = PipelineRunner(
        session_factory, blob_store, backend, settings, web_backend=web_backend, event_bus=event_bus,
        media_fetcher=media_fetcher, transcriber=transcriber,
    )

    app = FastAPI()
    app.state.pairing_token = pairing_token
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.blob_store = blob_store
    app.state.event_bus = event_bus
    app.state.runner = runner
    app.state.settings = settings
    app.state.media_fetcher = media_fetcher

    # Registered before CORS/CSRF below, which (Starlette adds each new
    # middleware *outside* the previous one) makes it the innermost of the
    # three -- it still runs ahead of every route, which is all that
    # matters: a rebound request never reaches a handler.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=_ALLOWED_HOSTS)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def _csrf_guard(request: Request, call_next):
        path = request.url.path
        if (
            request.method in _CSRF_GUARDED_METHODS
            and path.startswith("/api/")
            and not path.startswith(_CSRF_EXEMPT_PREFIX)
            and request.headers.get(_CSRF_HEADER) != "1"
        ):
            return JSONResponse({"detail": f"missing {_CSRF_HEADER} header"}, status_code=403)
        return await call_next(request)

    @app.get("/api/health")
    def health(request: Request) -> dict:
        return {
            "status": "ok",
            "version": __version__,
            "paired": _is_paired(request, pairing_token),
        }

    app.include_router(ingest_router)
    app.include_router(courses_router)
    app.include_router(graph_router)
    app.include_router(materials_router)
    app.include_router(media_router)
    app.include_router(pipeline_router)
    app.include_router(enrichment_router)
    app.include_router(events_router)
    app.include_router(settings_router)
    app.include_router(taxonomy_router)

    if FRONTEND_DIST.exists():

        @app.get("/{full_path:path}")
        async def spa_fallback(full_path: str) -> Response:
            return _spa_or_static_response(full_path)

    return app


def _spa_or_static_response(full_path: str) -> Response:
    """The Task-1 minor this task's brief ledgers: `StaticFiles(html=True)`
    only auto-serves `index.html` for a path that resolves to an actual
    *directory* on disk (see starlette.staticfiles.StaticFiles.get_response)
    -- it does nothing for a client-side route like `/courses/3`, which is
    neither a file nor a directory under `frontend/dist`, so it 404s on a
    hard refresh or a shared deep link instead of handing off to the SPA.

    Registered as a catch-all GET route (below `/api/*`'s routers in
    registration order, so those always win the match first): a path that
    resolves to a real file under `frontend/dist` (assets, favicon, etc.) is
    served as-is; a path with no file extension in its last segment falls
    back to `index.html` (the SPA shell, which client-side-routes from
    there); anything else (a typo'd asset URL, or `/api/*` -- excluded
    explicitly since this route would otherwise shadow FastAPI's own 404
    for an unknown API path) 404s.
    """
    if full_path == "api" or full_path.startswith("api/"):
        raise HTTPException(status_code=404)

    dist_root = FRONTEND_DIST.resolve()
    candidate = (dist_root / full_path).resolve()
    if candidate.is_relative_to(dist_root) and candidate.is_file():
        return FileResponse(candidate)

    last_segment = full_path.rsplit("/", 1)[-1]
    if "." not in last_segment:
        return FileResponse(dist_root / "index.html")

    raise HTTPException(status_code=404)


def _is_paired(request: Request, pairing_token: str) -> bool:
    auth = request.headers.get("Authorization", "")
    prefix = "Bearer "
    if not auth.startswith(prefix):
        return False
    token = auth[len(prefix) :]
    return secrets.compare_digest(token, pairing_token)


def cli() -> None:
    # Uvicorn only configures its own loggers; without a root handler the
    # app's INFO logs (pipeline progress, web-search cost accounting) are
    # silently dropped. basicConfig is a no-op if a handler already exists,
    # and uvicorn's loggers don't propagate, so nothing is double-printed.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = Settings()
    uvicorn.run(create_app(), host=settings.host, port=settings.port)
