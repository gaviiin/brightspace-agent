"""`GET /api/courses/{id}/graph`: `graph.build.build_graph()`'s output,
verbatim -- the keys are already the frontend's camelCase contract (see
graph/build.py's own docstring), so this endpoint is a thin HTTP wrapper
with no reshaping of its own.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from brightspace_agent.api.deps import get_session
from brightspace_agent.graph.build import build_graph

router = APIRouter(prefix="/api/courses", tags=["graph"])


@router.get("/{course_id}/graph")
def get_course_graph(course_id: int, session: Session = Depends(get_session)) -> dict:
    try:
        return build_graph(session, course_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="unknown course") from None
