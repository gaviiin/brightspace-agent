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

    # S3 classify
    classified: int = 0  # materials that got at least one assignment
    assignments: int = 0  # material_topics rows written
    unassigned: int = 0  # materials the model (validly) placed nowhere

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
