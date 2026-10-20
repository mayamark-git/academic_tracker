import json
import re
import sqlite3
from typing import Any, Callable


def parse_robust_json_profile(
    raw_text: str,
    sanitize_society_name: Callable[[str], str],
) -> dict[str, str]:
    result = {
        "field": "",
        "school_of_thought": "",
        "key_concepts": "",
        "academic_societies": "",
    }
    if not raw_text:
        return result

    json_match = re.search(r"\{[\s\S]*\}", raw_text)
    json_str = json_match.group(0) if json_match else raw_text.strip()

    parsed_data: Any = None
    try:
        parsed_data = json.loads(json_str)
    except json.JSONDecodeError:
        for key in ("field", "school_of_thought", "key_concepts", "academic_societies"):
            pattern = rf'"{key}"\s*:\s*"([^"]+)"'
            match = re.search(pattern, raw_text)
            if match:
                result[key] = match.group(1).strip()
            else:
                list_match = re.search(rf'"{key}"\s*:\s*\[([\s\S]*?)\]', raw_text)
                if list_match:
                    items = re.findall(r'"([^"]+)"', list_match.group(1))
                    result[key] = ", ".join(item.strip() for item in items if item.strip())

    if isinstance(parsed_data, dict):
        result["field"] = str(parsed_data.get("field", "") or "").strip()
        result["school_of_thought"] = str(
            parsed_data.get("school_of_thought", "") or ""
        ).strip()

        concepts = parsed_data.get("key_concepts", "")
        if isinstance(concepts, list):
            result["key_concepts"] = ", ".join(
                str(item).strip() for item in concepts if str(item).strip()
            )
        else:
            result["key_concepts"] = str(concepts or "").strip()

        societies = parsed_data.get("academic_societies", "")
        if isinstance(societies, list):
            society_items = []
            for item in societies:
                if isinstance(item, dict):
                    name = item.get("name", "") or item.get("society_name", "")
                    society = sanitize_society_name(str(name))
                else:
                    society = sanitize_society_name(str(item))
                if society and society not in {"不明", "なし", "特になし", "情報なし"}:
                    society_items.append(society)
            result["academic_societies"] = ", ".join(dict.fromkeys(society_items))
        elif isinstance(societies, str):
            society_items = [
                sanitize_society_name(item)
                for item in re.split(r"[,、\n/;]", societies)
                if item.strip()
            ]
            valid_societies = [
                society for society in society_items
                if society and society not in {"不明", "なし", "特になし", "情報なし"}
            ]
            result["academic_societies"] = ", ".join(dict.fromkeys(valid_societies))

    return result


def fetch_researcher_profile_with_ai(
    author_name: str,
    sanitize_name: Callable[[str], str],
    sanitize_society_name: Callable[[str], str],
    generate_with_search: Callable[..., str],
) -> dict[str, str]:
    clean_author = sanitize_name(author_name)
    prompt = f"""あなたは学術研究者のプロフィール調査・情報構造化を行うリサーチエージェントです。

対象研究者: {clean_author}

以下の検索クエリ等を駆使してWeb検索を行い、客観的な所属・専攻・所属学会情報を調査してください。
1. "{clean_author}" "大学"
2. "{clean_author}" "学会"
3. "{clean_author}" "researchmap"
4. "{clean_author}" 専門 研究 分野

【出力フォーマット】
必ず有効なJSONのみを出力してください。
{{
  "field": "専門分野・研究領域（例：社会学、都市計画、文化人類学など）",
  "school_of_thought": "学問的潮流・学派・アプローチ",
  "key_concepts": "主要キーワード（カンマ区切りで3〜5個程度）",
  "academic_societies": "所属学会名（カンマ区切りで正式名称を列挙）"
}}
"""
    try:
        text = generate_with_search(
            prompt,
            preferred_model="gemini-3.6-flash",
        )
        return parse_robust_json_profile(text, sanitize_society_name)
    except Exception as exc:
        raise RuntimeError(
            f"研究者「{clean_author}」のプロフィールを取得できませんでした: {exc}"
        ) from exc


def merge_researcher_profile(
    existing: dict[str, Any] | None,
    fetched: dict[str, Any],
) -> dict[str, str]:
    fields = ("field", "school_of_thought", "key_concepts", "academic_societies")
    return {
        field: str(fetched.get(field) or "").strip()
        or str((existing or {}).get(field) or "").strip()
        for field in fields
    }


def save_fetched_researcher_profile(
    conn: sqlite3.Connection,
    name: str,
    fetched: dict[str, Any],
) -> bool:
    fields = ("field", "school_of_thought", "key_concepts", "academic_societies")
    if not any(str(fetched.get(field) or "").strip() for field in fields):
        return False

    existing_row = conn.execute(
        """SELECT field, school_of_thought, key_concepts, academic_societies
        FROM researcher_profiles WHERE name = ?""",
        (name,),
    ).fetchone()
    existing = dict(existing_row) if existing_row else None
    merged = merge_researcher_profile(existing, fetched)
    conn.execute("""
        INSERT INTO researcher_profiles (
            name, field, school_of_thought, key_concepts, academic_societies
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            field = excluded.field,
            school_of_thought = excluded.school_of_thought,
            key_concepts = excluded.key_concepts,
            academic_societies = excluded.academic_societies
    """, (name, *(merged[field] for field in fields)))
    return True
