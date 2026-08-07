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


# --------------------------------------------------------------------------
# M3 enrichment: a five-step per-topic link-research team turns one course
# topic into a handful of verified supplementary web resources. Each step has
# its own schema so the (structured) planner/judge calls and the (web-tool)
# finder/verifier calls all coerce to a known shape -- and, as everywhere
# else in this project, nothing here is trusted as-is: pipeline/stages/
# enrich.py re-verifies, dedups, applies domain-reputation bias, and caps the
# kept set. The field docs state the intent; the stage code holds the line.
# --------------------------------------------------------------------------


IntentType = Literal[
    "alternative_explanation",  # a different route through the same idea
    "video_lecture",  # a recorded lecture / explainer
    "worked_examples",  # solved problems, problem sets with solutions
    "interactive_visualization",  # a simulator / animated demo
    "university_notes",  # course notes / lecture handouts from a university
    "past_exams",  # exams, quizzes, practice tests with answers
]


class SearchIntent(BaseModel):
    """One typed thing the planner wants found for a topic. `query` is written
    in the course's own terminology, never a generic web query."""

    intent: IntentType
    query: str  # grounded in the topic's own terminology
    rationale: str  # one line: why this intent for this topic


class SearchPlan(BaseModel):
    """Planner output: a small, diverse set of typed search intents."""

    intents: list[SearchIntent] = Field(min_length=1, max_length=6)


class Candidate(BaseModel):
    """One resource a finder proposes for a single intent. Not trusted until a
    verifier fetches the URL -- `claimed_coverage`/`why` are the finder's
    read of the search result, not established fact."""

    url: str
    title: str
    resource_type: str  # e.g. "video", "article", "notes", "problem_set", "interactive"
    intent: IntentType
    claimed_coverage: str  # what the finder thinks it covers
    why: str  # one line


class CandidateList(BaseModel):
    """A finder's proposals for one intent."""

    candidates: list[Candidate] = Field(max_length=8)


class Verification(BaseModel):
    """A verifier's verdict on one fetched candidate. `ok` is the AND of the
    other gates -- live, on-topic, accessible, and level-appropriate -- and
    `evidence_quote` must come from the fetched page, not the search snippet."""

    ok: bool  # live AND on-topic AND accessible AND level-appropriate
    accessible: bool  # not paywalled/login-walled/dead
    on_topic: bool
    level_fit: Literal["too_basic", "on_level", "too_advanced", "unknown"]
    evidence_quote: str  # <=25 words quoted from the fetched page proving on-topic
    reason: str


class JudgedResource(BaseModel):
    """The judge's rubric verdict on one verified candidate. `rank` is only
    meaningful when `keep=True`; the stage re-ranks after applying
    domain-reputation bias, so this rank is advisory."""

    url: str
    title: str
    resource_type: str
    intent: IntentType
    keep: bool
    rank: int  # 1 = best; only meaningful when keep=True
    rationale: str  # one line, doubles as UI copy
    # rubric axes in [0, 1]: relevance, authority, recency, level_match,
    # pedagogical_value. `dict` (not a fixed model) so the prompt can evolve
    # the rubric without a schema change; enrich.py reads axes defensively.
    scores: dict[str, float]


class JudgeResult(BaseModel):
    """Judge output for one topic: a verdict per verified candidate."""

    resources: list[JudgedResource]
