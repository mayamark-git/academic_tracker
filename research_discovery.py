import json
import os
import re
import sqlite3
from contextlib import closing
from typing import Any, Callable

MAX_KEYWORD_QUERIES = 5
MAX_RESEARCHER_QUERIES = 5
PAPERS_PER_QUERY = 30


def search_discovery_candidates(
    keywords: list[str],
    researchers: list[str],
    fetch_papers: Callable[..., list[dict[str, Any]]],
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    candidates = []
    failures = []
    clean_keywords = list(dict.fromkeys(
        str(keyword).strip() for keyword in keywords if str(keyword).strip()
    ))
    clean_researchers = list(dict.fromkeys(
        str(researcher).strip()
        for researcher in researchers
        if str(researcher).strip()
    ))
    if len(clean_keywords) > MAX_KEYWORD_QUERIES:
        raise ValueError(
            f"キーワード・概念は最大{MAX_KEYWORD_QUERIES}件まで検索できます。"
        )
    if len(clean_researchers) > MAX_RESEARCHER_QUERIES:
        raise ValueError(
            f"研究者名は最大{MAX_RESEARCHER_QUERIES}人まで検索できます。"
        )
    queries = [
        (keyword, False, keyword)
        for keyword in clean_keywords
    ] + [
        (researcher, True, f"研究者: {researcher}")
        for researcher in clean_researchers
    ]

    for query_index, (query, is_author, matched_term) in enumerate(queries, start=1):
        if on_progress:
            on_progress({
                "status": "started",
                "completed": query_index - 1,
                "total": len(queries),
                "query": query,
                "is_author": is_author,
            })
        try:
            results = fetch_papers(
                query,
                is_author=is_author,
                max_results=PAPERS_PER_QUERY,
                raise_on_error=True,
            )
            for result in results:
                result["matched_terms"] = [matched_term]
                candidates.append(result)
            if on_progress:
                on_progress({
                    "status": "completed",
                    "completed": query_index,
                    "total": len(queries),
                    "query": query,
                    "is_author": is_author,
                    "result_count": len(results),
                })
        except Exception as exc:
            label = f"研究者「{query}」" if is_author else f"キーワード「{query}」"
            failure = f"{label}: {exc}"
            failures.append(failure)
            if on_progress:
                on_progress({
                    "status": "failed",
                    "completed": query_index,
                    "total": len(queries),
                    "query": query,
                    "is_author": is_author,
                    "error": str(exc),
                })

    return deduplicate_candidates(candidates), failures


def parse_json_response(raw_text: str) -> dict[str, Any]:
    text = (raw_text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("AI応答にJSONオブジェクトがありません。")
    try:
        result = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"AI応答のJSONを読み取れません: {exc}") from exc
    if not isinstance(result, dict):
        raise ValueError("AI応答のJSON形式が不正です。")
    return result


def normalize_extraction(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("抽出結果はJSONオブジェクトである必要があります。")

    def clean_strings(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return list(dict.fromkeys(
            str(item).strip()
            for item in value
            if isinstance(item, (str, int, float)) and str(item).strip()
        ))

    researchers = []
    raw_researchers = payload.get("researchers", [])
    if isinstance(raw_researchers, list):
        for item in raw_researchers:
            if isinstance(item, str):
                name = item.strip()
                role = "参考文献"
                evidence = "参考文献に記載"
            elif isinstance(item, dict):
                name = str(item.get("name", "")).strip()
                role = str(item.get("role", "")).strip()
                evidence = str(item.get("evidence", "")).strip()
            else:
                continue
            if name and "参考文献" in role:
                researchers.append({
                    "name": name,
                    "role": "参考文献",
                    "evidence": evidence,
                })

    return {
        "research_question": str(payload.get("research_question", "")).strip(),
        "keywords": clean_strings(payload.get("keywords")),
        "concepts": clean_strings(payload.get("concepts")),
        "researchers": researchers,
    }


def build_baseline_context(
    title: str,
    analysis: dict[str, Any],
) -> dict[str, Any]:
    normalized = normalize_extraction(analysis)
    return {
        "title": title,
        "research_question": normalized["research_question"],
        "keywords": normalized["keywords"],
        "concepts": normalized["concepts"],
        "researchers": normalized["researchers"],
    }


def analyze_research_baseline(
    title: str,
    pdf_path: str,
    extract_pdf_text: Callable[[str], str],
    generate_text: Callable[[str], str],
) -> dict[str, Any]:
    chunks = split_text(extract_pdf_text(pdf_path))
    merged = {
        "research_question": "",
        "keywords": [],
        "concepts": [],
        "researchers": [],
    }
    seen_researchers = set()

    for chunk in chunks:
        prompt = build_extraction_prompt(title, chunk)
        extracted = normalize_extraction(
            parse_json_response(generate_text(prompt))
        )
        if extracted["research_question"] and not merged["research_question"]:
            merged["research_question"] = extracted["research_question"]
        for key in ("keywords", "concepts"):
            merged[key] = list(dict.fromkeys(merged[key] + extracted[key]))
        for researcher in extracted["researchers"]:
            researcher_key = (researcher["name"].casefold(), researcher["role"])
            if researcher_key not in seen_researchers:
                seen_researchers.add(researcher_key)
                merged["researchers"].append(researcher)
    return merged


def split_text(text: str, max_chars: int = 14000, overlap: int = 500) -> list[str]:
    if max_chars <= 0 or overlap < 0 or overlap >= max_chars:
        raise ValueError("テキスト分割サイズの指定が不正です。")
    text = (text or "").strip()
    if not text:
        return []

    chunks = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            boundary = text.rfind("\n", start, end)
            if boundary > start + max_chars // 2:
                end = boundary
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


def validate_pdf_batch_size(count: int) -> None:
    if count < 1:
        raise ValueError("PDF取得対象を1件以上選択してください。")


def is_pdf_candidate_eligible(grade: str, insufficient: bool) -> bool:
    return not insufficient and grade in {"A", "B", "C", "D"}


def get_candidates_ready_for_processing(
    conn: sqlite3.Connection,
    run_id: int,
) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT * FROM discovery_candidates
        WHERE run_id = ? AND is_selected = 1
        AND is_insufficient = 0 AND grade IN ('A', 'B', 'C', 'D')
        AND (processing_status != '完了' OR paper_id IS NULL)
        ORDER BY id""",
        (run_id,),
    ).fetchall()


def get_discovery_processing_status(
    conn: sqlite3.Connection,
    run_id: int,
) -> tuple[list[sqlite3.Row], int]:
    candidates = get_candidates_ready_for_processing(conn, run_id)
    completed_count = conn.execute(
        """SELECT COUNT(*) FROM discovery_candidates
        WHERE run_id = ? AND is_selected = 1
        AND is_insufficient = 0 AND grade IN ('A', 'B', 'C', 'D')
        AND processing_status = '完了' AND paper_id IS NOT NULL""",
        (run_id,),
    ).fetchone()[0]
    return candidates, completed_count


def set_discovery_candidate_selected(
    conn: sqlite3.Connection,
    candidate_id: int,
    selected: bool,
) -> None:
    cursor = conn.execute(
        "UPDATE discovery_candidates SET is_selected = ? WHERE id = ?",
        (int(selected), candidate_id),
    )
    if cursor.rowcount != 1:
        raise ValueError(f"関連文献候補が見つかりません: {candidate_id}")


def safe_paper_pdf_path(
    pdf_directory: str,
    paper_id: int,
    title: str,
) -> str:
    safe_title = re.sub(r'[\\/*?:"<>|]', "_", str(title or "paper"))
    safe_title = safe_title.strip(" .")[:100] or "paper"
    return os.path.join(pdf_directory, f"discovery_{paper_id}_{safe_title}.pdf")


def process_discovery_candidate(
    candidate: dict[str, Any],
    connection_factory: Callable[[], sqlite3.Connection],
    pdf_directory: str,
    fetch_pdf_url: Callable[..., str],
    get_jstage_pdf_url: Callable[[str], str],
    download_pdf: Callable[..., bool],
    summarize_pdf: Callable[[str, str], str],
    on_warning: Callable[[str], None] | None = None,
) -> tuple[int | None, str | None]:
    candidate_id = int(candidate["id"])
    paper_id = candidate.get("paper_id")
    try:
        with closing(connection_factory()) as conn:
            candidate_row = conn.execute(
                """SELECT local_pdf_path, ai_insight, paper_id
                FROM discovery_candidates WHERE id = ?""",
                (candidate_id,),
            ).fetchone()
            if not candidate_row:
                raise ValueError(f"関連文献候補が見つかりません: {candidate_id}")
            paper_id = candidate_row["paper_id"]
            conn.execute(
                """UPDATE discovery_candidates
                SET processing_status = 'PDF取得中', processing_error = ''
                WHERE id = ?""",
                (candidate_id,),
            )
            paper = None
            if paper_id is not None:
                paper = conn.execute(
                    "SELECT local_pdf_path, ai_insight FROM papers WHERE id = ?",
                    (paper_id,),
                ).fetchone()
            conn.commit()

        pdf_path = candidate_row["local_pdf_path"] or ""
        summary = candidate_row["ai_insight"] or ""
        if paper:
            pdf_path = pdf_path or paper["local_pdf_path"] or ""
            summary = summary or paper["ai_insight"] or ""
        if not pdf_path or not os.path.isfile(pdf_path):
            doi = normalize_doi(str(candidate.get("doi") or ""))
            paper_url = str(candidate.get("url") or "").strip()
            pdf_url = ""
            if doi:
                try:
                    pdf_url = fetch_pdf_url(doi, raise_on_error=True)
                except Exception as exc:
                    if on_warning:
                        on_warning(
                            f"Unpaywall検索に失敗しました。J-STAGEも確認します "
                            f"({candidate['title']}): {exc}"
                        )
            if not pdf_url and paper_url:
                pdf_url = get_jstage_pdf_url(paper_url)
            if not pdf_url:
                raise RuntimeError("公開PDFの直リンクが見つかりませんでした。")
            pdf_path = safe_paper_pdf_path(
                pdf_directory, candidate_id, candidate["title"]
            )
            download_pdf(pdf_url, pdf_path, raise_on_error=True)
            with closing(connection_factory()) as conn:
                conn.execute(
                    """UPDATE discovery_candidates SET local_pdf_path = ?,
                    processing_status = '要約中' WHERE id = ?""",
                    (pdf_path, candidate_id),
                )
                conn.commit()
        else:
            with closing(connection_factory()) as conn:
                conn.execute(
                    """UPDATE discovery_candidates
                    SET processing_status = '要約中' WHERE id = ?""",
                    (candidate_id,),
                )
                conn.commit()

        if not os.path.isfile(pdf_path):
            raise RuntimeError("取得したPDFファイルを確認できませんでした。")
        if not summary:
            summary = summarize_pdf(candidate["title"], pdf_path)
        if not str(summary).strip():
            raise ValueError("AI要約を取得できませんでした。")

        with closing(connection_factory()) as conn:
            conn.execute("BEGIN")
            try:
                conn.execute(
                    """UPDATE discovery_candidates
                    SET local_pdf_path = ?, ai_insight = ?
                    WHERE id = ?""",
                    (pdf_path, summary, candidate_id),
                )
                library_paper_id = add_discovery_candidate_to_library(
                    conn, candidate_id
                )
                conn.execute("""
                    UPDATE discovery_candidates
                    SET paper_id = ?, processing_status = '完了',
                        processing_error = ''
                    WHERE id = ?
                """, (library_paper_id, candidate_id))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return library_paper_id, None
    except Exception as exc:
        error_message = str(exc)
        status = (
            "PDF未入手"
            if "公開PDFの直リンクが見つかりませんでした" in error_message
            else "失敗"
        )
        with closing(connection_factory()) as conn:
            conn.execute("""
                UPDATE discovery_candidates
                SET paper_id = COALESCE(?, paper_id),
                    processing_status = ?,
                    processing_error = ?
                WHERE id = ?
            """, (paper_id, status, error_message, candidate_id))
            conn.commit()
        return paper_id, error_message


def add_discovery_candidate_to_library(
    conn: sqlite3.Connection,
    candidate_id: int,
) -> int:
    candidate_row = conn.execute(
        "SELECT * FROM discovery_candidates WHERE id = ?",
        (candidate_id,),
    ).fetchone()
    if not candidate_row:
        raise ValueError(f"関連文献候補が見つかりません: {candidate_id}")

    candidate = dict(candidate_row)
    candidate["matched_terms"] = json.loads(
        candidate.get("matched_terms_json") or "[]"
    )
    paper_id = merge_candidate_into_papers(candidate, conn)
    conn.execute(
        """UPDATE papers
        SET local_pdf_path = CASE
                WHEN local_pdf_path IS NULL OR local_pdf_path = ''
                THEN ? ELSE local_pdf_path END,
            ai_insight = CASE
                WHEN ai_insight IS NULL OR ai_insight = ''
                THEN ? ELSE ai_insight END
        WHERE id = ?""",
        (
            candidate.get("local_pdf_path") or "",
            candidate.get("ai_insight") or "",
            paper_id,
        ),
    )
    conn.execute(
        "UPDATE discovery_candidates SET paper_id = ? WHERE id = ?",
        (paper_id, candidate_id),
    )
    return paper_id


def normalize_title(title: str) -> str:
    return re.sub(r"[\W_]+", "", (title or "").casefold(), flags=re.UNICODE)


def normalize_doi(doi: str) -> str:
    value = (doi or "").strip()
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^doi:\s*", "", value, flags=re.IGNORECASE)
    return value.rstrip(".,;)]}").strip()


def initialize_discovery_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS research_baseline (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            title TEXT NOT NULL,
            local_pdf_path TEXT NOT NULL,
            analysis_json TEXT NOT NULL DEFAULT '{}',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS discovery_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            baseline_updated_at TEXT NOT NULL,
            criteria_json TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS discovery_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            paper_id INTEGER,
            title TEXT NOT NULL,
            author TEXT DEFAULT '',
            first_author TEXT DEFAULT '',
            journal TEXT DEFAULT '',
            pub_date TEXT DEFAULT '',
            url TEXT DEFAULT '',
            doi TEXT DEFAULT '',
            abstract TEXT DEFAULT '',
            matched_terms_json TEXT NOT NULL DEFAULT '[]',
            grade TEXT DEFAULT '',
            content_reason TEXT DEFAULT '',
            researcher_reason TEXT DEFAULT '',
            insufficient_reason TEXT DEFAULT '',
            is_insufficient INTEGER DEFAULT 0,
            is_selected INTEGER DEFAULT 0,
            processing_status TEXT DEFAULT '候補',
            processing_error TEXT DEFAULT '',
            local_pdf_path TEXT DEFAULT '',
            ai_insight TEXT DEFAULT '',
            UNIQUE(run_id, title)
        )
    """)
    _ensure_discovery_column(conn, "discovery_candidates", "local_pdf_path", "TEXT DEFAULT ''")
    _ensure_discovery_column(conn, "discovery_candidates", "ai_insight", "TEXT DEFAULT ''")
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_discovery_candidates_run
        ON discovery_candidates (run_id, is_selected, grade)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_papers_doi_nocase
        ON papers (doi COLLATE NOCASE)
    """)


def _ensure_discovery_column(
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


def save_research_baseline(
    conn: sqlite3.Connection,
    title: str,
    local_pdf_path: str,
    analysis: dict[str, Any],
) -> None:
    conn.execute("""
        INSERT INTO research_baseline (id, title, local_pdf_path, analysis_json, updated_at)
        VALUES (1, ?, ?, ?, strftime('%Y-%m-%d %H:%M:%f', 'now'))
        ON CONFLICT(id) DO UPDATE SET
            title = excluded.title,
            local_pdf_path = excluded.local_pdf_path,
            analysis_json = excluded.analysis_json,
            updated_at = strftime('%Y-%m-%d %H:%M:%f', 'now')
    """, (
        title,
        local_pdf_path,
        json.dumps(analysis, ensure_ascii=False),
    ))


def update_research_baseline_analysis(
    conn: sqlite3.Connection,
    analysis: dict[str, Any],
) -> None:
    cursor = conn.execute("""
        UPDATE research_baseline
        SET analysis_json = ?,
            updated_at = strftime('%Y-%m-%d %H:%M:%f', 'now')
        WHERE id = 1
    """, (json.dumps(analysis, ensure_ascii=False),))
    if cursor.rowcount != 1:
        raise ValueError("保存対象の基準論文が見つかりません。")


def get_discovery_runs(
    conn: sqlite3.Connection,
    baseline_updated_at: str,
) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT id, created_at, criteria_json FROM discovery_runs
        WHERE baseline_updated_at = ?
        ORDER BY id DESC""",
        (baseline_updated_at,),
    ).fetchall()


def get_discovery_candidates(
    conn: sqlite3.Connection,
    run_id: int,
) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT * FROM discovery_candidates
        WHERE run_id = ?
        ORDER BY CASE grade
            WHEN 'A' THEN 0 WHEN 'B' THEN 1
            WHEN 'C' THEN 2 WHEN 'D' THEN 3 ELSE 4 END,
            title COLLATE NOCASE""",
        (run_id,),
    ).fetchall()


def get_candidates_ready_for_evaluation_retry(
    conn: sqlite3.Connection,
    run_id: int,
) -> list[sqlite3.Row]:
    retry_reason_prefix = "AI判定エラー:"
    return conn.execute(
        """SELECT * FROM discovery_candidates
        WHERE run_id = ?
        AND (
            (grade = '' AND is_insufficient = 0)
            OR (
                is_insufficient = 1
                AND (
                    insufficient_reason = 'AI判定待ち'
                    OR insufficient_reason = 'AIから判定結果が返されませんでした。'
                    OR substr(insufficient_reason, 1, ?) = ?
                )
            )
        )
        ORDER BY CASE grade
            WHEN 'A' THEN 0 WHEN 'B' THEN 1
            WHEN 'C' THEN 2 WHEN 'D' THEN 3 ELSE 4 END,
            title COLLATE NOCASE""",
        (run_id, len(retry_reason_prefix), retry_reason_prefix),
    ).fetchall()


def get_candidate_evaluation_label(candidate: Any) -> str:
    grade = str(candidate["grade"] or "").strip()
    if grade and not candidate["is_insufficient"]:
        return grade

    reason = str(candidate["insufficient_reason"] or "")
    if reason == "AI判定待ち" or (
        not candidate["is_insufficient"] and not grade
    ):
        return "判定待ち"
    if reason.startswith("AI判定エラー:") or reason == "AIから判定結果が返されませんでした。":
        return "判定エラー"
    return "判定材料不足" if candidate["is_insufficient"] else "判定待ち"


def filter_discovery_candidates(
    candidates: list[sqlite3.Row],
    grade_filter: str,
) -> list[sqlite3.Row]:
    if grade_filter == "すべて":
        return candidates
    if grade_filter in {"判定待ち", "判定エラー", "判定材料不足"}:
        return [
            candidate for candidate in candidates
            if get_candidate_evaluation_label(candidate) == grade_filter
        ]
    if grade_filter in {"A", "B", "C", "D"}:
        return [
            candidate for candidate in candidates
            if not candidate["is_insufficient"] and candidate["grade"] == grade_filter
        ]
    raise ValueError(f"不明な関連度フィルターです: {grade_filter}")


def create_discovery_run(
    conn: sqlite3.Connection,
    baseline_updated_at: str,
    criteria: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> int:
    cursor = conn.execute(
        """INSERT INTO discovery_runs (baseline_updated_at, criteria_json)
        VALUES (?, ?)""",
        (baseline_updated_at, json.dumps(criteria, ensure_ascii=False)),
    )
    run_id = int(cursor.lastrowid)
    for candidate in candidates:
        conn.execute("""
            INSERT OR IGNORE INTO discovery_candidates (
                run_id, title, author, first_author, journal, pub_date,
                url, doi, abstract, matched_terms_json, insufficient_reason,
                is_insufficient
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """, (
            run_id,
            candidate["title"],
            candidate.get("author", ""),
            candidate.get("first_author", ""),
            candidate.get("journal", ""),
            candidate.get("pub_date", ""),
            candidate.get("url", ""),
            normalize_doi(str(candidate.get("doi") or "")),
            candidate.get("abstract", ""),
            json.dumps(candidate.get("matched_terms", []), ensure_ascii=False),
            "AI判定待ち",
        ))
    return run_id


def save_candidate_evaluations(
    conn: sqlite3.Connection,
    candidates: list[dict[str, Any]],
    evaluations: dict[str, dict[str, Any]],
    failures: dict[str, str],
    run_id: int | None = None,
) -> None:
    for index, candidate in enumerate(candidates):
        evaluation = evaluations.get(str(index))
        failure = failures.get(str(index))
        candidate_id = candidate.get("id")

        if evaluation:
            query = """
                UPDATE discovery_candidates
                SET grade = ?, content_reason = ?, researcher_reason = ?,
                    insufficient_reason = ?, is_insufficient = ?
            """
            values: tuple[Any, ...] = (
                evaluation["grade"],
                evaluation["content_reason"],
                evaluation["researcher_reason"],
                evaluation["insufficient_reason"],
                int(evaluation["insufficient"]),
            )
        elif failure:
            query = """
                UPDATE discovery_candidates
                SET insufficient_reason = ?, is_insufficient = 1
            """
            values = (f"AI判定エラー: {failure}",)
        else:
            continue

        if candidate_id is not None:
            query += " WHERE id = ?"
            values += (candidate_id,)
        elif run_id is not None:
            query += " WHERE run_id = ? AND title = ?"
            values += (run_id, candidate["title"])
        else:
            raise ValueError(
                "候補の判定結果を保存するには候補IDまたは検索履歴IDが必要です。"
            )
        conn.execute(query, values)


def find_existing_paper(
    candidate: dict[str, Any],
    conn: sqlite3.Connection,
) -> sqlite3.Row | None:
    doi = normalize_doi(str(candidate.get("doi") or ""))
    if doi:
        doi_variants = (
            doi,
            f"https://doi.org/{doi}",
            f"http://doi.org/{doi}",
            f"https://dx.doi.org/{doi}",
        )
        for doi_variant in doi_variants:
            row = conn.execute(
                "SELECT * FROM papers WHERE doi = ? COLLATE NOCASE LIMIT 1",
                (doi_variant,),
            ).fetchone()
            if row:
                return row

    target_title = normalize_title(str(candidate.get("title") or ""))
    if not target_title:
        return None
    exact = conn.execute(
        "SELECT * FROM papers WHERE title = ? COLLATE NOCASE LIMIT 1",
        (candidate.get("title", ""),),
    ).fetchone()
    if exact:
        return exact
    rows = conn.execute("SELECT id, title FROM papers")
    for row in rows:
        if normalize_title(str(row["title"] or "")) == target_title:
            return conn.execute(
                "SELECT * FROM papers WHERE id = ?", (row["id"],)
            ).fetchone()
    return None


def merge_candidate_into_papers(
    candidate: dict[str, Any],
    conn: sqlite3.Connection,
) -> int:
    existing = find_existing_paper(candidate, conn)
    metadata = {
        "title": candidate.get("title") or "",
        "author": candidate.get("author") or "",
        "first_author": candidate.get("first_author") or "",
        "journal": candidate.get("journal") or "",
        "pub_date": candidate.get("pub_date") or "",
        "url": candidate.get("url") or "",
        "doi": normalize_doi(str(candidate.get("doi") or "")),
        "abstract": candidate.get("abstract") or "",
    }

    if existing:
        paper_id = int(existing["id"])
        for field, value in metadata.items():
            if value and not existing[field]:
                conn.execute(
                    f"UPDATE papers SET {field} = ? WHERE id = ?",
                    (value, paper_id),
                )
        return paper_id

    matched_terms = candidate.get("matched_terms")
    if not matched_terms and candidate.get("matched_terms_json"):
        try:
            matched_terms = json.loads(candidate["matched_terms_json"])
        except (json.JSONDecodeError, TypeError):
            matched_terms = []

    cursor = conn.execute("""
        INSERT INTO papers (
            title, author, first_author, journal, pub_date, url, doi, abstract,
            matched_keyword
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        metadata["title"],
        metadata["author"],
        metadata["first_author"],
        metadata["journal"],
        metadata["pub_date"],
        metadata["url"],
        metadata["doi"],
        metadata["abstract"],
        ", ".join(matched_terms or []),
    ))
    return int(cursor.lastrowid)


def deduplicate_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    doi_indexes: dict[str, int] = {}
    title_indexes: dict[str, int] = {}

    for candidate in candidates:
        doi = str(candidate.get("doi", "") or "").strip().lower()
        title = normalize_title(str(candidate.get("title", "") or ""))
        existing_index = doi_indexes.get(doi) if doi else None
        if existing_index is None and title:
            existing_index = title_indexes.get(title)

        if existing_index is None:
            index = len(result)
            result.append(dict(candidate))
            if doi:
                doi_indexes[doi] = index
            if title:
                title_indexes[title] = index
            continue

        existing = result[existing_index]
        for key, value in candidate.items():
            if value and not existing.get(key):
                existing[key] = value
        terms = set(existing.get("matched_terms", []))
        terms.update(candidate.get("matched_terms", []))
        existing["matched_terms"] = sorted(terms)

    return result


def parse_relevance_response(
    raw_text: str,
    candidates: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    parsed = parse_json_response(raw_text)
    raw_items = parsed.get("candidates", [])
    by_id = {
        str(item.get("candidate_id")): item
        for item in raw_items
        if isinstance(item, dict) and item.get("candidate_id") is not None
    } if isinstance(raw_items, list) else {}

    results = {}
    for index, candidate in enumerate(candidates):
        candidate_id = str(index)
        item = by_id.get(candidate_id)
        if not item:
            results[candidate_id] = {
                "grade": "",
                "content_reason": "",
                "researcher_reason": "",
                "insufficient_reason": "AIから判定結果が返されませんでした。",
                "insufficient": True,
            }
            continue

        raw_insufficient = item.get("insufficient", False)
        if isinstance(raw_insufficient, bool):
            insufficient = raw_insufficient
        elif isinstance(raw_insufficient, str) and raw_insufficient.strip().casefold() in {
            "true", "false",
        }:
            insufficient = raw_insufficient.strip().casefold() == "true"
        else:
            insufficient = True
        grade = str(item.get("grade", "")).strip().upper()
        if insufficient or grade not in {"A", "B", "C", "D"}:
            grade = ""
            insufficient = True
        content_reason = str(item.get("content_reason", "")).strip()
        researcher_reason = str(item.get("researcher_reason", "")).strip()
        insufficient_reason = str(item.get("insufficient_reason", "")).strip()
        if not content_reason:
            grade = ""
            insufficient = True
            insufficient_reason = insufficient_reason or "論文内容に基づく判定理由がありません。"

        results[candidate_id] = {
            "grade": grade,
            "content_reason": content_reason,
            "researcher_reason": researcher_reason or "研究者関連の根拠なし",
            "insufficient_reason": insufficient_reason,
            "insufficient": insufficient,
        }
    return results


def evaluate_candidate_batches(
    baseline: dict[str, Any],
    candidates: list[dict[str, Any]],
    evaluator,
    batch_size: int = 15,
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    if batch_size < 1:
        raise ValueError("評価バッチサイズは1以上にしてください。")

    evaluations = {}
    failures = {}
    for offset in range(0, len(candidates), batch_size):
        batch = candidates[offset:offset + batch_size]
        try:
            raw_text = evaluator(build_relevance_prompt(baseline, batch))
            parsed = parse_relevance_response(raw_text, batch)
            for local_index, evaluation in parsed.items():
                evaluations[str(offset + int(local_index))] = evaluation
        except Exception as exc:
            for local_index in range(len(batch)):
                failures[str(offset + local_index)] = str(exc)
        if on_progress:
            on_progress(min(offset + len(batch), len(candidates)), len(candidates))
    return evaluations, failures


def build_researcher_discovery_prompt(
    title: str,
    research_question: str,
    keywords: list[str],
    concepts: list[str],
) -> str:
    return f"""次の研究テーマに関連する研究者候補を、Google検索で確認できる公開情報から提案してください。
候補はテーマとの関連を説明できる研究者に限り、著名さを断定しないでください。
各候補について、研究領域・候補とした理由・実際に検索結果の根拠として確認できたURLを最大3件示してください。
検索結果から確認できる根拠がない候補は含めないでください。名前やURLを推測で作らないでください。
出力は次のJSON形式のみとしてください。

基準論文タイトル: {title}
研究課題: {research_question}
キーワード: {json.dumps(keywords, ensure_ascii=False)}
概念: {json.dumps(concepts, ensure_ascii=False)}

{{
  "researchers": [
    {{
      "name": "研究者名",
      "field": "研究分野",
      "reason": "この研究テーマとの関連を示す具体的な理由",
      "source_urls": ["検索で実際に確認したURL"]
    }}
  ]
}}
"""


def parse_researcher_candidates(
    raw_text: str,
    grounded_sources: list[dict[str, str]],
) -> list[dict[str, str]]:
    source_by_url = {
        str(source.get("url") or "").strip(): source
        for source in grounded_sources
        if str(source.get("url") or "").strip().startswith(("https://", "http://"))
    }
    if not source_by_url:
        raise ValueError("検索結果の確認可能な出典URLがありません。")
    payload = parse_json_response(raw_text)
    raw_researchers = payload.get("researchers")
    if not isinstance(raw_researchers, list):
        raise ValueError("研究者候補がJSON形式で返されませんでした。")

    researchers = []
    seen_names = set()
    for item in raw_researchers:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        reason = str(item.get("reason") or "").strip()
        raw_urls = item.get("source_urls", [])
        if not name or not reason or not isinstance(raw_urls, list):
            continue
        source_urls = list(dict.fromkeys(
            str(url).strip()
            for url in raw_urls
            if str(url).strip() in source_by_url
        ))
        name_key = name.casefold()
        if not source_urls or name_key in seen_names:
            continue
        seen_names.add(name_key)
        researchers.append({
            "name": name,
            "field": str(item.get("field") or "").strip(),
            "reason": reason,
            "source_urls": json.dumps(source_urls, ensure_ascii=False),
        })
    return researchers


def build_extraction_prompt(title: str, text: str) -> str:
    return f"""あなたは学術論文の情報抽出を行います。以下は利用者自身の論文です。
記載された根拠だけを使い、推測で研究者名や概念を補わず、有効なJSONだけを返してください。

論文タイトル: {title}
本文:
{text}

形式:
{{
  "research_question": "研究課題を簡潔に",
  "keywords": ["検索に使える具体的な重要キーワードを最大10件"],
  "concepts": ["中心となる理論・概念を最大10件"],
  "researchers": [
    {{"name": "参考文献に記載された著者の氏名", "role": "参考文献", "evidence": "確認できた記載箇所"}}
  ]
}}
参考文献に記載されていることを確認できた研究者だけを抽出してください。
本文中で言及されたのみの研究者は含めないでください。
"""


def build_relevance_prompt(
    baseline: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> str:
    candidate_payload = [
        {
            "candidate_id": str(index),
            "title": candidate.get("title", ""),
            "authors": candidate.get("author", ""),
            "abstract": candidate.get("abstract", ""),
            "keywords_used_to_find": candidate.get("matched_terms", []),
        }
        for index, candidate in enumerate(candidates)
    ]
    return f"""あなたは学術文献の関連性を判定します。基準論文と各候補について、入力された情報だけを根拠に判定してください。
等級は候補論文のタイトルと抄録に基づく研究内容の関連性を主軸に判定してください。研究者名の一致は補足情報として扱い、
研究者が一致しないことだけを理由に内容の関連性を下げないでください。研究者名の一致は候補の著者欄に確認できる場合のみ認め、
検索語に使われたというだけで一致としないでください。候補著者が空欄でも、タイトルや抄録で内容を評価できるなら判定してください。
タイトルのみの場合も、その記述から関連性を評価できるなら判定し、抄録がないという理由だけで insufficient=true にしないでください。
タイトル・抄録のどちらもなく、内容との関連性を評価できない場合に限り insufficient=true としてください。
Aは研究課題・対象・概念が非常に明確に一致、Bは直接的で明確な関連、Cは部分的・周辺的な関連、
Dはタイトル・抄録から有意な関連が見られない場合です。情報が少ない場合は判定理由にその制約を明記してください。
researcher_reasonには著者情報がない場合「著者情報なし」、著者情報があるが一致しない場合は「確認できる一致なし」と記してください。
JSON以外の文章は出力しないでください。

基準論文:
{json.dumps(baseline, ensure_ascii=False)}

候補:
{json.dumps(candidate_payload, ensure_ascii=False)}

出力形式:
{{
  "candidates": [
    {{
      "candidate_id": "入力と同じID",
      "grade": "A / B / C / D",
      "content_reason": "研究内容の近さと抄録等の根拠",
      "researcher_reason": "著者欄と基準論文の重要研究者との一致。著者情報なしの場合はその旨を記載",
      "insufficient": false,
      "insufficient_reason": ""
    }}
  ]
}}
"""
