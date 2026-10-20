import csv
import io
import os
import sqlite3
import tempfile
from contextlib import closing
from os import PathLike

from research_discovery import initialize_discovery_schema
from research_watch import initialize_research_watch_schema


def initialize_database(
    conn: sqlite3.Connection,
    clear_papers_only: bool = False,
) -> None:
    if clear_papers_only:
        conn.execute("DROP TABLE IF EXISTS papers")
        conn.execute("DROP TABLE IF EXISTS ignored_papers")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS papers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT UNIQUE,
            author TEXT,
            first_author TEXT DEFAULT '',
            journal TEXT,
            pub_date TEXT,
            url TEXT,
            doi TEXT DEFAULT '',
            abstract TEXT DEFAULT '',
            ai_insight TEXT DEFAULT '',
            priority TEXT DEFAULT '中',
            suggested_section TEXT DEFAULT '',
            draft_text TEXT DEFAULT '',
            local_pdf_path TEXT DEFAULT '',
            matched_keyword TEXT DEFAULT '',
            user_memo TEXT DEFAULT '',
            is_read INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    for column, definition in (
        ("author", "TEXT"),
        ("first_author", "TEXT DEFAULT ''"),
        ("journal", "TEXT"),
        ("pub_date", "TEXT"),
        ("url", "TEXT"),
        ("doi", "TEXT DEFAULT ''"),
        ("abstract", "TEXT DEFAULT ''"),
        ("ai_insight", "TEXT DEFAULT ''"),
        ("priority", "TEXT DEFAULT '中'"),
        ("suggested_section", "TEXT DEFAULT ''"),
        ("draft_text", "TEXT DEFAULT ''"),
        ("local_pdf_path", "TEXT DEFAULT ''"),
        ("matched_keyword", "TEXT DEFAULT ''"),
        ("user_memo", "TEXT DEFAULT ''"),
        ("is_read", "INTEGER DEFAULT 0"),
        ("created_at", "TIMESTAMP"),
    ):
        _ensure_column(conn, "papers", column, definition)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS ignored_papers (
            title TEXT PRIMARY KEY,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS target_authors (
            name TEXT PRIMARY KEY,
            affiliation TEXT,
            memo TEXT,
            sort_order INTEGER DEFAULT 0,
            column_idx INTEGER DEFAULT 0
        )
    """)
    for column, definition in (
        ("affiliation", "TEXT"),
        ("memo", "TEXT"),
        ("sort_order", "INTEGER DEFAULT 0"),
        ("column_idx", "INTEGER DEFAULT 0"),
    ):
        _ensure_column(conn, "target_authors", column, definition)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS target_keywords (
            keyword TEXT PRIMARY KEY,
            category TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS researcher_profiles (
            name TEXT PRIMARY KEY,
            field TEXT,
            school_of_thought TEXT,
            key_concepts TEXT,
            memo TEXT,
            academic_societies TEXT DEFAULT ''
        )
    """)
    for column, definition in (
        ("field", "TEXT"),
        ("school_of_thought", "TEXT"),
        ("key_concepts", "TEXT"),
        ("memo", "TEXT DEFAULT ''"),
        ("academic_societies", "TEXT DEFAULT ''"),
    ):
        _ensure_column(conn, "researcher_profiles", column, definition)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS academic_societies (
            name TEXT PRIMARY KEY,
            related_authors TEXT DEFAULT '',
            category TEXT DEFAULT '未分類',
            official_url TEXT DEFAULT '',
            user_memo TEXT DEFAULT '',
            is_target INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS system_settings (
            key TEXT PRIMARY KEY,
            val TEXT
        )
    """)
    initialize_discovery_schema(conn)
    initialize_research_watch_schema(conn)
    conn.executemany(
        "INSERT OR IGNORE INTO system_settings (key, val) VALUES (?, ?)",
        [
            ("col_label_0", "第1列（メイン）"),
            ("col_label_1", "第2列（重要著者）"),
            ("col_label_2", "第3列（参考）"),
        ],
    )


def _ensure_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    existing_columns = {
        row[1] for row in conn.execute(f"PRAGMA table_info({table})")
    }
    if column not in existing_columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def create_database_backup(db_path: str | PathLike[str]) -> bytes:
    """Create a consistent SQLite snapshot, including committed WAL contents."""
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as temporary_file:
            temporary_path = temporary_file.name

        with closing(sqlite3.connect(db_path, timeout=30.0)) as source:
            with closing(sqlite3.connect(temporary_path, timeout=30.0)) as destination:
                source.backup(destination)

        with open(temporary_path, "rb") as backup_file:
            return backup_file.read()
    finally:
        if temporary_path and os.path.exists(temporary_path):
            os.remove(temporary_path)


def export_papers_csv(db_path: str | PathLike[str]) -> bytes:
    """Export all paper fields as UTF-8 with BOM for spreadsheet compatibility."""
    output = io.StringIO(newline="")
    with closing(sqlite3.connect(db_path, timeout=30.0)) as connection:
        cursor = connection.execute("SELECT * FROM papers ORDER BY id")
        writer = csv.writer(output)
        writer.writerow(column[0] for column in cursor.description)
        writer.writerows(cursor.fetchall())
    return output.getvalue().encode("utf-8-sig")


def get_connection(
    db_path: str | PathLike[str] | None = None,
    isolation_level: str | None = "",
) -> sqlite3.Connection:
    """Open the configured SQLite database, or an explicit test database."""
    configured_path = (
        db_path
        if db_path is not None
        else os.environ.get("ACADEMIC_TRACKER_DB_PATH", "papers.db")
    )
    conn = sqlite3.connect(
        configured_path,
        timeout=30.0,
        isolation_level=isolation_level,
    )
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.row_factory = sqlite3.Row
    return conn
