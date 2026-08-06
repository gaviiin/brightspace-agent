"""FastAPI app factory and CLI entry point."""

import secrets
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from brightspace_agent import __version__
from brightspace_agent.agents.llm import make_backend
from brightspace_agent.api.courses import router as courses_router
from brightspace_agent.api.events import router as events_router
from brightspace_agent.api.graph import router as graph_router
from brightspace_agent.api.ingest import router as ingest_router
from brightspace_agent.api.materials import router as materials_router
from brightspace_agent.api.pipeline import router as pipeline_router
from brightspace_agent.config import Settings, ensure_data_dir
from brightspace_agent.db.session import init_db
from brightspace_agent.ingest.store import BlobStore
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


def create_app() -> FastAPI:
    settings = Settings()
    config = ensure_data_dir(settings)
    pairing_token = config["pairing_token"]

    engine, session_factory = init_db(settings.db_path)
    blob_store = BlobStore(settings.blobs_dir, settings.text_dir)
    backend = make_backend(settings)
    event_bus = EventBus()
    runner = PipelineRunner(session_factory, blob_store, backend, settings, event_bus=event_bus)

    app = FastAPI()
    app.state.pairing_token = pairing_token
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.blob_store = blob_store
    app.state.event_bus = event_bus
    app.state.runner = runner

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
    app.include_router(pipeline_router)
    app.include_router(events_router)

    if FRONTEND_DIST.exists():
        app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")

    return app


def _is_paired(request: Request, pairing_token: str) -> bool:
    auth = request.headers.get("Authorization", "")
    prefix = "Bearer "
    if not auth.startswith(prefix):
        return False
    token = auth[len(prefix) :]
    return secrets.compare_digest(token, pairing_token)


def cli() -> None:
    settings = Settings()
    uvicorn.run(create_app(), host=settings.host, port=settings.port)
