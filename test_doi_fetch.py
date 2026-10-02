# test_doi_fetch.py
import sqlite3
import requests
import json
import re

DB_PATH = "papers.db"

def extract_doi_via_cinii_api(url):
    """CiNii公式JSON-LD APIを叩いてDOIを取得"""
    crid_match = re.search(r'crid/(\d+)', url)
    if not crid_match:
        return None, "CRID形式のURLではありません"
    
    crid = crid_match.group(1)
    json_url = f"https://cir.nii.ac.jp/crid/{crid}.json"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "application/ld+json, application/json"
    }
    
    try:
        res = requests.get(json_url, headers=headers, timeout=10)
        if res.status_code != 200:
            return None, f"HTTPエラー {res.status_code}"
        
        data = res.json()
        raw_text = json.dumps(data)
        
        # 1. doi.org リンク
        match = re.search(r'https?://doi\.org/(10\.\d{4,9}/[^\s"\'\\<>]+)', raw_text)
        if match:
            return f"https://doi.org/{match.group(1).rstrip('.,;')}", "公式JSON-LD API (doi.org)"
        
        # 2. info:doi 形式
        match_info = re.search(r'info:doi/(10\.\d{4,9}/[^\s"\'\\<>]+)', raw_text)
        if match_info:
            return f"https://doi.org/{match_info.group(1).rstrip('.,;')}", "公式JSON-LD API (info:doi)"

        # 3. 識別子リストの走査
        if "@graph" in data:
            for item in data["@graph"]:
                # sameAs や identifier の確認
                for key in ["sameAs", "identifier", "seeAlso"]:
                    if key in item:
                        val = item[key]
                        val_str = json.dumps(val)
                        d_match = re.search(r'(10\.\d{4,9}/[^\s"\'\\<>]+)', val_str)
                        if d_match:
                            return f"https://doi.org/{d_match.group(1).rstrip('.,;')}", f"JSON-LD ({key})"

        return None, "DOI未登録の論文（紀要・報告書等）"

    except Exception as e:
        return None, f"API通信エラー: {e}"

print("=== 🔬 CiNii 公式JSON-LD APIによるDOI抽出テスト ===")
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

# DOIが見つかるか確認するため上位20件をテストスキャン
rows = conn.execute("SELECT id, title, url FROM papers WHERE url LIKE '%crid%' LIMIT 20").fetchall()

found_count = 0
for r in rows:
    p_id, title, url = r['id'], r['title'], r['url']
    doi, method = extract_doi_via_cinii_api(url)
    print("\n" + "-"*50)
    print(f"【ID {p_id}】: {title[:35]}...")
    if doi:
        print(f"🎉 取得成功: {doi}")
        print(f"   (取得元: {method})")
        found_count += 1
    else:
        print(f"ℹ️ {method}")

print("\n" + "="*60)
print(f"テスト完了: 20件中 {found_count} 件でDOIを検出しました。")