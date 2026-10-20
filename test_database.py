import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

from database import (
    create_database_backup,
    export_papers_csv,
    get_connection,
    initialize_database,
)


class DatabaseConnectionTests(unittest.TestCase):
    def test_environment_path_uses_an_isolated_wal_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            test_db_path = os.path.join(temp_dir, "test-papers.db")
            with patch.dict(os.environ, {"ACADEMIC_TRACKER_DB_PATH": test_db_path}):
                connection = get_connection()
                try:
                    self.assertEqual(
                        connection.execute("PRAGMA database_list").fetchone()["file"],
                        test_db_path,
                    )
                    self.assertEqual(
                        connection.execute("PRAGMA journal_mode").fetchone()[0],
                        "wal",
                    )
                    connection.execute(
                        "CREATE TABLE test_records (value TEXT NOT NULL)"
                    )
                    connection.execute(
                        "INSERT INTO test_records (value) VALUES (?)",
                        ("isolated",),
                    )
                    self.assertEqual(
                        connection.execute(
                            "SELECT value FROM test_records"
                        ).fetchone()["value"],
                        "isolated",
                    )
                finally:
                    connection.close()

            self.assertTrue(os.path.isfile(test_db_path))

    def test_explicit_path_and_isolation_level_are_respected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            test_db_path = os.path.join(temp_dir, "explicit-test.db")
            connection = get_connection(test_db_path, isolation_level=None)
            try:
                self.assertIsNone(connection.isolation_level)
                self.assertEqual(
                    connection.execute("PRAGMA database_list").fetchone()["file"],
                    test_db_path,
                )
            finally:
                connection.close()

    def test_backup_includes_wal_committed_data_and_preserves_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "source.db")
            with closing(get_connection(db_path, isolation_level=None)) as connection:
                connection.execute("CREATE TABLE records (value TEXT)")
                connection.execute("INSERT INTO records VALUES ('committed')")

            backup_bytes = create_database_backup(db_path)
            self.assertTrue(backup_bytes.startswith(b"SQLite format 3"))

            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as backup_file:
                backup_path = backup_file.name
                backup_file.write(backup_bytes)
            try:
                with closing(sqlite3.connect(backup_path)) as backup:
                    self.assertEqual(
                        backup.execute("SELECT value FROM records").fetchone()[0],
                        "committed",
                    )
            finally:
                os.remove(backup_path)

            with closing(get_connection(db_path)) as source:
                self.assertEqual(
                    source.execute("SELECT value FROM records").fetchone()[0],
                    "committed",
                )

    def test_papers_csv_exports_headers_and_all_records(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "papers.db")
            with closing(get_connection(db_path, isolation_level=None)) as connection:
                connection.execute("""
                    CREATE TABLE papers (
                        id INTEGER PRIMARY KEY,
                        title TEXT,
                        user_memo TEXT
                    )
                """)
                connection.executemany(
                    "INSERT INTO papers (title, user_memo) VALUES (?, ?)",
                    [("Paper A", "Memo A"), ("Paper B", "Memo B")],
                )

            exported = export_papers_csv(db_path)
            text = exported.decode("utf-8-sig")
            self.assertIn("id,title,user_memo", text)
            self.assertIn("Paper A,Memo A", text)
            self.assertIn("Paper B,Memo B", text)

    def test_application_schema_initializes_repeatedly_and_upgrades_legacy_tables(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "legacy.db")
            with closing(get_connection(db_path, isolation_level=None)) as connection:
                connection.execute("CREATE TABLE papers (id INTEGER PRIMARY KEY, title TEXT)")
                connection.execute(
                    "CREATE TABLE researcher_profiles (name TEXT PRIMARY KEY, field TEXT)"
                )
                connection.execute("INSERT INTO papers (title) VALUES ('Legacy paper')")

                initialize_database(connection)
                initialize_database(connection)

                paper_columns = {
                    row["name"] for row in connection.execute("PRAGMA table_info(papers)")
                }
                profile_columns = {
                    row["name"]
                    for row in connection.execute("PRAGMA table_info(researcher_profiles)")
                }
                self.assertTrue({
                    "author", "first_author", "doi", "user_memo", "local_pdf_path"
                } <= paper_columns)
                self.assertTrue({"memo", "academic_societies"} <= profile_columns)
                self.assertEqual(
                    connection.execute("SELECT title FROM papers").fetchone()["title"],
                    "Legacy paper",
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM system_settings"
                    ).fetchone()[0],
                    3,
                )


if __name__ == "__main__":
    unittest.main()
