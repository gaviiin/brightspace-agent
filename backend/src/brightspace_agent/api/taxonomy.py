"""`PUT /api/courses/{id}/taxonomy` (Task 12): the taxonomy editor's save
endpoint. A PUT, and therefore covered by the CSRF guard in main.py (require
`X-BSA-Request: 1`) -- it can write a new taxonomy version and trigger a
real pipeline run (LLM spend).

Thin by design: this module parses the request body and maps
`TaxonomyValidationError` -> 422 / `RunActiveError` -> 409. The
patch-vs-structural decision, carry-over, and slug rules all live in
`pipeline/taxonomy_apply.py`.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from sqlalchemy.orm import Session

from brightspace_agent.api.deps import get_runner, get_session
from brightspace_agent.db.models import Course
from brightspace_agent.pipeline.runner import PipelineRunner, RunActiveError
from brightspace_agent.pipeline.taxonomy_apply import (
    EdgeEditIn,
    TaxonomyValidationError,
    TopicEditIn,
    apply_taxonomy_edit,
)

router = APIRouter(prefix="/api/courses", tags=["taxonomy"])


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class TopicEditRequest(CamelModel):
    id: int | None
    name: str
    description: str = ""
    merged_from_topic_ids: list[int] = Field(default_factory=list)


class EdgeEditRequest(CamelModel):
    from_index: int
    to_index: int
    relation: Literal["prerequisite", "related"]


class TaxonomyEditRequest(CamelModel):
    topics: list[TopicEditRequest]
    edges: list[EdgeEditRequest] = Field(default_factory=list)


class TaxonomyApplyOut(CamelModel):
    taxonomy_version: int
    reclassify: bool
    run_token: int | None = None


def _get_course_or_404(session: Session, course_id: int) -> Course:
    course = session.get(Course, course_id)
    if course is None:
        raise HTTPException(status_code=404, detail="unknown course")
    return course


@router.put("/{course_id}/taxonomy", response_model=TaxonomyApplyOut)
async def put_taxonomy(
    course_id: int,
    payload: TaxonomyEditRequest,
    session: Session = Depends(get_session),
    runner: PipelineRunner = Depends(get_runner),
) -> TaxonomyApplyOut:
    course = _get_course_or_404(session, course_id)

    topics = [
        TopicEditIn(
            id=topic.id, name=topic.name, description=topic.description,
            merged_from_topic_ids=list(topic.merged_from_topic_ids),
        )
        for topic in payload.topics
    ]
    edges = [
        EdgeEditIn(from_index=edge.from_index, to_index=edge.to_index, relation=edge.relation)
        for edge in payload.edges
    ]

    try:
        result = apply_taxonomy_edit(session, runner, course, topics, edges)
    except TaxonomyValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc
    except RunActiveError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return TaxonomyApplyOut(
        taxonomy_version=result.taxonomy_version, reclassify=result.reclassify, run_token=result.run_token
    )
