"""DS 6.1 schema, migrations, and insert-only versions (PRJ-5, NFR-L1)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from fvs.store import Database

DS_TABLES = {
    "clips", "projects", "project_clips", "script_versions", "sentences", "voice_sentences",
    "voiceovers", "edit_specs", "renders", "reviews", "jobs", "activity", "assets",
    "templates", "caption_styles", "voice_profiles", "style_guide", "cost_ledger", "settings",
    "inspiration_sets", "inspiration_videos",  # BUILD_PLAN 6.1
}
NOW = "2026-11-20T00:00:00+00:00"


@pytest.fixture
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "fvs.db")
    d.migrate()
    return d


def tables(db: Database) -> set[str]:
    with db.connect() as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                            "AND name NOT LIKE 'sqlite_%'").fetchall()
    return {r["name"] for r in rows}


def _project(conn: sqlite3.Connection, pid: str = "p1", number: int = 1) -> None:
    conn.execute(
        "INSERT INTO projects (id, number, title, slug, created_at, updated_at) "
        "VALUES (?, ?, 'T', 't', ?, ?)", (pid, number, NOW, NOW))


def test_all_ds_tables_exist(db: Database) -> None:
    assert tables(db) == DS_TABLES
    assert db.schema_version() == 3


def test_migrate_is_idempotent(db: Database) -> None:
    assert db.migrate() == 3
    assert tables(db) == DS_TABLES


def test_wal_mode(db: Database) -> None:
    with db.connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_script_versions_are_never_changed(db: Database) -> None:
    with db.connect() as conn:
        _project(conn)
        conn.execute(
            "INSERT INTO script_versions (id, project_id, version, title, voiceover_text, "
            "author, created_at) VALUES ('s1', 'p1', 1, 'T', 'Hello.', 'claude', ?)", (NOW,))
        with pytest.raises(sqlite3.DatabaseError, match="never changed"):
            conn.execute("UPDATE script_versions SET title = 'X' WHERE id = 's1'")


def test_cost_ledger_is_never_changed(db: Database) -> None:
    with db.connect() as conn:
        conn.execute("INSERT INTO cost_ledger (at, chars) VALUES (?, 10)", (NOW,))
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("UPDATE cost_ledger SET chars = 0")


def test_project_state_is_one_of_ten(db: Database) -> None:
    with db.connect() as conn:
        _project(conn)
        for state in ("new", "scripted", "voiced", "planned", "rendering", "claude_check",
                      "in_review", "changes_requested", "exported", "skipped"):
            conn.execute("UPDATE projects SET state = ? WHERE id = 'p1'", (state,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE projects SET state = 'failed' WHERE id = 'p1'")


def test_asset_needs_a_license(db: Database) -> None:
    with db.connect() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO assets (id, kind, name, path, license, created_at) "
            "VALUES ('a1', 'music', 'x', 'x.mp3', '  ', ?)", (NOW,))


def test_only_one_default_voice(db: Database) -> None:
    with db.connect() as conn:
        conn.execute("INSERT INTO voice_profiles (alias, voice_id, model_id, is_default) "
                     "VALUES ('calm', 'v1', 'eleven_multilingual_v2', 1)")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO voice_profiles (alias, voice_id, model_id, is_default) "
                         "VALUES ('loud', 'v2', 'eleven_multilingual_v2', 1)")


def test_transaction_rolls_back(db: Database) -> None:
    with pytest.raises(RuntimeError), db.transaction() as conn:
        _project(conn)
        raise RuntimeError("boom")
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0
