import os
from dotenv import load_dotenv
from google import genai

load_dotenv()

api_key = os.getenv("GEMINI_API_KEY")
client = genai.Client(api_key=api_key)

try:
    # まず検索ツールなしでモデル自体の疎通を確認
    res = client.models.generate_content(
        model="gemini-3.6-flash",
        contents="こんにちは。1文で自己紹介してください。"
    )
    print("=== 素のテキスト生成: 成功 ===")
    print(res.text)
except Exception as e:
    print(f"=== エラー ===\n{e}")