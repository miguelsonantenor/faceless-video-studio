"""SQLite access and migrations (DS 6.1).

One SQLite file in WAL mode. Each call to ``Database.connect()`` opens a short-lived
connection, so worker threads never share one. Migrations are numbered ``.sql`` files in
``fvs/store/migrations``; the applied version is kept in ``PRAGMA user_version``.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

BUSY_TIMEOUT_S = 10.0


def _migrations() -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for entry in resources.files("fvs.store.migrations").iterdir():
        name = entry.name
        if name.endswith(".sql") and name[:4].isdigit():
            found.append((int(name[:4]), entry.read_text(encoding="utf-8")))
    return sorted(found)


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._migrate_lock = threading.Lock()

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_S, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """A connection in autocommit mode. Use ``transaction()`` for atomic changes."""
        conn = self._open()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """``BEGIN IMMEDIATE`` … ``COMMIT``; rolled back on any error."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    def migrate(self) -> int:
        """Apply every pending migration. Returns the schema version."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._migrate_lock, self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            current = conn.execute("PRAGMA user_version").fetchone()[0]
            for version, sql in _migrations():
                if version <= current:
                    continue
                # executescript commits first; wrap the script itself in a transaction.
                conn.executescript(
                    f"BEGIN;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;"
                )
                current = version
            return current

    def schema_version(self) -> int:
        with self.connect() as conn:
            return conn.execute("PRAGMA user_version").fetchone()[0]
