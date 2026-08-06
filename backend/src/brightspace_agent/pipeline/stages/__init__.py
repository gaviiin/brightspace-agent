"""Individual pipeline stages (S1 summarize, ...). Each stage exposes a
single async `run_*_stage(session_factory, blob_store, ..., course_id, *,
concurrency=4, progress=None) -> StageStats` entry point."""
