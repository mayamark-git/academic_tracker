# test_single_doi.py
import requests
import json
import re

TARGET_URL = "https://cir.nii.ac.jp/crid/1390868903532749056"

def fetch_doi_via_cinii_json(crid_url):
    # 末尾に .json を付与してCiNii公式APIから取得
    base_url = crid_url.split("?")[0].rstrip("/")
    json_url = f"{base_url}.json"
    
    headers = {
        "Accept": "application/json, application/ld+json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    }
    
    print(f"リクエスト送信先: {json_url}")
    res = requests.get(json_url, headers=headers, timeout=10)
    
    if res.status_code != 200:
        print(f"エラー: HTTPステータス {res.status_code}")
        return None
    
    data = res.json()
    json_text = json.dumps(data, ensure_ascii=False)
    
    # JSON内からDOIを探索
    match = re.search(r'https?://doi\.org/(10\.\d{4,9}/[^\s"\'\\<>]+)', json_text)
    if match:
        return f"https://doi.org/{match.group(1).rstrip('.,;')}"
    
    match_doi = re.search(r'"(10\.\d{4,9}/[^\s"\'\\<>]+)"', json_text)
    if match_doi:
        return f"https://doi.org/{match_doi.group(1).rstrip('.,;')}"
        
    return None

doi = fetch_doi_via_cinii_json(TARGET_URL)
if doi:
    print(f"🎉 取得大成功: {doi}")
else:
    print("⚠️ DOIは見つかりませんでした。")