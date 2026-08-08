"""Tests for pipeline/reputation.py: domain extraction, the signed keep/
dismiss bias, and the domain_reputation upsert. All against a tmp SQLite db;
no network, no API key.
"""

from __future__ import annotations

import pytest

from brightspace_agent.db.models import DomainReputation
from brightspace_agent.db.session import init_db
from brightspace_agent.pipeline.reputation import bias_for, domain_of, record_feedback


@pytest.fixture
def session_factory(tmp_path):
    return init_db(tmp_path / "brightspace.db")[1]


# --------------------------------------------------------------------------
# domain_of
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://ocw.mit.edu/6-006/notes", "ocw.mit.edu"),
        ("https://www.khanacademy.org/x", "khanacademy.org"),  # leading www. stripped
        ("http://Cs.Stanford.EDU/Lectures", "cs.stanford.edu"),  # lowercased
        ("https://youtube.com/watch?v=abc", "youtube.com"),
    ],
)
def test_domain_of(url, expected):
    assert domain_of(url) == expected


# --------------------------------------------------------------------------
# bias_for
# --------------------------------------------------------------------------


def test_bias_is_zero_for_unseen_domain(session_factory):
    with session_factory() as session:
        assert bias_for(session, "ocw.mit.edu") == 0.0


def test_bias_is_positive_when_mostly_kept(session_factory):
    with session_factory() as session:
        for _ in range(5):
            record_feedback(session, "ocw.mit.edu", kept=True)
        session.commit()
        assert bias_for(session, "ocw.mit.edu") > 0.0


def test_bias_is_negative_when_mostly_dismissed(session_factory):
    with session_factory() as session:
        for _ in range(5):
            record_feedback(session, "spam.example", kept=False)
        session.commit()
        assert bias_for(session, "spam.example") < 0.0


def test_bias_stays_within_bounds(session_factory):
    with session_factory() as session:
        for _ in range(100):
            record_feedback(session, "ocw.mit.edu", kept=True)
        session.commit()
        assert 0.0 < bias_for(session, "ocw.mit.edu") <= 0.15


# --------------------------------------------------------------------------
# record_feedback upsert
# --------------------------------------------------------------------------


def test_record_feedback_upserts_counts(session_factory):
    with session_factory() as session:
        record_feedback(session, "ocw.mit.edu", kept=True)
        record_feedback(session, "ocw.mit.edu", kept=True)
        record_feedback(session, "ocw.mit.edu", kept=False)
        session.commit()

    with session_factory() as session:
        row = session.get(DomainReputation, "ocw.mit.edu")
        assert row.kept_count == 2
        assert row.dismissed_count == 1
