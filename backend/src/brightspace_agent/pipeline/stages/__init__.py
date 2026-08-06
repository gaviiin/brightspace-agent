"""Individual pipeline stages (S1 summarize, S2 taxonomy, S3 classify, ...).

Each stage exposes a single async `run_*_stage(session_factory, ..., course_id,
*, ...) -> StageStats` entry point (see pipeline/stats.py) and owns its own
sessions: a stage never receives one, so it is free to fan out across threads.
Stages that fan out take `concurrency` and `progress` keyword arguments.

Per-item problems are recorded in `StageStats.failed` and the stage carries
on; only a failure that invalidates the whole stage (see
`taxonomy.TaxonomyStageError`) raises.
"""
