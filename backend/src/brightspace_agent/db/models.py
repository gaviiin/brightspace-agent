"""SQLAlchemy 2.0 ORM models mirroring schema.sql's tables 1:1.

These models describe the shape of the tables that schema.sql creates; they
do not own DDL (migrate.py does). Do not call Base.metadata.create_all() in
production code -- schema.sql via migrate() is the source of truth for the
actual database structure (including CHECK constraints and the partial
unique index on materials, which are not represented here).
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Index, UniqueConstraint, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Course(Base):
    __tablename__ = "courses"

    id: Mapped[int] = mapped_column(primary_key=True)
    d2l_org_unit_id: Mapped[int] = mapped_column(unique=True)
    tenant_origin: Mapped[str]
    name: Mapped[str]
    code: Mapped[str | None]
    term: Mapped[str | None]
    toc_json: Mapped[str | None]
    taxonomy_version: Mapped[int] = mapped_column(server_default=text("0"))
    last_synced_at: Mapped[str | None]


class Module(Base):
    __tablename__ = "modules"
    __table_args__ = (UniqueConstraint("course_id", "d2l_module_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"))
    d2l_module_id: Mapped[int]
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("modules.id", ondelete="CASCADE"))
    title: Mapped[str]
    sort_order: Mapped[int] = mapped_column(server_default=text("0"))


class Material(Base):
    __tablename__ = "materials"

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"))
    module_id: Mapped[int | None] = mapped_column(ForeignKey("modules.id", ondelete="SET NULL"))
    d2l_topic_id: Mapped[int | None]
    kind: Mapped[str] = mapped_column(server_default=text("'other'"))
    title: Mapped[str]
    source_url: Mapped[str | None]
    sha256: Mapped[str | None]
    mime: Mapped[str | None]
    size_bytes: Mapped[int | None]
    d2l_updated_at: Mapped[str | None]
    fetched_at: Mapped[str | None]
    summary: Mapped[str | None]
    summary_meta_json: Mapped[str | None]
    status: Mapped[str] = mapped_column(server_default=text("'fetched'"))
    error: Mapped[str | None]
    # M3.5a (migration 5): grades/scheduling/office-hours/logistics, never
    # course content. S3 sets this and writes no material_topics rows when
    # true; S4 files it under the synthetic "Logistics & admin" bucket
    # instead of Unsorted. Plain int (not bool) to mirror every other
    # SQLite-boolean column in this file (e.g. EnrichmentResource.shared).
    is_administrative: Mapped[int] = mapped_column(server_default=text("0"))


class MediaSource(Base):
    __tablename__ = "media_sources"
    __table_args__ = (UniqueConstraint("course_id", "url"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"))
    # Nullable as of M2.6a: a manually-added URL/channel row (api/media.py's
    # POST .../media/add) has no backing `materials` row.
    material_id: Mapped[int | None] = mapped_column(ForeignKey("materials.id", ondelete="CASCADE"))
    platform: Mapped[str]
    url: Mapped[str]
    passcode: Mapped[str | None]
    status: Mapped[str] = mapped_column(server_default=text("'detected'"))
    error: Mapped[str | None]
    transcript_material_id: Mapped[int | None] = mapped_column(ForeignKey("materials.id", ondelete="SET NULL"))
    created_at: Mapped[str]
    updated_at: Mapped[str]


class Topic(Base):
    __tablename__ = "topics"
    __table_args__ = (UniqueConstraint("course_id", "taxonomy_version", "slug"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"))
    taxonomy_version: Mapped[int]
    slug: Mapped[str]
    name: Mapped[str]
    description: Mapped[str | None]
    order_index: Mapped[int] = mapped_column(server_default=text("0"))
    created_by: Mapped[str] = mapped_column(server_default=text("'agent'"))


class TopicEdge(Base):
    __tablename__ = "topic_edges"
    __table_args__ = (UniqueConstraint("from_topic_id", "to_topic_id", "relation"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"))
    from_topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id", ondelete="CASCADE"))
    to_topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id", ondelete="CASCADE"))
    relation: Mapped[str]
    created_by: Mapped[str] = mapped_column(server_default=text("'agent'"))


class MaterialTopic(Base):
    __tablename__ = "material_topics"
    __table_args__ = (UniqueConstraint("material_id", "topic_id", "taxonomy_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    material_id: Mapped[int] = mapped_column(ForeignKey("materials.id", ondelete="CASCADE"))
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id", ondelete="CASCADE"))
    taxonomy_version: Mapped[int]
    confidence: Mapped[float | None]
    rationale: Mapped[str | None]
    method: Mapped[str] = mapped_column(server_default=text("'llm'"))
    review_status: Mapped[str] = mapped_column(server_default=text("'auto'"))


class EnrichmentResource(Base):
    __tablename__ = "enrichment_resources"
    # Mirrors schema.sql's ux_enrichment_topic_url: one row per (topic, url),
    # backing the enrich stage's upsert-by-(topic_id, url).
    __table_args__ = (Index("ux_enrichment_topic_url", "topic_id", "url", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id", ondelete="CASCADE"))
    url: Mapped[str]
    title: Mapped[str | None]
    resource_type: Mapped[str | None]
    intent: Mapped[str | None]
    rationale: Mapped[str | None]
    scores_json: Mapped[str | None]
    verification_json: Mapped[str | None]
    shared: Mapped[int] = mapped_column(server_default=text("0"))
    rank: Mapped[int | None]
    status: Mapped[str] = mapped_column(server_default=text("'suggested'"))


class DomainReputation(Base):
    __tablename__ = "domain_reputation"

    domain: Mapped[str] = mapped_column(primary_key=True)
    kept_count: Mapped[int] = mapped_column(server_default=text("0"))
    dismissed_count: Mapped[int] = mapped_column(server_default=text("0"))


class SyncRun(Base):
    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"))
    source: Mapped[str]
    started_at: Mapped[str]
    finished_at: Mapped[str | None]
    status: Mapped[str] = mapped_column(server_default=text("'running'"))
    stats_json: Mapped[str | None]


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"))
    stage: Mapped[str]
    status: Mapped[str] = mapped_column(server_default=text("'running'"))
    started_at: Mapped[str]
    finished_at: Mapped[str | None]
    usage_json: Mapped[str | None]
    error: Mapped[str | None]


class LlmCache(Base):
    __tablename__ = "llm_cache"

    sha256: Mapped[str] = mapped_column(primary_key=True)
    stage: Mapped[str] = mapped_column(primary_key=True)
    prompt_version: Mapped[str] = mapped_column(primary_key=True)
    model: Mapped[str] = mapped_column(primary_key=True)
    output_json: Mapped[str]
    created_at: Mapped[str]
