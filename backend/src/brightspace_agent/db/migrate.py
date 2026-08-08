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

MIGRATIONS: list[tuple[int, str]] = [
    (1, _SCHEMA_SQL),
    (2, _ENRICHMENT_UNIQUE_INDEX),
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
