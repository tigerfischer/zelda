"""Database connection factory.

Returns a libsql embedded-replica connection when TURSO_DB_URL and
TURSO_AUTH_TOKEN are set, otherwise falls back to a plain sqlite3 connection.
The returned connection is API-compatible with sqlite3.Connection.

Usage in repositories:
    from zelda.db import connect
    self._conn = connect(self._db_path)
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def connect(db_path: Path | str, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """Return a DB connection for *db_path*.

    When TURSO_DB_URL + TURSO_AUTH_TOKEN are set in the environment the
    connection syncs to Turso; local reads still hit the embedded replica
    so latency is identical to plain SQLite.  Falls back to sqlite3 when
    the creds are absent (local-only mode, tests, CI).
    """
    import os

    url = os.environ.get("TURSO_DB_URL", "")
    token = os.environ.get("TURSO_AUTH_TOKEN", "")

    if url and token:
        return _libsql_connect(db_path, url, token, check_same_thread=check_same_thread)

    return sqlite3.connect(str(db_path), check_same_thread=check_same_thread)


def _libsql_connect(
    db_path: Path | str,
    url: str,
    token: str,
    *,
    check_same_thread: bool,
) -> sqlite3.Connection:
    try:
        import libsql_experimental as libsql  # type: ignore[import]
    except ImportError as exc:
        raise ImportError(
            "libsql_experimental is required for Turso sync. "
            "Install it with: pip install libsql-experimental"
        ) from exc

    local = str(db_path)
    conn = libsql.connect(local, sync_url=url, auth_token=token)
    conn.sync()
    return conn  # type: ignore[return-value]


__all__ = ["connect"]
