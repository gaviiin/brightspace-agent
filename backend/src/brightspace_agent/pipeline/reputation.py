"""Domain-reputation helpers for the enrich stage.

`domain_reputation` is a tiny learned prior: each time a student keeps or
dismisses a suggested link, the resource's domain gets a tally, and future
enrichment nudges that domain's ranking accordingly. The nudge is deliberately
small and bounded -- it is a tie-breaker between comparably-good resources, not
an override of the judge's rubric -- and it decays with volume via a smoothing
constant so three keeps out of three don't swing as hard as thirty out of
thirty.

The read (`bias_for`) and write (`record_feedback`) live together here so the
one place that understands the counts-to-bias formula also owns the upsert.
M3.2's keep/dismiss endpoint calls `record_feedback`; the enrich stage calls
`bias_for`.
"""

from __future__ import annotations

from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from brightspace_agent.db.models import DomainReputation

# Max absolute nudge, and the smoothing constant that makes a small sample
# count for less than a large one. `0.1 * (kept - dismissed) / (kept +
# dismissed + 3)` asymptotes toward +/-0.1 and never leaves [-0.1, +0.1], well
# inside the [-0.15, +0.15] envelope the rubric axes can absorb.
_BIAS_WEIGHT = 0.1
_BIAS_SMOOTHING = 3


def domain_of(url: str) -> str:
    """The lowercased registrable-ish host of `url`, with a leading `www.`
    stripped, so `https://WWW.Khan Academy...`-style variants of one site
    collapse to a single reputation key. Falls back to the raw (lowercased)
    string when `url` has no parseable host, so a malformed URL still keys
    consistently rather than raising."""
    host = urlparse(url).hostname or url
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def bias_for(session: Session, domain: str) -> float:
    """A small signed ranking nudge in [-0.1, +0.1] for `domain`, 0.0 when the
    domain has no feedback yet. Positive means students have kept links from
    this domain more than they have dismissed them."""
    row = session.get(DomainReputation, domain)
    if row is None:
        return 0.0
    kept, dismissed = row.kept_count, row.dismissed_count
    total = kept + dismissed
    if total == 0:
        return 0.0
    return _BIAS_WEIGHT * (kept - dismissed) / (total + _BIAS_SMOOTHING)


def record_feedback(session: Session, domain: str, kept: bool) -> None:
    """Upsert one keep/dismiss into `domain`'s tally. Does not commit -- the
    caller owns the transaction."""
    column = "kept_count" if kept else "dismissed_count"
    session.execute(
        sqlite_insert(DomainReputation)
        .values(
            domain=domain,
            kept_count=1 if kept else 0,
            dismissed_count=0 if kept else 1,
        )
        .on_conflict_do_update(
            index_elements=["domain"],
            set_={column: getattr(DomainReputation, column) + 1},
        )
    )
