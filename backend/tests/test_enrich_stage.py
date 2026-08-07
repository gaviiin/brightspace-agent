"""Tests for the M3 enrich stage (pipeline/stages/enrich.py): the per-topic
planner -> finders -> verifiers -> judge -> dedup/reputation pipeline and the
batch entry point. All against MockBackend / MockWebBackend or small stub
backends -- no network, no API key.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import delete, select

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
from brightspace_agent.db.models import (
    Course,
    EnrichmentResource,
    LlmCache,
    Material,
    MaterialTopic,
    Topic,
)
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


def test_ok_verdict_without_evidence_quote_is_not_written(session_factory, topic_id):
    # The verifier gates on FETCHED evidence: an ok=True verdict that produced
    # no evidence quote has not actually established on-topic and must not land.
    candidates = CandidateList(
        candidates=[
            Candidate(
                url="https://ocw.mit.edu/bfs-notes", title="MIT Notes",
                resource_type="notes", intent="university_notes",
                claimed_coverage="bfs", why="mit",
            )
        ]
    )
    ok_but_no_evidence = Verification(
        ok=True, accessible=True, on_topic=True, level_fit="on_level",
        evidence_quote="   ", reason="looked fine but quoted nothing from the page",
    )
    web = _StubWeb(find=candidates, verify=ok_but_no_evidence)

    stats = _run_topic(session_factory, MockBackend(), web, topic_id)

    assert _rows(session_factory, topic_id) == []
    assert stats.enriched == 0


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


def test_rerun_after_cache_miss_never_duplicates_urls(session_factory, topic_id):
    _run_topic(session_factory, MockBackend(), MockWebBackend(), topic_id)
    first = _rows(session_factory, topic_id)
    urls_first = sorted(row.url for row in first)
    assert urls_first  # something was written

    # Force a cache miss so the full pipeline -- and its upsert-by-(topic,url) --
    # runs a second time rather than replaying the cached payload.
    with session_factory() as session:
        session.execute(delete(LlmCache).where(LlmCache.stage == "enrich"))
        session.commit()

    _run_topic(session_factory, MockBackend(), MockWebBackend(), topic_id)
    second = _rows(session_factory, topic_id)
    urls_second = sorted(row.url for row in second)

    assert urls_second == urls_first  # same URLs, updated in place
    assert len(urls_second) == len(set(urls_second))  # no duplicate URLs
    assert len(second) == len(first)  # no new rows


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


# --------------------------------------------------------------------------
# (7) Cost cap: an aborted run must not poison the cache (or prune)
# --------------------------------------------------------------------------


class _CostlyLLM:
    """Delegates to `inner` but reports a real dollar cost per call, so the
    optimistic cost cap actually trips. `model_for_tier` is `inner`'s, so the
    llm_cache key matches what an uncapped `inner`-driven run would use."""

    def __init__(self, inner, cost_per_call: float) -> None:
        self._inner = inner
        self._cost = cost_per_call
        self.calls = 0

    def structured_call(self, schema, *, system, user, tier):
        self.calls += 1
        parsed, _ = self._inner.structured_call(schema, system=system, user=user, tier=tier)
        return parsed, {
            "model": self.model_for_tier(tier),
            "input_tokens": 1000,
            "output_tokens": 500,
            "est_cost_usd": self._cost,
        }

    def model_for_tier(self, tier):
        return self._inner.model_for_tier(tier)


class _CostlyWeb:
    def __init__(self, inner, cost_per_call: float) -> None:
        self._inner = inner
        self._cost = cost_per_call
        self.calls = 0

    def _usage(self, tier):
        return {
            "model": f"mock-{tier}",
            "input_tokens": 1000,
            "output_tokens": 500,
            "est_cost_usd": self._cost,
        }

    def find(self, *, system, user, tier):
        self.calls += 1
        result, _ = self._inner.find(system=system, user=user, tier=tier)
        return result, self._usage(tier)

    def verify(self, *, system, user, tier):
        self.calls += 1
        result, _ = self._inner.verify(system=system, user=user, tier=tier)
        return result, self._usage(tier)


def _enrich_cache_rows(session_factory) -> list[LlmCache]:
    with session_factory() as session:
        rows = list(
            session.execute(select(LlmCache).where(LlmCache.stage == "enrich")).scalars().all()
        )
        for row in rows:
            session.expunge(row)
        return rows


def test_cost_cap_abort_writes_no_cache_row(session_factory, topic_id):
    # Each paid call costs $0.50 against a $0.60 cap: the planner alone
    # exhausts it, so the run aborts partway and produces nothing usable.
    llm = _CostlyLLM(MockBackend(), 0.5)
    web = _CostlyWeb(MockWebBackend(), 0.5)

    stats = _run_topic(session_factory, llm, web, topic_id, cost_cap_usd=0.6)

    assert stats.aborted is True
    # THE bug this guards: caching an aborted run makes every later run a
    # "successful" cache hit replaying the empty result -- raising the cap and
    # pressing Refresh would then silently do nothing.
    assert _enrich_cache_rows(session_factory) == []
    # An aborted run is not "we searched and there's nothing good".
    assert stats.thin_topics == 0


def test_uncapped_rerun_after_an_abort_actually_re_enriches(session_factory, topic_id):
    llm = _CostlyLLM(MockBackend(), 0.5)
    web = _CostlyWeb(MockWebBackend(), 0.5)
    aborted = _run_topic(session_factory, llm, web, topic_id, cost_cap_usd=0.6)
    assert aborted.aborted is True
    assert _rows(session_factory, topic_id) == []

    # Same models (so the same cache key), no cap this time.
    second_llm = _CountingLLM(MockBackend())
    second_web = _CountingWeb(MockWebBackend())
    stats = _run_topic(session_factory, second_llm, second_web, topic_id)

    assert stats.cached_hits == 0  # it really re-ran rather than replaying
    assert second_web.find_calls > 0
    rows = _rows(session_factory, topic_id)
    assert rows
    assert stats.enriched == len(rows)
    assert len(_enrich_cache_rows(session_factory)) == 1  # now it's cached


def test_cost_cap_abort_leaves_earlier_suggestions_alone(session_factory, topic_id):
    # A good run first...
    _run_topic(session_factory, MockBackend(), MockWebBackend(), topic_id)
    before = {row.url for row in _rows(session_factory, topic_id)}
    assert before

    # ...then a capped run that aborts. Its (partial, empty) result must not
    # be treated as "these suggestions are stale now".
    with session_factory() as session:
        session.execute(delete(LlmCache).where(LlmCache.stage == "enrich"))
        session.commit()
    stats = _run_topic(
        session_factory,
        _CostlyLLM(MockBackend(), 0.5),
        _CostlyWeb(MockWebBackend(), 0.5),
        topic_id,
        cost_cap_usd=0.6,
    )

    assert stats.aborted is True
    assert {row.url for row in _rows(session_factory, topic_id)} == before


# --------------------------------------------------------------------------
# (8) URL safety: only http(s) is ever stored or accepted
# --------------------------------------------------------------------------


_UNSAFE_URLS = ["javascript:alert(document.cookie)", "data:text/html,<script>x</script>", "file:///etc/passwd"]


@pytest.mark.parametrize("bad_url", _UNSAFE_URLS)
def test_schema_rejects_non_http_urls(bad_url):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Candidate(
            url=bad_url, title="Evil", resource_type="article",
            intent="alternative_explanation", claimed_coverage="x", why="y",
        )
    with pytest.raises(ValidationError):
        JudgedResource(
            url=bad_url, title="Evil", resource_type="article",
            intent="alternative_explanation", keep=True, rank=1, rationale="x", scores={},
        )


def test_javascript_url_candidate_is_never_written(session_factory, topic_id):
    # A prompt-injected page could steer a finder into emitting a javascript:
    # URL. `model_construct` bypasses the schema validator on purpose here, so
    # this exercises the WRITE-time backstop rather than re-testing the schema.
    evil = Candidate.model_construct(
        url="javascript:alert(1)", title="Totally Legit Notes", resource_type="notes",
        intent="university_notes", claimed_coverage="bfs", why="trust me",
    )
    good = Candidate(
        url="https://ocw.mit.edu/bfs-notes", title="MIT Notes", resource_type="notes",
        intent="university_notes", claimed_coverage="bfs", why="mit",
    )
    ok = Verification(
        ok=True, accessible=True, on_topic=True, level_fit="on_level",
        evidence_quote="breadth first search", reason="good",
    )

    def _judge(_user):
        return JudgeResult.model_construct(
            resources=[
                JudgedResource.model_construct(
                    url=candidate.url, title=candidate.title, resource_type="notes",
                    intent="university_notes", keep=True, rank=rank, rationale="ok",
                    scores={"relevance": 0.9, "authority": 0.9, "recency": 0.9,
                            "level_match": 0.9, "pedagogical_value": 0.9},
                )
                for rank, candidate in enumerate((evil, good), start=1)
            ]
        )

    plan = SearchPlan(intents=[SearchIntent(intent="university_notes", query="bfs notes", rationale="notes")])
    llm = _StubLLM(plan=plan, judge=_judge)
    web = _StubWeb(find=CandidateList.model_construct(candidates=[evil, good]), verify=ok)

    _run_topic(session_factory, llm, web, topic_id)

    urls = [row.url for row in _rows(session_factory, topic_id)]
    assert urls == ["https://ocw.mit.edu/bfs-notes"]
    assert not any(url.startswith("javascript:") for url in urls)


def test_unsafe_url_in_a_cached_payload_is_not_written(session_factory, topic_id):
    # Covers a cache row written before the URL rules existed: the replay path
    # goes through the same write-time guard.
    _run_topic(session_factory, MockBackend(), MockWebBackend(), topic_id)
    with session_factory() as session:
        row = session.execute(select(LlmCache).where(LlmCache.stage == "enrich")).scalar_one()
        payload = json.loads(row.output_json)
        payload["resources"].append(
            {
                "url": "javascript:alert(1)", "title": "Evil", "resource_type": "notes",
                "intent": "university_notes", "rationale": "evil",
                "scores": {"relevance": 1.0}, "verification": {"ok": True}, "rank": 99,
            }
        )
        row.output_json = json.dumps(payload)
        session.commit()

    with session_factory() as session:
        session.execute(delete(EnrichmentResource).where(EnrichmentResource.topic_id == topic_id))
        session.commit()

    stats = _run_topic(session_factory, MockBackend(), MockWebBackend(), topic_id)

    assert stats.cached_hits == 1
    assert all(row.url.startswith("https://") for row in _rows(session_factory, topic_id))


# --------------------------------------------------------------------------
# (9) Stale 'suggested' rows are pruned; student decisions never are
# --------------------------------------------------------------------------


_OK_VERIFICATION = Verification(
    ok=True, accessible=True, on_topic=True, level_fit="on_level",
    evidence_quote="breadth first search", reason="good",
)


def _candidates(*urls) -> CandidateList:
    return CandidateList(
        candidates=[
            Candidate(
                url=url, title=f"Notes {index}", resource_type="notes",
                intent="university_notes", claimed_coverage="bfs", why="edu",
            )
            for index, url in enumerate(urls, start=1)
        ]
    )


def _force_cache_miss(session_factory):
    with session_factory() as session:
        session.execute(delete(LlmCache).where(LlmCache.stage == "enrich"))
        session.commit()


def test_stale_suggestions_are_pruned_on_the_next_run(session_factory, topic_id):
    old = ("https://old-a.mit.edu/notes", "https://old-b.mit.edu/notes")
    new = ("https://new-a.mit.edu/notes",)

    _run_topic(
        session_factory, MockBackend(), _StubWeb(find=_candidates(*old), verify=_OK_VERIFICATION), topic_id
    )
    assert sorted(row.url for row in _rows(session_factory, topic_id)) == sorted(old)

    _force_cache_miss(session_factory)
    _run_topic(
        session_factory, MockBackend(), _StubWeb(find=_candidates(*new), verify=_OK_VERIFICATION), topic_id
    )

    rows = _rows(session_factory, topic_id)
    assert [row.url for row in rows] == list(new)  # no stale rows interleaved
    assert [row.rank for row in rows] == [1]


def test_prune_never_deletes_kept_or_dismissed_rows(session_factory, topic_id):
    old = ("https://old-a.mit.edu/notes", "https://old-b.mit.edu/notes", "https://old-c.mit.edu/notes")
    _run_topic(
        session_factory, MockBackend(), _StubWeb(find=_candidates(*old), verify=_OK_VERIFICATION), topic_id
    )
    with session_factory() as session:
        rows = list(
            session.execute(
                select(EnrichmentResource).where(EnrichmentResource.topic_id == topic_id)
                .order_by(EnrichmentResource.url)
            ).scalars().all()
        )
        rows[0].status = "kept"
        rows[1].status = "dismissed"
        session.commit()

    _force_cache_miss(session_factory)
    _run_topic(
        session_factory,
        MockBackend(),
        _StubWeb(find=_candidates("https://fresh.mit.edu/notes"), verify=_OK_VERIFICATION),
        topic_id,
    )

    by_url = {row.url: row.status for row in _rows(session_factory, topic_id)}
    assert by_url == {
        "https://old-a.mit.edu/notes": "kept",  # student decision survives
        "https://old-b.mit.edu/notes": "dismissed",  # so does this one
        "https://fresh.mit.edu/notes": "suggested",
    }
    assert "https://old-c.mit.edu/notes" not in by_url  # un-actioned + stale -> pruned


def test_a_run_that_finds_nothing_clears_stale_suggestions(session_factory, topic_id):
    _run_topic(
        session_factory,
        MockBackend(),
        _StubWeb(find=_candidates("https://old.mit.edu/notes"), verify=_OK_VERIFICATION),
        topic_id,
    )
    assert _rows(session_factory, topic_id)

    _force_cache_miss(session_factory)
    rejected = Verification(
        ok=False, accessible=False, on_topic=False, level_fit="unknown", evidence_quote="", reason="dead",
    )
    stats = _run_topic(
        session_factory,
        MockBackend(),
        _StubWeb(find=_candidates("https://old.mit.edu/notes"), verify=rejected),
        topic_id,
    )

    assert stats.thin_topics == 1
    assert _rows(session_factory, topic_id) == []


# --------------------------------------------------------------------------
# (10) Cross-topic dedup: one URL, one topic, `shared` actually means something
# --------------------------------------------------------------------------


def _rows_for_course(session_factory, course_id) -> list[EnrichmentResource]:
    with session_factory() as session:
        rows = list(
            session.execute(
                select(EnrichmentResource)
                .join(Topic, Topic.id == EnrichmentResource.topic_id)
                .where(Topic.course_id == course_id)
                .order_by(EnrichmentResource.url, EnrichmentResource.topic_id)
            ).scalars().all()
        )
        for row in rows:
            session.expunge(row)
        return rows


class _ScoringLLM:
    """MockBackend's planner, but a judge that scores each URL from a supplied
    table -- so a cross-topic duplicate can be made to score better on one
    topic than the other."""

    def __init__(self, scores_by_url: dict[str, float]) -> None:
        self._inner = MockBackend()
        self._scores = scores_by_url

    def structured_call(self, schema, *, system, user, tier):
        if schema is not JudgeResult:
            return self._inner.structured_call(schema, system=system, user=user, tier=tier)
        judged, usage = self._inner.structured_call(schema, system=system, user=user, tier=tier)
        for resource in judged.resources:
            value = self._scores.get(resource.url, 0.5)
            resource.scores = {axis: value for axis in
                               ("relevance", "authority", "recency", "level_match", "pedagogical_value")}
        return judged, usage

    def model_for_tier(self, tier):
        return self._inner.model_for_tier(tier)


_SHARED_URL = "https://ocw.mit.edu/shared-lecture"


def _seed_two_topics_sharing_a_url(session_factory, course_id):
    first = _seed_topic(session_factory, course_id, slug="bfs", name="Breadth-First Search")
    second = _seed_topic(session_factory, course_id, slug="dfs", name="Depth-First Search", order_index=1)
    return first, second


def _shared_url_web():
    """Every topic's finder proposes the same shared URL plus a topic-specific
    one, so exactly one URL is contested."""

    def _find(user):
        topic = "bfs" if "Breadth" in user else "dfs"
        return _candidates(_SHARED_URL, f"https://ocw.mit.edu/{topic}-only")

    return _StubWeb(find=_find, verify=_OK_VERIFICATION)


def test_batch_keeps_a_shared_url_on_its_best_fit_topic_only(session_factory, course_id):
    first, second = _seed_two_topics_sharing_a_url(session_factory, course_id)
    # The shared URL scores better while judging the SECOND topic's prompt --
    # emulated here by scoring per URL and giving the loser topic's own URL a
    # lower score, so the tie-break is exercised by topic id rather than luck.
    llm = _ScoringLLM({_SHARED_URL: 0.9})

    stats = asyncio.run(run_enrich_stage(session_factory, llm, _shared_url_web(), course_id))

    shared_rows = [row for row in _rows_for_course(session_factory, course_id) if row.url == _SHARED_URL]
    assert len(shared_rows) == 1  # exactly one survivor across the course
    assert shared_rows[0].topic_id == min(first, second)  # deterministic tie-break
    assert shared_rows[0].shared == 1  # and it says so
    assert stats.deduped == 1
    # The topic-specific URLs are untouched.
    other_urls = {row.url for row in _rows_for_course(session_factory, course_id)} - {_SHARED_URL}
    assert other_urls == {"https://ocw.mit.edu/bfs-only", "https://ocw.mit.edu/dfs-only"}


def test_dedup_never_destroys_a_student_decision(session_factory, course_id):
    first, second = _seed_two_topics_sharing_a_url(session_factory, course_id)
    asyncio.run(run_enrich_stage(session_factory, _ScoringLLM({_SHARED_URL: 0.9}), _shared_url_web(), course_id))

    # Re-seed the duplicate by hand and mark the SECOND topic's copy kept: the
    # student has decided this link belongs there.
    with session_factory() as session:
        session.add(
            EnrichmentResource(
                topic_id=second, url=_SHARED_URL, title="Shared", resource_type="notes",
                intent="university_notes", rationale="student kept this here",
                scores_json=json.dumps({"relevance": 0.1}), verification_json="{}", rank=1, status="kept",
            )
        )
        session.commit()

    with session_factory() as session:
        session.execute(delete(LlmCache).where(LlmCache.stage == "enrich"))
        session.commit()
    asyncio.run(run_enrich_stage(session_factory, _ScoringLLM({_SHARED_URL: 0.9}), _shared_url_web(), course_id))

    shared_rows = [row for row in _rows_for_course(session_factory, course_id) if row.url == _SHARED_URL]
    # The kept row wins outright despite its far worse scores, and survives.
    assert [(row.topic_id, row.status) for row in shared_rows] == [(second, "kept")]
    assert shared_rows[0].shared == 1
    assert first  # sanity: the other topic existed and lost its suggested copy


def test_dedup_leaves_a_dismissed_duplicate_alone(session_factory, course_id):
    first, second = _seed_two_topics_sharing_a_url(session_factory, course_id)
    asyncio.run(run_enrich_stage(session_factory, _ScoringLLM({_SHARED_URL: 0.9}), _shared_url_web(), course_id))

    with session_factory() as session:
        session.add(
            EnrichmentResource(
                topic_id=second, url=_SHARED_URL, title="Shared", resource_type="notes",
                intent="university_notes", rationale="student said no",
                scores_json=json.dumps({"relevance": 0.9}), verification_json="{}", rank=1, status="dismissed",
            )
        )
        session.execute(delete(LlmCache).where(LlmCache.stage == "enrich"))
        session.commit()

    asyncio.run(run_enrich_stage(session_factory, _ScoringLLM({_SHARED_URL: 0.9}), _shared_url_web(), course_id))

    shared_rows = [row for row in _rows_for_course(session_factory, course_id) if row.url == _SHARED_URL]
    statuses = {(row.topic_id, row.status) for row in shared_rows}
    assert (second, "dismissed") in statuses  # never deleted, never flipped
    assert all(row.shared == 1 for row in shared_rows)
    assert first  # sanity


def test_a_dismissal_on_one_topic_does_not_hide_the_link_on_another(session_factory, course_id):
    """Dismissing a shared link on topic B must not delete the live suggestion
    on topic A: a dismissal means "not here", not "nowhere"."""
    first, second = _seed_two_topics_sharing_a_url(session_factory, course_id)
    asyncio.run(run_enrich_stage(session_factory, _ScoringLLM({_SHARED_URL: 0.9}), _shared_url_web(), course_id))

    # After the first run the shared URL lives (suggested) on `first` only.
    # The student dismisses a copy of it on `second`.
    with session_factory() as session:
        session.add(
            EnrichmentResource(
                topic_id=second, url=_SHARED_URL, title="Shared", resource_type="notes",
                intent="university_notes", rationale="student said no on this topic",
                scores_json=json.dumps({"relevance": 0.9}), verification_json="{}", rank=1, status="dismissed",
            )
        )
        session.execute(delete(LlmCache).where(LlmCache.stage == "enrich"))
        session.commit()

    asyncio.run(run_enrich_stage(session_factory, _ScoringLLM({_SHARED_URL: 0.9}), _shared_url_web(), course_id))

    shared_rows = [row for row in _rows_for_course(session_factory, course_id) if row.url == _SHARED_URL]
    statuses = {(row.topic_id, row.status) for row in shared_rows}
    # The live suggestion on `first` survives the dismissal on `second`...
    assert (first, "suggested") in statuses
    # ...and the dismissal itself is untouched.
    assert (second, "dismissed") in statuses


def test_single_topic_run_never_marks_anything_shared(session_factory, topic_id):
    stats = _run_topic(session_factory, MockBackend(), MockWebBackend(), topic_id)

    assert stats.deduped == 0
    assert all(row.shared == 0 for row in _rows(session_factory, topic_id))
