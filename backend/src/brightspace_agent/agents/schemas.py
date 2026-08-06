"""Structured-output schemas for LLM calls.

These are the `schema` argument to `LLMBackend.structured_call` -- passed
straight to LangChain's `with_structured_output` for the real backend, and
used as a registry key by `MockBackend` (see agents/llm.py).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class DocSummary(BaseModel):
    """S1 summarize-stage output: one course material, summarized."""

    summary: str  # 5-10 lines: topics covered, level, what it's for
    doc_kind_guess: Literal[
        "syllabus",
        "slides",
        "document",
        "assignment",
        "announcement",
        "video",
        "transcript",
        "link",
        "other",
    ]
    key_terms: list[str] = Field(default_factory=list, max_length=10)


# --------------------------------------------------------------------------
# S2 taxonomy: one smart-model call proposes the whole course topic map.
#
# Nothing here is trusted as-is: pipeline/stages/taxonomy.py normalizes slugs,
# drops duplicates and dangling/self edges, and fails the stage outright on a
# degenerate proposal. Constraints are stated in the field docs (and at length
# in prompts/taxonomy.md) so the model aims at the right shape; validation in
# code is what actually holds the line.
# --------------------------------------------------------------------------


class TopicDef(BaseModel):
    """One topic in a course's taxonomy."""

    slug: str  # kebab-case, unique within the taxonomy
    name: str
    description: str  # 1-3 sentences: what this topic covers in THIS course
    module_hints: list[str] = Field(default_factory=list)  # source module titles


class TopicEdgeDef(BaseModel):
    """A directed relationship between two topics.

    For `prerequisite`, direction matters: `from_slug` must be understood
    *before* `to_slug`. `related` is symmetric and should be emitted once.
    """

    from_slug: str
    to_slug: str
    relation: Literal["prerequisite", "related"]


class TaxonomyOut(BaseModel):
    """S2 taxonomy-stage output: the proposed topic map for one course."""

    topics: list[TopicDef]  # aim 8-20
    edges: list[TopicEdgeDef] = Field(default_factory=list)


# --------------------------------------------------------------------------
# S3 classify: one cheap-model call per material against a fixed taxonomy.
# --------------------------------------------------------------------------


class TopicAssignment(BaseModel):
    """One material-to-topic assignment proposed by the classifier."""

    topic_slug: str
    confidence: float  # 0-1: how central the material is to the topic
    rationale: str  # one line, citing evidence from the material's summary


class ClassificationOut(BaseModel):
    """S3 classify-stage output for one material. An empty list is a valid
    answer -- the material is then filed as unsorted by S4."""

    assignments: list[TopicAssignment] = Field(default_factory=list)  # 1-3 typical
