"""Database connection factory.

Returns a libsql embedded-replica connection when TURSO_DB_URL and
TURSO_AUTH_TOKEN are set, otherwise falls back to a plain sqlite3 connection.
The returned connection is API-compatible with sqlite3.Connection.

Usage in repositories:
    from zelda.db import connect
    self._conn = connect(self._db_path)
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from dotenv import load_dotenv

# pydantic-settings reads .env into its own namespace but does NOT set
# os.environ. load_dotenv() here ensures the Turso creds reach os.environ
# so every repo's connect() call sees them, regardless of boot order.
load_dotenv()


def connect(db_path: Path | str, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """Return a DB connection for *db_path*.

    When TURSO_DB_URL + TURSO_AUTH_TOKEN are set the connection uses a
    libsql embedded replica (fast local reads + cloud sync). Falls back to
    plain sqlite3 when the creds are absent (tests, CI, offline use).
    """
    url = os.environ.get("TURSO_DB_URL", "")
    token = os.environ.get("TURSO_AUTH_TOKEN", "")
    path = str(db_path)

    if url and token and path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        return _libsql_connect(path, url, token)

    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(path, check_same_thread=check_same_thread)


def _libsql_connect(path: str, url: str, token: str) -> sqlite3.Connection:
    try:
        import libsql_experimental as libsql  # type: ignore[import]
    except ImportError as exc:
        raise ImportError(
            "libsql_experimental is required for Turso sync. "
            "Install it with: pip install libsql-experimental"
        ) from exc

    conn = libsql.connect(path, sync_url=url, auth_token=token)
    conn.sync()
    return conn  # type: ignore[return-value]


__all__ = ["connect"]
