import os
import sqlite3
import math
import time
import warnings
import urllib.parse
import re
import json
import hashlib
import requests

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from database import (
    create_database_backup,
    export_papers_csv,
    get_connection as connect_database,
    initialize_database,
)

from cron_fetcher import (
    fetch_new_papers_from_cinii,
    fetch_single_detailed_abstract,
    clean_url,
    extract_doi_from_cinii_url,
    fetch_papers_from_cinii_api,
    fetch_pdf_url_from_unpaywall,
    get_jstage_pdf_url,
    download_pdf_file
)
from research_discovery import (
    MAX_PDF_BATCH_SIZE,
    analyze_research_baseline,
    build_baseline_context,
    create_discovery_run,
    evaluate_candidate_batches,
    filter_discovery_candidates,
    get_discovery_candidates,
    get_candidates_ready_for_evaluation_retry,
    get_discovery_runs,
    get_discovery_processing_status,
    is_pdf_candidate_eligible,
    merge_candidate_into_papers,
    normalize_extraction,
    normalize_title,
    process_discovery_candidate,
    save_candidate_evaluations,
    save_research_baseline,
    safe_paper_pdf_path,
    search_discovery_candidates,
    set_discovery_candidate_selected,
    split_text,
    update_research_baseline_analysis,
    validate_pdf_batch_size,
)

import streamlit as st

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

try:
    from google import genai
    from google.genai import types
    from google.genai.errors import APIError
except ImportError:
    genai = None
    types = None
    APIError = Exception

warnings.filterwarnings("ignore", category=DeprecationWarning)

load_dotenv()

DB_PATH = os.environ.get("ACADEMIC_TRACKER_DB_PATH", "papers.db")
PDF_DIR = "pdf_files"


# ============================================================
# DB接続・初期化
# ============================================================

def get_connection():
    return connect_database(DB_PATH, isolation_level=None)


# ============================================================
# サニタイズ & URL生成・正規化
# ============================================================

def sanitize_name(raw_name: str) -> str:
    """著者名やキーワードからURL・Markdown記号・改行等を除去し、検索に適した文字列のみを抽出"""
    if not raw_name:
        return ""

    text = urllib.parse.unquote(str(raw_name)).strip()
    text = re.sub(r'https?://[^\s\)\]\"\'>]+', '', text)
    text = re.sub(r'\[.*?\]', '', text)
    text = re.sub(r'\(.*?\)', '', text)
    text = re.sub(r'[\[\]\(\)\{\}\<\>\"\'`]', '', text)
    m = re.findall(r'[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ffA-Za-z0-9]+', text)

    if m:
        return " ".join(m).strip()

    return text.strip()


def sanitize_society_name(raw_soc: str) -> str:
    """学術団体名から不要な記号・注釈・余計な修飾語を除去し、正式名称のみを整形取得"""
    if not raw_soc:
        return ""

    text = urllib.parse.unquote(str(raw_soc)).strip()
    text = re.sub(r'https?://[^\s\)\]\"\'>]+', '', text)
    text = re.sub(r'\[.*?\]', '', text)
    text = re.sub(r'\(.*?\)', '', text)
    text = re.sub(r'[\(\)\[\]\{\}\<\>\"\'`]', '', text)
    text = re.sub(r'^(一般社団法人|公益社団法人|特定非営利活動法人|学会|協会)[:\s・]*', '', text)
    text = re.sub(r'(など|等)$', '', text)
    return text.strip()


def sanitize_url(raw_url: str) -> str:
    """有効なHTTP/HTTPS URLを抽出し、Markdownリンク記号等は排除してクリーン化"""
    if not raw_url:
        return ""

    text = str(raw_url).strip()
    m = re.search(r'https?://[^\s\)\]\"\'>]+', text, flags=re.IGNORECASE)

    if not m:
        return ""

    url = m.group(0).rstrip('.,;)]}')
    return url


def sanitize_doi(raw_doi: str) -> str:
    """DOI文字列から接頭辞や余計なプレフィックスを除去してDOI本体のみを取得"""
    if not raw_doi:
        return ""

    text = str(raw_doi).strip()
    markdown_match = re.search(r'\[([^\]]+)\]\([^)]+\)', text)
    if markdown_match:
        text = markdown_match.group(1).strip()

    text = re.sub(r'^https?://(?:dx\.)?doi\.org/', '', text, flags=re.IGNORECASE)
    text = re.sub(r'^doi:\s*', '', text, flags=re.IGNORECASE)
    text = text.strip().rstrip('.,;)]}')

    return text


def build_search_url(base_url: str, param_name: str, value: str) -> str:
    """指定されたHTTPS URLベースにパラメータをURLエンコードして付与"""
    clean_base = sanitize_url(base_url) or base_url.strip()
    encoded_value = urllib.parse.quote(str(value).strip(), safe="")
    return f"{clean_base}?{param_name}={encoded_value}"


def build_doi_url(doi: str) -> str:
    """DOI識別子から直接飛べるDOI URL（または有効なHTTPS URL）を生成"""
    clean_doi = sanitize_doi(doi)

    if not clean_doi:
        return ""

    if clean_doi.lower().startswith("http://") or clean_doi.lower().startswith("https://"):
        return clean_doi

    return f"https://doi.org/{clean_doi}"


def normalize_for_match(text: str) -> str:
    """タイトル等の文字列照合用：スペース・記号・英大文字小文字の違いを吸収して正規化"""
    if not text:
        return ""
    text = re.sub(r'[\s・_,\-()「」\'`\[\]\+\\\/*!#?&@^%$]+', '', text)
    return text.lower().strip()


# ============================================================
# DB内データ・著者名・URLのクリーンアップ処理
# ============================================================

def clean_all_database():
    """DB内に保存された著者名や学会名、URLの表記揺れやゴミデータを一括正規化"""
    with get_connection() as conn:
        c = conn.cursor()

        try:
            authors = c.execute("SELECT name, affiliation, memo, sort_order, column_idx FROM target_authors").fetchall()
            for a in authors:
                raw_name = a["name"]
                clean = sanitize_name(raw_name)
                if clean and clean != raw_name:
                    c.execute("DELETE FROM target_authors WHERE name = ?", (raw_name,))
                    c.execute("""
                        INSERT INTO target_authors (name, affiliation, memo, sort_order, column_idx)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(name) DO UPDATE SET
                            sort_order = excluded.sort_order,
                            column_idx = excluded.column_idx
                    """, (clean, a["affiliation"], a["memo"], a["sort_order"], a["column_idx"]))
        except Exception as e:
            print(f"target_authors clean error: {e}")

        try:
            profiles = c.execute("SELECT name, field, school_of_thought, key_concepts, memo, academic_societies FROM researcher_profiles").fetchall()
            for p in profiles:
                raw_name = p["name"]
                clean = sanitize_name(raw_name)
                if clean and clean != raw_name:
                    c.execute("DELETE FROM researcher_profiles WHERE name = ?", (raw_name,))
                    c.execute("""
                        INSERT INTO researcher_profiles (name, field, school_of_thought, key_concepts, memo, academic_societies)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(name) DO UPDATE SET
                            field = excluded.field,
                            school_of_thought = excluded.school_of_thought,
                            key_concepts = excluded.key_concepts,
                            memo = excluded.memo,
                            academic_societies = excluded.academic_societies
                    """, (clean, p["field"], p["school_of_thought"], p["key_concepts"], p["memo"], p["academic_societies"]))
        except Exception as e:
            print(f"researcher_profiles clean error: {e}")


# ============================================================
# PDF関連
# ============================================================

def get_pdf_files_list():
    if not os.path.exists(PDF_DIR):
        os.makedirs(PDF_DIR)

    files = [f for f in os.listdir(PDF_DIR) if f.endswith(".pdf")]
    files.sort(key=lambda f: os.path.getmtime(os.path.join(PDF_DIR, f)), reverse=True)
    return files


def extract_text_from_pdf(pdf_path, max_pages=2):
    """スキャン・インデックス用のPDFから冒頭2ページ分のテキストを最大2,500文字程度抽出"""
    if not PdfReader or not pdf_path or not os.path.exists(str(pdf_path)):
        return ""
    try:
        reader = PdfReader(str(pdf_path), strict=False)
        extracted = []
        for i, page in enumerate(reader.pages):
            if i >= max_pages:
                break
            txt = page.extract_text() or ""
            if txt.strip():
                extracted.append(txt.strip())
        full_text = "\n\n".join(extracted)
        return str(full_text.strip()[:2500])
    except Exception as e:
        print(f"PDF extraction error: {e}")
        return ""


# ============================================================
# Gemini API & AIリサーチ・要約
# ============================================================

def _is_quota_error(exc: Exception) -> bool:
    text = str(exc).upper()
    return "429" in text or "RESOURCE_EXHAUSTED" in text or "TOO_MANY_REQUESTS" in text


def _is_transient_error(exc: Exception) -> bool:
    text = str(exc).upper()
    return any(token in text for token in (
        "503", "UNAVAILABLE", "DEADLINE_EXCEEDED", "TIMEOUT", "TIMED OUT", "CONNECTION RESET", "HIGH DEMAND"
    ))


def _gemini_client():
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError(".env に GEMINI_API_KEY が設定されていません。")
    if not genai:
        raise ValueError("google-genai ライブラリがインストールされていません。")
    return genai.Client(api_key=api_key)


def call_gemini_with_retry(prompt: str, preferred_model: str = "gemini-3.6-flash"):
    client = _gemini_client()
    last_err = None

    for attempt in range(4):
        try:
            response = client.models.generate_content(
                model=preferred_model,
                contents=prompt,
            )
            if response and response.text:
                return response.text.strip()
            last_err = RuntimeError("Geminiからの応答本文が取得できませんでした。")
            break
        except Exception as e:
            last_err = e
            if _is_quota_error(e):
                break
            if _is_transient_error(e) and attempt < 3:
                time.sleep(2 ** (attempt + 1))
                continue
            break

    if _is_quota_error(last_err or Exception()):
        raise RuntimeError(
            "Gemini APIのリクエスト制限（429 / RESOURCE_EXHAUSTED）に達しています。16:00のリセット以降にお試しください。"
        ) from last_err
    raise RuntimeError(f"Gemini APIエラー: {last_err}") from last_err


def call_gemini_with_google_search(prompt: str, preferred_model: str = "gemini-3.6-flash"):
    client = _gemini_client()
    last_err = None

    try:
        if types is not None:
            config = types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.1,
            )
            response = client.models.generate_content(
                model=preferred_model,
                contents=prompt,
                config=config,
            )
            if response and response.text:
                return response.text.strip()
    except Exception as e:
        last_err = e

    fallback_prompt = prompt + """

【注意】Google検索ツールが利用できない場合は、
過去の学習データから知りうる情報のみを基に回答してください。
推測による不正確な学会名や所属の捏造は行わず、確証がある情報のみを記述してください。
"""
    try:
        return call_gemini_with_retry(fallback_prompt, preferred_model=preferred_model)
    except Exception as fallback_err:
        raise RuntimeError(
            f"Gemini呼び出しでエラーが発生しました: {last_err or fallback_err}"
        ) from fallback_err


def summarize_pdf_with_gemini(paper_title, pdf_path):
    """Geminiを使ってPDF論文の内容を精査・要約。論文ドラフト作成に資する洞察を抽出"""
    pdf_text = extract_text_from_pdf(pdf_path, max_pages=2)
    if not pdf_text or len(pdf_text) < 50:
        raise ValueError("PDFから十分なテキストを抽出できませんでした。")

    safe_p_title = str(paper_title or "論文")
    prompt = f"""あなたは学術論文の査読者兼リサーチアシスタントです。
以下の論文PDF冒頭テキストを精読し、自身の研究・論文執筆の参考となるように以下のフォーマットで要約・洞察を出力してください。
余計な前置きや挨拶文は省き、--- 以降の内容のみを簡潔に出力してください。

【対象論文】: {safe_p_title}
【PDF抽出テキスト】:
{pdf_text}

---
【論文インサイト】
- **研究の背景と課題**: （1〜2文で）
- **コアとなるリサーチクエスチョン**: （1〜2文で）
- **使用されている理論・概念・分析枠組み**: （箇条書き、特に重要なキーワードを明記）
- **新規性・独自性・示唆**: （本研究の独自貢献点）
- **自身の研究への接続・活用アイデア**: （自らの研究の論理構成や先行研究としてどう活かせるか）
"""
    res_text = call_gemini_with_retry(prompt, preferred_model="gemini-3.6-flash")
    if "---" in res_text:
        res_text = res_text.split("---")[-1].strip()
    return res_text


def extract_full_pdf_text(pdf_path):
    if not PdfReader:
        raise RuntimeError("PDF解析ライブラリ pypdf がインストールされていません。")
    if not pdf_path or not os.path.isfile(pdf_path):
        raise FileNotFoundError(f"PDFファイルが見つかりません: {pdf_path}")

    try:
        reader = PdfReader(str(pdf_path), strict=False)
        extracted = []
        for page_number, page in enumerate(reader.pages, start=1):
            page_text = (page.extract_text() or "").strip()
            if page_text:
                extracted.append(f"[p.{page_number}]\n{page_text}")
    except Exception as exc:
        raise RuntimeError(f"PDFを読み取れませんでした: {exc}") from exc

    full_text = "\n\n".join(extracted).strip()
    if len(full_text) < 100:
        raise ValueError("PDFから十分なテキストを抽出できません。スキャン画像PDFの場合はOCR済みPDFを使用してください。")
    return full_text


def summarize_full_pdf_with_gemini(paper_title, pdf_path):
    chunks = split_text(extract_full_pdf_text(pdf_path))
    partial_summaries = []
    for index, chunk in enumerate(chunks, start=1):
        prompt = f"""学術論文の本文抜粋を、後で論文全体を統合要約するために整理してください。
入力にない情報は補わず、重要な主張と根拠をページ番号とともに残してください。
論文タイトル: {paper_title}
本文抜粋 {index}/{len(chunks)}:
{chunk}
"""
        partial_summaries.append(call_gemini_with_retry(prompt))

    while len(partial_summaries) > 8:
        condensed = []
        for offset in range(0, len(partial_summaries), 8):
            summary_batch = partial_summaries[offset:offset + 8]
            condense_prompt = f"""以下は同一論文の本文チャンク要約です。重複を整理し、重要な主張・方法・根拠ページを保持して簡潔に統合してください。
入力にない情報は追加しないでください。

{chr(10).join(summary_batch)}
"""
            condensed.append(call_gemini_with_retry(condense_prompt))
        partial_summaries = condensed

    final_prompt = f"""あなたは学術論文を精読するリサーチアシスタントです。
以下の本文全体から作成した分割要約を統合し、記載された根拠だけで回答してください。
全体を通じた知見を統合し、相反や情報不足があれば明記してください。
ページ番号が示されている場合は根拠とともに保持してください。

論文タイトル: {paper_title}
分割要約:
{chr(10).join(partial_summaries)}

以下の見出しで、各項目を数行程度にまとめてください。
- 研究目的・研究課題
- 理論・概念
- 研究方法・対象
- 主な知見
- 限界・今後の課題
- 自分の研究との接点
"""
    return call_gemini_with_retry(final_prompt)


def parse_robust_json_profile(raw_text: str) -> dict:
    result = {
        "field": "",
        "school_of_thought": "",
        "key_concepts": "",
        "academic_societies": ""
    }
    if not raw_text:
        return result

    json_match = re.search(r'\{[\s\S]*\}', raw_text)
    json_str = json_match.group(0) if json_match else raw_text.strip()

    parsed_data = None
    try:
        parsed_data = json.loads(json_str)
    except Exception:
        for key in ["field", "school_of_thought", "key_concepts", "academic_societies"]:
            pattern = rf'"{key}"\s*:\s*"([^"]+)"'
            m = re.search(pattern, raw_text)
            if m:
                result[key] = m.group(1).strip()
            else:
                m_list = re.search(rf'"{key}"\s*:\s*\[([\s\S]*?)\]', raw_text)
                if m_list:
                    items = re.findall(r'"([^"]+)"', m_list.group(1))
                    result[key] = ", ".join([it.strip() for it in items if it.strip()])

    if isinstance(parsed_data, dict):
        result["field"] = str(parsed_data.get("field", "") or "").strip()
        result["school_of_thought"] = str(parsed_data.get("school_of_thought", "") or "").strip()

        kc = parsed_data.get("key_concepts", "")
        if isinstance(kc, list):
            result["key_concepts"] = ", ".join([str(x).strip() for x in kc if str(x).strip()])
        else:
            result["key_concepts"] = str(kc or "").strip()

        soc = parsed_data.get("academic_societies", "")
        if isinstance(soc, list):
            soc_items = []
            for item in soc:
                if isinstance(item, dict):
                    name_val = item.get("name", "") or item.get("society_name", "")
                    s = sanitize_society_name(str(name_val))
                else:
                    s = sanitize_society_name(str(item))
                if s and s not in {"不明", "なし", "特になし", "情報なし"}:
                    soc_items.append(s)
            result["academic_societies"] = ", ".join(list(dict.fromkeys(soc_items)))
        elif isinstance(soc, str):
            soc_items = [sanitize_society_name(s) for s in re.split(r'[,、\n/;]', soc) if s.strip()]
            valid_items = [s for s in soc_items if s and s not in {"不明", "なし", "特になし", "情報なし"}]
            result["academic_societies"] = ", ".join(list(dict.fromkeys(valid_items)))

    return result


def fetch_researcher_profile_with_ai(author_name: str) -> dict:
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
        text = call_gemini_with_google_search(prompt, preferred_model="gemini-3.6-flash")
        return parse_robust_json_profile(text)
    except Exception as e:
        print(f"fetch_researcher_profile_with_ai error for {clean_author}: {e}")
        return {
            "field": "",
            "school_of_thought": "",
            "key_concepts": "",
            "academic_societies": ""
        }


def ai_research_for_researcher(author_name: str, current_field: str = "") -> str:
    clean_author = sanitize_name(author_name)
    prompt = f"""あなたは学術論文執筆における専門のアドバイザーです。
対象の研究者について、学術的な系譜・主要理論・執筆中の論文にどう接続できるかをリサーチして整理してください。

研究者名: {clean_author}
現在の専門分野情報: {current_field}

【整理フォーマット】
- **研究の系譜・理論的立場**:
- **主要な着眼点・キーワード**:
- **自研究・論文執筆への示唆・接続ポイント**:
- **チェックすべき代表的な論文・著作**:
"""
    return call_gemini_with_retry(prompt, preferred_model="gemini-3.6-flash")


def ai_research_for_society(society_name: str, related_authors: str = "") -> str:
    clean_soc = sanitize_society_name(society_name)
    prompt = f"""あなたは学術誌への投稿・学会発表の戦略を支援するリサーチアドバイザーです。

対象学会: {clean_soc}
関連する注目研究者: {related_authors}

【整理フォーマット】
- **学会の概要・特徴・学問的傾向**:
- **主な査読誌・学術ジャーナル**:
- **発表・投稿における評価基準・トレンド**:
- **自研究との親和性・アプローチ方法**:
"""
    grounded_prompt = prompt + """

【検索補足】
学会の公式サイト、J-STAGE、researchmap等の正確な情報を基に客観的に記述してください。
"""
    return call_gemini_with_google_search(grounded_prompt, preferred_model="gemini-3.6-flash")


# ============================================================
# 学会・研究者プロファイル 双方向同期
# ============================================================

def sync_all_bidirectional_societies() -> tuple[int, int]:
    """研究者プロファイルの所属学会情報を集約して学会マスターへ反映し、相互に関連付けを同期"""
    with get_connection() as conn:
        c = conn.cursor()

        prof_rows = c.execute("SELECT name, academic_societies FROM researcher_profiles").fetchall()
        society_map = {}
        for row in prof_rows:
            author = sanitize_name(row["name"])
            raw_socs = row["academic_societies"] or ""
            soc_list = [s.strip() for s in re.split(r'[,、\n/;]', raw_socs) if s.strip()]
            for soc in soc_list:
                soc_clean = sanitize_society_name(soc)
                if len(soc_clean) >= 3 and not soc_clean.startswith("http"):
                    society_map.setdefault(soc_clean, set()).add(author)

        for soc_name, authors in society_map.items():
            author_str = ", ".join(sorted(list(authors)))
            c.execute("""
                INSERT INTO academic_societies (name, related_authors)
                VALUES (?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    related_authors = CASE
                        WHEN excluded.related_authors != '' THEN excluded.related_authors
                        ELSE academic_societies.related_authors
                    END
            """, (soc_name, author_str))

        soc_rows = c.execute("SELECT name, related_authors FROM academic_societies WHERE related_authors != ''").fetchall()
        author_to_socs = {}
        for s_row in soc_rows:
            s_name = sanitize_society_name(s_row["name"])
            rel_auths = [a.strip() for a in re.split(r'[,、\n/;]', s_row["related_authors"]) if a.strip()]
            for a_name in rel_auths:
                clean_a = sanitize_name(a_name)
                if clean_a:
                    author_to_socs.setdefault(clean_a, set()).add(s_name)

        updated_profs_cnt = 0
        for a_name, new_soc_set in author_to_socs.items():
            curr_row = c.execute("SELECT academic_societies FROM researcher_profiles WHERE name = ?", (a_name,)).fetchone()
            if curr_row:
                curr_socs = curr_row[0] or ""
                curr_soc_list = [sanitize_society_name(s) for s in re.split(r'[,、\n/;]', curr_socs) if s.strip()]
                merged_socs = sorted(list(set(curr_soc_list) | new_soc_set))
                merged_soc_str = ", ".join([s for s in merged_socs if s])
                if merged_soc_str != curr_socs:
                    c.execute("UPDATE researcher_profiles SET academic_societies = ? WHERE name = ?", (merged_soc_str, a_name))
                    updated_profs_cnt += 1

        total_socs = c.execute("SELECT count(*) FROM academic_societies").fetchone()[0]
        return total_socs, updated_profs_cnt


def update_researcher_profiles_from_single_society(soc_name: str, related_authors_str: str):
    """単一学会の関連研究者入力から、各研究者のプロファイル側へ所属学会を安全に追加同期"""
    clean_soc = sanitize_society_name(soc_name)
    if not clean_soc:
        return

    authors = [a.strip() for a in re.split(r'[,、\n/;]', related_authors_str) if a.strip()]
    with get_connection() as conn:
        c = conn.cursor()
        for a_name in authors:
            clean_a = sanitize_name(a_name)
            if not clean_a:
                continue
            curr_row = c.execute("SELECT academic_societies FROM researcher_profiles WHERE name = ?", (clean_a,)).fetchone()
            if curr_row:
                curr_socs = curr_row[0] or ""
                curr_soc_list = [sanitize_society_name(s) for s in re.split(r'[,、\n/;]', curr_socs) if s.strip()]
                if clean_soc not in curr_soc_list:
                    curr_soc_list.append(clean_soc)
                    new_soc_str = ", ".join(sorted(list(set([s for s in curr_soc_list if s]))))
                    c.execute("UPDATE researcher_profiles SET academic_societies = ? WHERE name = ?", (new_soc_str, clean_a))


# ============================================================
# DB初期化
# ============================================================

def init_db(clear_papers_only=False):
    with get_connection() as conn:
        initialize_database(conn, clear_papers_only=clear_papers_only)


@st.cache_resource
def initialize_app_database():
    init_db(clear_papers_only=False)


def evaluate_discovery_candidates(baseline, candidates):
    return evaluate_candidate_batches(
        baseline,
        candidates,
        call_gemini_with_retry,
        batch_size=10,
    )


# ============================================================
# Streamlit UI設定
# ============================================================

st.set_page_config(
    page_title="Academic Tracker",
    page_icon="📚",
    layout="wide",
    initial_sidebar_state="collapsed"
)

initialize_app_database()

st.title("📚 論文アーカイブ & リサーチトラッカー")

if "nav_page" not in st.session_state:
    st.session_state.nav_page = "📄 論文一覧"

page = st.radio(
    "ナビゲーション",
    [
        "📄 論文一覧",
        "🧭 自分の論文から探す",
        "👤 研究者プロファイル",
        "🏛️ 関連学会アーカイブ",
        "⚙️ 著者・キーワード管理"
    ],
    horizontal=True,
    label_visibility="collapsed",
    key="nav_page"
)

st.markdown("---")


# ============================================================
# 1: 論文一覧
# ============================================================

if page == "📄 論文一覧":

    col_t1, col_t2, col_t3 = st.columns([2.0, 1.0, 1.0])

    with col_t1:
        st.subheader("📄 収集論文一覧")

    with col_t2:
        if st.button("🔄 CiNiiから新着を取得", type="primary", use_container_width=True):
            with st.spinner("CiNiiからターゲット著者・キーワードの新着論文を取得中..."):
                try:
                    added = fetch_new_papers_from_cinii(raise_on_error=True)
                except Exception as exc:
                    st.error(f"CiNii検索が一部または全部失敗しました: {exc}")
                else:
                    st.success(f"取得完了: 新規 {added} 件追加")
                    st.rerun()

    with col_t3:
        if st.button("📂 ローカルPDFスキャン", type="secondary", use_container_width=True, help="pdf_filesフォルダ内のPDFをタイトル照合してDBに紐付け"):
            if not os.path.exists(PDF_DIR):
                os.makedirs(PDF_DIR)
            
            pdf_files = [f for f in os.listdir(PDF_DIR) if f.lower().endswith(".pdf") and not f.startswith("~")]
            
            if not pdf_files:
                st.warning("pdf_files フォルダ内にPDFファイルが見つかりません。")
            else:
                with get_connection() as conn:
                    papers = [dict(row) for row in conn.execute("SELECT id, title, pub_date, local_pdf_path, ai_insight FROM papers").fetchall()]
                
                normalized_papers = {}
                for paper in papers:
                    norm_t = normalize_for_match(paper["title"])
                    if norm_t:
                        normalized_papers[norm_t] = paper
                
                mapped_count = 0
                to_summarize = []
                
                for f_name in pdf_files:
                    base_name = os.path.splitext(f_name)[0]
                    clean_f_title = re.sub(r'^\d{4}[_\-\s]+', '', base_name)
                    norm_f_title = normalize_for_match(clean_f_title)
                    
                    matched_paper = None
                    if norm_f_title in normalized_papers:
                        matched_paper = normalized_papers[norm_f_title]
                    else:
                        for norm_p_title, p_obj in normalized_papers.items():
                            if norm_f_title in norm_p_title or norm_p_title in norm_f_title:
                                matched_paper = p_obj
                                break
                    
                    if matched_paper:
                        p_id = matched_paper["id"]
                        curr_pdf = matched_paper["local_pdf_path"] or ""
                        abs_pdf_path = os.path.join(PDF_DIR, f_name)
                        if not curr_pdf or not os.path.exists(curr_pdf):
                            with get_connection() as conn:
                                conn.execute("UPDATE papers SET local_pdf_path = ? WHERE id = ?", (abs_pdf_path, p_id))
                            mapped_count += 1
                        
                        if not matched_paper["ai_insight"]:
                            to_summarize.append((p_id, matched_paper["title"], abs_pdf_path))
                
                st.write(f"📂 スキャン完了: {len(pdf_files)} 件のPDF")
                st.write(f"🔗 新規紐付け: {mapped_count} 件")
                
                if to_summarize:
                    st.write(f"🤖 未要約の {len(to_summarize)} 件についてAI要約を生成中...")
                    progress_bar = st.progress(0.0)
                    for idx_sum, (p_id, p_title, pdf_path) in enumerate(to_summarize):
                        progress_bar.progress((idx_sum) / len(to_summarize))
                        with st.spinner(f"({idx_sum+1}/{len(to_summarize)}) 「{p_title}」の要約を生成中..."):
                            try:
                                summary_res = summarize_pdf_with_gemini(p_title, pdf_path)
                                with get_connection() as conn:
                                    conn.execute("UPDATE papers SET ai_insight = ?, is_read = 1 WHERE id = ?", (summary_res, p_id))
                            except Exception as e:
                                st.warning(f"「{p_title}」の要約生成中にエラー: {e}")
                    progress_bar.progress(1.0)
                    st.success(f"自動紐付け（{mapped_count}件）および AI要約（{len(to_summarize)}件）が完了しました。")
                else:
                    st.success(f"自動紐付け（{mapped_count}件）が完了しました（未要約の論文はありません）。")
                
                time.sleep(2)
                st.rerun()

    with st.expander("データのバックアップとエクスポート", expanded=False):
        st.caption(
            "SQLiteバックアップには論文・研究者・学会・設定などのDB情報が含まれます。"
            "PDFファイル自体は含まれないため、必要な場合は pdf_files フォルダも別途保管してください。"
        )
        backup_col, export_col = st.columns(2)
        with backup_col:
            if st.button("最新のDBバックアップを作成", key="create_db_backup"):
                try:
                    st.session_state["db_backup_data"] = create_database_backup(DB_PATH)
                    st.session_state["db_backup_name"] = (
                        f"academic_tracker_backup_{time.strftime('%Y%m%d_%H%M%S')}.db"
                    )
                except Exception as exc:
                    st.error(f"DBバックアップを作成できませんでした: {exc}")
            if st.session_state.get("db_backup_data"):
                st.download_button(
                    "SQLiteバックアップをダウンロード",
                    data=st.session_state["db_backup_data"],
                    file_name=st.session_state["db_backup_name"],
                    mime="application/vnd.sqlite3",
                    key="download_db_backup",
                )
        with export_col:
            if st.button("論文一覧CSVを作成", key="create_papers_csv"):
                try:
                    st.session_state["papers_csv_data"] = export_papers_csv(DB_PATH)
                    st.session_state["papers_csv_name"] = (
                        f"academic_tracker_papers_{time.strftime('%Y%m%d_%H%M%S')}.csv"
                    )
                except Exception as exc:
                    st.error(f"論文CSVを作成できませんでした: {exc}")
            if st.session_state.get("papers_csv_data"):
                st.download_button(
                    "論文CSVをダウンロード",
                    data=st.session_state["papers_csv_data"],
                    file_name=st.session_state["papers_csv_name"],
                    mime="text/csv",
                    key="download_papers_csv",
                )

    with get_connection() as conn:
        total_count = conn.execute("SELECT count(*) FROM papers").fetchone()[0]
        db_keywords = [
            row["keyword"]
            for row in conn.execute("SELECT keyword FROM target_keywords ORDER BY keyword ASC").fetchall()
        ]

    # フィルタ・セッション初期化
    if "filter_author" not in st.session_state:
        st.session_state.filter_author = ""
    if "filter_keyword" not in st.session_state:
        st.session_state.filter_keyword = "すべて"
    if "filter_search" not in st.session_state:
        st.session_state.filter_search = ""
    if "filter_doi" not in st.session_state:
        st.session_state.filter_doi = "すべて"
    if "filter_read" not in st.session_state:
        st.session_state.filter_read = "すべて"
    if "filter_sort" not in st.session_state:
        st.session_state.filter_sort = "出版年 (新しい順)"
    if "filter_per_page" not in st.session_state:
        st.session_state.filter_per_page = 10
    if "filter_page" not in st.session_state:
        st.session_state.filter_page = 1

    col1, col2, col3, col4, col5, col6 = st.columns([1.6, 1.6, 1.8, 1.0, 1.0, 1.6])

    with col1:
        author_query = st.text_input("著者名絞り込み", value=st.session_state.filter_author)
        st.session_state.filter_author = author_query

    with col2:
        kw_list = ["すべて"] + db_keywords
        kw_idx = kw_list.index(st.session_state.filter_keyword) if st.session_state.filter_keyword in kw_list else 0
        selected_req = st.selectbox("キーワード", kw_list, index=kw_idx)
        st.session_state.filter_keyword = selected_req

    with col3:
        search_query = st.text_input("全文検索キーワード", value=st.session_state.filter_search)
        st.session_state.filter_search = search_query

    with col4:
        doi_opts = ["すべて", "あり", "なし"]
        doi_idx = doi_opts.index(st.session_state.filter_doi) if st.session_state.filter_doi in doi_opts else 0
        selected_doi = st.selectbox("DOI", doi_opts, index=doi_idx)
        st.session_state.filter_doi = selected_doi

    with col5:
        read_opts = ["すべて", "未読", "既読"]
        read_idx = read_opts.index(st.session_state.filter_read) if st.session_state.filter_read in read_opts else 0
        selected_read = st.selectbox("読了状態", read_opts, index=read_idx)
        st.session_state.filter_read = selected_read

    with col6:
        sort_opts = [
            "出版年 (新しい順)",
            "出版年 (古い順)",
            "登録順 (新しい順)",
            "登録順 (古い順)"
        ]
        sort_idx = sort_opts.index(st.session_state.filter_sort) if st.session_state.filter_sort in sort_opts else 0
        sort_order = st.selectbox("並び替え", sort_opts, index=sort_idx)
        st.session_state.filter_sort = sort_order

    query = "SELECT * FROM papers WHERE 1=1"
    params = []

    if author_query:
        query += " AND (author LIKE ? OR first_author LIKE ?)"
        params.extend([f"%{author_query}%", f"%{author_query}%"])

    if selected_req != "すべて":
        query += " AND matched_keyword LIKE ?"
        params.append(f"%{selected_req}%")

    if search_query:
        query += """
            AND (
                title LIKE ?
                OR abstract LIKE ?
                OR ai_insight LIKE ?
                OR user_memo LIKE ?
            )
        """
        params.extend([f"%{search_query}%"] * 4)

    if selected_doi == "あり":
        query += " AND (doi IS NOT NULL AND doi != '')"
    elif selected_doi == "なし":
        query += " AND (doi IS NULL OR doi = '')"

    if selected_read == "未読":
        query += " AND is_read = 0"
    elif selected_read == "既読":
        query += " AND is_read = 1"

    count_query = query.replace("SELECT *", "SELECT COUNT(*)", 1)

    if sort_order == "出版年 (新しい順)":
        query += " ORDER BY pub_date DESC, id DESC"
    elif sort_order == "出版年 (古い順)":
        query += """
            ORDER BY
                CASE
                    WHEN pub_date = '不明' OR pub_date = '' THEN '9999'
                    ELSE pub_date
                END ASC,
                id ASC
        """
    elif sort_order == "登録順 (新しい順)":
        query += " ORDER BY id DESC"
    elif sort_order == "登録順 (古い順)":
        query += " ORDER BY id ASC"

    with get_connection() as conn:
        total_matched = conn.execute(count_query, params).fetchone()[0]

    p_col1, p_col2, p_col3 = st.columns([2.6, 1.0, 1.2])

    with p_col2:
        per_page_opts = [10, 20, 50]
        per_page_idx = per_page_opts.index(st.session_state.filter_per_page) if st.session_state.filter_per_page in per_page_opts else 0
        per_page = st.selectbox("1ページの表示件数", per_page_opts, index=per_page_idx)
        st.session_state.filter_per_page = per_page

    total_pages = max(1, math.ceil(total_matched / per_page))
    current_page_val = min(max(st.session_state.filter_page, 1), total_pages)

    with p_col3:
        current_page = st.number_input(f"ページ (全 {total_pages} ページ)", min_value=1, max_value=total_pages, value=current_page_val, step=1)
        st.session_state.filter_page = current_page

    start_idx = (current_page - 1) * per_page
    end_idx = start_idx + per_page
    with get_connection() as conn:
        display_papers = [
            dict(row)
            for row in conn.execute(
                query + " LIMIT ? OFFSET ?",
                params + [per_page, start_idx],
            ).fetchall()
        ]

    with p_col1:
        display_range = f"{start_idx + 1}〜{min(end_idx, total_matched)}" if total_matched > 0 else "0"
        st.markdown(
            f"""
            <div style='padding-top: 28px; font-size: 14px;'>
            全登録件数: <b>{total_count} 件</b> ｜ 該当件数: <b>{total_matched} 件</b> ｜ 表示中: <b>{display_range} 件目</b>
            </div>
            """,
            unsafe_allow_html=True
        )

    if not display_papers:
        st.info("💡 該当する論文は見つかりませんでした。")

    pdf_files_in_dir = get_pdf_files_list()

    def toggle_paper_read_status(paper_id: int, current_status: int):
        new_status = 0 if current_status else 1
        with get_connection() as conn:
            conn.execute("UPDATE papers SET is_read = ? WHERE id = ?", (new_status, paper_id))
        st.session_state[f"exp_open_{paper_id}"] = True

    for idx, p in enumerate(display_papers):
        p_id = p["id"]
        p_title = str(p.get("title") or "タイトル未設定")
        local_path = str(p.get("local_pdf_path") or "")
        p_author = str(p.get("author") or "著者不明")
        first_auth = str(p.get("first_author") or "").strip() or p_author.split(",")[0].split("・")[0].strip()
        matched_kw = str(p.get("matched_keyword") or "").strip()
        abstract_val = str(p.get("abstract") or "").strip()
        ai_insight_val = str(p.get("ai_insight") or "").strip()
        pub_date_val = str(p.get("pub_date") or "不明")

        url_val = sanitize_url(str(p.get("url") or ""))
        doi_val = sanitize_doi(str(p.get("doi") or ""))

        has_pdf = False
        if local_path and os.path.exists(local_path):
            has_pdf = True
        elif local_path and os.path.exists(os.path.join(PDF_DIR, os.path.basename(local_path))):
            has_pdf = True
            local_path = os.path.join(PDF_DIR, os.path.basename(local_path))

        badge = " [PDFあり] " if has_pdf else " [PDFなし] "
        doi_badge = " [DOI] " if doi_val else ""
        author_kw_str = f"{first_auth} ｜ {matched_kw}" if matched_kw else first_auth
        expander_title = f"{badge}{doi_badge} ({pub_date_val}) {p_title} — {author_kw_str}"

        exp_key = f"exp_open_{p_id}"
        if exp_key not in st.session_state:
            st.session_state[exp_key] = False

        row_card_col, row_del_col = st.columns([11.2, 0.8])

        with row_del_col:
            if st.button("🗑️", key=f"quick_del_{p_id}_{idx}", help="この論文を削除して除外リストに登録", use_container_width=True):
                with get_connection() as conn:
                    conn.execute("INSERT OR IGNORE INTO ignored_papers (title) VALUES (?)", (p_title,))
                    conn.execute("DELETE FROM papers WHERE id = ?", (p_id,))
                st.rerun()

        with row_card_col:
            with st.expander(expander_title, expanded=st.session_state[exp_key]):
                journal_name = str(p.get("journal") or "掲載誌不明")
                clean_p_title = sanitize_name(p_title) or p_title

                cinii_open_url = url_val if url_val else build_search_url("https://cir.nii.ac.jp/all", "q", clean_p_title)
                jstage_search_url = build_search_url("https://www.jstage.jst.go.jp/result/global/-char/ja", "globalSearchKey", clean_p_title)
                scholar_search_url = build_search_url("https://scholar.google.com/scholar", "q", clean_p_title)

                top_c1, top_c2, top_c3 = st.columns([4.2, 1.2, 1.0])

                with top_c1:
                    st.markdown(f"**著者**: {p_author}")
                    st.markdown(f"**掲載誌**: {journal_name} ｜ **出版年**: {pub_date_val} ｜ **キーワード**: `{matched_kw}`")

                with top_c2:
                    raw_read = int(p.get("is_read", 0) or 0)
                    btn_label = "✅ 既読" if raw_read else "📖 未読"
                    st.button(
                        btn_label,
                        key=f"read_btn_{p_id}_{idx}",
                        on_click=toggle_paper_read_status,
                        args=(p_id, raw_read),
                        use_container_width=True
                    )

                with top_c3:
                    if st.button("🗑️ 削除", key=f"del_{p_id}_{idx}", use_container_width=True):
                        with get_connection() as conn:
                            conn.execute("INSERT OR IGNORE INTO ignored_papers (title) VALUES (?)", (p_title,))
                            conn.execute("DELETE FROM papers WHERE id = ?", (p_id,))
                        st.rerun()

                link_c1, link_c2, link_c3, link_c4 = st.columns(4)

                with link_c1:
                    st.link_button("🌐 CiNiiで確認", cinii_open_url, use_container_width=True)

                with link_c2:
                    st.link_button("🔍 J-STAGEで検索", jstage_search_url, use_container_width=True)

                with link_c3:
                    st.link_button("📚 Scholarで検索", scholar_search_url, use_container_width=True)

                with link_c4:
                    if doi_val:
                        doi_link = build_doi_url(doi_val)
                        st.link_button("🔗 DOIリンク", doi_link, use_container_width=True)
                    else:
                        if st.button("🔄 DOI再取得", key=f"fetch_doi_{p_id}_{idx}", use_container_width=True):
                            with st.spinner("DOI検索中..."):
                                try:
                                    found_doi = extract_doi_from_cinii_url(
                                        cinii_open_url, raise_on_error=True
                                    )
                                    if found_doi:
                                        clean_found_doi = sanitize_doi(found_doi)
                                        with get_connection() as conn:
                                            conn.execute("UPDATE papers SET doi = ? WHERE id = ?", (clean_found_doi, p_id))
                                        st.session_state[exp_key] = True
                                        st.success("DOIを取得しました")
                                        st.rerun()
                                    else:
                                        st.session_state[exp_key] = True
                                        st.info("CiNiiにDOIが登録されていません。再試行する場合はボタンをもう一度押してください。")
                                except Exception as exc:
                                    st.session_state[exp_key] = True
                                    st.error(f"DOI取得に失敗しました。しばらく待って再試行してください: {exc}")

                st.markdown("**📑 論文抄録・要約**")
                if abstract_val:
                    st.markdown(f"<div style='background-color: #f8f9fa; border-left: 3px solid #0066cc; padding: 10px 14px; font-size: 13.5px; line-height: 1.6; border-radius: 4px;'>{abstract_val}</div>", unsafe_allow_html=True)
                else:
                    c_abs1, c_abs2 = st.columns([3, 1])
                    with c_abs1:
                        st.caption("抄録データがまだ登録されていません。右のボタンでCiNiiから詳細を取得できます。")
                    with c_abs2:
                        if st.button("🔄 CiNiiから抄録取得", key=f"fetch_abs_{p_id}_{idx}", use_container_width=True):
                            with st.spinner("抄録検索中..."):
                                try:
                                    detailed_text = fetch_single_detailed_abstract(
                                        cinii_open_url, raise_on_error=True
                                    )
                                    found_doi = extract_doi_from_cinii_url(
                                        cinii_open_url, raise_on_error=True
                                    )
                                    clean_found_doi = sanitize_doi(found_doi) if found_doi else ""

                                    with get_connection() as conn:
                                        if detailed_text:
                                            conn.execute("UPDATE papers SET abstract = ? WHERE id = ?", (detailed_text, p_id))
                                        if clean_found_doi:
                                            conn.execute("UPDATE papers SET doi = ? WHERE id = ?", (clean_found_doi, p_id))

                                    st.session_state[exp_key] = True
                                    if detailed_text:
                                        st.success("抄録を取得しました")
                                    else:
                                        st.info("CiNiiに抄録が登録されていません。")
                                    st.rerun()
                                except Exception as exc:
                                    st.session_state[exp_key] = True
                                    st.error(f"CiNiiからの抄録取得に失敗しました。しばらく待って再試行してください: {exc}")

                st.markdown("---")
                if has_pdf:
                    dcol1, dcol2, dcol3 = st.columns(3)
                    with dcol1:
                        with open(local_path, "rb") as f:
                            st.download_button("📥 PDFを開く / 保存", data=f, file_name=os.path.basename(local_path), mime="application/pdf", key=f"dl_{p_id}_{idx}", use_container_width=True)
                    with dcol2:
                        if st.button("PDF紐付け解除", key=f"ul_{p_id}_{idx}", use_container_width=True):
                            with get_connection() as conn:
                                conn.execute("UPDATE papers SET local_pdf_path = '' WHERE id = ?", (p_id,))
                            st.session_state[exp_key] = True
                            st.rerun()
                    with dcol3:
                        if st.button("🤖 PDFをAI要約", key=f"ai_btn_{p_id}_{idx}", type="primary", use_container_width=True):
                            with st.spinner("PDFテキストを抽出・分析してAI要約を生成中..."):
                                try:
                                    summary_res = summarize_pdf_with_gemini(p_title, local_path)
                                    with get_connection() as conn:
                                        conn.execute("UPDATE papers SET ai_insight = ? WHERE id = ?", (summary_res, p_id))
                                    st.session_state[exp_key] = True
                                    st.success("要約完了")
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"エラー: {e}")

                    if ai_insight_val:
                        st.markdown(f"<div style='background-color: #fdfaf2; border-left: 3px solid #ff9900; padding: 12px 16px; font-size: 13.5px; line-height: 1.6; border-radius: 4px; margin-top: 10px;'>{ai_insight_val}</div>", unsafe_allow_html=True)
                else:
                    btn_auto_dl_c, _ = st.columns([1.2, 1.0])
                    with btn_auto_dl_c:
                        can_auto_dl = bool(doi_val or "jstage.jst.go.jp" in url_val)
                        dl_label = "🔄 WebからPDF自動取得" if can_auto_dl else "🔄 WebからPDF自動取得 (DOIなし)"
                        if st.button(dl_label, key=f"auto_dl_{p_id}_{idx}", type="secondary", use_container_width=True):
                            with st.spinner("PDFの直リンクを探索中..."):
                                pdf_url = ""
                                if doi_val:
                                    try:
                                        pdf_url = fetch_pdf_url_from_unpaywall(
                                            doi_val, raise_on_error=True
                                        )
                                    except Exception as exc:
                                        st.warning(
                                            f"Unpaywall検索に失敗しました。J-STAGEも確認します: {exc}"
                                        )
                                if not pdf_url and url_val:
                                    pdf_url = get_jstage_pdf_url(url_val)
                                
                                if pdf_url:
                                    st.toast(f"PDF直リンク取得成功: {pdf_url}")
                                    safe_title = re.sub(r'[\\/*?:"<>|]', '_', p_title)
                                    new_filename = f"{pub_date_val}_{safe_title}.pdf"
                                    save_path = os.path.join(PDF_DIR, new_filename)
                                    
                                    with st.spinner("PDFダウンロード中..."):
                                        try:
                                            download_pdf_file(
                                                pdf_url, save_path, raise_on_error=True
                                            )
                                            with get_connection() as conn:
                                                conn.execute("UPDATE papers SET local_pdf_path = ?, is_read = 1 WHERE id = ?", (save_path, p_id))
                                            
                                            st.success("PDFダウンロード成功！自動紐付けを行いました。")
                                            
                                            with st.spinner("AI要約を生成中..."):
                                                try:
                                                    summary_res = summarize_pdf_with_gemini(p_title, save_path)
                                                    with get_connection() as conn:
                                                        conn.execute("UPDATE papers SET ai_insight = ? WHERE id = ?", (summary_res, p_id))
                                                    st.success("PDFの自動取得と要約が完了しました！")
                                                except Exception as e:
                                                    st.warning(f"PDFは取得できましたが、AI要約に失敗しました: {e}")
                                            
                                            st.session_state[exp_key] = True
                                            st.rerun()
                                        except Exception as exc:
                                            st.error(f"PDFの取得に失敗しました。リンクを確認して再試行してください: {exc}")
                                            st.session_state[exp_key] = True
                                else:
                                    st.warning("オープンアクセスのPDF直リンクが見つかりませんでした。手動でアップロードするか、ファイルを選択してください。")
                                    st.session_state[exp_key] = True
                    
                    st.markdown(" ")
                    
                    uploaded_file = st.file_uploader("📤 PDFを手動アップロードして登録・AI要約", type=["pdf"], key=f"upload_{p_id}_{idx}")
                    if uploaded_file is not None:
                        safe_title = re.sub(r'[\\/*?:"<>|]', '_', p_title)
                        new_filename = f"{pub_date_val}_{safe_title}.pdf"
                        save_path = os.path.join(PDF_DIR, new_filename)
                        
                        with st.spinner("PDFファイルを保存して自動登録中..."):
                            try:
                                with open(save_path, "wb") as f:
                                    f.write(uploaded_file.getbuffer())
                                with get_connection() as conn:
                                    conn.execute("UPDATE papers SET local_pdf_path = ?, is_read = 1 WHERE id = ?", (save_path, p_id))
                                st.toast(f"PDFを保存しました: {new_filename}")
                            except Exception as e:
                                st.error(f"ファイル保存エラー: {e}")
                        
                        with st.spinner("AIによる論文の要約を生成中..."):
                            try:
                                summary_res = summarize_pdf_with_gemini(p_title, save_path)
                                with get_connection() as conn:
                                    conn.execute("UPDATE papers SET ai_insight = ? WHERE id = ?", (summary_res, p_id))
                                st.session_state[exp_key] = True
                                st.success("PDFの自動登録と要約が完了しました！")
                                st.rerun()
                            except Exception as e:
                                st.warning(f"PDFは登録されましたが、AI要約に失敗しました: {e}")
                                st.session_state[exp_key] = True
                                st.rerun()

                    st.markdown("<p style='font-size: 12px; color: gray; margin-top: 10px; margin-bottom: 2px;'>または、フォルダ内の既存PDFを選択して手動紐付け：</p>", unsafe_allow_html=True)
                    scol1, scol2, scol3 = st.columns([3, 0.6, 0.8])
                    with scol1:
                        sel_pdf = st.selectbox("PDF選択", ["(選択してください)"] + pdf_files_in_dir, key=f"sel_{p_id}_{idx}", label_visibility="collapsed")
                    with scol2:
                        if st.button("🔄", key=f"refresh_pdf_{p_id}_{idx}", help="pdf_filesフォルダを再スキャンして一覧更新", use_container_width=True):
                            st.session_state[exp_key] = True
                            st.rerun()
                    with scol3:
                        if st.button("🔗 紐付け", key=f"lnk_{p_id}_{idx}", use_container_width=True) and sel_pdf != "(選択してください)":
                            target_pdf = os.path.join(PDF_DIR, sel_pdf)
                            with get_connection() as conn:
                                conn.execute("UPDATE papers SET local_pdf_path = ? WHERE id = ?", (target_pdf, p_id))
                            st.session_state[exp_key] = True
                            st.success("紐付け完了")
                            st.rerun()

                st.markdown("---")
                st.markdown("**📝 研究メモ**")
                user_memo = st.text_area("メモ入力", value=p.get("user_memo", ""), key=f"memo_{p_id}_{idx}", height=140, label_visibility="collapsed", placeholder="分析の着眼点や自身の論文のどこで引用するかなど、自由にメモを記入できます...")

                m_col_left, m_col_right = st.columns([4.2, 1.0])
                with m_col_right:
                    if st.button("メモ保存", key=f"save_{p_id}_{idx}", use_container_width=True):
                        with get_connection() as conn:
                            conn.execute("UPDATE papers SET user_memo = ? WHERE id = ?", (user_memo, p_id))
                        st.session_state[exp_key] = True
                        st.success("メモを保存しました")


# ============================================================
# 2: 自分の論文から関連文献を探索
# ============================================================

elif page == "🧭 自分の論文から探す":
    st.subheader("🧭 自分の論文から関連文献を探す")
    st.warning(
        "解析時に基準論文の抽出テキスト、候補論文のタイトル・著者・抄録をGemini APIへ送信します。"
    )

    with get_connection() as conn:
        baseline_row = conn.execute(
            "SELECT * FROM research_baseline WHERE id = 1"
        ).fetchone()

    uploaded_baseline = st.file_uploader(
        "基準にする自分の論文PDF",
        type=["pdf"],
        help="新しいPDFを登録すると、現在の基準論文を差し替えます。",
        key="research_baseline_pdf",
    )
    baseline_title_input = st.text_input(
        "論文タイトル",
        value=(
            os.path.splitext(uploaded_baseline.name)[0]
            if uploaded_baseline else ""
        ),
        key="research_baseline_title",
    )
    analyze_baseline = st.button(
        "基準論文を保存して解析",
        type="primary",
        disabled=uploaded_baseline is None,
        key="save_research_baseline",
    )

    if analyze_baseline and uploaded_baseline is not None:
        baseline_bytes = uploaded_baseline.getvalue()
        if not baseline_bytes.startswith(b"%PDF"):
            st.error("選択されたファイルは有効なPDFとして認識できません。")
        else:
            os.makedirs(PDF_DIR, exist_ok=True)
            digest = hashlib.sha256(baseline_bytes).hexdigest()[:16]
            baseline_path = os.path.join(PDF_DIR, f"research_baseline_{digest}.pdf")
            try:
                with open(baseline_path, "wb") as baseline_file:
                    baseline_file.write(baseline_bytes)
                baseline_title = baseline_title_input.strip() or uploaded_baseline.name
                with st.spinner("基準論文の全文を読み取り、重要語と参考文献の研究者を抽出中..."):
                    extraction = analyze_research_baseline(
                        baseline_title,
                        baseline_path,
                        extract_full_pdf_text,
                        call_gemini_with_retry,
                    )
                    if not extraction["keywords"] and not extraction["concepts"] and not extraction["researchers"]:
                        raise ValueError("検索に使えるキーワード・概念・研究者を抽出できませんでした。")
                    with get_connection() as conn:
                        save_research_baseline(
                            conn, baseline_title, baseline_path, extraction
                        )
                st.success("基準論文を登録し、解析結果を保存しました。")
                st.rerun()
            except Exception as exc:
                st.error(f"基準論文を解析できませんでした: {exc}")

    if not baseline_row:
        st.info("最初に基準にする論文PDFを登録してください。")
    else:
        try:
            analysis = normalize_extraction(json.loads(baseline_row["analysis_json"] or "{}"))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            analysis = {"research_question": "", "keywords": [], "concepts": [], "researchers": []}
            st.error(f"保存された解析結果を読み取れません: {exc}")

        st.markdown(f"**基準論文:** {baseline_row['title']}")
        st.caption(f"登録PDF: {os.path.basename(baseline_row['local_pdf_path'])}")
        st.markdown("#### 抽出内容の確認・編集")

        reference_names = [
            researcher["name"] for researcher in analysis["researchers"]
            if "参考文献" in researcher["role"]
        ]
        with st.form("edit_research_baseline_analysis"):
            edited_question = st.text_area(
                "研究課題",
                value=analysis["research_question"],
            )
            edited_keywords = st.text_area(
                "重要キーワード（1行に1つ）",
                value="\n".join(analysis["keywords"]),
            )
            edited_concepts = st.text_area(
                "理論・概念（1行に1つ）",
                value="\n".join(analysis["concepts"]),
            )
            edited_reference_names = st.text_area(
                "参考文献に出てくる研究者（1行に1人）",
                value="\n".join(reference_names),
            )
            save_analysis = st.form_submit_button("抽出内容を保存")

        if save_analysis:
            updated_analysis = {
                "research_question": edited_question.strip(),
                "keywords": list(dict.fromkeys(
                    line.strip() for line in edited_keywords.splitlines() if line.strip()
                )),
                "concepts": list(dict.fromkeys(
                    line.strip() for line in edited_concepts.splitlines() if line.strip()
                )),
                "researchers": [
                    {"name": name, "role": "参考文献", "evidence": "利用者が確認"}
                    for name in dict.fromkeys(
                        line.strip() for line in edited_reference_names.splitlines() if line.strip()
                    )
                ],
            }
            with get_connection() as conn:
                update_research_baseline_analysis(
                    conn, updated_analysis
                )
            st.success("抽出内容を保存しました。")
            st.rerun()

        available_keywords = list(dict.fromkeys(analysis["keywords"] + analysis["concepts"]))
        available_researchers = list(dict.fromkeys(
            researcher["name"] for researcher in analysis["researchers"]
        ))
        st.markdown("#### CiNiiから関連候補を収集")
        if not available_keywords and not available_researchers:
            st.warning("検索に使えるキーワード・概念・研究者がありません。抽出内容を編集してください。")
        else:
            selected_keywords = st.multiselect(
                "検索するキーワード・概念（最大5件）",
                available_keywords,
                default=available_keywords[:5],
                key=f"discovery_keywords_{baseline_row['updated_at']}",
            )
            selected_researchers = st.multiselect(
                "著者検索する研究者（最大5件）",
                available_researchers,
                default=available_researchers[:5],
                key=f"discovery_researchers_{baseline_row['updated_at']}",
            )
            st.caption("各検索条件から最大30件を取得します。PDFはこの段階ではダウンロードしません。")
            if len(selected_keywords) > 5 or len(selected_researchers) > 5:
                st.error("検索負荷を抑えるため、キーワード・研究者はそれぞれ最大5件にしてください。")
            elif st.button(
                "CiNiiで候補を検索・関連度判定",
                type="primary",
                disabled=not (selected_keywords or selected_researchers),
            ):
                try:
                    with st.spinner("CiNiiから候補を収集しています..."):
                        candidates, search_failures = search_discovery_candidates(
                            selected_keywords,
                            selected_researchers,
                            fetch_papers_from_cinii_api,
                        )
                    total_candidate_count = len(candidates)
                    own_title = normalize_title(baseline_row["title"])
                    candidates = [
                        candidate for candidate in candidates
                        if normalize_title(candidate.get("title", "")) != own_title
                    ]
                    candidates.sort(
                        key=lambda candidate: (
                            len(candidate.get("matched_terms", [])),
                            bool(candidate.get("abstract")),
                        ),
                        reverse=True,
                    )
                    candidates = candidates[:100]
                    if not candidates:
                        st.warning("CiNiiから候補文献が見つかりませんでした。")
                    else:
                        baseline_context = build_baseline_context(
                            baseline_row["title"], analysis
                        )
                        with get_connection() as conn:
                            ignored_titles = {
                                normalize_title(row["title"])
                                for row in conn.execute(
                                    "SELECT title FROM ignored_papers"
                                ).fetchall()
                            }
                            candidates = [
                                candidate for candidate in candidates
                                if normalize_title(candidate["title"]) not in ignored_titles
                            ]
                            run_id = create_discovery_run(
                                conn,
                                baseline_row["updated_at"],
                                {
                                    "keywords": selected_keywords,
                                    "researchers": selected_researchers,
                                },
                                candidates,
                            )
                        with st.spinner(f"{len(candidates)}件の候補を関連度判定中..."):
                            evaluations, evaluation_failures = evaluate_discovery_candidates(
                                baseline_context, candidates
                            )
                        with get_connection() as conn:
                            save_candidate_evaluations(
                                conn,
                                candidates,
                                evaluations,
                                evaluation_failures,
                                run_id=run_id,
                            )
                        evaluated_count = len(evaluations)
                        failed_count = len(evaluation_failures)
                        if failed_count:
                            st.warning(
                                f"{evaluated_count}件は関連度判定済み、{failed_count}件はGeminiの一時エラーで未判定です。"
                                "候補は保存されているので、下の「未判定候補を再判定」から再試行できます。"
                            )
                        else:
                            st.success(f"{evaluated_count}件の候補を評価しました。")
                        if total_candidate_count > 100:
                            st.info("候補が100件を超えたため、最初の100件を評価しました。検索条件を絞ると候補を入れ替えられます。")
                        if search_failures:
                            st.warning("一部の検索条件で取得エラーがありました:\n" + "\n".join(search_failures))
                        st.rerun()
                except Exception as exc:
                    st.error(f"候補の検索・関連度判定に失敗しました: {exc}")

        with get_connection() as conn:
            available_runs = get_discovery_runs(conn, baseline_row["updated_at"])

        latest_run = None
        if available_runs:
            available_run_ids = [row["id"] for row in available_runs]
            runs_by_id = {row["id"]: row for row in available_runs}
            selected_run_id = st.selectbox(
                "この基準論文の検索履歴",
                available_run_ids,
                format_func=lambda run_id: (
                    f"#{run_id} — {runs_by_id[run_id]['created_at']}"
                ),
                key=f"discovery_run_{baseline_row['updated_at']}",
            )
            latest_run = runs_by_id[selected_run_id]
            run_candidates = get_discovery_candidates(conn, latest_run["id"])

        if latest_run:
            st.markdown(f"#### 候補一覧（検索日時: {latest_run['created_at']}）")
            with get_connection() as conn:
                retryable_rows = [
                    dict(candidate)
                    for candidate in get_candidates_ready_for_evaluation_retry(
                        conn, latest_run["id"]
                    )
                ]
            if retryable_rows:
                st.warning(
                    f"Gemini APIの一時エラーで未判定の候補が{len(retryable_rows)}件あります。"
                    "CiNii検索をやり直さず、保存済みの候補だけ再判定できます。"
                )
                if st.button(
                    "未判定候補を再判定",
                    key=f"retry_discovery_{latest_run['id']}",
                ):
                    retry_candidates = []
                    for candidate in retryable_rows:
                        candidate["matched_terms"] = json.loads(
                            candidate["matched_terms_json"] or "[]"
                        )
                        retry_candidates.append(candidate)
                    baseline_context = build_baseline_context(
                        baseline_row["title"], analysis
                    )
                    with st.spinner(f"{len(retry_candidates)}件を再判定中..."):
                        evaluations, evaluation_failures = evaluate_discovery_candidates(
                            baseline_context, retry_candidates
                        )
                    with get_connection() as conn:
                        save_candidate_evaluations(
                            conn,
                            retry_candidates,
                            evaluations,
                            evaluation_failures,
                        )
                    if evaluation_failures:
                        st.warning(
                            f"{len(evaluations)}件を判定し、{len(evaluation_failures)}件はまだ未判定です。"
                            "時間をおいて再試行してください。"
                        )
                    else:
                        st.success(f"{len(evaluations)}件の再判定が完了しました。")
                    st.rerun()

            st.caption(
                "A:内容・研究者の両方が強く関連 / B:一方が強い、または両方に明確な関連 / "
                "C:一方に限定的な関連 / D:どちらも有意な関連なし。"
                "PDF取得を選べるのはA・Bのみです。判定材料不足は等級を付けません。"
            )
            grade_filter = st.selectbox(
                "関連度で絞り込み",
                ["すべて", "A", "B", "C", "D", "判定材料不足"],
                key=f"discovery_grade_filter_{latest_run['id']}",
            )
            filtered_candidates = filter_discovery_candidates(
                run_candidates, grade_filter
            )
            st.caption(f"表示中: {len(filtered_candidates)} / {len(run_candidates)} 件")
            for candidate in filtered_candidates:
                candidate = dict(candidate)
                candidate_id = candidate["id"]
                terms = ", ".join(json.loads(candidate["matched_terms_json"] or "[]"))
                grade = "判定材料不足" if candidate["is_insufficient"] else candidate["grade"]
                selected = st.checkbox(
                    f"[{grade or '判定材料不足'}] {candidate['title']} — {candidate['first_author'] or candidate['author'] or '著者不明'} ({candidate['pub_date'] or '年不明'})",
                    value=bool(candidate["is_selected"]),
                    disabled=not is_pdf_candidate_eligible(
                        candidate["grade"], bool(candidate["is_insufficient"])
                    ),
                    key=f"discovery_candidate_{candidate_id}",
                )
                if selected != bool(candidate["is_selected"]):
                    with get_connection() as conn:
                        set_discovery_candidate_selected(
                            conn, candidate_id, selected
                        )
                with st.expander("判定根拠・書誌情報", expanded=False):
                    st.markdown(f"**内容との関連:** {candidate['content_reason'] or '根拠なし'}")
                    st.markdown(f"**研究者との関連:** {candidate['researcher_reason'] or '根拠なし'}")
                    if candidate["is_insufficient"]:
                        st.warning(candidate["insufficient_reason"] or "抄録等の判定材料が不足しています。")
                    st.caption(f"検索語: {terms or '不明'}")
                    if candidate["abstract"]:
                        st.write(candidate["abstract"])
                    if candidate["url"]:
                        st.link_button("CiNiiの書誌情報を開く", candidate["url"])
                    if candidate["processing_status"] != "候補":
                        st.info(
                            f"処理状態: {candidate['processing_status']}"
                            + (f" — {candidate['processing_error']}" if candidate["processing_error"] else "")
                        )

            with get_connection() as conn:
                ready_rows, completed_selected_count = (
                    get_discovery_processing_status(conn, latest_run["id"])
                )
                selected_rows = [dict(row) for row in ready_rows]
            selected_count = len(selected_rows)
            if completed_selected_count:
                st.caption(
                    f"選択済みの完了項目 {completed_selected_count} 件は再処理対象から除外します。"
                )

            if selected_count > MAX_PDF_BATCH_SIZE:
                st.error(
                    f"未完了の選択項目が{selected_count}件あります。"
                    f"1回の処理は最大{MAX_PDF_BATCH_SIZE}件です。"
                )
            elif st.button(
                f"未完了の選択した{selected_count}件をPDF取得・要約",
                type="primary",
                disabled=selected_count == 0,
            ):
                try:
                    validate_pdf_batch_size(len(selected_rows))
                except ValueError as exc:
                    st.error(str(exc))
                    st.stop()
                progress = st.progress(0.0)
                completed = 0
                failures = []
                for index, candidate in enumerate(selected_rows, start=1):
                    progress.progress((index - 1) / len(selected_rows))
                    with st.spinner(f"({index}/{len(selected_rows)}) {candidate['title']} をPDF取得・要約中..."):
                        paper_id, error_message = process_discovery_candidate(
                            candidate,
                            get_connection,
                            PDF_DIR,
                            fetch_pdf_url_from_unpaywall,
                            get_jstage_pdf_url,
                            download_pdf_file,
                            summarize_full_pdf_with_gemini,
                            on_warning=st.warning,
                        )
                    if error_message:
                        failures.append(f"{candidate['title']}: {error_message}")
                    else:
                        completed += 1
                    progress.progress(index / len(selected_rows))
                if failures:
                    st.warning(
                        f"{completed}件は処理完了、{len(failures)}件は失敗しました。\n"
                        + "\n".join(failures)
                    )
                else:
                    st.success(f"{completed}件のPDF取得・要約が完了しました。")
                st.rerun()


# ============================================================
# 3: 研究者プロファイル
# ============================================================

elif page == "👤 研究者プロファイル":

    col_h1, col_h2 = st.columns([2.5, 1.5])

    with col_h1:
        st.subheader("👤 研究者プロファイル・学問分野")

    with col_h2:
        if st.button("全対象研究者のWeb情報を一括取得・更新", type="primary", use_container_width=True, help="登録されている全対象研究者の情報をWeb/researchmapから自動取得して更新します"):
            with get_connection() as conn:
                all_authors = [
                    r["name"]
                    for r in conn.execute("SELECT name FROM target_authors ORDER BY column_idx ASC, sort_order ASC, name ASC").fetchall()
                ]

            if not all_authors:
                st.warning("登録されている対象研究者がいません。「⚙️ 著者・キーワード管理」から追加してください。")
            else:
                updated_cnt = 0
                with st.spinner(f"登録中の研究者 {len(all_authors)} 名のプロファイルをWeb検索中..."):
                    for a_name in all_authors:
                        try:
                            prof_data = fetch_researcher_profile_with_ai(a_name)
                            if prof_data.get("field") or prof_data.get("school_of_thought") or prof_data.get("academic_societies"):
                                with get_connection() as conn:
                                    conn.execute("""
                                        INSERT INTO researcher_profiles (name, field, school_of_thought, key_concepts, academic_societies)
                                        VALUES (?, ?, ?, ?, ?)
                                        ON CONFLICT(name) DO UPDATE SET
                                            field = excluded.field,
                                            school_of_thought = excluded.school_of_thought,
                                            key_concepts = excluded.key_concepts,
                                            academic_societies = excluded.academic_societies
                                    """, (a_name, prof_data["field"], prof_data["school_of_thought"], prof_data["key_concepts"], prof_data["academic_societies"]))
                                updated_cnt += 1
                            time.sleep(2)
                        except Exception as e:
                            print(f"Error fetching {a_name}: {e}")

                sync_all_bidirectional_societies()
                st.success(f"登録中の {updated_cnt} 名の研究者情報を更新し、所属学会データベースとも自動同期しました。")
                st.rerun()

    with get_connection() as conn:
        c = conn.cursor()
        lbl0 = (c.execute("SELECT val FROM system_settings WHERE key = 'col_label_0'").fetchone() or ["第1列（メイン）"])[0]
        lbl1 = (c.execute("SELECT val FROM system_settings WHERE key = 'col_label_1'").fetchone() or ["第2列（重要著者）"])[0]
        lbl2 = (c.execute("SELECT val FROM system_settings WHERE key = 'col_label_2'").fetchone() or ["第3列（参考）"])[0]

    with st.expander("⚙️ 3つのカラム分類表示名の変更", expanded=False):
        with st.form("form_col_labels"):
            l_col1, l_col2, l_col3 = st.columns(3)
            with l_col1:
                new_lbl0 = st.text_input("第1カラム名", value=lbl0)
            with l_col2:
                new_lbl1 = st.text_input("第2カラム名", value=lbl1)
            with l_col3:
                new_lbl2 = st.text_input("第3カラム名", value=lbl2)

            if st.form_submit_button("カラム名を保存", use_container_width=True):
                with get_connection() as conn:
                    conn.execute("INSERT OR REPLACE INTO system_settings (key, val) VALUES ('col_label_0', ?)", (new_lbl0,))
                    conn.execute("INSERT OR REPLACE INTO system_settings (key, val) VALUES ('col_label_1', ?)", (new_lbl1,))
                    conn.execute("INSERT OR REPLACE INTO system_settings (key, val) VALUES ('col_label_2', ?)", (new_lbl2,))
                st.success("カラム名を更新しました")
                st.rerun()

    search_author_query = st.text_input("🔍 研究者・専門分野・キーワード・学会・メモで絞り込み", "", placeholder="検索したいキーワードを入力...")

    with get_connection() as conn:
        c = conn.cursor()
        query_prof = """
            SELECT
                a.name,
                a.column_idx,
                a.sort_order,
                p.field,
                p.school_of_thought,
                p.key_concepts,
                p.academic_societies,
                p.memo
            FROM target_authors a
            LEFT JOIN researcher_profiles p ON a.name = p.name
            WHERE 1=1
        """
        params_prof = []

        if search_author_query.strip():
            sq = f"%{search_author_query.strip()}%"
            query_prof += """
                AND (
                    a.name LIKE ?
                    OR p.field LIKE ?
                    OR p.school_of_thought LIKE ?
                    OR p.key_concepts LIKE ?
                    OR p.academic_societies LIKE ?
                    OR p.memo LIKE ?
                )
            """
            params_prof.extend([sq] * 6)

        query_prof += """
            ORDER BY
                a.column_idx ASC,
                a.sort_order ASC,
                a.name ASC
        """
        profiles = [dict(row) for row in c.execute(query_prof, params_prof).fetchall()]

    def render_researcher_card(prof, col_labels):
        raw_db_name = prof["name"]
        p_name = sanitize_name(raw_db_name)
        field = prof.get("field") or "（未設定）"
        school = prof.get("school_of_thought") or "（未設定）"
        concepts = prof.get("key_concepts") or "（未設定）"
        societies = prof.get("academic_societies") or "（未設定）"
        memo_val = prof.get("memo") or ""
        curr_col = prof.get("column_idx", 0)
        curr_order = prof.get("sort_order", 0)

        name_no_space = p_name.replace(" ", "")

        url_map = build_search_url("https://researchmap.jp/researchers", "q", name_no_space)
        url_kaken = build_search_url("https://kaken.nii.ac.jp/ja/search/", "kw", p_name)
        url_scholar = build_search_url("https://scholar.google.com/scholar", "q", p_name)
        url_cinii = build_search_url("https://cir.nii.ac.jp/all", "q", p_name)

        with st.expander(f"👤 {p_name} — {field}", expanded=False):
            st.markdown(f"**🎯 専門分野**: {field}")
            st.markdown(f"**🏛️ 学派・潮流**: {school}")
            st.markdown(f"**🔑 主要概念**: {concepts}")
            st.markdown(f"**🏢 所属学会**: `{societies}`")
            st.markdown("---")

            btn_c1, btn_c2, btn_c3, btn_c4 = st.columns(4)
            with btn_c1:
                st.link_button("researchmap", url_map, use_container_width=True)
            with btn_c2:
                st.link_button("KAKEN", url_kaken, use_container_width=True)
            with btn_c3:
                st.link_button("Scholar", url_scholar, use_container_width=True)
            with btn_c4:
                st.link_button("CiNii", url_cinii, use_container_width=True)

            st.markdown("---")
            m_header_c1, m_header_c2, m_header_c3 = st.columns([2.6, 1.4, 1.4])
            with m_header_c1:
                st.markdown("**📝 研究メモ**")
            with m_header_c2:
                if st.button("AIリサーチ", key=f"ai_research_{raw_db_name}", use_container_width=True, help="Geminiで研究者の理論的背景や自身の論文への接続ポイントをリサーチしてメモに追加"):
                    with st.spinner(f"「{p_name}」の研究背景をAIリサーチ中..."):
                        try:
                            ai_memo = ai_research_for_researcher(p_name, field)
                            combined_memo = ((memo_val + "\n\n" + ai_memo).strip() if memo_val else ai_memo)
                            with get_connection() as conn:
                                conn.execute("""
                                    INSERT INTO researcher_profiles (name, field, school_of_thought, key_concepts, academic_societies, memo)
                                    VALUES (?, '', '', '', '', ?)
                                    ON CONFLICT(name) DO UPDATE SET
                                        memo = excluded.memo
                                """, (p_name, combined_memo))
                            st.success("AIリサーチ結果をメモに追加しました")
                            st.rerun()
                        except Exception as e:
                            st.error(f"リサーチエラー: {e}")
            with m_header_c3:
                if st.button("所属学会取得", key=f"ai_soc_single_{raw_db_name}", use_container_width=True, help="この研究者の所属学会情報をWebから再取得して更新"):
                    with st.spinner(f"「{p_name}」の所属学会を検索中..."):
                        try:
                            single_prof = fetch_researcher_profile_with_ai(p_name)
                            new_soc_str = single_prof.get("academic_societies", "")
                            if new_soc_str:
                                with get_connection() as conn:
                                    conn.execute("""
                                        INSERT INTO researcher_profiles (name, field, school_of_thought, key_concepts, academic_societies, memo)
                                        VALUES (?, '', '', '', ?, '')
                                        ON CONFLICT(name) DO UPDATE SET
                                            academic_societies = excluded.academic_societies
                                    """, (p_name, new_soc_str))
                                sync_all_bidirectional_societies()
                                st.success(f"所属学会を更新しました: {new_soc_str}")
                                st.rerun()
                            else:
                                st.warning("所属学会情報を特定できませんでした。手動で追加してください。")
                        except Exception as e:
                            st.error(f"検索エラー: {e}")

            author_memo = st.text_area("メモ入力", value=memo_val, key=f"author_memo_{raw_db_name}", height=120, label_visibility="collapsed", placeholder="この研究者の主要な着眼点や、自研究との接点についてのメモ...")

            m_left, m_right = st.columns([3.0, 1.4])
            with m_right:
                if st.button("メモ保存", key=f"save_prof_memo_{raw_db_name}", use_container_width=True):
                    with get_connection() as conn:
                        conn.execute("""
                            INSERT INTO researcher_profiles (name, field, school_of_thought, key_concepts, academic_societies, memo)
                            VALUES (?, '', '', '', '', ?)
                            ON CONFLICT(name) DO UPDATE SET
                                memo = excluded.memo
                        """, (p_name, author_memo))
                    st.success("メモを保存しました")
                    st.rerun()

            st.markdown("---")
            pos_c1, pos_c2, pos_c3 = st.columns([1.6, 1.1, 1.3])
            with pos_c1:
                new_col_name = st.selectbox("配置カラム", col_labels, index=min(max(curr_col, 0), 2), key=f"pos_col_{raw_db_name}", label_visibility="collapsed")
                new_col_val = col_labels.index(new_col_name)
            with pos_c2:
                new_ord_val = st.number_input("並び順", min_value=0, max_value=50, value=curr_order, step=1, key=f"pos_ord_{raw_db_name}", label_visibility="collapsed")
            with pos_c3:
                if (new_col_val != curr_col or new_ord_val != curr_order):
                    if st.button("移動", key=f"btn_move_{raw_db_name}", use_container_width=True):
                        with get_connection() as conn:
                            conn.execute("""
                                UPDATE target_authors
                                SET column_idx = ?, sort_order = ?
                                WHERE name = ?
                            """, (new_col_val, new_ord_val, raw_db_name))
                        st.rerun()
                else:
                    if st.button("編集", key=f"edit_prof_{raw_db_name}", use_container_width=True):
                        st.session_state[f"is_editing_{raw_db_name}"] = True

            if st.session_state.get(f"is_editing_{raw_db_name}", False):
                st.markdown("#### プロフィール情報の編集")
                with st.form(f"form_prof_{raw_db_name}"):
                    f_field = st.text_input("専門分野", value=(prof.get("field") if prof.get("field") != "（未設定）" else ""))
                    f_school = st.text_input("学問的潮流・学派", value=(prof.get("school_of_thought") if prof.get("school_of_thought") != "（未設定）" else ""))
                    f_concepts = st.text_input("主要概念", value=(prof.get("key_concepts") if prof.get("key_concepts") != "（未設定）" else ""))
                    f_societies = st.text_input("所属学会（カンマ区切り）", value=(prof.get("academic_societies") if prof.get("academic_societies") != "（未設定）" else ""))

                    f_c1, f_c2 = st.columns([1, 1])
                    with f_c1:
                        if st.form_submit_button("保存", use_container_width=True):
                            with get_connection() as conn:
                                conn.execute("""
                                    INSERT INTO researcher_profiles (name, field, school_of_thought, key_concepts, academic_societies, memo)
                                    VALUES (?, ?, ?, ?, ?, ?)
                                    ON CONFLICT(name) DO UPDATE SET
                                        field = excluded.field,
                                        school_of_thought = excluded.school_of_thought,
                                        key_concepts = excluded.key_concepts,
                                        academic_societies = excluded.academic_societies
                                """, (p_name, f_field, f_school, f_concepts, f_societies, memo_val))

                            sync_all_bidirectional_societies()
                            st.session_state[f"is_editing_{raw_db_name}"] = False
                            st.success("プロフィールを更新し、学会リストと同期しました")
                            st.rerun()
                    with f_c2:
                        if st.form_submit_button("キャンセル", use_container_width=True):
                            st.session_state[f"is_editing_{raw_db_name}"] = False
                            st.rerun()

    if profiles:
        col_labels_list = [lbl0, lbl1, lbl2]
        col_left = [p for p in profiles if p.get("column_idx", 0) == 0]
        col_mid = [p for p in profiles if p.get("column_idx", 0) == 1]
        col_right = [p for p in profiles if p.get("column_idx", 0) == 2]

        c1, c2, c3 = st.columns(3)
        with c1:
            st.markdown(f"#### {lbl0}")
            for prof in col_left:
                render_researcher_card(prof, col_labels_list)
        with c2:
            st.markdown(f"#### {lbl1}")
            for prof in col_mid:
                render_researcher_card(prof, col_labels_list)
        with c3:
            st.markdown(f"#### {lbl2}")
            for prof in col_right:
                render_researcher_card(prof, col_labels_list)
    else:
        st.info("💡 該当する研究者は見つかりませんでした。")


# ============================================================
# 3: 関連学会アーカイブ
# ============================================================

elif page == "🏛️ 関連学会アーカイブ":

    col_s1, col_s2 = st.columns([2.5, 1.5])

    with col_s1:
        st.subheader("🏛️ 関連学会アーカイブ")

    with col_s2:
        if st.button("🔄 学会・研究者プロファイルを相互同期", type="primary", use_container_width=True, help="研究者プロファイルの所属学会情報と学会マスターの関連研究者を双方向同期"):
            with st.spinner("学会と研究者プロファイルを双方向同期中..."):
                total_s, updated_p = sync_all_bidirectional_societies()
                st.success(f"同期完了: 登録学会 {total_s} 件 ｜ プロファイル更新: {updated_p} 件")
                st.rerun()

    with get_connection() as conn:
        c = conn.cursor()
        total_p_cnt = c.execute("SELECT count(*) FROM researcher_profiles").fetchone()[0]
        has_soc_p_cnt = c.execute("SELECT count(*) FROM researcher_profiles WHERE academic_societies IS NOT NULL AND academic_societies != ''").fetchone()[0]
        total_soc_cnt = c.execute("SELECT count(*) FROM academic_societies").fetchone()[0]
        target_soc_cnt = c.execute("SELECT count(*) FROM academic_societies WHERE is_target = 1").fetchone()[0]

    with st.expander("📊 学会データ登録状況サマリー", expanded=False):
        d_c1, d_c2, d_c3, d_c4 = st.columns(4)
        d_c1.metric("研究者登録数", f"{total_p_cnt} 名")
        d_c2.metric("所属学会情報あり", f"{has_soc_p_cnt} 名")
        d_c3.metric("登録学会総数", f"{total_soc_cnt} 件")
        d_c4.metric("投稿・発表ターゲット学会", f"{target_soc_cnt} 件")
        if has_soc_p_cnt == 0:
            st.warning("⚠️ 研究者プロファイルに所属学会情報がまだ登録されていません。「👤 研究者プロファイル」画面から「全対象研究者のWeb情報を一括取得・更新」を実行するか、手動で登録してください。")

    with st.expander("➕ 新しい学会を手動登録（研究者との紐付け）", expanded=False):
        with st.form("form_add_single_society", clear_on_submit=True):
            add_s_name = st.text_input("学会名（例：日本都市計画学会、日本建築学会など）")
            add_s_authors = st.text_input("関連研究者（カンマ区切りで入力。例：坂倉杏介、西村幸夫）")
            add_s_category = st.text_input("カテゴリー/分類（例：都市・地域計画、コミュニティデザインなど）", value="未分類")
            add_s_url = st.text_input("公式サイトURL（例：https://www.cpij.or.jp/）")
            add_s_memo = st.text_area("学会メモ・投稿や発表の狙い", placeholder="査読付き論文の投稿ターゲット、大会発表の傾向など...")
            add_s_target = st.checkbox("投稿・発表の重点ターゲット学会に設定する", value=False)

            if st.form_submit_button("学会を登録して研究者と同期") and add_s_name.strip():
                clean_add_s = sanitize_society_name(add_s_name.strip())
                clean_add_auths = add_s_authors.strip()
                clean_add_url = sanitize_url(add_s_url.strip())
                with get_connection() as conn:
                    conn.execute("""
                        INSERT INTO academic_societies (name, related_authors, category, official_url, user_memo, is_target)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(name) DO UPDATE SET
                            related_authors = excluded.related_authors,
                            category = excluded.category,
                            official_url = excluded.official_url,
                            user_memo = excluded.user_memo,
                            is_target = excluded.is_target
                    """, (clean_add_s, clean_add_auths, add_s_category, clean_add_url, add_s_memo, 1 if add_s_target else 0))

                update_researcher_profiles_from_single_society(clean_add_s, clean_add_auths)
                st.success(f"学会「{clean_add_s}」を登録し、研究者プロファイルとも同期しました。")
                st.rerun()

    f_c1, f_c2 = st.columns([3, 1])
    with f_c1:
        search_soc_query = st.text_input("🔍 学会名・関連研究者・メモ・カテゴリーで検索", "", placeholder="学会名や研究者名を入力...")
    with f_c2:
        filter_target_only = st.checkbox("重点ターゲット学会のみ表示", value=False)

    with get_connection() as conn:
        c = conn.cursor()
        q_soc = "SELECT * FROM academic_societies WHERE 1=1"
        p_soc = []

        if search_soc_query.strip():
            sq = f"%{search_soc_query.strip()}%"
            q_soc += " AND (name LIKE ? OR related_authors LIKE ? OR user_memo LIKE ? OR category LIKE ?)"
            p_soc.extend([sq] * 4)

        if filter_target_only:
            q_soc += " AND is_target = 1"

        q_soc += " ORDER BY is_target DESC, name ASC"
        societies = [dict(row) for row in c.execute(q_soc, p_soc).fetchall()]

    total_soc_count = len(societies)
    st.markdown(f"<div style='margin-bottom: 14px; font-size: 14px;'>登録学会数: <b>{total_soc_count} 件</b></div>", unsafe_allow_html=True)

    if not societies:
        st.info("💡 該当する学会はありません。上の「相互同期」ボタンを押すか、手動で追加してください。")

    for soc in societies:
        soc_name = soc["name"]
        clean_soc_name = sanitize_society_name(soc_name)
        related_auth = soc.get("related_authors", "") or "（関連研究者なし）"
        is_target = bool(soc.get("is_target", 0))
        memo_val = soc.get("user_memo", "") or ""
        official_url_val = sanitize_url(soc.get("official_url", "") or "")
        soc_category = soc.get("category", "") or "未分類"

        target_badge = "★【重点ターゲット】" if is_target else "🏛️"
        exp_soc_title = f"{target_badge} {clean_soc_name} — 関連研究者: {related_auth}"

        exp_soc_key = f"exp_soc_{soc_name}"
        if exp_soc_key not in st.session_state:
            st.session_state[exp_soc_key] = False

        with st.expander(exp_soc_title, expanded=st.session_state[exp_soc_key]):
            url_jstage_soc = build_search_url("https://www.jstage.jst.go.jp/result/global/-char/ja", "globalSearchKey", clean_soc_name)
            url_scholar_soc = build_search_url("https://scholar.google.com/scholar", "q", clean_soc_name)
            url_google_soc = build_search_url("https://www.google.com/search", "q", clean_soc_name)

            url_official_btn = official_url_val if official_url_val else url_google_soc
            official_btn_label = "🌐 公式サイトへ" if official_url_val else "🔍 Googleで公式サイト検索"

            s_top_c1, s_top_c2, s_top_c3 = st.columns([4.0, 1.4, 0.8])
            with s_top_c1:
                st.markdown(f"**関連する登録研究者**: `{related_auth}` ｜ **分類**: `{soc_category}`")
            with s_top_c2:
                target_btn_label = "★ ターゲット解除" if is_target else "☆ 重点ターゲットに設定"
                if st.button(target_btn_label, key=f"tgt_btn_{soc_name}", use_container_width=True):
                    with get_connection() as conn:
                        conn.execute("UPDATE academic_societies SET is_target = ? WHERE name = ?", (0 if is_target else 1, soc_name))
                    st.session_state[exp_soc_key] = True
                    st.rerun()
            with s_top_c3:
                if st.button("🗑️", key=f"del_soc_{soc_name}", help="この学会を削除", use_container_width=True):
                    with get_connection() as conn:
                        conn.execute("DELETE FROM academic_societies WHERE name = ?", (soc_name,))
                    st.rerun()

            s_btn_c1, s_btn_c2, s_btn_c3, s_btn_c4 = st.columns(4)
            with s_btn_c1:
                st.link_button("J-STAGEで学会誌検索", url_jstage_soc, use_container_width=True)
            with s_btn_c2:
                st.link_button("Scholarで検索", url_scholar_soc, use_container_width=True)
            with s_btn_c3:
                st.link_button("Googleで検索", url_google_soc, use_container_width=True)
            with s_btn_c4:
                st.link_button(official_btn_label, url_official_btn, use_container_width=True)

            st.markdown("---")
            soc_m_c1, soc_m_c2 = st.columns([3.0, 1.4])
            with soc_m_c1:
                st.markdown("**📝 学会メモ・投稿や発表の狙い**")
            with soc_m_c2:
                if st.button("AIで学会をリサーチ", key=f"ai_soc_{soc_name}", use_container_width=True, help="Geminiで学会の特徴や投稿傾向をリサーチしてメモに追加"):
                    with st.spinner(f"「{clean_soc_name}」についてAIリサーチ中..."):
                        try:
                            ai_soc_memo = ai_research_for_society(clean_soc_name, related_auth)
                            combined_soc_memo = ((memo_val + "\n\n" + ai_soc_memo).strip() if memo_val else ai_soc_memo)
                            with get_connection() as conn:
                                conn.execute("UPDATE academic_societies SET user_memo = ? WHERE name = ?", (combined_soc_memo, soc_name))
                            st.session_state[exp_soc_key] = True
                            st.success("AIリサーチ結果をメモに追加しました")
                            st.rerun()
                        except Exception as e:
                            st.error(f"リサーチエラー: {e}")

            soc_memo = st.text_area("メモ入力", value=memo_val, key=f"soc_memo_area_{soc_name}", height=120, label_visibility="collapsed", placeholder="学会の査読基準や論文投稿のアイデアなど...")

            st.markdown("**⚙️ 学会情報の編集**")
            edit_c1, edit_c2, edit_c3 = st.columns([2.0, 2.0, 1.2])
            with edit_c1:
                edit_rel_authors = st.text_input("関連研究者（カンマ区切り）", value=soc.get("related_authors", ""), key=f"edit_rel_auth_{soc_name}")
            with edit_c2:
                edit_official_url = st.text_input("公式サイトURL", value=soc.get("official_url", "") or "", key=f"edit_url_{soc_name}", placeholder="https://...")
            with edit_c3:
                edit_category = st.text_input("カテゴリー/分類", value=soc.get("category", "") or "未分類", key=f"edit_cat_{soc_name}")

            soc_save_l, soc_save_r = st.columns([3.8, 1.4])
            with soc_save_r:
                if st.button("💾 学会情報・メモを保存", key=f"save_soc_memo_{soc_name}", use_container_width=True):
                    clean_edit_url = sanitize_url(edit_official_url.strip())
                    with get_connection() as conn:
                        conn.execute("""
                            UPDATE academic_societies
                            SET user_memo = ?, related_authors = ?, official_url = ?, category = ?
                            WHERE name = ?
                        """, (soc_memo, edit_rel_authors, clean_edit_url, edit_category, soc_name))

                    update_researcher_profiles_from_single_society(clean_soc_name, edit_rel_authors)
                    st.session_state[exp_soc_key] = True
                    st.success("学会情報を保存し、研究者プロファイルと同期しました")
                    st.rerun()


# ============================================================
# 4: 著者・キーワード管理
# ============================================================

elif page == "⚙️ 著者・キーワード管理":

    st.subheader("⚙️ 著者・キーワード管理（CiNii自動収集対象の設定）")

    def delete_author(name):
        with get_connection() as conn:
            conn.execute("DELETE FROM target_authors WHERE name = ?", (name,))
            conn.execute("DELETE FROM researcher_profiles WHERE name = ?", (name,))

    def delete_keyword(keyword):
        with get_connection() as conn:
            conn.execute("DELETE FROM target_keywords WHERE keyword = ?", (keyword,))

    col_a, col_k = st.columns(2)

    with col_a:
        st.markdown("#### 👤 対象研究者の追加")
        with st.form("form_add_author", clear_on_submit=True):
            new_a = st.text_input("追加する研究者名")
            if st.form_submit_button("追加") and new_a.strip():
                cleaned_name = sanitize_name(new_a.strip())
                with get_connection() as conn:
                    max_order = conn.execute("SELECT COALESCE(MAX(sort_order), 0) FROM target_authors").fetchone()[0]
                    conn.execute("INSERT OR IGNORE INTO target_authors (name, sort_order, column_idx) VALUES (?, ?, 0)", (cleaned_name, max_order + 1))
                    conn.execute("INSERT OR IGNORE INTO researcher_profiles (name, field, school_of_thought, key_concepts, academic_societies, memo) VALUES (?, '', '', '', '', '')", (cleaned_name,))
                st.success(f"「{cleaned_name}」を追加しました")
                st.rerun()

        st.markdown("---")
        with get_connection() as conn:
            authors = [
                dict(row)
                for row in conn.execute("SELECT * FROM target_authors ORDER BY column_idx ASC, sort_order ASC, name ASC").fetchall()
            ]

        for idx, a in enumerate(authors):
            a_name = a["name"]
            ac1, ac2 = st.columns([3, 1])
            ac1.markdown(f"- **{a_name}**")
            if ac2.button("削除", key=f"del_a_{idx}_{a_name}", on_click=delete_author, args=(a_name,)):
                st.rerun()

    with col_k:
        st.markdown("#### 🔑 対象キーワードの追加")
        with st.form("form_add_keyword", clear_on_submit=True):
            new_k = st.text_input("追加するキーワード")
            if st.form_submit_button("追加") and new_k.strip():
                cleaned_kw = new_k.strip()
                with get_connection() as conn:
                    conn.execute("INSERT OR IGNORE INTO target_keywords (keyword) VALUES (?)", (cleaned_kw,))
                st.success(f"「{cleaned_kw}」を追加しました")
                st.rerun()

        st.markdown("---")
        with get_connection() as conn:
            keywords = [
                dict(row)
                for row in conn.execute("SELECT * FROM target_keywords ORDER BY keyword ASC").fetchall()
            ]

        for idx, k in enumerate(keywords):
            k_word = k["keyword"]
            kc1, kc2 = st.columns([3, 1])
            kc1.markdown(f"- **{k_word}**")
            if kc2.button("削除", key=f"del_k_{idx}_{k_word}", on_click=delete_keyword, args=(k_word,)):
                st.rerun()
