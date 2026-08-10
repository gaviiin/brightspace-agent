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

-- M2.1: lecture-recording URLs (Mediasite/Zoom/Google Drive) the detector
-- (media/detect.py) finds in already-synced materials -- link materials'
-- source_url, plus hrefs inside HTML page materials. One row per distinct
-- (course, url); later M2 tasks drive status through fetching/transcribing
-- to done/failed/skipped and point transcript_material_id at the transcript
-- they produce. IF NOT EXISTS: also reapplied as migration 3 for databases
-- that predate this table (see migrate.py).
--
-- M2.6a: `material_id` is nullable -- a manually-added URL/channel entry
-- (api/media.py's POST .../media/add) has no backing `materials` row (the
-- whole point: it's for a recording the sync couldn't see in the first
-- place, e.g. one sitting behind an LTI-embedded channel). Databases that
-- predate this are brought up to it by migration 4, a table rebuild (SQLite
-- can't drop a NOT NULL constraint in place).
CREATE TABLE IF NOT EXISTS media_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    material_id INTEGER REFERENCES materials(id) ON DELETE CASCADE,
    platform TEXT NOT NULL CHECK(platform IN ('mediasite','zoom','gdrive')),
    url TEXT NOT NULL,
    passcode TEXT,
    status TEXT NOT NULL CHECK(status IN ('detected','fetching','transcribing','done','failed','skipped')) DEFAULT 'detected',
    error TEXT,
    transcript_material_id INTEGER REFERENCES materials(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(course_id, url)
);

-- M2.7: one row per material recording an LTI-launch resolution attempt --
-- the extension performs the launch (the D2L ToC only ever gives us the LTI
-- quicklink stub; the real Mediasite/Zoom URL only materializes once a
-- logged-in browser actually launches it), reads the final URL, and POSTs it
-- here. `launch_url` is the quicklink `classify_url` structurally can't see
-- through; `final_url`/`platform` are populated once a launch lands
-- somewhere, NULL for a `failed` attempt (tab closed, off-origin launch URL,
-- no final URL at all). `status`: 'resolved' (final_url classified to a
-- supported platform -- media_sources rows follow via the same
-- expand-and-upsert path manual-add uses), 'unrecognized' (a real landing
-- page, just not a supported platform -- diagnostic gold for the drawer,
-- not an error), 'failed' (no usable final_url; re-offered on the next
-- sync, unlike the other two statuses). UNIQUE(material_id): one row per
-- material, overwritten on every re-resolution. IF NOT EXISTS: also
-- reapplied as migration 7 for databases that predate this table (see
-- migrate.py).
CREATE TABLE IF NOT EXISTS lti_resolutions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    material_id INTEGER NOT NULL REFERENCES materials(id) ON DELETE CASCADE,
    launch_url TEXT NOT NULL,
    final_url TEXT,
    platform TEXT,
    status TEXT NOT NULL CHECK(status IN ('resolved','unrecognized','failed')),
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(material_id)
);

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
    -- M3.5b: 'inherited' is the recording-topic-inheritance post-pass's
    -- method (pipeline/stages/classify.py's `_inherit_recording_topics`) --
    -- a row mirrored onto a recording's source material from its
    -- transcript's own assignment, not the model's or a user's own
    -- judgment. Databases that predate this are brought up to it by
    -- migration 6 (SQLite can't ALTER a CHECK constraint in place).
    method TEXT NOT NULL CHECK(method IN ('llm','embedding','user','inherited')) DEFAULT 'llm',
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

-- One row per (topic, url): the enrich stage upserts by this pair, and the
-- index makes "never duplicated" structural rather than a promise the upsert
-- has to keep on its own. IF NOT EXISTS so this DDL is safe both here (fresh
-- db, migration 1) and as migration 2 for already-migrated databases.
CREATE UNIQUE INDEX IF NOT EXISTS ux_enrichment_topic_url ON enrichment_resources(topic_id, url);

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
