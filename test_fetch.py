import urllib.request
import urllib.parse
from bs4 import BeautifulSoup

APP_ID = "AXGGAvFMJbYaP3WY5tNz"

def run_cinii_exact_spec_smoke_test():
    author_name = "坂倉 杏介"
    encoded_author = urllib.parse.quote(author_name)
    
    api_url = f"https://ci.nii.ac.jp/opensearch/search?author={encoded_author}&format=rss&count=50&appid={APP_ID}"
    
    print("=== CiNii OpenSearch API 公式仕様完全準拠テスト ===")
    print(f"リクエストURL: {api_url}\n")
    
    headers = {
        "User-Agent": "AcademicResearchTracker/1.0"
    }
    
    try:
        req = urllib.request.Request(api_url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as res:
            xml_data = res.read()
            soup = BeautifulSoup(xml_data, "xml")
            
            total_tag = soup.find(['opensearch:totalResults', 'totalResults'])
            total_count = total_tag.text.strip() if total_tag else "不明"
            
            items = soup.find_all('item')
            print(f"👉 CiNii総ヒット件数: {total_count} 件")
            print(f"👉 今回抽出件数: {len(items)} 件\n")
            
            for i, item in enumerate(items[:15], 1):
                title_tag = item.find('title')
                title = title_tag.text.strip() if title_tag else "タイトルなし"
                
                link_tag = item.find('link')
                link = link_tag.text.strip() if link_tag else ""
                
                creators = [c.text.strip() for c in item.find_all(['dc:creator', 'creator']) if c.text]
                creator_str = ", ".join(creators) if creators else "著者不明"
                first_author = creators[0] if creators else "著者不明"
                
                journal_tag = item.find(['prism:publicationName', 'publicationName'])
                journal = journal_tag.text.strip() if journal_tag else "掲載誌不明"
                
                date_tag = item.find(['prism:publicationDate', 'publicationDate', 'dc:date', 'date'])
                pub_date = date_tag.text.strip()[:4] if date_tag and date_tag.text else "年不明"
                
                print(f"  {i}. 【{pub_date}】{title}")
                print(f"     筆頭著者: {first_author} ｜ 全著者: {creator_str}")
                print(f"     掲載誌: {journal}")
                print(f"     URL: {link}\n")
                
    except Exception as e:
        print(f"❌ エラー: {e}")

if __name__ == "__main__":
    run_cinii_exact_spec_smoke_test()