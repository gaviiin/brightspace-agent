"""Tests for the SQLite persistence layer: schema, migrations, ORM models."""

import sqlite3

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from brightspace_agent.db.migrate import MIGRATIONS, migrate
from brightspace_agent.db.models import Course, Material, Module
from brightspace_agent.db.session import init_db

EXPECTED_TABLES = {
    "courses",
    "modules",
    "materials",
    "topics",
    "topic_edges",
    "material_topics",
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
