-- Schema for the BrightSpace Agent SQLite database.
-- This file is the DDL source of truth; migrate.py applies it as migration 1.
-- All timestamps are TEXT, ISO-8601 UTC. PRAGMA foreign_keys=ON is set
-- per-connection in session.py, not here.

CREATE TABLE courses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    d2l_org_unit_id INTEGER NOT NULL UNIQUE,
    tenant_origin TEXT NOT NULL,
    name TEXT NOT NULL,
    code TEXT,
    term TEXT,
    toc_json TEXT,
    taxonomy_version INTEGER NOT NULL DEFAULT 0,
    last_synced_at TEXT
);

CREATE TABLE modules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    d2l_module_id INTEGER NOT NULL,
    parent_id INTEGER REFERENCES modules(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    UNIQUE(course_id, d2l_module_id)
);

CREATE TABLE materials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    module_id INTEGER REFERENCES modules(id) ON DELETE SET NULL,
    d2l_topic_id INTEGER,
    kind TEXT NOT NULL CHECK(kind IN ('syllabus','slides','document','assignment','announcement','video','transcript','link','other')) DEFAULT 'other',
    title TEXT NOT NULL,
    source_url TEXT,
    sha256 TEXT,
    mime TEXT,
    size_bytes INTEGER,
    d2l_updated_at TEXT,
    fetched_at TEXT,
    summary TEXT,
    summary_meta_json TEXT,
    status TEXT NOT NULL CHECK(status IN ('fetched','extracted','summarized','failed')) DEFAULT 'fetched',
    error TEXT
);

CREATE UNIQUE INDEX ux_materials_course_topic ON materials(course_id, d2l_topic_id) WHERE d2l_topic_id IS NOT NULL;

CREATE TABLE topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    taxonomy_version INTEGER NOT NULL,
    slug TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    order_index INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL CHECK(created_by IN ('agent','user')) DEFAULT 'agent',
    UNIQUE(course_id, taxonomy_version, slug)
);

CREATE TABLE topic_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    from_topic_id INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    to_topic_id INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    relation TEXT NOT NULL CHECK(relation IN ('prerequisite','related')),
    created_by TEXT NOT NULL DEFAULT 'agent',
    UNIQUE(from_topic_id, to_topic_id, relation)
);

CREATE TABLE material_topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    material_id INTEGER NOT NULL REFERENCES materials(id) ON DELETE CASCADE,
    topic_id INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    taxonomy_version INTEGER NOT NULL,
    confidence REAL,
    rationale TEXT,
    method TEXT NOT NULL CHECK(method IN ('llm','embedding','user')) DEFAULT 'llm',
    review_status TEXT NOT NULL CHECK(review_status IN ('auto','confirmed','rejected')) DEFAULT 'auto',
    UNIQUE(material_id, topic_id, taxonomy_version)
);

CREATE TABLE enrichment_resources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    title TEXT,
    resource_type TEXT,
    intent TEXT,
    rationale TEXT,
    scores_json TEXT,
    verification_json TEXT,
    shared INTEGER NOT NULL DEFAULT 0,
    rank INTEGER,
    status TEXT NOT NULL CHECK(status IN ('suggested','kept','dismissed')) DEFAULT 'suggested'
);

CREATE TABLE domain_reputation (
    domain TEXT PRIMARY KEY,
    kept_count INTEGER NOT NULL DEFAULT 0,
    dismissed_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    source TEXT NOT NULL CHECK(source IN ('extension','zip')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK(status IN ('running','complete','failed')) DEFAULT 'running',
    stats_json TEXT
);

CREATE TABLE pipeline_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    stage TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running','complete','failed','aborted')) DEFAULT 'running',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    usage_json TEXT,
    error TEXT
);

CREATE TABLE llm_cache (
    sha256 TEXT NOT NULL,
    stage TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    model TEXT NOT NULL,
    output_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(sha256, stage, prompt_version, model)
);
