import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from research_watch import (
    add_research_topic,
    collect_research_topic,
    delete_research_topic,
    extract_grounded_sources,
    get_due_research_topics,
    get_research_topics,
    get_research_watch_history,
    initialize_research_watch_schema,
    parse_research_keywords,
    run_due_research_topics,
)


class ResearchWatchTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            "CREATE TABLE papers (id INTEGER PRIMARY KEY, doi TEXT)"
        )
        initialize_research_watch_schema(self.connection)

    def test_topics_validate_and_deduplicate_keywords(self):
        topic_id = add_research_topic(
            self.connection,
            "Community studies",
            "community\n地域コミュニティ, community\nサードプレイス",
        )

        topic = get_research_topics(self.connection)[0]
        self.assertEqual(topic["id"], topic_id)
        self.assertEqual(
            json.loads(topic["keywords_json"]),
            ["community", "地域コミュニティ", "サードプレイス"],
        )
        with self.assertRaisesRegex(ValueError, "1件以上"):
            parse_research_keywords("  \n ")
        with self.assertRaisesRegex(ValueError, "最大5件"):
            parse_research_keywords("a\nb\nc\nd\ne\nf")
        with self.assertRaises(sqlite3.IntegrityError):
            add_research_topic(
                self.connection,
                "Community studies",
                "community",
            )

    def test_weekly_due_topics_become_due_again_after_seven_days(self):
        topic_id = add_research_topic(
            self.connection, "Community studies", "community"
        )
        now = datetime(2026, 10, 21, tzinfo=timezone.utc)

        self.assertEqual(
            [topic["id"] for topic in get_due_research_topics(self.connection, now)],
            [topic_id],
        )
        self.connection.execute(
            "UPDATE research_topics SET last_attempt_at = ? WHERE id = ?",
            ((now - timedelta(days=6)).isoformat(), topic_id),
        )
        self.assertEqual(get_due_research_topics(self.connection, now), [])
        self.connection.execute(
            "UPDATE research_topics SET last_attempt_at = ? WHERE id = ?",
            ((now - timedelta(days=7)).isoformat(), topic_id),
        )
        self.assertEqual(
            [topic["id"] for topic in get_due_research_topics(self.connection, now)],
            [topic_id],
        )

    def test_due_runner_only_collects_topics_past_weekly_interval(self):
        due_id = add_research_topic(self.connection, "Due topic", "community")
        not_due_id = add_research_topic(self.connection, "Recent topic", "place")
        now = datetime(2026, 10, 21, tzinfo=timezone.utc)
        self.connection.execute(
            "UPDATE research_topics SET last_attempt_at = ? WHERE id = ?",
            ((now - timedelta(days=2)).isoformat(), not_due_id),
        )

        outcomes = run_due_research_topics(
            self.connection,
            lambda keyword: [],
            lambda topic, papers: f"No papers for {topic}",
            lambda topic: {
                "summary": f"No new reports for {topic}",
                "sources": [],
            },
            now=now,
        )

        self.assertEqual([outcome["topic"] for outcome in outcomes], ["Due topic"])
        self.assertEqual(outcomes[0]["status"], "完了")
        self.assertEqual(
            self.connection.execute(
                "SELECT last_attempt_at FROM research_topics WHERE id = ?",
                (due_id,),
            ).fetchone()[0],
            now.isoformat(),
        )

    def test_collection_saves_both_summaries_and_grounded_sources(self):
        topic_id = add_research_topic(
            self.connection, "Community studies", "community\nplace"
        )
        paper_calls = []

        def search_papers(keyword):
            paper_calls.append(keyword)
            return [{
                "title": f"Paper for {keyword}",
                "author": "Researcher",
                "pub_date": "2025",
                "url": f"https://example.org/{keyword}",
                "doi": "",
                "abstract": "Study abstract",
            }]

        def summarize_papers(topic, papers):
            return f"{topic}: {len(papers)} papers summarized"

        def search_reports(topic):
            return {
                "summary": f"Official reports about {topic}",
                "sources": [{
                    "title": "Official report",
                    "url": "https://agency.example/report",
                }],
            }

        outcome = collect_research_topic(
            self.connection,
            topic_id,
            search_papers,
            summarize_papers,
            search_reports,
            now=datetime(2026, 10, 21, tzinfo=timezone.utc),
        )

        self.assertEqual(outcome["status"], "完了")
        self.assertEqual(outcome["item_count"], 3)
        self.assertEqual(paper_calls, ["community", "place"])
        history = get_research_watch_history(self.connection, topic_id)
        self.assertEqual(
            {row["source_type"] for row in history},
            {"academic_papers", "public_reports"},
        )
        report = next(row for row in history if row["source_type"] == "public_reports")
        self.assertEqual(
            json.loads(report["sources_json"])[0]["url"],
            "https://agency.example/report",
        )

    def test_collection_keeps_successful_source_when_other_source_fails(self):
        topic_id = add_research_topic(
            self.connection, "Community studies", "community"
        )

        outcome = collect_research_topic(
            self.connection,
            topic_id,
            lambda keyword: [],
            lambda topic, papers: "No recent papers",
            lambda topic: (_ for _ in ()).throw(RuntimeError("search unavailable")),
        )

        self.assertEqual(outcome["status"], "一部失敗")
        self.assertEqual(len(outcome["errors"]), 1)
        history = get_research_watch_history(self.connection, topic_id)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["source_type"], "academic_papers")
        self.assertIn("公的機関", history[0]["error_message"])

    def test_topic_delete_removes_its_history(self):
        topic_id = add_research_topic(
            self.connection, "Community studies", "community"
        )
        collect_research_topic(
            self.connection,
            topic_id,
            lambda keyword: [],
            lambda topic, papers: "No papers",
            lambda topic: {"summary": "No reports", "sources": [
                {"title": "Report", "url": "https://example.org/report"}
            ]},
        )

        delete_research_topic(self.connection, topic_id)

        self.assertEqual(get_research_topics(self.connection), [])
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM research_watch_runs"
            ).fetchone()[0],
            0,
        )

    def test_grounded_sources_are_http_only_and_deduplicated(self):
        class Web:
            def __init__(self, title, uri):
                self.title = title
                self.uri = uri

        class Chunk:
            def __init__(self, web):
                self.web = web

        class Metadata:
            grounding_chunks = [
                Chunk(Web("Report", "https://agency.example/report")),
                Chunk(Web("Same report", "https://agency.example/report")),
                Chunk(Web("Invalid", "javascript:alert(1)")),
                Chunk(None),
            ]

        class Candidate:
            grounding_metadata = Metadata()

        class Response:
            candidates = [Candidate()]

        self.assertEqual(
            extract_grounded_sources(Response()),
            [{"title": "Report", "url": "https://agency.example/report"}],
        )

    def test_grounded_sources_accept_sdk_dictionary_shape_and_all_candidates(self):
        response = {
            "candidates": [
                {
                    "grounding_metadata": {
                        "grounding_chunks": [{
                            "web": {
                                "title": "Grounding result",
                                "uri": "https://agency.example/grounding",
                            }
                        }]
                    }
                },
                {
                    "content": {
                        "citation_metadata": {
                            "citations": [{
                                "title": "Citation result",
                                "uri": "https://agency.example/citation",
                            }]
                        }
                    }
                },
            ]
        }

        self.assertEqual(
            extract_grounded_sources(response),
            [
                {
                    "title": "Grounding result",
                    "url": "https://agency.example/grounding",
                },
                {
                    "title": "Citation result",
                    "url": "https://agency.example/citation",
                },
            ],
        )


if __name__ == "__main__":
    unittest.main()
