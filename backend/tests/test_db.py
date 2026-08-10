"""Tests for the SQLite persistence layer: schema, migrations, ORM models."""

import sqlite3

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from brightspace_agent.db.migrate import MIGRATIONS, check_migration_versions, migrate
from brightspace_agent.db.models import (
    Course,
    EnrichmentResource,
    LtiResolution,
    Material,
    MediaSource,
    Module,
    Topic,
)
from brightspace_agent.db.session import init_db

EXPECTED_TABLES = {
    "courses",
    "modules",
    "materials",
    "topics",
    "topic_edges",
    "material_topics",
    "media_sources",
    "lti_resolutions",
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


def test_duplicate_or_decreasing_migration_versions_raise(db_path):
    """The stacked-branch hazard `check_migration_versions` exists for: two
    branches each append "the next" migration, both pick the same number,
    and the merged list carries it twice. `migrate()` skips anything
    `<= current_version`, so the second entry would never run on a database
    that already reached that version -- surfacing much later as a missing
    column at startup."""
    del db_path  # this guard is pure list validation; no database involved

    check_migration_versions(MIGRATIONS)  # the real list is well-formed

    with pytest.raises(ValueError, match="strictly increasing"):
        check_migration_versions([(1, "SELECT 1;"), (2, "SELECT 1;"), (2, "SELECT 1;")])

    with pytest.raises(ValueError, match="strictly increasing"):
        check_migration_versions([(1, "SELECT 1;"), (3, "SELECT 1;"), (2, "SELECT 1;")])


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
    # used to reproduce the pre-hardening scenario this test targets. Bare
    # `materials`/`material_topics` stubs are included too -- a REAL v1
    # database always has them (schema.sql creates them), and migration 5's
    # ALTER TABLE and migration 6's rebuild both need them to exist when
    # migrate() runs every later migration in this same call.
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            BEGIN;
            CREATE TABLE materials (id INTEGER PRIMARY KEY AUTOINCREMENT);
            CREATE TABLE topics (id INTEGER PRIMARY KEY AUTOINCREMENT);
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
        # migrate() always brings a database to the LATEST version, not just
        # the next one -- from v2 that's migration 3 (adds the table), then
        # migration 4 (M2.6a's material_id-nullable rebuild), then migration
        # 5 (M3.5a's materials.is_administrative column), then migration 6
        # (M3.5b's material_topics.method CHECK widening) in the same call,
        # so the version lands on whatever's newest, not literally 3.
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
        assert MIGRATIONS[-1][0] == 7  # nothing newer has been appended without updating this test

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

        assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
        row = conn.execute("SELECT url, status FROM media_sources").fetchone()
        assert row == ("https://zoom.us/rec/share/abc", "detected")
    finally:
        conn.close()


# --------------------------------------------------------------------------
# M2.6a's migration 4 (media_sources.material_id becomes nullable). Same
# table-rebuild shape as any SQLite "drop a NOT NULL constraint" migration:
# hand-build a v3 database (media_sources still NOT NULL on material_id --
# schema.sql itself already reflects the nullable column as of this task, so
# it can't be used to reproduce the pre-migration-4 state, same reasoning as
# test_migration_2's own hand-built v1 setup above) WITH data rows, including
# one with every nullable column populated, and prove the rebuild carries
# every row across byte-identical.
# --------------------------------------------------------------------------


def test_migration_4_makes_media_sources_material_id_nullable(db_path, tmp_path):
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(MIGRATIONS[0][1])  # courses/materials/etc. -- unaffected by this migration
        conn.executescript(
            """
            BEGIN;
            DROP TABLE media_sources;
            CREATE TABLE media_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
                material_id INTEGER NOT NULL REFERENCES materials(id) ON DELETE CASCADE,
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
            PRAGMA user_version = 3;
            COMMIT;
            """
        )
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3

        # Two materials (one the transcript target) plus two media_sources
        # rows -- one with EVERY nullable column populated (passcode, error,
        # transcript_material_id), the other minimal, so the byte-identical
        # check below actually exercises every column of the table.
        conn.executescript(
            """
            INSERT INTO courses (d2l_org_unit_id, tenant_origin, name)
                VALUES (1, 'school.d2l.com', 'Intro to CS');
            INSERT INTO materials (course_id, title) VALUES (1, 'Lecture 1');
            INSERT INTO materials (course_id, title) VALUES (1, 'Lecture 1 (transcript)');
            INSERT INTO media_sources (
                course_id, material_id, platform, url, passcode, status, error,
                transcript_material_id, created_at, updated_at
            ) VALUES (
                1, 1, 'zoom', 'https://zoom.us/rec/share/full', 's3cret', 'failed',
                'wrong_passcode: mock wrong_passcode', 2,
                '2026-01-01T00:00:00+00:00', '2026-01-02T00:00:00+00:00'
            );
            INSERT INTO media_sources (course_id, material_id, platform, url, created_at, updated_at)
                VALUES (1, 1, 'mediasite', 'https://mediasite.example.edu/Mediasite/Play/x',
                        '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00');
            """
        )

        columns = (
            "id, course_id, material_id, platform, url, passcode, status, error, "
            "transcript_material_id, created_at, updated_at"
        )
        before = conn.execute(f"SELECT {columns} FROM media_sources ORDER BY id").fetchall()
        assert len(before) == 2  # the seeded precondition really held

        migrate(conn)  # applies migration 4, then migrations 5 and 6, on top of this v3 db

        assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
        assert MIGRATIONS[-1][0] == 7  # nothing newer has been appended without updating this test

        after = conn.execute(f"SELECT {columns} FROM media_sources ORDER BY id").fetchall()
        assert after == before  # every row, every column, survives byte-identical

        # The migrated table must match what a fresh database gets from
        # schema.sql.
        fresh_path = tmp_path / "fresh.db"
        init_db(fresh_path)
        fresh_conn = sqlite3.connect(fresh_path)
        try:
            assert _table_columns(conn, "media_sources") == _table_columns(fresh_conn, "media_sources")
        finally:
            fresh_conn.close()

        # The whole point: a NULL material_id insert now works.
        conn.execute(
            "INSERT INTO media_sources (course_id, material_id, platform, url, created_at, updated_at) "
            "VALUES (1, NULL, 'gdrive', 'https://drive.google.com/file/d/manual/view', "
            "'2026-01-03T00:00:00+00:00', '2026-01-03T00:00:00+00:00')"
        )
        conn.commit()
        null_row = conn.execute(
            "SELECT material_id FROM media_sources WHERE url = 'https://drive.google.com/file/d/manual/view'"
        ).fetchone()
        assert null_row == (None,)

        # UNIQUE(course_id, url) survived the rebuild.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO media_sources (course_id, material_id, platform, url, created_at, updated_at) "
                "VALUES (1, NULL, 'gdrive', 'https://drive.google.com/file/d/manual/view', "
                "'2026-01-03T00:00:00+00:00', '2026-01-03T00:00:00+00:00')"
            )
        conn.rollback()

        # A second migrate() is a no-op: version and data both unchanged
        # (data includes the manually-added NULL-material_id row above --
        # this is the "same shape usable after the rebuild" check, mirroring
        # migration 3's test's own post-migration insert-then-remigrate).
        before_second_migrate = conn.execute(f"SELECT {columns} FROM media_sources ORDER BY id").fetchall()
        migrate(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
        after2 = conn.execute(f"SELECT {columns} FROM media_sources ORDER BY id").fetchall()
        assert after2 == before_second_migrate
    finally:
        conn.close()


# --------------------------------------------------------------------------
# M3.5a's migration 5 (materials.is_administrative). Plain ADD COLUMN, not a
# rebuild: hand-build a v4 database (materials has no is_administrative
# column -- schema.sql never gains this column directly; see migrate.py's
# comment on why) WITH data rows, and prove the column shows up with the
# right default and every row survives untouched.
# --------------------------------------------------------------------------


def test_migration_5_adds_materials_is_administrative_column(db_path, tmp_path):
    conn = sqlite3.connect(db_path)
    try:
        # Build a v4 database the same way migrate() itself would have,
        # applying migrations 1-4 only (not migrate(), which would also
        # apply migration 5 and defeat the point of this test).
        for version, sql in MIGRATIONS[:4]:
            conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;\n")
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        assert "is_administrative" not in _table_columns(conn, "materials")

        conn.executescript(
            """
            INSERT INTO courses (d2l_org_unit_id, tenant_origin, name)
                VALUES (1, 'school.d2l.com', 'Intro to CS');
            INSERT INTO materials (course_id, title, kind, status)
                VALUES (1, 'Final Grades', 'announcement', 'summarized');
            INSERT INTO materials (course_id, title, kind, status)
                VALUES (1, 'Lecture 1', 'slides', 'summarized');
            """
        )
        before = conn.execute("SELECT id, title FROM materials ORDER BY id").fetchall()
        assert len(before) == 2  # the seeded precondition really held

        migrate(conn)  # applies migrations 5 and 6 on top of this v4 db

        assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
        assert MIGRATIONS[-1][0] == 7  # nothing newer has been appended without updating this test

        assert "is_administrative" in _table_columns(conn, "materials")
        rows = conn.execute("SELECT id, is_administrative FROM materials ORDER BY id").fetchall()
        assert rows == [(1, 0), (2, 0)]  # existing rows default to 0, not NULL

        after = conn.execute("SELECT id, title FROM materials ORDER BY id").fetchall()
        assert after == before  # every row survives, untouched

        # The migrated table must match what a fresh database gets.
        fresh_path = tmp_path / "fresh.db"
        init_db(fresh_path)
        fresh_conn = sqlite3.connect(fresh_path)
        try:
            assert _table_columns(conn, "materials") == _table_columns(fresh_conn, "materials")
        finally:
            fresh_conn.close()

        # Usable: a new row can set it explicitly.
        conn.execute(
            "INSERT INTO materials (course_id, title, kind, status, is_administrative) "
            "VALUES (1, 'Office Hours Moved', 'announcement', 'summarized', 1)"
        )
        conn.commit()
        new_row = conn.execute(
            "SELECT is_administrative FROM materials WHERE title = 'Office Hours Moved'"
        ).fetchone()
        assert new_row == (1,)

        # A second migrate() is a no-op: version and data both unchanged.
        before_second_migrate = conn.execute(
            "SELECT id, title, is_administrative FROM materials ORDER BY id"
        ).fetchall()
        migrate(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
        after2 = conn.execute("SELECT id, title, is_administrative FROM materials ORDER BY id").fetchall()
        assert after2 == before_second_migrate
    finally:
        conn.close()



# --------------------------------------------------------------------------
# M3.5b's migration 6 (material_topics.method CHECK gains 'inherited'). Same
# table-rebuild shape as migration 4: hand-build a v5 database (method's
# CHECK still only allows llm/embedding/user -- schema.sql already reflects
# the widened CHECK as of this task, so it can't be used to reproduce the
# pre-migration-6 state, same reasoning as migration 4's own hand-built v3
# setup) WITH data rows, including one of every pre-existing method value,
# and prove the rebuild carries every row across byte-identical and the
# CHECK constraint now actually accepts 'inherited'.
# --------------------------------------------------------------------------


def test_migration_6_adds_inherited_to_material_topics_method_check(db_path, tmp_path):
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(MIGRATIONS[0][1])  # courses/materials/topics/etc. -- unaffected by this migration
        conn.executescript(
            """
            BEGIN;
            DROP TABLE material_topics;
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
            PRAGMA user_version = 5;
            COMMIT;
            """
        )
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5

        # A material/topic pair per pre-existing method value, so the
        # byte-identical check below actually exercises every one of them,
        # plus a NULL-confidence/rationale row (both nullable columns).
        conn.executescript(
            """
            INSERT INTO courses (d2l_org_unit_id, tenant_origin, name)
                VALUES (1, 'school.d2l.com', 'Intro to CS');
            INSERT INTO materials (course_id, title) VALUES (1, 'Lecture 1');
            INSERT INTO topics (course_id, taxonomy_version, slug, name)
                VALUES (1, 1, 'arrays', 'Arrays');
            INSERT INTO topics (course_id, taxonomy_version, slug, name)
                VALUES (1, 1, 'sorting', 'Sorting');
            INSERT INTO material_topics (material_id, topic_id, taxonomy_version, confidence, rationale, method, review_status)
                VALUES (1, 1, 1, 0.9, 'model said so', 'llm', 'auto');
            INSERT INTO material_topics (material_id, topic_id, taxonomy_version, confidence, rationale, method, review_status)
                VALUES (1, 2, 1, NULL, NULL, 'user', 'confirmed');
            """
        )

        columns = "id, material_id, topic_id, taxonomy_version, confidence, rationale, method, review_status"
        before = conn.execute(f"SELECT {columns} FROM material_topics ORDER BY id").fetchall()
        assert len(before) == 2  # the seeded precondition really held

        migrate(conn)  # applies migration 6 on top of this v5 db

        assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
        assert MIGRATIONS[-1][0] == 7  # nothing newer has been appended without updating this test

        after = conn.execute(f"SELECT {columns} FROM material_topics ORDER BY id").fetchall()
        assert after == before  # every row, every column, survives byte-identical

        # The migrated table must match what a fresh database gets from
        # schema.sql.
        fresh_path = tmp_path / "fresh.db"
        init_db(fresh_path)
        fresh_conn = sqlite3.connect(fresh_path)
        try:
            assert _table_columns(conn, "material_topics") == _table_columns(fresh_conn, "material_topics")
        finally:
            fresh_conn.close()

        # The whole point: 'inherited' is now an accepted method.
        conn.execute(
            "INSERT INTO material_topics (material_id, topic_id, taxonomy_version, confidence, rationale, method, review_status) "
            "VALUES (1, 1, 2, 0.9, 'inherited from the lecture transcript', 'inherited', 'auto')"
        )
        conn.commit()
        inherited_row = conn.execute(
            "SELECT method FROM material_topics WHERE taxonomy_version = 2"
        ).fetchone()
        assert inherited_row == ("inherited",)

        # An out-of-set value is still rejected.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO material_topics (material_id, topic_id, taxonomy_version, method) "
                "VALUES (1, 1, 3, 'bogus')"
            )
        conn.rollback()

        # UNIQUE(material_id, topic_id, taxonomy_version) survived the rebuild.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO material_topics (material_id, topic_id, taxonomy_version, method) "
                "VALUES (1, 1, 2, 'llm')"
            )
        conn.rollback()

        # A second migrate() is a no-op: version and data both unchanged
        # (data includes the 'inherited' row above -- the "same shape usable
        # after the rebuild" check, mirroring migration 4's own test).
        before_second_migrate = conn.execute(f"SELECT {columns} FROM material_topics ORDER BY id").fetchall()
        migrate(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
        after2 = conn.execute(f"SELECT {columns} FROM material_topics ORDER BY id").fetchall()
        assert after2 == before_second_migrate
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Migrations 5 and 6 in ONE migrate() call. The tests above each start from
# the version immediately before their own migration, so neither exercises
# what an actual M3.5 upgrade does: a v4 database (the last shipped version)
# taking an ADD COLUMN on `materials` and a full table rebuild of
# `material_topics` back to back, with real rows in both. Migration 6's
# rebuild copies `material_topics` by explicit column list while migration 5
# has just altered a different table -- a combination only a combined run
# can get wrong.
# --------------------------------------------------------------------------


def test_migrations_5_and_6_apply_together_on_a_v4_database_with_data(db_path, tmp_path):
    conn = sqlite3.connect(db_path)
    try:
        for version, sql in MIGRATIONS[:4]:
            conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;\n")
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        assert "is_administrative" not in _table_columns(conn, "materials")

        # Rows in BOTH affected tables: two materials, and a material_topics
        # row per pre-existing method value (one with both nullable columns
        # NULL), so the byte-identical checks below cover every column of
        # the rebuilt table.
        conn.executescript(
            """
            INSERT INTO courses (d2l_org_unit_id, tenant_origin, name)
                VALUES (1, 'school.d2l.com', 'Intro to CS');
            INSERT INTO materials (course_id, title, kind, status)
                VALUES (1, 'Office Hours Moved', 'announcement', 'summarized');
            INSERT INTO materials (course_id, title, kind, status)
                VALUES (1, 'Lecture 1', 'slides', 'summarized');
            INSERT INTO topics (course_id, taxonomy_version, slug, name)
                VALUES (1, 1, 'arrays', 'Arrays');
            INSERT INTO topics (course_id, taxonomy_version, slug, name)
                VALUES (1, 1, 'sorting', 'Sorting');
            INSERT INTO material_topics (material_id, topic_id, taxonomy_version, confidence, rationale, method, review_status)
                VALUES (2, 1, 1, 0.9, 'model said so', 'llm', 'auto');
            INSERT INTO material_topics (material_id, topic_id, taxonomy_version, confidence, rationale, method, review_status)
                VALUES (2, 2, 1, NULL, NULL, 'user', 'confirmed');
            """
        )

        material_columns = "id, course_id, title, kind, status"
        topic_columns = (
            "id, material_id, topic_id, taxonomy_version, confidence, rationale, method, review_status"
        )
        materials_before = conn.execute(
            f"SELECT {material_columns} FROM materials ORDER BY id"
        ).fetchall()
        topics_before = conn.execute(f"SELECT {topic_columns} FROM material_topics ORDER BY id").fetchall()
        assert len(materials_before) == 2 and len(topics_before) == 2  # preconditions really held

        migrate(conn)  # ONE call, applying 5 and 6 back to back

        assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
        assert MIGRATIONS[-1][0] == 7  # nothing newer has been appended without updating this test

        # Migration 5's change is present...
        assert "is_administrative" in _table_columns(conn, "materials")
        assert conn.execute("SELECT id, is_administrative FROM materials ORDER BY id").fetchall() == [
            (1, 0), (2, 0),
        ]
        # ...and migration 6's, on a table migration 5 never touched.
        conn.execute(
            "INSERT INTO material_topics (material_id, topic_id, taxonomy_version, confidence, rationale, method, review_status) "
            "VALUES (2, 1, 2, 0.9, 'inherited from the lecture transcript', 'inherited', 'auto')"
        )
        conn.commit()

        # Every pre-existing row in both tables survives byte-identical.
        assert conn.execute(f"SELECT {material_columns} FROM materials ORDER BY id").fetchall() == materials_before
        assert conn.execute(
            f"SELECT {topic_columns} FROM material_topics WHERE taxonomy_version = 1 ORDER BY id"
        ).fetchall() == topics_before

        # Both tables match what a fresh database gets.
        fresh_path = tmp_path / "fresh.db"
        init_db(fresh_path)
        fresh_conn = sqlite3.connect(fresh_path)
        try:
            assert _table_columns(conn, "materials") == _table_columns(fresh_conn, "materials")
            assert _table_columns(conn, "material_topics") == _table_columns(fresh_conn, "material_topics")
        finally:
            fresh_conn.close()

        # A second migrate() is a no-op: version and data both unchanged.
        materials_before_second = conn.execute(
            "SELECT id, title, is_administrative FROM materials ORDER BY id"
        ).fetchall()
        topics_before_second = conn.execute(f"SELECT {topic_columns} FROM material_topics ORDER BY id").fetchall()
        migrate(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
        assert conn.execute(
            "SELECT id, title, is_administrative FROM materials ORDER BY id"
        ).fetchall() == materials_before_second
        assert conn.execute(
            f"SELECT {topic_columns} FROM material_topics ORDER BY id"
        ).fetchall() == topics_before_second
    finally:
        conn.close()


def test_media_sources_material_id_fk_still_enforced_and_cascades(db_path):
    """The rebuilt table's FK on `material_id` (when NOT NULL) still points
    at `materials` and still cascades on delete -- the rebuild must not have
    silently dropped the constraint along with the NOT NULL."""
    _, Session = init_db(db_path)

    with Session() as session:
        course = _make_course()
        session.add(course)
        session.flush()
        material = Material(course_id=course.id, title="Lecture 1")
        session.add(material)
        session.commit()
        course_id, material_id = course.id, material.id

    with Session() as session:
        session.add(
            MediaSource(
                course_id=999999,  # bogus course -- FK on course_id must still be enforced too
                material_id=material_id,
                platform="zoom",
                url="https://zoom.us/rec/share/orphan-course",
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()

    with Session() as session:
        row = MediaSource(
            course_id=course_id,
            material_id=material_id,
            platform="zoom",
            url="https://zoom.us/rec/share/cascade-me",
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )
        session.add(row)
        session.commit()
        row_id = row.id

    with Session() as session:
        session.execute(text("DELETE FROM materials WHERE id = :id"), {"id": material_id})
        session.commit()

    with Session() as session:
        assert session.get(MediaSource, row_id) is None  # cascaded away with its material


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


# --------------------------------------------------------------------------
# M2.7's migration 7 (lti_resolutions). Same "brand new table" shape as
# migration 3's media_sources: a database that predates the table must gain
# it on migrate(), and a second migrate() must change nothing. Built by
# applying schema.sql and dropping the table (schema.sql already creates it
# for a fresh database), same reasoning as migration 3's own test.
# --------------------------------------------------------------------------


def test_migration_7_adds_lti_resolutions_to_a_v6_database(db_path, tmp_path):
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(MIGRATIONS[0][1])
        conn.executescript("BEGIN;\nDROP TABLE lti_resolutions;\nPRAGMA user_version = 6;\nCOMMIT;\n")
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 6

        migrate(conn)

        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'lti_resolutions'"
        ).fetchone()
        assert exists is not None
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
        assert MIGRATIONS[-1][0] == 7  # nothing newer has been appended without updating this test

        # The migrated table must match what a fresh database gets from
        # schema.sql.
        fresh_path = tmp_path / "fresh.db"
        init_db(fresh_path)
        fresh_conn = sqlite3.connect(fresh_path)
        try:
            assert _table_columns(conn, "lti_resolutions") == _table_columns(fresh_conn, "lti_resolutions")
        finally:
            fresh_conn.close()

        # Usable, and a second migrate() leaves both the schema and the data
        # exactly as they are.
        conn.executescript(
            """
            INSERT INTO courses (d2l_org_unit_id, tenant_origin, name)
                VALUES (1, 'school.d2l.com', 'Intro to CS');
            INSERT INTO materials (course_id, title) VALUES (1, 'Mediasite Channel');
            INSERT INTO lti_resolutions (
                course_id, material_id, launch_url, final_url, platform, status, error,
                created_at, updated_at
            ) VALUES (
                1, 1, 'https://school.d2l.com/d2l/lti/launch?x=1',
                'https://mediasite.example.edu/Mediasite/Play/xyz', 'mediasite', 'resolved', NULL,
                '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'
            );
            """
        )

        migrate(conn)

        assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
        row = conn.execute("SELECT launch_url, final_url, platform, status FROM lti_resolutions").fetchone()
        assert row == (
            "https://school.d2l.com/d2l/lti/launch?x=1",
            "https://mediasite.example.edu/Mediasite/Play/xyz",
            "mediasite",
            "resolved",
        )
    finally:
        conn.close()


def test_lti_resolutions_unique_on_material_id(db_path):
    """UNIQUE(material_id): a second row for the same material must raise,
    matching the "one row per material" upsert contract Task 1's endpoints
    rely on."""
    _, Session = init_db(db_path)

    with Session() as session:
        course = _make_course()
        session.add(course)
        session.flush()
        material = Material(course_id=course.id, title="Mediasite Channel")
        session.add(material)
        session.commit()
        course_id, material_id = course.id, material.id

    with Session() as session:
        session.add(
            LtiResolution(
                course_id=course_id,
                material_id=material_id,
                launch_url="https://school.d2l.com/d2l/lti/launch?x=1",
                status="unrecognized",
                final_url="https://example.com/landing",
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
            )
        )
        session.commit()

    with Session() as session:
        session.add(
            LtiResolution(
                course_id=course_id,
                material_id=material_id,
                launch_url="https://school.d2l.com/d2l/lti/launch?x=1",
                status="failed",
                error="tab closed",
                created_at="2026-01-02T00:00:00+00:00",
                updated_at="2026-01-02T00:00:00+00:00",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_lti_resolutions_status_check_constraint(db_path):
    _, Session = init_db(db_path)

    with Session() as session:
        course = _make_course()
        session.add(course)
        session.flush()
        material = Material(course_id=course.id, title="Mediasite Channel")
        session.add(material)
        session.commit()
        course_id, material_id = course.id, material.id

    with Session() as session:
        session.add(
            LtiResolution(
                course_id=course_id,
                material_id=material_id,
                launch_url="https://school.d2l.com/d2l/lti/launch?x=1",
                status="bogus",
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_lti_resolutions_material_fk_cascades(db_path):
    _, Session = init_db(db_path)

    with Session() as session:
        course = _make_course()
        session.add(course)
        session.flush()
        material = Material(course_id=course.id, title="Mediasite Channel")
        session.add(material)
        session.commit()
        course_id, material_id = course.id, material.id

    with Session() as session:
        row = LtiResolution(
            course_id=course_id,
            material_id=material_id,
            launch_url="https://school.d2l.com/d2l/lti/launch?x=1",
            status="unrecognized",
            final_url="https://example.com/landing",
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )
        session.add(row)
        session.commit()
        row_id = row.id

    with Session() as session:
        session.execute(text("DELETE FROM materials WHERE id = :id"), {"id": material_id})
        session.commit()

    with Session() as session:
        assert session.get(LtiResolution, row_id) is None  # cascaded away with its material
