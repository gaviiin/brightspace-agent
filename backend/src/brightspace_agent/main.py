"""FastAPI app factory and CLI entry point."""

import secrets
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from brightspace_agent import __version__
from brightspace_agent.config import Settings, ensure_data_dir

# backend/src/brightspace_agent/main.py -> repo root
REPO_ROOT = Path(__file__).resolve().parents[3]
FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"


def create_app() -> FastAPI:
    settings = Settings()
    config = ensure_data_dir(settings)
    pairing_token = config["pairing_token"]

    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    def health(request: Request) -> dict:
        return {
            "status": "ok",
            "version": __version__,
            "paired": _is_paired(request, pairing_token),
        }

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
