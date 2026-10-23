import json
import os
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

import cron_fetcher
from database import get_connection, initialize_database
from research_discovery import (
    add_discovery_candidate_to_library,
    create_discovery_run,
    get_candidates_ready_for_processing,
    merge_candidate_into_papers,
    process_discovery_candidate,
)


class OfflineWorkflowIntegrationTests(unittest.TestCase):
    def _create_selected_candidate(self, connection, title="New discovery"):
        run_id = create_discovery_run(
            connection,
            "2026-10-20",
            {"keywords": ["community"], "researchers": []},
            [{
                "title": title,
                "author": "Researcher",
                "first_author": "Researcher",
                "journal": "Journal",
                "pub_date": "2025",
                "url": "https://example.org/article",
                "doi": "10.1234/discovery",
                "abstract": "Abstract",
                "matched_terms": ["community"],
            }],
        )
        connection.execute(
            """UPDATE discovery_candidates
            SET grade = 'A', is_insufficient = 0, is_selected = 1
            WHERE run_id = ?""",
            (run_id,),
        )
        return dict(get_candidates_ready_for_processing(connection, run_id)[0])

    def test_sync_discovery_selection_and_paper_merge_use_initialized_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "workflow.db")
            with closing(get_connection(db_path, isolation_level=None)) as connection:
                initialize_database(connection)
                connection.execute(
                    "INSERT INTO target_authors (name) VALUES (?)", ("Researcher",)
                )
                connection.execute(
                    "INSERT INTO target_keywords (keyword) VALUES (?)", ("fail-query",)
                )
                connection.execute(
                    "INSERT INTO ignored_papers (title) VALUES (?)", ("Ignored title",)
                )

            def fake_search(query, is_author=False, sort_order="0", **kwargs):
                if query == "fail-query":
                    raise RuntimeError("simulated CiNii outage")
                if sort_order == "0":
                    return [{
                        "title": "Existing paper",
                        "author": "Researcher",
                        "first_author": "Researcher",
                        "journal": "Journal",
                        "pub_date": "2024",
                        "url": "https://example.org/existing",
                        "doi": "10.1234/existing",
                        "abstract": "Original abstract",
                    }, {
                        "title": "Ignored title",
                        "author": "Researcher",
                        "first_author": "Researcher",
                        "journal": "Journal",
                        "pub_date": "2023",
                        "url": "https://example.org/ignored",
                        "doi": "",
                        "abstract": "",
                    }]
                return [{
                    "title": "Second paper",
                    "author": "Researcher",
                    "first_author": "Researcher",
                    "journal": "Journal",
                    "pub_date": "2022",
                    "url": "https://example.org/second",
                    "doi": "10.1234/second",
                    "abstract": "Second abstract",
                }, {
                    "title": "Existing paper",
                    "author": "Researcher",
                    "first_author": "Researcher",
                    "journal": "Journal",
                    "pub_date": "2024",
                    "url": "https://example.org/existing",
                    "doi": "10.1234/existing",
                    "abstract": "Duplicate response",
                }]

            with (
                patch(
                    "cron_fetcher.get_connection",
                    side_effect=lambda: closing(
                        get_connection(db_path, isolation_level=None)
                    ),
                ),
                patch(
                    "cron_fetcher.fetch_papers_from_cinii_api",
                    side_effect=fake_search,
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "新規追加: 2件"):
                    cron_fetcher.fetch_new_papers_from_cinii(raise_on_error=True)

            with closing(get_connection(db_path)) as connection:
                papers = connection.execute(
                    "SELECT * FROM papers ORDER BY title"
                ).fetchall()
                self.assertEqual([paper["title"] for paper in papers], [
                    "Existing paper", "Second paper"
                ])
                existing = connection.execute(
                    "SELECT * FROM papers WHERE title = 'Existing paper'"
                ).fetchone()
                connection.execute(
                    "UPDATE papers SET user_memo = ? WHERE id = ?",
                    ("Keep my notes", existing["id"]),
                )

                run_id = create_discovery_run(
                    connection,
                    "2026-10-20",
                    {"keywords": ["community"], "researchers": ["Researcher"]},
                    [{
                        "title": "A translated duplicate title",
                        "author": "Researcher and Collaborator",
                        "first_author": "Researcher",
                        "journal": "Updated Journal",
                        "pub_date": "2024",
                        "url": "https://example.org/duplicate",
                        "doi": "https://doi.org/10.1234/existing",
                        "abstract": "New abstract",
                        "matched_terms": ["community"],
                    }, {
                        "title": "New discovery",
                        "author": "Another Author",
                        "first_author": "Another Author",
                        "journal": "New Journal",
                        "pub_date": "2025",
                        "url": "https://example.org/new",
                        "doi": "10.1234/new",
                        "abstract": "New discovery abstract",
                        "matched_terms": ["community"],
                    }],
                )
                connection.execute(
                    """UPDATE discovery_candidates
                    SET grade = 'A', is_insufficient = 0, is_selected = 1
                    WHERE run_id = ?""",
                    (run_id,),
                )
                candidates = get_candidates_ready_for_processing(connection, run_id)
                self.assertEqual(len(candidates), 2)
                self.assertEqual(json.loads(candidates[0]["matched_terms_json"]), ["community"])

                merged_ids = [
                    merge_candidate_into_papers(dict(candidate), connection)
                    for candidate in candidates
                ]
                connection.executemany(
                    "UPDATE discovery_candidates SET paper_id = ? WHERE id = ?",
                    [
                        (merged_id, candidate["id"])
                        for merged_id, candidate in zip(merged_ids, candidates)
                    ],
                )
                updated_existing = connection.execute(
                    "SELECT * FROM papers WHERE id = ?", (existing["id"],)
                ).fetchone()
                self.assertEqual(updated_existing["user_memo"], "Keep my notes")
                self.assertEqual(updated_existing["abstract"], "Original abstract")
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM papers").fetchone()[0],
                    3,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM discovery_candidates WHERE run_id = ? AND paper_id IS NOT NULL",
                        (run_id,),
                    ).fetchone()[0],
                    2,
                )

    def test_candidate_processing_records_failure_and_resumes_to_completion(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "processing.db")
            pdf_dir = os.path.join(temp_dir, "pdfs")
            with closing(get_connection(db_path, isolation_level=None)) as connection:
                initialize_database(connection)
                candidate = self._create_selected_candidate(connection)

            connection_factory = lambda: get_connection(
                db_path, isolation_level=None
            )
            warning_messages = []
            paper_id, error = process_discovery_candidate(
                candidate,
                connection_factory,
                pdf_dir,
                fetch_pdf_url=lambda doi, **kwargs: "",
                get_jstage_pdf_url=lambda url: "",
                download_pdf=lambda *args, **kwargs: self.fail(
                    "Downloader should not run when no PDF URL is found"
                ),
                summarize_pdf=lambda title, path: self.fail(
                    "Summary should not run when PDF retrieval fails"
                ),
                on_warning=warning_messages.append,
            )

            self.assertIsNone(paper_id)
            self.assertEqual(error, "公開PDFの直リンクが見つかりませんでした。")
            with closing(get_connection(db_path)) as connection:
                failed_row = connection.execute(
                    "SELECT * FROM discovery_candidates WHERE id = ?",
                    (candidate["id"],),
                ).fetchone()
                self.assertEqual(failed_row["processing_status"], "PDF未入手")
                self.assertIsNone(failed_row["paper_id"])

            def download_pdf(url, path, **kwargs):
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as downloaded:
                    downloaded.write(b"%PDF-1.4\nfixture")
                return True

            retry_candidate = dict(failed_row)
            retry_candidate["doi"] = "10.1234/discovery"
            retry_candidate["url"] = "https://example.org/article"
            retry_paper_id, retry_error = process_discovery_candidate(
                retry_candidate,
                connection_factory,
                pdf_dir,
                fetch_pdf_url=lambda doi, **kwargs: "",
                get_jstage_pdf_url=lambda url: (
                    "https://example.org/article.pdf"
                ),
                download_pdf=download_pdf,
                summarize_pdf=lambda title, path: "Verified summary",
                on_warning=warning_messages.append,
            )

            self.assertIsNone(retry_paper_id)
            self.assertIsNone(retry_error)
            with closing(get_connection(db_path)) as connection:
                completed = connection.execute(
                    """SELECT processing_status, processing_error,
                    local_pdf_path, ai_insight, paper_id
                    FROM discovery_candidates WHERE id = ?""",
                    (candidate["id"],),
                ).fetchone()
                self.assertEqual(completed["processing_status"], "完了")
                self.assertEqual(completed["processing_error"], "")
                self.assertTrue(os.path.isfile(completed["local_pdf_path"]))
                self.assertEqual(completed["ai_insight"], "Verified summary")
                self.assertIsNone(completed["paper_id"])
                paper_id = add_discovery_candidate_to_library(
                    connection, candidate["id"]
                )
                library_paper = connection.execute(
                    "SELECT * FROM papers WHERE id = ?", (paper_id,)
                ).fetchone()
                self.assertEqual(
                    library_paper["local_pdf_path"],
                    completed["local_pdf_path"],
                )
                self.assertEqual(library_paper["ai_insight"], "Verified summary")
            self.assertEqual(warning_messages, [])

    def test_explicit_candidate_import_preserves_existing_user_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "import.db")
            with closing(get_connection(db_path, isolation_level=None)) as connection:
                initialize_database(connection)
                cursor = connection.execute(
                    """INSERT INTO papers
                    (title, doi, user_memo, is_read, local_pdf_path, ai_insight)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        "Existing title",
                        "10.1234/import",
                        "Keep this note",
                        1,
                        "existing.pdf",
                        "Existing summary",
                    ),
                )
                existing_id = cursor.lastrowid
                run_id = create_discovery_run(
                    connection,
                    "baseline",
                    {"keywords": ["topic"]},
                    [{
                        "title": "New title for same paper",
                        "doi": "https://doi.org/10.1234/import",
                        "author": "Researcher",
                        "abstract": "New abstract",
                        "matched_terms": ["topic"],
                    }],
                )
                candidate_id = connection.execute(
                    "SELECT id FROM discovery_candidates WHERE run_id = ?",
                    (run_id,),
                ).fetchone()["id"]
                imported_id = add_discovery_candidate_to_library(
                    connection, candidate_id
                )
                paper = connection.execute(
                    "SELECT * FROM papers WHERE id = ?", (existing_id,)
                ).fetchone()

                self.assertEqual(imported_id, existing_id)
                self.assertEqual(paper["user_memo"], "Keep this note")
                self.assertEqual(paper["is_read"], 1)
                self.assertEqual(paper["local_pdf_path"], "existing.pdf")
                self.assertEqual(paper["ai_insight"], "Existing summary")
                self.assertEqual(paper["abstract"], "New abstract")
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM papers").fetchone()[0],
                    1,
                )


if __name__ == "__main__":
    unittest.main()
