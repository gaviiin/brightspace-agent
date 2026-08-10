"""Tests for the SQLite persistence layer: schema, migrations, ORM models."""

import sqlite3

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from brightspace_agent.db.migrate import MIGRATIONS, migrate
from brightspace_agent.db.models import Course, EnrichmentResource, Material, Module, Topic
from brightspace_agent.db.session import init_db

EXPECTED_TABLES = {
    "courses",
    "modules",
    "materials",
    "topics",
    "topic_edges",
    "material_topics",
    "media_sources",
    "enrichment_resources",
    "domain_reputation",
    "sync_runs",
    "pipeline_runs",
    "llm_cache",
}


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "brightspace.db"


def _make_course(**overrides):
    defaults = dict(d2l_org_unit_id=1, tenant_origin="example.d2l.com", name="Intro to CS")
    defaults.update(overrides)
    return Course(**defaults)


def test_init_db_creates_all_tables_and_sets_latest_user_version(db_path):
    init_db(db_path)

    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]

    assert EXPECTED_TABLES <= tables
    assert user_version == MIGRATIONS[-1][0]


def test_migrate_called_twice_is_a_noop(db_path):
    conn = sqlite3.connect(db_path)
    try:
        migrate(conn)
        first_version = conn.execute("PRAGMA user_version").fetchone()[0]

        migrate(conn)  # second call must not error and must not change anything
        second_version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()

    assert first_version == MIGRATIONS[-1][0]
    assert second_version == first_version


def test_foreign_key_enforced_bogus_course_id_raises(db_path):
    _, Session = init_db(db_path)

    with Session() as session:
        session.add(Material(course_id=999999, title="orphan material"))
        with pytest.raises(IntegrityError):
            session.commit()


def test_course_module_material_roundtrip_uses_column_defaults(db_path):
    _, Session = init_db(db_path)

    with Session() as session:
        course = _make_course()
        session.add(course)
        session.flush()

        module = Module(course_id=course.id, d2l_module_id=10, title="Week 1")
        session.add(module)
        session.flush()

        material = Material(course_id=course.id, module_id=module.id, title="Syllabus")
        session.add(material)
        session.commit()
        material_id = material.id

    # Fresh session/query to prove the defaults came from the DB, not Python state.
    with Session() as session:
        fetched = session.get(Material, material_id)
        assert fetched.kind == "other"
        assert fetched.status == "fetched"


def test_llm_cache_composite_primary_key_duplicate_insert_raises(db_path):
    _, Session = init_db(db_path)
    row = dict(
        sha256="abc123",
        stage="summarize",
        prompt_version="v1",
        model="gpt-test",
        output_json="{}",
        created_at="2026-01-01T00:00:00Z",
    )
    insert_sql = text(
        "INSERT INTO llm_cache (sha256, stage, prompt_version, model, output_json, created_at) "
        "VALUES (:sha256, :stage, :prompt_version, :model, :output_json, :created_at)"
    )

    with Session() as session:
        session.execute(insert_sql, row)
        session.commit()

    with Session() as session:
        with pytest.raises(IntegrityError):
            session.execute(insert_sql, row)


def test_materials_partial_unique_index_on_course_and_topic(db_path):
    _, Session = init_db(db_path)

    with Session() as session:
        course = _make_course(d2l_org_unit_id=2, name="Bio 101")
        session.add(course)
        session.commit()
        course_id = course.id

    with Session() as session:
        session.add(Material(course_id=course_id, d2l_topic_id=5, title="Lecture 1"))
        session.commit()

    with Session() as session:
        session.add(Material(course_id=course_id, d2l_topic_id=5, title="Lecture 1 duplicate"))
        with pytest.raises(IntegrityError):
            session.commit()

    with Session() as session:
        session.add(Material(course_id=course_id, d2l_topic_id=None, title="No topic A"))
        session.add(Material(course_id=course_id, d2l_topic_id=None, title="No topic B"))
        session.commit()  # both NULL d2l_topic_id rows must insert fine

    with Session() as session:
        count = session.execute(
            text(
                "SELECT COUNT(*) FROM materials WHERE course_id = :cid AND d2l_topic_id IS NULL"
            ),
            {"cid": course_id},
        ).scalar_one()
        assert count == 2


# --------------------------------------------------------------------------
# M3.2 folded-in hardening: migration 2 (enrichment_resources(topic_id, url)
# unique index) must dedup existing duplicate rows BEFORE creating the
# index, so a database that somehow already has a duplicate (topic_id, url)
# row -- e.g. one written before the M3.2 runner made this table's write
# path live -- migrates cleanly instead of raising IntegrityError and
# aborting startup.
# --------------------------------------------------------------------------


def test_migration_2_dedups_duplicate_topic_url_rows_before_creating_unique_index(db_path):
    # Simulate a v1 database (pre-M3.1: enrichment_resources exists, but
    # without the unique index) that already has a duplicate (topic_id, url)
    # pair -- built from raw SQL, not schema.sql, since schema.sql (as of
    # M3.1) already creates the index for a fresh database and so can't be
    # used to reproduce the pre-hardening scenario this test targets.
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            BEGIN;
            CREATE TABLE enrichment_resources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic_id INTEGER NOT NULL,
                url TEXT NOT NULL,
                title TEXT
            );
            INSERT INTO enrichment_resources (topic_id, url, title)
                VALUES (1, 'https://example.com/a', 'first');
            INSERT INTO enrichment_resources (topic_id, url, title)
                VALUES (1, 'https://example.com/a', 'duplicate');
            INSERT INTO enrichment_resources (topic_id, url, title)
                VALUES (1, 'https://example.com/b', 'other');
            PRAGMA user_version = 1;
            COMMIT;
            """
        )

        migrate(conn)  # applies migration 2+ on top of this v1 db

        rows = conn.execute(
            "SELECT id, title FROM enrichment_resources WHERE topic_id = 1 AND url = 'https://example.com/a'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][1] == "first"  # lowest id survives

        total = conn.execute("SELECT COUNT(*) FROM enrichment_resources").fetchone()[0]
        assert total == 2  # the duplicate was removed; the distinct url kept

        index_row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'ux_enrichment_topic_url'"
        ).fetchone()
        assert index_row is not None

        assert conn.execute("PRAGMA user_version").fetchone()[0] == MIGRATIONS[-1][0]
    finally:
        conn.close()


# --------------------------------------------------------------------------
# M2.1's migration 3 (media_sources). Same shape as migration 2's test above:
# a database that predates the table must gain it on migrate(), and a second
# migrate() must change nothing.
# --------------------------------------------------------------------------


def _table_columns(conn, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def test_migration_3_adds_media_sources_to_a_v2_database(db_path, tmp_path):
    # A v2 database: everything schema.sql builds EXCEPT media_sources (the
    # table migration 3 exists to add), at user_version = 2. Built by
    # applying schema.sql and dropping that one table rather than by
    # hand-copying its DDL -- schema.sql is the source of truth for a fresh
    # database and already creates media_sources, so this is the only way to
    # reproduce the pre-M2.1 state without a second copy of the schema
    # drifting out of sync with it.
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(MIGRATIONS[0][1])
        conn.executescript("BEGIN;\nDROP TABLE media_sources;\nPRAGMA user_version = 2;\nCOMMIT;\n")
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2

        migrate(conn)

        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'media_sources'"
        ).fetchone()
        assert exists is not None
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert MIGRATIONS[-1][0] == 3  # nothing newer has been appended without updating this test

        # The migrated table must match what a fresh database gets from
        # schema.sql -- a migration that produces a differently-shaped table
        # is worse than no migration at all.
        fresh_path = tmp_path / "fresh.db"
        init_db(fresh_path)
        fresh_conn = sqlite3.connect(fresh_path)
        try:
            assert _table_columns(conn, "media_sources") == _table_columns(fresh_conn, "media_sources")
        finally:
            fresh_conn.close()

        # Usable, and a second migrate() leaves both the schema and the data
        # exactly as they are.
        conn.executescript(
            """
            INSERT INTO courses (d2l_org_unit_id, tenant_origin, name)
                VALUES (1, 'school.d2l.com', 'Intro to CS');
            INSERT INTO materials (course_id, title) VALUES (1, 'Lecture 1');
            INSERT INTO media_sources (course_id, material_id, platform, url, created_at, updated_at)
                VALUES (1, 1, 'zoom', 'https://zoom.us/rec/share/abc',
                        '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00');
            """
        )

        migrate(conn)

        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        row = conn.execute("SELECT url, status FROM media_sources").fetchone()
        assert row == ("https://zoom.us/rec/share/abc", "detected")
    finally:
        conn.close()


def test_enrichment_resources_unique_index_on_topic_and_url(db_path):
    """Mirrors test_materials_partial_unique_index_on_course_and_topic: a
    raw duplicate (topic_id, url) insert must raise IntegrityError now that
    migration 2's index is live."""
    _, Session = init_db(db_path)

    with Session() as session:
        course = _make_course()
        session.add(course)
        session.commit()
        course_id = course.id

    with Session() as session:
        topic = Topic(course_id=course_id, taxonomy_version=1, slug="intro", name="Intro")
        session.add(topic)
        session.commit()
        topic_id = topic.id

    with Session() as session:
        session.add(EnrichmentResource(topic_id=topic_id, url="https://example.com/a"))
        session.commit()

    with Session() as session:
        session.add(EnrichmentResource(topic_id=topic_id, url="https://example.com/a"))
        with pytest.raises(IntegrityError):
            session.commit()
