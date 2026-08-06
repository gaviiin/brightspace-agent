"""SQLAlchemy engine/session wiring for the SQLite database."""

import sqlite3
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from brightspace_agent.db.migrate import migrate


def make_engine(db_path: Path) -> Engine:
    """Create a SQLAlchemy engine for the SQLite database at `db_path`.

    Sets `PRAGMA journal_mode=WAL` and `PRAGMA foreign_keys=ON` on every
    connection the engine hands out.
    """
    engine = create_engine(f"sqlite:///{db_path}")

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine)


def init_db(db_path: Path) -> tuple[Engine, sessionmaker[Session]]:
    """Ensure the database at `db_path` exists and is migrated, then wire it up.

    Returns (engine, session_factory).
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(db_path)
    try:
        migrate(connection)
    finally:
        connection.close()

    engine = make_engine(db_path)
    return engine, make_session_factory(engine)
