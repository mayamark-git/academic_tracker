import json
import sqlite3
import unittest

from researcher_profiles import (
    fetch_researcher_profile_with_ai,
    merge_researcher_profile,
    parse_robust_json_profile,
    save_fetched_researcher_profile,
)


def sanitize_society_name(value: str) -> str:
    return value.removeprefix("学会:").strip()


class ResearcherProfileTests(unittest.TestCase):
    def test_profile_parser_normalizes_lists_and_filters_empty_societies(self):
        result = parse_robust_json_profile(
            "```json\n" + json.dumps({
                "field": "  Sociology ",
                "school_of_thought": "Interpretive",
                "key_concepts": ["agency", " community ", ""],
                "academic_societies": [
                    "学会:日本社会学会",
                    {"name": "学会:日本社会学会"},
                    {"society_name": "地域研究会"},
                    "情報なし",
                ],
            }, ensure_ascii=False) + "\n```",
            sanitize_society_name,
        )

        self.assertEqual(result, {
            "field": "Sociology",
            "school_of_thought": "Interpretive",
            "key_concepts": "agency, community",
            "academic_societies": "日本社会学会, 地域研究会",
        })

    def test_profile_parser_keeps_legacy_partial_json_fallback(self):
        result = parse_robust_json_profile(
            '{"field":"Sociology", "key_concepts":["agency", "community"]',
            sanitize_society_name,
        )

        self.assertEqual(result["field"], "Sociology")
        self.assertEqual(result["key_concepts"], "agency, community")

    def test_profile_fetch_uses_sanitized_name_and_search_model(self):
        calls = []

        def generate(prompt, **kwargs):
            calls.append((prompt, kwargs))
            return json.dumps({
                "field": "Sociology",
                "academic_societies": "学会:日本社会学会",
            }, ensure_ascii=False)

        result = fetch_researcher_profile_with_ai(
            "Dr. Jane Doe (profile)",
            lambda name: "Jane Doe",
            sanitize_society_name,
            generate,
        )

        self.assertEqual(result["field"], "Sociology")
        self.assertEqual(result["academic_societies"], "日本社会学会")
        self.assertEqual(len(calls), 1)
        self.assertIn("対象研究者: Jane Doe", calls[0][0])
        self.assertEqual(calls[0][1], {"preferred_model": "gemini-3.6-flash"})

    def test_profile_fetch_returns_empty_profile_on_search_failure(self):
        def fail_search(prompt, **kwargs):
            raise RuntimeError("offline")

        with self.assertRaisesRegex(RuntimeError, "Jane Doe.*offline"):
            fetch_researcher_profile_with_ai(
                "Jane Doe",
                lambda name: name,
                sanitize_society_name,
                fail_search,
            )

    def test_profile_merge_preserves_existing_values_when_response_is_partial(self):
        result = merge_researcher_profile(
            {
                "field": "Existing field",
                "school_of_thought": "Existing school",
                "key_concepts": "Existing concepts",
                "academic_societies": "Existing society",
            },
            {
                "field": "",
                "school_of_thought": "Updated school",
                "key_concepts": " ",
                "academic_societies": "Updated society",
            },
        )

        self.assertEqual(result, {
            "field": "Existing field",
            "school_of_thought": "Updated school",
            "key_concepts": "Existing concepts",
            "academic_societies": "Updated society",
        })

    def test_profile_save_preserves_existing_fields_and_memo(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute("""
            CREATE TABLE researcher_profiles (
                name TEXT PRIMARY KEY,
                field TEXT,
                school_of_thought TEXT,
                key_concepts TEXT,
                academic_societies TEXT,
                memo TEXT
            )
        """)
        connection.execute("""
            INSERT INTO researcher_profiles
            VALUES ('Jane Doe', 'Existing field', 'Existing school',
                'Existing concepts', 'Existing society', 'Keep memo')
        """)

        saved = save_fetched_researcher_profile(connection, "Jane Doe", {
            "field": "",
            "school_of_thought": "Updated school",
            "key_concepts": "",
            "academic_societies": "",
        })

        row = connection.execute(
            "SELECT * FROM researcher_profiles WHERE name = 'Jane Doe'"
        ).fetchone()
        self.assertTrue(saved)
        self.assertEqual(row["field"], "Existing field")
        self.assertEqual(row["school_of_thought"], "Updated school")
        self.assertEqual(row["key_concepts"], "Existing concepts")
        self.assertEqual(row["academic_societies"], "Existing society")
        self.assertEqual(row["memo"], "Keep memo")

    def test_empty_profile_is_not_saved_or_counted_as_updated(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute("""
            CREATE TABLE researcher_profiles (
                name TEXT PRIMARY KEY,
                field TEXT,
                school_of_thought TEXT,
                key_concepts TEXT,
                academic_societies TEXT
            )
        """)

        saved = save_fetched_researcher_profile(
            connection,
            "Jane Doe",
            {
                "field": "",
                "school_of_thought": "",
                "key_concepts": "",
                "academic_societies": "",
            },
        )

        self.assertFalse(saved)
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM researcher_profiles").fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()
