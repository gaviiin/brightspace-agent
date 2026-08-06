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
