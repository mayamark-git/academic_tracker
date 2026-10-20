import os
import re
import urllib.parse
from email.utils import parseaddr

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from database import get_connection as connect_database

load_dotenv()
DB_PATH = os.environ.get("ACADEMIC_TRACKER_DB_PATH", "papers.db")

def get_connection():
    return connect_database(DB_PATH, isolation_level=None)

def clean_url(raw_url: str) -> str:
    if not raw_url:
        return ""
    text = str(raw_url).strip()
    m = re.search(r'https?://[^\s\)\]\"\'>]+', text)
    if m:
        return m.group(0).rstrip('.,;)]')
    return ""

def extract_doi_from_cinii_url(cinii_url: str, raise_on_error: bool = False) -> str:
    target_url = clean_url(cinii_url)
    if not target_url or not target_url.startswith("http"):
        return ""
    try:
        if "/crid/" in target_url:
            base_url = target_url.split("?")[0].rstrip("/")
            json_url = base_url + ".json"
            headers = {"Accept": "application/ld+json, application/json"}
            res = requests.get(json_url, headers=headers, timeout=6)
            if res.status_code == 200:
                data = res.json()
                if isinstance(data, dict):
                    items = data.get("@graph", [data])
                    for item in items:
                        for k, v in item.items():
                            if "doi" in k.lower() or "sameas" in k.lower():
                                val_str = str(v)
                                m = re.search(r'10\.\d{4,9}/[^\s"\'<>]+', val_str)
                                if m:
                                    return "https://doi.org/" + m.group(0).rstrip('.,;)]')
                            if isinstance(v, list):
                                for sub in v:
                                    val_str = str(sub)
                                    m = re.search(r'10\.\d{4,9}/[^\s"\'<>]+', val_str)
                                    if m:
                                        return "https://doi.org/" + m.group(0).rstrip('.,;)]')

        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        res = requests.get(target_url, headers=headers, timeout=6)
        res.encoding = res.apparent_encoding
        html = res.text
        m = re.search(r'https?://doi\.org/(10\.\d{4,9}/[^\s"\'<>]+)', html)
        if m:
            return "https://doi.org/" + m.group(1).rstrip('.,;)]')
    except Exception as e:
        if raise_on_error:
            raise RuntimeError(f"CiNiiからDOIを取得できませんでした: {e}") from e
        print(f"DOI fetch error: {e}")
    return ""

def fetch_single_detailed_abstract(cinii_url: str, raise_on_error: bool = False) -> str:
    target_url = clean_url(cinii_url)
    if not target_url or not target_url.startswith("http"):
        return ""
    try:
        if "/crid/" in target_url:
            base_url = target_url.split("?")[0].rstrip("/")
            json_url = base_url + ".json"
            headers = {"Accept": "application/ld+json, application/json"}
            res = requests.get(json_url, headers=headers, timeout=6)
            if res.status_code == 200:
                data = res.json()
                if isinstance(data, dict):
                    items = data.get("@graph", [data])
                    for item in items:
                        for k, v in item.items():
                            if "description" in k.lower() or "abstract" in k.lower():
                                return str(v).strip()

        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        res = requests.get(target_url, headers=headers, timeout=6)
        res.encoding = res.apparent_encoding
        soup = BeautifulSoup(res.text, "html.parser")
        
        abs_tag = soup.find(class_=re.compile(r'abstract|description|summary', re.I))
        if abs_tag and abs_tag.get_text(strip=True):
            return abs_tag.get_text(strip=True)
    except Exception as e:
        if raise_on_error:
            raise RuntimeError(f"CiNiiから抄録を取得できませんでした: {e}") from e
        print(f"Abstract fetch error: {e}")
    return ""

def fetch_papers_from_cinii_api(query_str: str, is_author: bool = False, year_from: int = None, year_to: int = None, sort_order: str = "0", max_results: int = 30, raise_on_error: bool = False) -> list:
    """CiNii Research OpenSearch API経由で論文メタデータを安全に取得"""
    cinii_appid = os.getenv("CINII_APPID", "")
    base_api = "https://cir.nii.ac.jp/opensearch/articles"
    
    # 全角スペースを半角スペースに統一し、前後のゴミを除去
    clean_query = re.sub(r'[\s ]+', ' ', str(query_str)).strip()
    if not clean_query:
        return []
    
    params = {
        "format": "json",
        "count": max_results,
        "sortorder": sort_order
    }
    if cinii_appid:
        params["appid"] = cinii_appid
        
    if is_author:
        params["creator"] = clean_query
    else:
        params["q"] = clean_query
        
    if year_from:
        params["year_from"] = str(year_from)
    if year_to:
        params["year_to"] = str(year_to)

    results = []
    try:
        # 短めのタイムアウト（7秒）でハングアップを確実に防ぐ
        res = requests.get(base_api, params=params, timeout=7)
        if res.status_code != 200:
            raise requests.exceptions.HTTPError(
                f"HTTP {res.status_code}", response=res
            )
        data = res.json()
        if isinstance(data, dict):
            items = []
            if "@graph" in data and isinstance(data["@graph"], list) and len(data["@graph"]) > 0:
                items = data["@graph"][0].get("items", [])
            elif "items" in data:
                items = data.get("items", [])

            for it in items:
                title_value = it.get("title", "")
                if isinstance(title_value, dict):
                    title_value = title_value.get("@value", title_value.get("value", ""))
                title = str(title_value or "").strip()
                if not title:
                    continue
                    
                author_val = ""
                creators = it.get("dc:creator", [])
                if isinstance(creators, list):
                    author_val = "・".join([c.get("@value", str(c)) if isinstance(c, dict) else str(c) for c in creators])
                elif isinstance(creators, str):
                    author_val = creators
                elif isinstance(creators, dict):
                    author_val = creators.get("@value", "")

                first_author = author_val.split("・")[0].split(",")[0].strip() if author_val else ""
                journal_value = it.get("prism:publicationName", "") or it.get("publicationName", "不明")
                if isinstance(journal_value, dict):
                    journal_value = journal_value.get("@value", journal_value.get("value", "不明"))
                journal = str(journal_value or "不明")
                pub_date = str(it.get("prism:publicationDate", "") or it.get("publicationDate", ""))[:4]
                if not pub_date or not pub_date.isdigit():
                    pub_date = "年不明"
                    
                link_value = it.get("link", {})
                if isinstance(link_value, dict):
                    link_value = link_value.get("@id", "")
                url = clean_url(it.get("@id", "") or link_value)
                doi_value = it.get("prism:doi", "") or it.get("doi", "")
                if isinstance(doi_value, dict):
                    doi_value = doi_value.get("@value", doi_value.get("value", ""))
                doi = clean_url(str(doi_value)) or str(doi_value or "").strip()
                description = it.get("description", "") or it.get("dc:description", "") or ""
                if isinstance(description, dict):
                    description = description.get("@value", description.get("value", ""))
                elif isinstance(description, list):
                    description = " ".join(str(item) for item in description if item)
                description = str(description or "")

                results.append({
                    "title": title,
                    "author": author_val,
                    "first_author": first_author,
                    "journal": journal,
                    "pub_date": pub_date,
                    "url": url,
                    "doi": doi,
                    "abstract": description,
                    "matched_keyword": query_str
                })
    except (requests.exceptions.Timeout, requests.exceptions.RequestException) as e:
        if raise_on_error:
            raise RuntimeError(f"CiNii APIから「{clean_query}」を取得できませんでした: {e}") from e
        print(f"CiNii API timeout or error for [{clean_query}]: {e}")
    except Exception as e:
        if raise_on_error:
            raise RuntimeError(f"CiNiiの応答を「{clean_query}」について解析できませんでした: {e}") from e
        print(f"CiNii parsing error for [{clean_query}]: {e}")
        
    return results

def fetch_new_papers_from_cinii(raise_on_error: bool = False) -> int:
    """研究者・キーワードから論文を取得。ハングアップせず確実に処理を完了する"""
    added_count = 0
    failures = []
    with get_connection() as conn:
        c = conn.cursor()
        
        existing_titles = set(row[0] for row in c.execute("SELECT title FROM papers").fetchall())
        ignored_titles = set(row[0] for row in c.execute("SELECT title FROM ignored_papers").fetchall())
        
        target_keywords = [row[0] for row in c.execute("SELECT keyword FROM target_keywords").fetchall()]
        target_authors = [row[0] for row in c.execute("SELECT name FROM target_authors").fetchall()]

        # 1. 研究者ごとの取得（関連度順 ＋ 古い順）
        for author in target_authors:
            author_papers = []
            for sort_order, limit in (("0", 30), ("2", 20)):
                try:
                    author_papers.extend(fetch_papers_from_cinii_api(
                        author, is_author=True, sort_order=sort_order,
                        max_results=limit, raise_on_error=raise_on_error,
                    ))
                except Exception as exc:
                    failures.append(f"著者「{author}」: {exc}")

            for p in author_papers:
                t = p['title']
                if t in existing_titles or t in ignored_titles:
                    continue
                c.execute("""
                    INSERT OR IGNORE INTO papers (title, author, first_author, journal, pub_date, url, doi, abstract, matched_keyword)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (p['title'], p['author'], p['first_author'], p['journal'], p['pub_date'], p['url'], p['doi'], p['abstract'], f"著者: {author}"))
                existing_titles.add(t)
                added_count += 1

        # 2. キーワードごとの取得
        for kw in target_keywords:
            # 複数ワードにも対応できるよう、全年代・関連度順で安全に取得
            try:
                papers = fetch_papers_from_cinii_api(
                    kw, is_author=False, sort_order="0", max_results=30,
                    raise_on_error=raise_on_error,
                )
            except Exception as exc:
                failures.append(f"キーワード「{kw}」: {exc}")
                continue
            
            for p in papers:
                t = p['title']
                if t in existing_titles or t in ignored_titles:
                    continue
                c.execute("""
                    INSERT OR IGNORE INTO papers (title, author, first_author, journal, pub_date, url, doi, abstract, matched_keyword)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (p['title'], p['author'], p['first_author'], p['journal'], p['pub_date'], p['url'], p['doi'], p['abstract'], kw))
                existing_titles.add(t)
                added_count += 1

    if failures:
        message = (
            f"CiNii検索で{len(failures)}件の条件に失敗しました。"
            f"新規追加: {added_count}件。\n" + "\n".join(failures)
        )
        if raise_on_error:
            raise RuntimeError(message)
        print(message)
    return added_count


def fetch_pdf_url_from_unpaywall(doi: str, raise_on_error: bool = False) -> str:
    """Unpaywall APIを使用してオープンアクセスPDFの直リンクURLを取得"""
    clean_doi_str = doi.strip()
    if not clean_doi_str:
        return ""
    # DOIプレフィックス等の除去
    clean_doi_str = re.sub(r'^https?://(?:dx\.)?doi\.org/', '', clean_doi_str, flags=re.IGNORECASE)
    clean_doi_str = re.sub(r'^doi:\s*', '', clean_doi_str, flags=re.IGNORECASE)
    clean_doi_str = clean_doi_str.strip().rstrip('.,;)]}')

    email = os.getenv("UNPAYWALL_EMAIL", "").strip()
    parsed_email = parseaddr(email)[1]
    if (
        not parsed_email
        or parsed_email != email
        or "@" not in parsed_email
        or "." not in parsed_email.rsplit("@", 1)[-1]
    ):
        message = "Unpaywall APIには有効な連絡先メールが必要です。.env に UNPAYWALL_EMAIL を設定してください。"
        if raise_on_error:
            raise RuntimeError(message)
        print(message)
        return ""

    url = "https://api.unpaywall.org/v2/" + urllib.parse.quote(clean_doi_str, safe="/")
    try:
        res = requests.get(url, params={"email": email}, timeout=5)
        if res.status_code == 200:
            data = res.json()
            if data.get("is_oa"):
                best_location = data.get("best_oa_location") or {}
                pdf_url = best_location.get("url_for_pdf")
                if pdf_url:
                    return pdf_url
        else:
            raise requests.exceptions.HTTPError(
                f"HTTP {res.status_code}", response=res
            )
    except Exception as e:
        if raise_on_error:
            raise RuntimeError(f"UnpaywallからPDF情報を取得できませんでした: {e}") from e
        print(f"Unpaywall API error for DOI [{clean_doi_str}]: {e}")
    return ""


def get_jstage_pdf_url(url: str) -> str:
    """J-STAGEの論文詳細URLからPDFダウンロード用のURLを推測・変換"""
    clean_u = url.strip()
    if "jstage.jst.go.jp" in clean_u:
        # _article を _pdf に変換してみる
        if "_article" in clean_u:
            return clean_u.replace("_article", "_pdf")
    return ""


def download_pdf_file(url: str, save_path: str, raise_on_error: bool = False) -> bool:
    """指定されたURLからPDFファイルをダウンロードして保存"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    temporary_path = f"{save_path}.part"
    try:
        res = requests.get(url, headers=headers, stream=True, timeout=15)
        if res.status_code != 200:
            raise requests.exceptions.HTTPError(
                f"HTTP {res.status_code}", response=res
            )

        content_type = res.headers.get('Content-Type', '').lower()
        if 'html' in content_type:
            raise ValueError("PDFではなくHTMLが返されました。")

        parent_dir = os.path.dirname(save_path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
        with open(temporary_path, "wb") as output_file:
            for chunk in res.iter_content(chunk_size=8192):
                if chunk:
                    output_file.write(chunk)
        with open(temporary_path, "rb") as downloaded_file:
            if downloaded_file.read(5) != b"%PDF-":
                raise ValueError("取得したファイルが有効なPDFではありません。")
        os.replace(temporary_path, save_path)
        return True
    except Exception as e:
        if os.path.exists(temporary_path):
            os.remove(temporary_path)
        if raise_on_error:
            raise RuntimeError(f"PDFのダウンロードに失敗しました: {e}") from e
        print(f"Failed to download PDF from {url}: {e}")
        return False
