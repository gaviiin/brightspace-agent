"""The shared vocabulary of the *user-message payload* the pipeline stages
send to the LLM: section markers, slug normalization, and a reader for
pulling one section back out.

Two very different callers need to agree on this format, which is why it
lives in its own dependency-free module rather than inside a stage:

- the stages (pipeline/stages/taxonomy.py, classify.py) *write* the payload
- MockBackend's builders (agents/llm.py) *read* it back, so the offline mock
  can produce output that actually responds to the prompt it was given

Sections are plain `=== NAME ===` lines. That is deliberately dumber than
XML/JSON: it survives being pasted next to arbitrary document text (which
may itself contain markup), and it reads cleanly to a human debugging a
prompt dump.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable

# Section markers -- taxonomy (S2)
SECTION_COURSE = "=== COURSE ==="
SECTION_SYLLABUS = "=== SYLLABUS ==="
SECTION_MODULE_OUTLINE = "=== MODULE OUTLINE ==="
SECTION_MATERIAL_SUMMARIES = "=== MATERIAL SUMMARIES ==="

# Section markers -- classify (S3)
SECTION_COURSE_TOPICS = "=== COURSE TOPICS ==="
SECTION_MATERIAL = "=== MATERIAL ==="

_SECTION_RE = re.compile(r"^===\s.*\s===$")
_NON_ASCII_SLUG_CHARS = re.compile(r"[^a-zA-Z0-9]+")
_WHITESPACE = re.compile(r"\s+")

TAXONOMY_DIGEST_CHARS = 12


def render_topic_block(topics: Iterable[tuple[str, str, str | None]]) -> str:
    """The numbered `N. slug — name — description` list of a taxonomy.

    Written once and used three ways, which is exactly why it lives here:
    S3 puts it in every classify prompt, S3's cache key is its digest, and
    S2 digests the same rendering of a *proposed* taxonomy to decide whether
    anything actually changed. Those three have to agree character for
    character or the cache silently misbehaves.
    """
    return "\n".join(
        f"{index}. {slug} — {name} — {description or name}"
        for index, (slug, name, description) in enumerate(topics, start=1)
    )


def taxonomy_digest(block: str) -> str:
    """A short content fingerprint of a rendered taxonomy block."""
    return hashlib.sha256(block.encode("utf-8")).hexdigest()[:TAXONOMY_DIGEST_CHARS]


def slugify(value: str) -> str:
    """Normalize `value` into a kebab-case slug.

    Accents are folded (`Données` -> `donnees`) and everything that isn't
    alphanumeric collapses to a single hyphen. For scripts that leave
    nothing behind after ASCII folding (e.g. Chinese, Arabic), the original
    characters are kept and only whitespace is normalized -- an unreadable
    empty slug would be worse than a non-Latin one.
    """
    folded = unicodedata.normalize("NFKD", value)
    without_marks = "".join(char for char in folded if not unicodedata.combining(char))
    slug = _NON_ASCII_SLUG_CHARS.sub("-", without_marks).strip("-").lower()
    if slug:
        return slug
    return _WHITESPACE.sub("-", value.strip()).strip("-").lower()


def section_body(prompt: str, header: str) -> str:
    """The text under `header` in `prompt`, up to the next section marker.

    Returns "" when the section isn't present. Used by MockBackend builders
    to react to the actual prompt content instead of ignoring it.
    """
    lines = prompt.splitlines()
    try:
        start = lines.index(header) + 1
    except ValueError:
        return ""

    collected: list[str] = []
    for line in lines[start:]:
        if _SECTION_RE.match(line.strip()):
            break
        collected.append(line)
    return "\n".join(collected).strip("\n")
