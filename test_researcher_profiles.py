import json
import unittest

from researcher_profiles import (
    fetch_researcher_profile_with_ai,
    parse_robust_json_profile,
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

        result = fetch_researcher_profile_with_ai(
            "Jane Doe",
            lambda name: name,
            sanitize_society_name,
            fail_search,
        )

        self.assertEqual(result, {
            "field": "",
            "school_of_thought": "",
            "key_concepts": "",
            "academic_societies": "",
        })


if __name__ == "__main__":
    unittest.main()
