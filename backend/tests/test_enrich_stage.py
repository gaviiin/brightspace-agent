"""Tests for the M3 enrich stage (pipeline/stages/enrich.py): the per-topic
planner -> finders -> verifiers -> judge -> dedup/reputation pipeline and the
batch entry point. All against MockBackend / MockWebBackend or small stub
backends -- no network, no API key.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import select

from brightspace_agent.agents.llm import MockBackend
from brightspace_agent.agents.schemas import (
    Candidate,
    CandidateList,
    JudgedResource,
    JudgeResult,
    SearchIntent,
    SearchPlan,
    Verification,
)
from brightspace_agent.agents.web import MockWebBackend
from brightspace_agent.db.models import Course, EnrichmentResource, Material, MaterialTopic, Topic
from brightspace_agent.db.session import init_db
from brightspace_agent.pipeline.reputation import record_feedback
from brightspace_agent.pipeline.stages.enrich import run_enrich_stage, run_topic_enrichment


# --------------------------------------------------------------------------
# Fixtures + seeding
# --------------------------------------------------------------------------


@pytest.fixture
def session_factory(tmp_path):
    return init_db(tmp_path / "brightspace.db")[1]


@pytest.fixture
def course_id(session_factory):
    with session_factory() as session:
        course = Course(
            d2l_org_unit_id=1,
            tenant_origin="school.d2l.com",
            name="Data Structures and Algorithms",
            code="CS 2110",
            taxonomy_version=1,
        )
        session.add(course)
        session.commit()
        return course.id


def _seed_topic(
    session_factory,
    course_id,
    *,
    version=1,
    slug="breadth-first-search",
    name="Breadth-First Search",
    description="Layer-by-layer graph traversal with a FIFO queue.",
    order_index=0,
) -> int:
    with session_factory() as session:
        topic = Topic(
            course_id=course_id,
            taxonomy_version=version,
            slug=slug,
            name=name,
            description=description,
            order_index=order_index,
        )
        session.add(topic)
        session.commit()
        return topic.id


def _attach_material(session_factory, course_id, topic_id, *, version=1, title, summary):
    with session_factory() as session:
        material = Material(
            course_id=course_id,
            kind="slides",
            title=title,
            mime="text/plain",
            sha256=f"sha-{title.lower().replace(' ', '-')}",
            summary=summary,
            status="summarized",
        )
        session.add(material)
        session.flush()
        session.add(
            MaterialTopic(
                material_id=material.id,
                topic_id=topic_id,
                taxonomy_version=version,
                confidence=0.9,
                rationale="core material",
                method="llm",
            )
        )
        session.commit()
        return material.id


@pytest.fixture
def topic_id(session_factory, course_id):
    tid = _seed_topic(session_factory, course_id)
    _attach_material(
        session_factory, course_id, tid,
        title="Lecture 7 BFS", summary="Covers BFS on unweighted graphs, queue frontier, shortest paths.",
    )
    _attach_material(
        session_factory, course_id, tid,
        title="BFS Problem Set", summary="Practice implementing BFS and level-order traversal.",
    )
    return tid


def _rows(session_factory, topic_id) -> list[EnrichmentResource]:
    with session_factory() as session:
        rows = list(
            session.execute(
                select(EnrichmentResource)
                .where(EnrichmentResource.topic_id == topic_id)
                .order_by(EnrichmentResource.rank)
            ).scalars().all()
        )
        for row in rows:
            session.expunge(row)
        return rows


def _run_topic(session_factory, backend, web_backend, topic_id, **kwargs):
    return asyncio.run(
        run_topic_enrichment(session_factory, backend, web_backend, topic_id, **kwargs)
    )


# --------------------------------------------------------------------------
# Test doubles
# --------------------------------------------------------------------------


def _zero_usage(model):
    return {"model": model, "input_tokens": 0, "output_tokens": 0, "est_cost_usd": 0.0}


class _CountingLLM:
    def __init__(self, inner) -> None:
        self._inner = inner
        self.plan_calls = 0
        self.judge_calls = 0

    def structured_call(self, schema, *, system, user, tier):
        if schema is SearchPlan:
            self.plan_calls += 1
        elif schema is JudgeResult:
            self.judge_calls += 1
        return self._inner.structured_call(schema, system=system, user=user, tier=tier)

    def model_for_tier(self, tier):
        return self._inner.model_for_tier(tier)


class _CountingWeb:
    def __init__(self, inner) -> None:
        self._inner = inner
        self.find_calls = 0
        self.verify_calls = 0

    def find(self, *, system, user, tier):
        self.find_calls += 1
        return self._inner.find(system=system, user=user, tier=tier)

    def verify(self, *, system, user, tier):
        self.verify_calls += 1
        return self._inner.verify(system=system, user=user, tier=tier)


class _StubLLM:
    def __init__(self, *, plan, judge) -> None:
        self._plan = plan
        self._judge = judge
        self.plan_calls = 0
        self.judge_calls = 0

    def structured_call(self, schema, *, system, user, tier):
        usage = _zero_usage(self.model_for_tier(tier))
        if schema is SearchPlan:
            self.plan_calls += 1
            return (self._plan(user) if callable(self._plan) else self._plan), usage
        if schema is JudgeResult:
            self.judge_calls += 1
            return (self._judge(user) if callable(self._judge) else self._judge), usage
        raise AssertionError(f"unexpected schema {schema!r}")

    def model_for_tier(self, tier):
        return f"stub-{tier}"


class _StubWeb:
    def __init__(self, *, find, verify) -> None:
        self._find = find
        self._verify = verify
        self.find_calls = 0
        self.verify_calls = 0

    def find(self, *, system, user, tier):
        self.find_calls += 1
        result = self._find(user) if callable(self._find) else self._find
        return result, _zero_usage(f"stub-{tier}")

    def verify(self, *, system, user, tier):
        self.verify_calls += 1
        result = self._verify(user) if callable(self._verify) else self._verify
        return result, _zero_usage(f"stub-{tier}")


class _RaisingForTopicWeb:
    """Delegates to `inner`, but raises whenever the (finder) prompt mentions
    `boom_name` -- so run_enrich_stage's per-topic isolation can be exercised."""

    def __init__(self, inner, boom_name) -> None:
        self._inner = inner
        self._boom = boom_name

    def find(self, *, system, user, tier):
        if self._boom in user:
            raise RuntimeError("web search exploded")
        return self._inner.find(system=system, user=user, tier=tier)

    def verify(self, *, system, user, tier):
        return self._inner.verify(system=system, user=user, tier=tier)


# --------------------------------------------------------------------------
# (1) Happy path
# --------------------------------------------------------------------------


def test_happy_path_writes_ranked_suggested_resources(session_factory, topic_id):
    stats = _run_topic(session_factory, MockBackend(), MockWebBackend(), topic_id)

    rows = _rows(session_factory, topic_id)
    assert 3 <= len(rows) <= 5
    for row in rows:
        assert row.status == "suggested"
        assert row.url and row.title and row.resource_type and row.intent
        assert row.rationale
    # Ranks are contiguous 1..N.
    assert [row.rank for row in rows] == list(range(1, len(rows) + 1))
    # scores_json carries the full rubric.
    scores = json.loads(rows[0].scores_json)
    for axis in ("relevance", "authority", "recency", "level_match", "pedagogical_value"):
        assert axis in scores
    # verification evidence is stored.
    verification = json.loads(rows[0].verification_json)
    assert verification["ok"] is True
    assert verification["evidence_quote"]
    # Format diversity: the mock offers variety, so the kept set is not all one type.
    assert len({row.resource_type for row in rows}) > 1
    assert stats.enriched == len(rows)
    assert stats.thin_topics == 0
    assert stats.failed == 0


def test_planner_prompt_carries_topic_and_material_context(session_factory, topic_id):
    counting = _CountingLLM(MockBackend())
    captured = {}
    inner_call = counting.structured_call

    def _capture(schema, *, system, user, tier):
        captured.setdefault(schema.__name__, user)
        return inner_call(schema, system=system, user=user, tier=tier)

    counting.structured_call = _capture  # type: ignore[method-assign]
    _run_topic(session_factory, counting, MockWebBackend(), topic_id)

    plan_prompt = captured["SearchPlan"]
    assert "Breadth-First Search" in plan_prompt
    assert "CS 2110" in plan_prompt
    assert "queue frontier" in plan_prompt  # an attached material's summary


# --------------------------------------------------------------------------
# (2) Verification filters out a paywalled candidate
# --------------------------------------------------------------------------


def test_paywall_candidate_never_lands_in_results(session_factory, topic_id):
    _run_topic(session_factory, MockBackend(), MockWebBackend(), topic_id)

    rows = _rows(session_factory, topic_id)
    assert rows  # something survived
    assert all("paywall" not in row.url for row in rows)


# --------------------------------------------------------------------------
# (3) Cache reuse: second run on an unchanged topic makes 0 new calls, no dupes
# --------------------------------------------------------------------------


def test_second_run_reuses_cache_no_new_calls_no_dupes(session_factory, topic_id):
    llm = _CountingLLM(MockBackend())
    web = _CountingWeb(MockWebBackend())

    _run_topic(session_factory, llm, web, topic_id)
    first_count = len(_rows(session_factory, topic_id))
    assert first_count > 0
    assert llm.plan_calls >= 1
    assert web.find_calls >= 1

    calls_before = (llm.plan_calls, llm.judge_calls, web.find_calls, web.verify_calls)
    stats2 = _run_topic(session_factory, llm, web, topic_id)
    calls_after = (llm.plan_calls, llm.judge_calls, web.find_calls, web.verify_calls)

    assert calls_after == calls_before  # no new LLM or web calls
    assert stats2.cached_hits == 1
    assert len(_rows(session_factory, topic_id)) == first_count  # no duplicate rows


# --------------------------------------------------------------------------
# (4) Thin-topic retry
# --------------------------------------------------------------------------


def test_thin_topic_retries_planner_once_and_records_count(session_factory, topic_id):
    # A finder that always returns candidates, and a verifier that rejects
    # everything -> nothing survives either round -> thin topic.
    candidates = CandidateList(
        candidates=[
            Candidate(
                url="https://random-blog.example/bfs",
                title="Blog",
                resource_type="article",
                intent="alternative_explanation",
                claimed_coverage="bfs",
                why="maybe",
            )
        ]
    )
    rejected = Verification(
        ok=False, accessible=False, on_topic=False, level_fit="unknown",
        evidence_quote="", reason="dead link",
    )
    llm = _CountingLLM(MockBackend())
    web = _StubWeb(find=candidates, verify=rejected)

    stats = _run_topic(session_factory, llm, web, topic_id)

    assert llm.plan_calls == 2  # initial plan + exactly one retry
    assert stats.thin_topics == 1
    assert stats.failed == 0
    assert _rows(session_factory, topic_id) == []  # nothing padded in


# --------------------------------------------------------------------------
# (5) Reputation bias: a dismissed domain ranks below an equal-scored kept one
# --------------------------------------------------------------------------


def test_dismissed_domain_ranks_below_equal_scored_kept_domain(session_factory, topic_id):
    url_kept = "https://ocw.mit.edu/bfs-notes"
    url_dismissed = "https://notes.cmu.edu/bfs-notes"

    def _find(_user):
        return CandidateList(
            candidates=[
                Candidate(
                    url=url_kept, title="MIT Notes", resource_type="notes",
                    intent="university_notes", claimed_coverage="bfs", why="mit",
                ),
                Candidate(
                    url=url_dismissed, title="CMU Notes", resource_type="notes",
                    intent="university_notes", claimed_coverage="bfs", why="cmu",
                ),
            ]
        )

    ok = Verification(
        ok=True, accessible=True, on_topic=True, level_fit="on_level",
        evidence_quote="breadth first search", reason="good",
    )

    equal_scores = {
        "relevance": 0.7, "authority": 0.7, "recency": 0.7,
        "level_match": 0.7, "pedagogical_value": 0.7,
    }

    def _judge(_user):
        return JudgeResult(
            resources=[
                JudgedResource(
                    url=url_kept, title="MIT Notes", resource_type="notes",
                    intent="university_notes", keep=True, rank=1,
                    rationale="clear", scores=dict(equal_scores),
                ),
                JudgedResource(
                    url=url_dismissed, title="CMU Notes", resource_type="notes",
                    intent="university_notes", keep=True, rank=2,
                    rationale="clear", scores=dict(equal_scores),
                ),
            ]
        )

    plan = SearchPlan(
        intents=[SearchIntent(intent="university_notes", query="bfs notes", rationale="notes")]
    )
    llm = _StubLLM(plan=plan, judge=_judge)
    web = _StubWeb(find=_find, verify=ok)

    # Seed reputation: MIT kept-heavy (positive bias), CMU dismissed-heavy.
    with session_factory() as session:
        for _ in range(5):
            record_feedback(session, "ocw.mit.edu", kept=True)
            record_feedback(session, "notes.cmu.edu", kept=False)
        session.commit()

    _run_topic(session_factory, llm, web, topic_id)

    rows = _rows(session_factory, topic_id)
    by_url = {row.url: row.rank for row in rows}
    assert by_url[url_kept] < by_url[url_dismissed]


# --------------------------------------------------------------------------
# (6) Per-topic isolation in run_enrich_stage
# --------------------------------------------------------------------------


def test_run_enrich_stage_isolates_a_failing_topic(session_factory, course_id):
    good = _seed_topic(session_factory, course_id, slug="breadth-first-search", name="Breadth-First Search")
    _seed_topic(session_factory, course_id, slug="boom-topic", name="Boom Topic", order_index=1)

    web = _RaisingForTopicWeb(MockWebBackend(), boom_name="Boom Topic")

    stats = asyncio.run(run_enrich_stage(session_factory, MockBackend(), web, course_id))

    assert stats.failed == 1  # the boom topic
    assert _rows(session_factory, good)  # the healthy topic was still enriched
