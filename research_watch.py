import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from research_discovery import deduplicate_candidates

RESEARCH_WATCH_INTERVAL_DAYS = 7
MAX_RESEARCH_WATCH_KEYWORDS = 5
MAX_RESEARCH_WATCH_PAPERS = 30


def initialize_research_watch_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS research_topics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            keywords_json TEXT NOT NULL,
            last_attempt_at TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS research_watch_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic_id INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            status TEXT NOT NULL,
            error_message TEXT NOT NULL DEFAULT '',
            item_count INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS research_watch_digests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            topic_id INTEGER NOT NULL,
            source_type TEXT NOT NULL,
            summary TEXT NOT NULL,
            sources_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(run_id, source_type)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_research_watch_runs_topic
        ON research_watch_runs (topic_id, started_at DESC)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_research_watch_digests_topic
        ON research_watch_digests (topic_id, created_at DESC)
    """)


def parse_research_keywords(raw_keywords: str) -> list[str]:
    keywords = list(dict.fromkeys(
        item.strip()
        for item in raw_keywords.replace("、", "\n").replace(",", "\n").splitlines()
        if item.strip()
    ))
    if not keywords:
        raise ValueError("検索キーワードを1件以上入力してください。")
    if len(keywords) > MAX_RESEARCH_WATCH_KEYWORDS:
        raise ValueError(
            f"検索キーワードは最大{MAX_RESEARCH_WATCH_KEYWORDS}件です。"
        )
    return keywords


def add_research_topic(
    conn: sqlite3.Connection,
    name: str,
    raw_keywords: str,
) -> int:
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("研究テーマ名を入力してください。")
    keywords = parse_research_keywords(raw_keywords)
    cursor = conn.execute(
        "INSERT INTO research_topics (name, keywords_json) VALUES (?, ?)",
        (clean_name, json.dumps(keywords, ensure_ascii=False)),
    )
    return int(cursor.lastrowid)


def get_research_topics(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("""
        SELECT * FROM research_topics
        WHERE is_active = 1
        ORDER BY name COLLATE NOCASE
    """).fetchall()


def delete_research_topic(conn: sqlite3.Connection, topic_id: int) -> None:
    conn.execute("DELETE FROM research_watch_digests WHERE topic_id = ?", (topic_id,))
    conn.execute("DELETE FROM research_watch_runs WHERE topic_id = ?", (topic_id,))
    cursor = conn.execute("DELETE FROM research_topics WHERE id = ?", (topic_id,))
    if cursor.rowcount != 1:
        raise ValueError(f"研究テーマが見つかりません: {topic_id}")


def get_due_research_topics(
    conn: sqlite3.Connection,
    now: datetime | None = None,
) -> list[sqlite3.Row]:
    current_time = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    due_before = current_time - timedelta(days=RESEARCH_WATCH_INTERVAL_DAYS)
    return conn.execute(
        """SELECT * FROM research_topics
        WHERE is_active = 1
        AND (
            last_attempt_at IS NULL
            OR last_attempt_at <= ?
        )
        ORDER BY id""",
        (due_before.isoformat(),),
    ).fetchall()


def save_research_watch_digest(
    conn: sqlite3.Connection,
    run_id: int,
    topic_id: int,
    source_type: str,
    summary: str,
    sources: list[dict[str, Any]],
) -> None:
    conn.execute("""
        INSERT INTO research_watch_digests (
            run_id, topic_id, source_type, summary, sources_json
        ) VALUES (?, ?, ?, ?, ?)
    """, (
        run_id,
        topic_id,
        source_type,
        summary.strip(),
        json.dumps(sources, ensure_ascii=False),
    ))


def collect_research_topic(
    conn: sqlite3.Connection,
    topic_id: int,
    search_papers: Callable[[str], list[dict[str, Any]]],
    summarize_papers: Callable[[str, list[dict[str, Any]]], str],
    search_public_reports: Callable[[str], dict[str, Any]],
    now: datetime | None = None,
) -> dict[str, Any]:
    topic = conn.execute(
        "SELECT * FROM research_topics WHERE id = ? AND is_active = 1",
        (topic_id,),
    ).fetchone()
    if topic is None:
        raise ValueError(f"有効な研究テーマが見つかりません: {topic_id}")

    started_at = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    cursor = conn.execute(
        """INSERT INTO research_watch_runs (topic_id, started_at, status)
        VALUES (?, ?, '実行中')""",
        (topic_id, started_at.isoformat()),
    )
    run_id = int(cursor.lastrowid)
    conn.execute(
        "UPDATE research_topics SET last_attempt_at = ? WHERE id = ?",
        (started_at.isoformat(), topic_id),
    )
    conn.commit()

    errors = []
    completed_sources = 0
    item_count = 0
    keywords = json.loads(topic["keywords_json"])
    papers = []
    for keyword in keywords:
        try:
            papers.extend(search_papers(keyword))
        except Exception as exc:
            errors.append(f"学術論文検索「{keyword}」: {exc}")

    papers = deduplicate_candidates(papers)[:MAX_RESEARCH_WATCH_PAPERS]
    try:
        paper_summary = summarize_papers(topic["name"], papers)
        paper_sources = [
            {
                "title": paper.get("title", ""),
                "author": paper.get("author", ""),
                "published": paper.get("pub_date", ""),
                "url": paper.get("url", ""),
                "doi": paper.get("doi", ""),
            }
            for paper in papers
        ]
        save_research_watch_digest(
            conn,
            run_id,
            topic_id,
            "academic_papers",
            paper_summary,
            paper_sources,
        )
        item_count += len(papers)
        completed_sources += 1
    except Exception as exc:
        errors.append(f"学術論文の要約: {exc}")

    try:
        report_result = search_public_reports(topic["name"])
        report_summary = str(report_result.get("summary") or "").strip()
        report_sources = report_result.get("sources") or []
        if not report_summary:
            raise ValueError("報告の要約が空でした。")
        if not isinstance(report_sources, list):
            raise ValueError("報告の出典情報が不正です。")
        save_research_watch_digest(
            conn,
            run_id,
            topic_id,
            "public_reports",
            report_summary,
            report_sources,
        )
        item_count += len(report_sources)
        completed_sources += 1
    except Exception as exc:
        errors.append(f"公的機関の報告検索・要約: {exc}")

    completed_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    status = (
        "完了" if not errors else
        "一部失敗" if completed_sources else
        "失敗"
    )
    conn.execute(
        """UPDATE research_watch_runs
        SET completed_at = ?, status = ?, error_message = ?, item_count = ?
        WHERE id = ?""",
        (completed_at, status, "\n".join(errors), item_count, run_id),
    )
    conn.commit()
    return {
        "run_id": run_id,
        "status": status,
        "item_count": item_count,
        "errors": errors,
    }


def get_research_watch_history(
    conn: sqlite3.Connection,
    topic_id: int,
    limit: int = 10,
) -> list[sqlite3.Row]:
    if limit < 1:
        raise ValueError("履歴表示件数は1件以上にしてください。")
    return conn.execute(
        """SELECT r.id AS run_id, r.started_at, r.completed_at, r.status,
            r.error_message, r.item_count, d.source_type, d.summary,
            d.sources_json, d.created_at AS digest_created_at
        FROM research_watch_runs r
        LEFT JOIN research_watch_digests d ON d.run_id = r.id
        WHERE r.topic_id = ?
        ORDER BY r.id DESC, d.source_type
        LIMIT ?""",
        (topic_id, limit * 2),
    ).fetchall()


def run_due_research_topics(
    conn: sqlite3.Connection,
    search_papers: Callable[[str], list[dict[str, Any]]],
    summarize_papers: Callable[[str, list[dict[str, Any]]], str],
    search_public_reports: Callable[[str], dict[str, Any]],
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    due_topics = [dict(row) for row in get_due_research_topics(conn, now)]
    outcomes = []
    for topic in due_topics:
        outcomes.append({
            "topic": topic["name"],
            **collect_research_topic(
                conn,
                topic["id"],
                search_papers,
                summarize_papers,
                search_public_reports,
                now=now,
            ),
        })
    return outcomes


def build_gemini_research_watch_adapters(
    generate_content: Callable[..., Any],
    google_search_tool: Any,
) -> tuple[
    Callable[[str], list[dict[str, Any]]],
    Callable[[str, list[dict[str, Any]]], str],
    Callable[[str], dict[str, Any]],
]:
    from cron_fetcher import fetch_papers_from_cinii_api

    def search_papers(keyword: str) -> list[dict[str, Any]]:
        return fetch_papers_from_cinii_api(
            keyword,
            max_results=15,
            raise_on_error=True,
        )

    def summarize_papers(topic: str, papers: list[dict[str, Any]]) -> str:
        if not papers:
            return "今週の検索では、関連する学術論文は見つかりませんでした。"
        response = generate_content(build_paper_summary_prompt(topic, papers))
        text = str(getattr(response, "text", "") or "").strip()
        if not text:
            raise RuntimeError("学術論文の要約を取得できませんでした。")
        return text

    def search_public_reports(topic: str) -> dict[str, Any]:
        response = generate_content(
            build_public_reports_prompt(topic),
            config=google_search_tool,
        )
        summary = str(getattr(response, "text", "") or "").strip()
        sources = extract_grounded_sources(response)
        if not summary:
            raise RuntimeError("公的報告の要約を取得できませんでした。")
        if not sources:
            raise RuntimeError("検索結果の確認可能な出典URLがありません。")
        return {"summary": summary, "sources": sources}

    return search_papers, summarize_papers, search_public_reports


def extract_grounded_sources(response: Any) -> list[dict[str, str]]:
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return []
    metadata = getattr(candidates[0], "grounding_metadata", None)
    chunks = getattr(metadata, "grounding_chunks", None) or []
    sources = []
    seen_urls = set()
    for chunk in chunks:
        web = getattr(chunk, "web", None)
        if not web:
            continue
        url = str(getattr(web, "uri", "") or "").strip()
        title = str(getattr(web, "title", "") or "").strip()
        if url.startswith(("https://", "http://")) and url not in seen_urls:
            seen_urls.add(url)
            sources.append({"title": title, "url": url})
    return sources


def create_gemini_research_watch_adapters(
    api_key: str | None = None,
) -> tuple[
    Callable[[str], list[dict[str, Any]]],
    Callable[[str, list[dict[str, Any]]], str],
    Callable[[str], dict[str, Any]],
]:
    from google import genai
    from google.genai import types

    resolved_api_key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not resolved_api_key:
        raise ValueError("GEMINI_API_KEY または GOOGLE_API_KEY が設定されていません。")
    client = genai.Client(api_key=resolved_api_key)

    def generate_content(prompt: str, config: Any = None) -> Any:
        return client.models.generate_content(
            model="gemini-3.6-flash",
            contents=prompt,
            config=config,
        )

    search_papers, summarize_papers, search_public_reports = (
        build_gemini_research_watch_adapters(
            generate_content,
            types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.1,
            ),
        )
    )
    return search_papers, summarize_papers, search_public_reports


def build_paper_summary_prompt(topic: str, papers: list[dict[str, Any]]) -> str:
    if not papers:
        return f"テーマ「{topic}」に関する学術論文は見つかりませんでした。"
    source_material = [
        {
            "id": index,
            "title": paper.get("title", ""),
            "authors": paper.get("author", ""),
            "year": paper.get("pub_date", ""),
            "abstract": paper.get("abstract", ""),
        }
        for index, paper in enumerate(papers, start=1)
    ]
    return f"""あなたは研究動向を整理するアシスタントです。
テーマ「{topic}」に関するCiNii検索結果を日本語で要約してください。
以下の情報にない事実は補わず、研究の共通傾向、注目点、情報不足を簡潔に述べてください。
各論文の番号とタイトルを示し、抄録がない論文は内容を推測しないでください。

検索結果:
{json.dumps(source_material, ensure_ascii=False)}
"""


def build_public_reports_prompt(topic: str) -> str:
    return f"""テーマ「{topic}」に関して、官公庁・自治体・公的研究機関が最近公開した報告書、調査結果、政策資料をGoogle検索で調べてください。
見つかった情報を日本語で要約し、内容が確認できる公式ページ・報告書だけを出典にしてください。
検索対象期間は直近1週間を優先し、該当する情報がなければそのことを明記してください。
検索結果にない事実は推測しないでください。"""


def main() -> int:
    from dotenv import load_dotenv

    load_dotenv()
    from database import get_connection, initialize_database

    with closing(get_connection()) as conn:
        initialize_database(conn)
        due_topics = get_due_research_topics(conn)
        if not due_topics:
            print("週次収集が必要な研究テーマはありません。")
            return 0
        adapters = create_gemini_research_watch_adapters()
        outcomes = run_due_research_topics(conn, *adapters)

    for outcome in outcomes:
        print(
            f"{outcome['topic']}: {outcome['status']} "
            f"({outcome['item_count']}件)"
        )
        for error in outcome["errors"]:
            print(f"  - {error}")
    return 1 if any(outcome["status"] == "失敗" for outcome in outcomes) else 0


if __name__ == "__main__":
    raise SystemExit(main())
