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
MIGRATIONS: list[tuple[int, str]] = [
    (1, _SCHEMA_SQL),
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
