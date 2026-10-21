import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import requests
from cron_fetcher import fetch_papers_from_cinii_api
from cron_fetcher import download_pdf_file, fetch_pdf_url_from_unpaywall
from research_discovery import (
    analyze_research_baseline,
    build_baseline_context,
    create_discovery_run,
    deduplicate_candidates,
    evaluate_candidate_batches,
    filter_discovery_candidates,
    get_candidate_evaluation_label,
    get_candidates_ready_for_evaluation_retry,
    get_discovery_processing_status,
    get_discovery_candidates,
    get_discovery_runs,
    get_candidates_ready_for_processing,
    initialize_discovery_schema,
    is_pdf_candidate_eligible,
    merge_candidate_into_papers,
    normalize_extraction,
    normalize_doi,
    parse_relevance_response,
    save_candidate_evaluations,
    save_research_baseline,
    search_discovery_candidates,
    set_discovery_candidate_selected,
    split_text,
    update_research_baseline_analysis,
    validate_pdf_batch_size,
)


class ResearchDiscoveryTests(unittest.TestCase):
    def test_baseline_context_normalizes_analysis_for_evaluation(self):
        context = build_baseline_context("Baseline", {
            "research_question": "Question",
            "keywords": ["agency", "agency", ""],
            "concepts": ["participation"],
            "researchers": [
                {"name": "Jane Doe", "role": "参考文献", "evidence": "p. 4"},
                {"name": "Other person", "role": "本文中の言及"},
            ],
        })

        self.assertEqual(context, {
            "title": "Baseline",
            "research_question": "Question",
            "keywords": ["agency"],
            "concepts": ["participation"],
            "researchers": [
                {"name": "Jane Doe", "role": "参考文献", "evidence": "p. 4"}
            ],
        })

    def test_baseline_analysis_merges_chunks_and_deduplicates_researchers(self):
        generated_responses = iter([
            json.dumps({
                "research_question": "First question",
                "keywords": ["community", "agency"],
                "concepts": ["participation"],
                "researchers": [
                    {"name": "Jane Doe", "role": "参考文献", "evidence": "p. 1"}
                ],
            }),
            json.dumps({
                "research_question": "Later question",
                "keywords": ["agency", "platform"],
                "concepts": ["participation", "dialogue"],
                "researchers": [
                    {"name": "jane doe", "role": "参考文献", "evidence": "p. 2"},
                    {"name": "John Smith", "role": "参考文献", "evidence": "p. 3"},
                ],
            }),
        ])
        prompts = []

        def generate_text(prompt):
            prompts.append(prompt)
            return next(generated_responses)

        result = analyze_research_baseline(
            "Baseline paper",
            "baseline.pdf",
            lambda path: "A" * 15000,
            generate_text,
        )

        self.assertEqual(len(prompts), 2)
        self.assertEqual(result["research_question"], "First question")
        self.assertEqual(result["keywords"], ["community", "agency", "platform"])
        self.assertEqual(result["concepts"], ["participation", "dialogue"])
        self.assertEqual(
            [researcher["name"] for researcher in result["researchers"]],
            ["Jane Doe", "John Smith"],
        )
        self.assertEqual(result["researchers"][0]["evidence"], "p. 1")

    def test_baseline_analysis_surfaces_invalid_model_json(self):
        with self.assertRaisesRegex(ValueError, "JSONオブジェクト"):
            analyze_research_baseline(
                "Baseline paper",
                "baseline.pdf",
                lambda path: "Enough extracted text",
                lambda prompt: "not JSON",
            )

    def test_research_baseline_save_and_analysis_update(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute("CREATE TABLE IF NOT EXISTS papers (id INTEGER PRIMARY KEY, doi TEXT)")
        initialize_discovery_schema(connection)

        first_analysis = {"keywords": ["first"], "researchers": []}
        save_research_baseline(
            connection, "First title", "first.pdf", first_analysis
        )
        first_timestamp = connection.execute(
            "SELECT updated_at FROM research_baseline WHERE id = 1"
        ).fetchone()[0]

        updated_analysis = {"keywords": ["updated"], "researchers": []}
        update_research_baseline_analysis(connection, updated_analysis)
        save_research_baseline(
            connection, "Replacement title", "replacement.pdf", updated_analysis
        )

        row = connection.execute(
            "SELECT title, local_pdf_path, analysis_json FROM research_baseline WHERE id = 1"
        ).fetchone()
        self.assertEqual(row[0], "Replacement title")
        self.assertEqual(row[1], "replacement.pdf")
        self.assertEqual(json.loads(row[2]), updated_analysis)
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM research_baseline"
        ).fetchone()[0], 1)
        self.assertGreaterEqual(
            connection.execute(
                "SELECT updated_at FROM research_baseline WHERE id = 1"
            ).fetchone()[0],
            first_timestamp,
        )

    def test_research_baseline_analysis_update_requires_existing_baseline(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute("CREATE TABLE IF NOT EXISTS papers (id INTEGER PRIMARY KEY, doi TEXT)")
        initialize_discovery_schema(connection)

        with self.assertRaisesRegex(ValueError, "基準論文が見つかりません"):
            update_research_baseline_analysis(
                connection, {"keywords": [], "researchers": []}
            )

    def test_discovery_history_queries_are_scoped_and_ordered(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE papers (id INTEGER PRIMARY KEY, doi TEXT)")
        initialize_discovery_schema(connection)
        first_run = create_discovery_run(
            connection, "baseline-one", {}, [
                {"title": "zeta", "matched_terms": []},
                {"title": "Alpha", "matched_terms": []},
                {"title": "Error", "matched_terms": []},
                {"title": "Material", "matched_terms": []},
                {"title": "Pending", "matched_terms": []},
            ],
        )
        second_run = create_discovery_run(
            connection, "baseline-one", {}, [{"title": "second", "matched_terms": []}]
        )
        create_discovery_run(
            connection, "baseline-two", {}, [{"title": "other baseline", "matched_terms": []}]
        )
        connection.execute(
            """UPDATE discovery_candidates SET grade = 'A', is_insufficient = 0
            WHERE run_id = ? AND title = 'zeta'""",
            (first_run,),
        )
        connection.execute(
            """UPDATE discovery_candidates SET grade = 'B', is_insufficient = 0
            WHERE run_id = ? AND title = 'Alpha'""",
            (first_run,),
        )
        connection.execute(
            """UPDATE discovery_candidates SET is_insufficient = 1,
            insufficient_reason = '判定可能な抄録がありません。'
            WHERE run_id = ? AND title = 'Material'""",
            (first_run,),
        )
        connection.execute(
            """UPDATE discovery_candidates
            SET is_insufficient = 1,
                insufficient_reason = 'AI判定エラー: temporary failure'
            WHERE run_id = ? AND title = 'Error'""",
            (first_run,),
        )

        runs = get_discovery_runs(connection, "baseline-one")
        candidates = get_discovery_candidates(connection, first_run)

        self.assertEqual([run["id"] for run in runs], [second_run, first_run])
        self.assertEqual(
            [candidate["title"] for candidate in candidates],
            ["zeta", "Alpha", "Error", "Material", "Pending"],
        )
        self.assertEqual(
            [candidate["title"] for candidate in filter_discovery_candidates(candidates, "A")],
            ["zeta"],
        )
        self.assertEqual(
            [candidate["title"] for candidate in filter_discovery_candidates(candidates, "すべて")],
            ["zeta", "Alpha", "Error", "Material", "Pending"],
        )
        self.assertEqual(
            [candidate["title"] for candidate in filter_discovery_candidates(candidates, "判定材料不足")],
            ["Material"],
        )
        self.assertEqual(
            [candidate["title"] for candidate in filter_discovery_candidates(candidates, "判定エラー")],
            ["Error"],
        )
        self.assertEqual(
            [candidate["title"] for candidate in filter_discovery_candidates(candidates, "判定待ち")],
            ["Pending"],
        )
        self.assertEqual(
            [
                candidate["title"]
                for candidate in get_candidates_ready_for_evaluation_retry(
                    connection, first_run
                )
            ],
            ["Error", "Pending"],
        )
        self.assertEqual(
            get_candidate_evaluation_label(candidates[-1]),
            "判定待ち",
        )
        with self.assertRaisesRegex(ValueError, "不明な関連度フィルター"):
            filter_discovery_candidates(candidates, "unknown")

    def test_candidate_selection_and_processing_status_are_persisted(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE papers (id INTEGER PRIMARY KEY, doi TEXT)")
        initialize_discovery_schema(connection)
        run_id = create_discovery_run(
            connection, "baseline", {}, [
                {"title": "Ready", "matched_terms": []},
                {"title": "Completed", "matched_terms": []},
                {"title": "Unqualified", "matched_terms": []},
            ],
        )
        connection.execute(
            """UPDATE discovery_candidates
            SET grade = 'A', is_insufficient = 0 WHERE run_id = ?""",
            (run_id,),
        )
        ready_id = connection.execute(
            """SELECT id FROM discovery_candidates
            WHERE run_id = ? AND title = 'Ready'""",
            (run_id,),
        ).fetchone()[0]
        completed_id = connection.execute(
            """SELECT id FROM discovery_candidates
            WHERE run_id = ? AND title = 'Completed'""",
            (run_id,),
        ).fetchone()[0]
        unqualified_id = connection.execute(
            """SELECT id FROM discovery_candidates
            WHERE run_id = ? AND title = 'Unqualified'""",
            (run_id,),
        ).fetchone()[0]
        set_discovery_candidate_selected(connection, ready_id, True)
        set_discovery_candidate_selected(connection, completed_id, True)
        set_discovery_candidate_selected(connection, unqualified_id, True)
        connection.execute(
            """UPDATE discovery_candidates SET processing_status = '完了'
            WHERE id = ?""",
            (completed_id,),
        )
        connection.execute(
            """UPDATE discovery_candidates SET is_insufficient = 1
            WHERE id = ?""",
            (unqualified_id,),
        )

        ready, completed_count = get_discovery_processing_status(connection, run_id)

        self.assertEqual([candidate["title"] for candidate in ready], ["Ready"])
        self.assertEqual(completed_count, 1)
        set_discovery_candidate_selected(connection, ready_id, False)
        self.assertEqual(
            connection.execute(
                "SELECT is_selected FROM discovery_candidates WHERE id = ?",
                (ready_id,),
            ).fetchone()[0],
            0,
        )
        with self.assertRaisesRegex(ValueError, "関連文献候補が見つかりません"):
            set_discovery_candidate_selected(connection, -1, True)

    @patch("cron_fetcher.requests.get")
    def test_cinii_parser_handles_jsonld_values_and_bare_doi(self, mock_get):
        class FakeResponse:
            status_code = 200

            def json(self):
                return {
                    "@graph": [{
                        "items": [{
                            "title": {"@value": "Candidate paper"},
                            "dc:creator": [{"@value": "Jane Doe"}],
                            "prism:publicationName": {"@value": "Journal"},
                            "prism:publicationDate": "2024-02-01",
                            "link": {"@id": "https://example.org/paper"},
                            "prism:doi": "10.1234/example",
                            "description": {"@value": "Candidate abstract"},
                        }]
                    }]
                }

        mock_get.return_value = FakeResponse()
        results = fetch_papers_from_cinii_api("agency", raise_on_error=True)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["doi"], "10.1234/example")
        self.assertEqual(results[0]["abstract"], "Candidate abstract")
        self.assertEqual(results[0]["journal"], "Journal")

    @patch("cron_fetcher.requests.get", side_effect=requests.exceptions.Timeout("timeout"))
    def test_cinii_strict_search_surfaces_request_failure(self, mock_get):
        with self.assertRaisesRegex(RuntimeError, "CiNii APIから"):
            fetch_papers_from_cinii_api("agency", raise_on_error=True)

    @patch("cron_fetcher.requests.get")
    def test_cinii_strict_search_surfaces_http_errors(self, mock_get):
        class FakeResponse:
            status_code = 503

        mock_get.return_value = FakeResponse()
        with self.assertRaisesRegex(RuntimeError, "HTTP 503"):
            fetch_papers_from_cinii_api("agency", raise_on_error=True)

    def test_candidate_search_keeps_successful_terms_and_deduplicates_results(self):
        calls = []

        def fetch_papers(query, **kwargs):
            calls.append((query, kwargs))
            if query == "failed":
                raise RuntimeError("temporary outage")
            if kwargs.get("is_author"):
                return [{
                    "title": "Shared result",
                    "doi": "10.1234/shared",
                    "author": "Researcher",
                }]
            return [
                {
                    "title": "Shared result",
                    "doi": "10.1234/shared",
                    "abstract": "Abstract",
                },
                {"title": "Keyword result", "doi": ""},
            ]

        candidates, failures = search_discovery_candidates(
            ["successful", "failed"], ["Researcher"], fetch_papers
        )

        self.assertEqual([item["title"] for item in candidates], [
            "Shared result", "Keyword result"
        ])
        self.assertEqual(candidates[0]["abstract"], "Abstract")
        self.assertEqual(
            set(candidates[0]["matched_terms"]),
            {"successful", "研究者: Researcher"},
        )
        self.assertEqual(len(failures), 1)
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(call[1]["raise_on_error"] for call in calls))

    def test_candidate_search_raises_when_all_terms_fail(self):
        def fetch_papers(query, **kwargs):
            raise RuntimeError(f"{query} unavailable")

        with self.assertRaisesRegex(RuntimeError, "CiNii検索に失敗"):
            search_discovery_candidates(
                ["keyword"], ["Researcher"], fetch_papers
            )

    def test_evaluation_results_and_failures_are_saved_to_run_candidates(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute("""
            CREATE TABLE papers (id INTEGER PRIMARY KEY, doi TEXT)
        """)
        initialize_discovery_schema(connection)
        run_id = connection.execute(
            """INSERT INTO discovery_runs (baseline_updated_at, criteria_json)
            VALUES ('2026-10-20', '{}')"""
        ).lastrowid
        connection.executemany(
            """INSERT INTO discovery_candidates
            (run_id, title, is_insufficient, insufficient_reason)
            VALUES (?, ?, 1, 'AI判定待ち')""",
            [(run_id, "Evaluated"), (run_id, "Failed")],
        )

        save_candidate_evaluations(
            connection,
            [{"title": "Evaluated"}, {"title": "Failed"}],
            {
                "0": {
                    "grade": "B",
                    "content_reason": "Topic is related",
                    "researcher_reason": "No direct author match",
                    "insufficient_reason": "",
                    "insufficient": False,
                }
            },
            {"1": "temporary Gemini outage"},
            run_id=run_id,
        )

        rows = connection.execute(
            """SELECT title, grade, content_reason, researcher_reason,
            insufficient_reason, is_insufficient
            FROM discovery_candidates WHERE run_id = ? ORDER BY title""",
            (run_id,),
        ).fetchall()
        self.assertEqual(rows[0]["title"], "Evaluated")
        self.assertEqual(rows[0]["grade"], "B")
        self.assertEqual(rows[0]["content_reason"], "Topic is related")
        self.assertEqual(rows[0]["researcher_reason"], "No direct author match")
        self.assertEqual(rows[0]["is_insufficient"], 0)
        self.assertEqual(rows[1]["title"], "Failed")
        self.assertEqual(rows[1]["grade"], "")
        self.assertEqual(rows[1]["is_insufficient"], 1)
        self.assertEqual(
            rows[1]["insufficient_reason"],
            "AI判定エラー: temporary Gemini outage",
        )

    def test_new_candidates_are_pending_not_marked_as_insufficient(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE papers (id INTEGER PRIMARY KEY, doi TEXT)")
        initialize_discovery_schema(connection)

        run_id = create_discovery_run(
            connection, "baseline", {}, [{"title": "Pending paper"}]
        )

        candidate = connection.execute(
            "SELECT * FROM discovery_candidates WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        self.assertEqual(candidate["is_insufficient"], 0)
        self.assertEqual(candidate["insufficient_reason"], "AI判定待ち")
        self.assertEqual(get_candidate_evaluation_label(candidate), "判定待ち")
        self.assertEqual(
            [row["title"] for row in get_candidates_ready_for_evaluation_retry(
                connection, run_id
            )],
            ["Pending paper"],
        )

    def test_evaluation_retry_updates_candidate_by_id(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE papers (id INTEGER PRIMARY KEY, doi TEXT)")
        initialize_discovery_schema(connection)
        run_id = connection.execute(
            """INSERT INTO discovery_runs (baseline_updated_at, criteria_json)
            VALUES ('2026-10-20', '{}')"""
        ).lastrowid
        candidate_id = connection.execute(
            """INSERT INTO discovery_candidates
            (run_id, title, is_insufficient, insufficient_reason)
            VALUES (?, 'Retry candidate', 1, 'AI判定エラー: old error')""",
            (run_id,),
        ).lastrowid

        save_candidate_evaluations(
            connection,
            [{"id": candidate_id, "title": "Retry candidate"}],
            {
                "0": {
                    "grade": "A",
                    "content_reason": "Strong topic match",
                    "researcher_reason": "Author is named in references",
                    "insufficient_reason": "",
                    "insufficient": False,
                }
            },
            {},
        )

        row = connection.execute(
            "SELECT * FROM discovery_candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
        self.assertEqual(row["run_id"], run_id)
        self.assertEqual(row["grade"], "A")
        self.assertEqual(row["is_insufficient"], 0)
        self.assertEqual(row["insufficient_reason"], "")

    @patch("cron_fetcher.requests.get")
    def test_pdf_download_validates_and_atomically_saves_pdf(self, mock_get):
        class FakeResponse:
            status_code = 200
            headers = {"Content-Type": "application/pdf"}

            def iter_content(self, chunk_size):
                yield b"%PDF-1.4\nbody"

        mock_get.return_value = FakeResponse()
        with tempfile.TemporaryDirectory() as temp_dir:
            target_path = os.path.join(temp_dir, "paper.pdf")
            self.assertTrue(download_pdf_file(
                "https://example.org/paper.pdf",
                target_path,
                raise_on_error=True,
            ))
            with open(target_path, "rb") as downloaded:
                self.assertTrue(downloaded.read().startswith(b"%PDF-"))
            self.assertFalse(os.path.exists(target_path + ".part"))

    @patch("cron_fetcher.requests.get")
    def test_pdf_download_rejects_invalid_payload_and_cleans_partial_file(self, mock_get):
        class FakeResponse:
            status_code = 200
            headers = {"Content-Type": "application/pdf"}

            def iter_content(self, chunk_size):
                yield b"not a PDF"

        mock_get.return_value = FakeResponse()
        with tempfile.TemporaryDirectory() as temp_dir:
            target_path = os.path.join(temp_dir, "paper.pdf")
            with self.assertRaisesRegex(RuntimeError, "有効なPDF"):
                download_pdf_file(
                    "https://example.org/paper.pdf",
                    target_path,
                    raise_on_error=True,
                )
            self.assertFalse(os.path.exists(target_path))
            self.assertFalse(os.path.exists(target_path + ".part"))

    @patch("cron_fetcher.requests.get")
    def test_unpaywall_requires_contact_email_before_request(self, mock_get):
        with patch.dict(os.environ, {"UNPAYWALL_EMAIL": ""}):
            with self.assertRaisesRegex(RuntimeError, "UNPAYWALL_EMAIL"):
                fetch_pdf_url_from_unpaywall(
                    "10.1234/example", raise_on_error=True
                )
        mock_get.assert_not_called()

    @patch("cron_fetcher.requests.get")
    def test_unpaywall_returns_open_access_pdf_url_and_sends_email(self, mock_get):
        class FakeResponse:
            status_code = 200

            def json(self):
                return {
                    "is_oa": True,
                    "best_oa_location": {
                        "url_for_pdf": "https://example.org/paper.pdf"
                    },
                }

        mock_get.return_value = FakeResponse()
        with patch.dict(os.environ, {"UNPAYWALL_EMAIL": "researcher@example.org"}):
            result = fetch_pdf_url_from_unpaywall(
                "https://doi.org/10.1234/example", raise_on_error=True
            )

        self.assertEqual(result, "https://example.org/paper.pdf")
        self.assertEqual(
            mock_get.call_args.kwargs["params"],
            {"email": "researcher@example.org"},
        )

    @patch("cron_fetcher.requests.get")
    def test_unpaywall_non_open_access_returns_empty(self, mock_get):
        class FakeResponse:
            status_code = 200

            def json(self):
                return {"is_oa": False}

        mock_get.return_value = FakeResponse()
        with patch.dict(os.environ, {"UNPAYWALL_EMAIL": "researcher@example.org"}):
            result = fetch_pdf_url_from_unpaywall(
                "10.1234/example", raise_on_error=True
            )
        self.assertEqual(result, "")

    @patch("cron_fetcher.requests.get")
    def test_unpaywall_strict_mode_surfaces_http_errors(self, mock_get):
        class FakeResponse:
            status_code = 429

        mock_get.return_value = FakeResponse()
        with patch.dict(os.environ, {"UNPAYWALL_EMAIL": "researcher@example.org"}):
            with self.assertRaisesRegex(RuntimeError, "HTTP 429"):
                fetch_pdf_url_from_unpaywall(
                    "10.1234/example", raise_on_error=True
                )

    def test_normalize_extraction_keeps_researcher_evidence(self):
        result = normalize_extraction({
            "research_question": "How is agency formed?",
            "keywords": ["agency", "agency", "", 42],
            "concepts": ["participation"],
            "researchers": [
                {"name": "Jane Doe", "role": "参考文献", "evidence": "p. 10"},
                {"name": "John Smith", "role": "本文中の言及"},
                "Old legacy string",
            ],
        })

        self.assertEqual(result["keywords"], ["agency", "42"])
        self.assertEqual(result["researchers"][0]["role"], "参考文献")
        self.assertEqual(result["researchers"][0]["evidence"], "p. 10")
        self.assertEqual([r["name"] for r in result["researchers"]], ["Jane Doe"])

    def test_relevance_missing_result_is_marked_insufficient(self):
        candidates = [{"title": "A"}, {"title": "B"}]
        response = json.dumps({
            "candidates": [{
                "candidate_id": "0",
                "grade": "A",
                "content_reason": "Close topic",
                "researcher_reason": "No match",
                "insufficient": False,
            }]
        })

        result = parse_relevance_response(response, candidates)

        self.assertEqual(result["0"]["grade"], "A")
        self.assertTrue(result["1"]["insufficient"])
        self.assertEqual(result["1"]["grade"], "")

    def test_invalid_grade_is_not_exposed_as_a_score(self):
        result = parse_relevance_response(
            '{"candidates":[{"candidate_id":"0","grade":"A+","insufficient":false}]}',
            [{"title": "A"}],
        )

        self.assertTrue(result["0"]["insufficient"])
        self.assertEqual(result["0"]["grade"], "")

    def test_content_reason_is_sufficient_without_researcher_reason(self):
        result = parse_relevance_response(
            json.dumps({
                "candidates": [{
                    "candidate_id": "0",
                    "grade": "B",
                    "content_reason": "The abstract directly examines the same process.",
                    "researcher_reason": "",
                    "insufficient": False,
                }]
            }),
            [{"title": "Related", "abstract": "Study of the same process."}],
        )

        self.assertEqual(result["0"]["grade"], "B")
        self.assertFalse(result["0"]["insufficient"])
        self.assertEqual(result["0"]["researcher_reason"], "研究者関連の根拠なし")

    def test_string_false_is_not_treated_as_insufficient(self):
        result = parse_relevance_response(
            json.dumps({
                "candidates": [{
                    "candidate_id": "0",
                    "grade": "C",
                    "content_reason": "The topic is partially related.",
                    "researcher_reason": "No confirmed match.",
                    "insufficient": "false",
                }]
            }),
            [{"title": "Somewhat related"}],
        )

        self.assertFalse(result["0"]["insufficient"])
        self.assertEqual(result["0"]["grade"], "C")

    def test_relevance_prompt_prioritizes_content_and_allows_title_only(self):
        from research_discovery import build_relevance_prompt

        prompt = build_relevance_prompt(
            {"title": "Baseline", "keywords": ["community"]},
            [{"title": "A candidate"}],
        )

        self.assertIn("研究者名の一致は補足情報", prompt)
        self.assertIn("抄録がないという理由だけで insufficient=true にしない", prompt)
        self.assertIn("タイトル・抄録のどちらもなく", prompt)

    def test_candidates_are_deduplicated_by_doi_and_normalized_title(self):
        result = deduplicate_candidates([
            {"title": "Community & Agency", "doi": "10.1/x", "matched_terms": ["agency"]},
            {"title": "Other title", "doi": "10.1/x", "abstract": "Abstract"},
            {"title": "Community Agency", "doi": "", "author": "A. Author"},
        ])

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["abstract"], "Abstract")
        self.assertEqual(result[0]["author"], "A. Author")
        self.assertEqual(result[0]["matched_terms"], ["agency"])

    def test_split_text_preserves_all_source_characters(self):
        text = "A" * 2500 + "\n" + "B" * 2500
        chunks = split_text(text, max_chars=1000, overlap=100)
        joined = "".join(chunks)

        self.assertGreater(len(chunks), 2)
        self.assertIn("A" * 200, joined)
        self.assertIn("B" * 200, joined)
        self.assertTrue(all(len(chunk) <= 1000 for chunk in chunks))

    def test_split_text_rejects_invalid_chunk_settings(self):
        with self.assertRaises(ValueError):
            split_text("text", max_chars=100, overlap=100)

    def test_pdf_batch_limit_is_enforced(self):
        validate_pdf_batch_size(10)
        with self.assertRaises(ValueError):
            validate_pdf_batch_size(11)

    def test_only_high_confidence_candidates_are_pdf_eligible(self):
        self.assertTrue(is_pdf_candidate_eligible("A", False))
        self.assertTrue(is_pdf_candidate_eligible("B", False))
        self.assertFalse(is_pdf_candidate_eligible("C", False))
        self.assertFalse(is_pdf_candidate_eligible("A", True))

    def test_processing_retry_returns_only_selected_incomplete_candidates(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute("""
            CREATE TABLE discovery_candidates (
                id INTEGER PRIMARY KEY,
                run_id INTEGER,
                title TEXT,
                is_selected INTEGER,
                is_insufficient INTEGER,
                grade TEXT,
                processing_status TEXT
            )
        """)
        connection.executemany("""
            INSERT INTO discovery_candidates
                (id, run_id, title, is_selected, is_insufficient, grade, processing_status)
            VALUES (?, 1, ?, ?, ?, ?, ?)
        """, [
            (1, "Pending", 1, 0, "A", "候補"),
            (2, "Failed retry", 1, 0, "B", "失敗"),
            (3, "Already complete", 1, 0, "A", "完了"),
            (4, "Not selected", 0, 0, "A", "失敗"),
            (5, "Insufficient", 1, 1, "A", "失敗"),
            (6, "Low relevance", 1, 0, "C", "失敗"),
        ])

        ready = get_candidates_ready_for_processing(connection, 1)

        self.assertEqual([row["title"] for row in ready], ["Pending", "Failed retry"])

    def test_candidate_batch_failure_does_not_discard_other_batches(self):
        candidates = [
            {"title": f"Paper {index}", "abstract": "An abstract"}
            for index in range(3)
        ]
        calls = 0

        def evaluator(prompt):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("503 UNAVAILABLE")
            return json.dumps({
                "candidates": [{
                    "candidate_id": "0",
                    "grade": "B",
                    "content_reason": "Related content",
                    "researcher_reason": "No author match",
                    "insufficient": False,
                }]
            })

        evaluations, failures = evaluate_candidate_batches(
            {"title": "Baseline"},
            candidates,
            evaluator,
            batch_size=2,
        )

        self.assertEqual(set(failures), {"0", "1"})
        self.assertEqual(evaluations["2"]["grade"], "B")

    def test_discovery_schema_initializes_repeatedly(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute("CREATE TABLE papers (id INTEGER PRIMARY KEY, doi TEXT)")
        initialize_discovery_schema(connection)
        initialize_discovery_schema(connection)

        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertTrue({"research_baseline", "discovery_runs", "discovery_candidates"} <= tables)

    def test_merge_matches_doi_and_preserves_existing_user_fields(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute("""
            CREATE TABLE papers (
                id INTEGER PRIMARY KEY,
                title TEXT UNIQUE, author TEXT, first_author TEXT, journal TEXT,
                pub_date TEXT, url TEXT, doi TEXT, abstract TEXT, matched_keyword TEXT,
                user_memo TEXT, local_pdf_path TEXT, ai_insight TEXT, is_read INTEGER
            )
        """)
        cursor = connection.execute("""
            INSERT INTO papers (title, doi, user_memo, local_pdf_path, ai_insight, is_read)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ("Existing title", "https://doi.org/10.1234/example", "Keep this note", "local.pdf", "My summary", 1))
        existing_id = cursor.lastrowid
        candidate = {
            "title": "Different title from duplicate record",
            "doi": "10.1234/example",
            "author": "New metadata",
            "abstract": "New abstract",
        }

        merged_id = merge_candidate_into_papers(candidate, connection)
        row = connection.execute("SELECT * FROM papers WHERE id = ?", (existing_id,)).fetchone()

        self.assertEqual(merged_id, existing_id)
        self.assertEqual(row["title"], "Existing title")
        self.assertEqual(row["user_memo"], "Keep this note")
        self.assertEqual(row["local_pdf_path"], "local.pdf")
        self.assertEqual(row["ai_insight"], "My summary")
        self.assertEqual(row["is_read"], 1)
        self.assertEqual(row["author"], "New metadata")
        self.assertEqual(row["abstract"], "New abstract")

    def test_merge_new_candidate_reads_matched_terms_from_history(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute("""
            CREATE TABLE papers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT UNIQUE, author TEXT, first_author TEXT, journal TEXT,
                pub_date TEXT, url TEXT, doi TEXT, abstract TEXT, matched_keyword TEXT
            )
        """)
        candidate = {
            "title": "A new paper",
            "matched_terms_json": '["agency", "participation"]',
        }

        paper_id = merge_candidate_into_papers(candidate, connection)
        row = connection.execute(
            "SELECT matched_keyword FROM papers WHERE id = ?", (paper_id,)
        ).fetchone()

        self.assertEqual(row["matched_keyword"], "agency, participation")
        self.assertEqual(normalize_doi("doi: 10.1000/test."), "10.1000/test")


if __name__ == "__main__":
    unittest.main()
