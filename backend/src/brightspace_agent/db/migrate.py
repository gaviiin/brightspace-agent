"""Hand-rolled SQLite migrations, tracked via `PRAGMA user_version`.

`schema.sql` is the DDL source of truth for the current schema; it is applied
as migration 1. Future schema changes should be appended to MIGRATIONS as
additional (version, sql) entries rather than editing schema.sql in place.
"""

from importlib import resources
from sqlite3 import Connection

_SCHEMA_SQL = resources.files("brightspace_agent.db").joinpath("schema.sql").read_text(
    encoding="utf-8"
)

# Each entry is (target_user_version, sql_to_apply_to_get_there).
#
# Migration 2 adds the enrichment_resources(topic_id, url) unique index. It is
# also present in schema.sql (via IF NOT EXISTS) so the source-of-truth DDL
# stays complete for fresh databases; the separate migration is what brings
# databases already at version 1 (M1 shipped this table empty) up to it. The
# IF NOT EXISTS makes running both harmless.
#
# M3.2 hardening: dedup BEFORE creating the index. A database already at
# version 1 could in principle already carry a duplicate (topic_id, url) row
# (M3.2 is what makes this table's write path -- the enrich stage's upsert --
# actually live), and creating a unique index over duplicates raises
# IntegrityError, aborting startup with no recovery short of hand-editing the
# database. The DELETE keeps the lowest id per (topic_id, url) pair (an
# arbitrary but deterministic tie-break -- "first written" is as good a
# survivor as any for rows that are, by definition, duplicates) and matches
# nothing on an already-clean table, so it's a no-op there and safe to run on
# every migrate() (idempotent, like the IF NOT EXISTS index creation next to
# it).
_ENRICHMENT_UNIQUE_INDEX = (
    "DELETE FROM enrichment_resources WHERE id NOT IN ("
    "SELECT MIN(id) FROM enrichment_resources GROUP BY topic_id, url"
    ");\n"
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_enrichment_topic_url "
    "ON enrichment_resources(topic_id, url);"
)

# Migration 3 adds the media_sources table (M2.1's recording-URL detector).
# Same "also in schema.sql via IF NOT EXISTS" shape as migration 2 above: a
# fresh database gets the table from schema.sql at migration 1, so this
# statement is a no-op there; it only does real work bringing a database
# already at version 1 or 2 up to date.
_MEDIA_SOURCES_TABLE = (
    "CREATE TABLE IF NOT EXISTS media_sources (\n"
    "    id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
    "    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,\n"
    "    material_id INTEGER NOT NULL REFERENCES materials(id) ON DELETE CASCADE,\n"
    "    platform TEXT NOT NULL CHECK(platform IN ('mediasite','zoom','gdrive')),\n"
    "    url TEXT NOT NULL,\n"
    "    passcode TEXT,\n"
    "    status TEXT NOT NULL CHECK(status IN ('detected','fetching','transcribing','done','failed','skipped')) DEFAULT 'detected',\n"
    "    error TEXT,\n"
    "    transcript_material_id INTEGER REFERENCES materials(id) ON DELETE SET NULL,\n"
    "    created_at TEXT NOT NULL,\n"
    "    updated_at TEXT NOT NULL,\n"
    "    UNIQUE(course_id, url)\n"
    ");"
)

# Migration 4 (M2.6a) makes media_sources.material_id nullable, so a
# manually-added recording URL/channel row (api/media.py's POST
# .../media/add) can exist with no backing `materials` row. SQLite can't
# drop a NOT NULL constraint with ALTER TABLE, so this is the standard
# SQLite table-rebuild dance: create the new shape under a temp name, copy
# every row across unchanged, drop the old table, rename the new one into
# place. `UNIQUE(course_id, url)` and both FKs are carried over verbatim --
# only the `NOT NULL` on `material_id` is dropped. Also in schema.sql
# (already nullable there as of this task) for the same "fresh database
# reasoning" as migrations 2/3: this statement only does real work bringing
# an already-migrated (v1-v3) database up to date.
_MEDIA_SOURCES_MATERIAL_ID_NULLABLE = (
    "CREATE TABLE media_sources_new (\n"
    "    id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
    "    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,\n"
    "    material_id INTEGER REFERENCES materials(id) ON DELETE CASCADE,\n"
    "    platform TEXT NOT NULL CHECK(platform IN ('mediasite','zoom','gdrive')),\n"
    "    url TEXT NOT NULL,\n"
    "    passcode TEXT,\n"
    "    status TEXT NOT NULL CHECK(status IN ('detected','fetching','transcribing','done','failed','skipped')) DEFAULT 'detected',\n"
    "    error TEXT,\n"
    "    transcript_material_id INTEGER REFERENCES materials(id) ON DELETE SET NULL,\n"
    "    created_at TEXT NOT NULL,\n"
    "    updated_at TEXT NOT NULL,\n"
    "    UNIQUE(course_id, url)\n"
    ");\n"
    "INSERT INTO media_sources_new (\n"
    "    id, course_id, material_id, platform, url, passcode, status, error,\n"
    "    transcript_material_id, created_at, updated_at\n"
    ")\n"
    "SELECT id, course_id, material_id, platform, url, passcode, status, error,\n"
    "       transcript_material_id, created_at, updated_at\n"
    "FROM media_sources;\n"
    "DROP TABLE media_sources;\n"
    "ALTER TABLE media_sources_new RENAME TO media_sources;"
)

# Migration 5 (M3.5a) adds materials.is_administrative: a flag S3's classify
# stage sets for grades/scheduling/office-hours/logistics materials, so S4
# can file them under their own "Logistics & admin" bucket instead of
# Unsorted. Deliberately NOT also added to schema.sql's `CREATE TABLE
# materials` (unlike migrations 2-4's "also in schema.sql" pairing):
# `ALTER TABLE ... ADD COLUMN` has no `IF NOT EXISTS` form in SQLite, so a
# fresh database -- which starts at user_version 0 and therefore runs EVERY
# migration in one `migrate()` call, schema.sql (migration 1) included --
# would hit "duplicate column name" the moment this ran if schema.sql had
# already created the column. Keeping it exclusively in this migration
# (which fresh databases run just like any upgraded one) is what keeps a
# single, crash-free code path for both.
_MATERIALS_IS_ADMINISTRATIVE_COLUMN = (
    "ALTER TABLE materials ADD COLUMN is_administrative INTEGER NOT NULL DEFAULT 0;"
)

MIGRATIONS: list[tuple[int, str]] = [
    (1, _SCHEMA_SQL),
    (2, _ENRICHMENT_UNIQUE_INDEX),
    (3, _MEDIA_SOURCES_TABLE),
    (4, _MEDIA_SOURCES_MATERIAL_ID_NULLABLE),
    (5, _MATERIALS_IS_ADMINISTRATIVE_COLUMN),
]


def migrate(connection: Connection) -> None:
    """Bring `connection`'s database up to the latest schema version.

    Reads `PRAGMA user_version`, then applies every migration whose target
    version is greater than the current version, each inside its own
    transaction, bumping `PRAGMA user_version` as part of that same
    transaction. Idempotent: calling this twice is a no-op the second time.
    """
    current_version = connection.execute("PRAGMA user_version").fetchone()[0]

    for version, sql in MIGRATIONS:
        if version <= current_version:
            continue
        # BEGIN/COMMIT are embedded in the script itself (rather than issued
        # via connection.execute beforehand) so the DDL and the user_version
        # bump commit together atomically, regardless of sqlite3's implicit
        # "commit pending transaction" behavior on executescript().
        script = f"BEGIN;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;\n"
        connection.executescript(script)
