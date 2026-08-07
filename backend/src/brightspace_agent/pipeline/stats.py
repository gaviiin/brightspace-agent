"""`StageStats`: the counters every pipeline stage returns.

One dataclass shared by every stage rather than one per stage, so the runner
(Task 9) can sum, log, and stream progress without knowing which stage it is
looking at. Each stage fills only the counters that apply to it; the rest
stay at zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class StageStats:
    # S1 summarize
    extracted: int = 0
    summarized: int = 0

    # S2 taxonomy. `topics`/`edges` count rows *written*, so they are 0 on an
    # unchanged run; `unchanged` says the proposal matched the taxonomy the
    # course already has, so no new version was created.
    topics: int = 0
    edges: int = 0
    taxonomy_version: int = 0
    unchanged: bool = False
    # S2 declined to propose at all because the course's current taxonomy
    # contains at least one user-authored topic: re-proposing would
    # overwrite a student's own edit with the agent's map. Distinct from
    # `unchanged` (which means "the agent proposed, and it matched"), so
    # "we skipped you" is never mistaken for "we agreed with you". Cleared
    # by an explicit forceTaxonomy run -- see run_taxonomy_stage's `force`.
    skipped_user_taxonomy: bool = False

    # S3 classify
    classified: int = 0  # materials that got at least one assignment
    assignments: int = 0  # material_topics rows written
    unassigned: int = 0  # materials the model (validly) placed nowhere

    # M3 enrich. `enriched` counts enrichment_resources rows written (inserted
    # or updated in place) across the run -- 0 when every topic was a clean
    # cache hit with its rows already present. `thin_topics` counts topics that
    # still had fewer than `target_min` verified resources after the one
    # allowed planner retry: reported honestly rather than padded, so the UI
    # can say "we couldn't find much for this one" instead of showing filler.
    enriched: int = 0
    thin_topics: int = 0

    # Every stage
    cached_hits: int = 0
    failed: int = 0
    usage_total: dict[str, float] = field(
        default_factory=lambda: {"input_tokens": 0, "output_tokens": 0, "est_cost_usd": 0.0}
    )

    # Task 9's cost cap (see pipeline/runner.py + the `cost_cap_usd` kwarg on
    # run_summarize_stage/run_classify_stage): True if the stage stopped its
    # worklist early because accumulated spend reached the cap. The
    # remaining worklist items are left untouched (not marked failed) so a
    # later run retries them.
    aborted: bool = False

    def add_usage(self, usage: dict) -> None:
        self.usage_total["input_tokens"] += usage["input_tokens"]
        self.usage_total["output_tokens"] += usage["output_tokens"]
        self.usage_total["est_cost_usd"] += usage["est_cost_usd"]
