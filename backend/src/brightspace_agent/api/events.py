"""`GET /api/events`: an SSE stream of the runner's event bus (pipeline
progress from pipeline/runner.py, plus sync-completion events from
api/ingest.py). A GET, so it stays open to browser + CORS rules like every
other read endpoint -- no CSRF header required (see main.py's CSRF guard).
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from sse_starlette.sse import EventSourceResponse

from brightspace_agent.api.deps import get_event_bus
from brightspace_agent.pipeline.runner import EventBus

router = APIRouter(prefix="/api", tags=["events"])

_HEARTBEAT_SECONDS = 15


@router.get("/events")
async def stream_events(request: Request, event_bus: EventBus = Depends(get_event_bus)) -> EventSourceResponse:
    queue = event_bus.subscribe()

    async def event_source():
        try:
            while True:
                event = await queue.get()
                yield {"event": "message", "data": json.dumps(event)}
        finally:
            event_bus.unsubscribe(queue)

    # `ping` makes sse-starlette send a `: ping` comment on this interval
    # whenever no real event has gone out -- the heartbeat the brief asks
    # for, and what keeps idle proxies/browsers from timing the connection
    # out.
    return EventSourceResponse(event_source(), ping=_HEARTBEAT_SECONDS)
