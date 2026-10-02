import os
from google import genai
from google.genai import types

# 仕様に基づくモデル指定
MODEL_NAME = "gemini-2.5-flash-lite"

def get_gemini_client():
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise ValueError("GOOGLE_API_KEY is not set.")
    return genai.Client(api_key=api_key)

def call_gemini_with_google_search(prompt: str):
    """
    Google Search Groundingを使用したGemini呼び出し（gemini-3.6-flash）。
    失敗時は自動的にテキストモードへフォールバックする。
    """
    try:
        client = get_gemini_client()
        # SDK仕様に従いGoogleSearchツールを定義
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
            ),
        )
        return response.text
    except Exception as e:
        # フォールバック処理：推測を抑止する指示を追加してテキストのみで再試行
        print(f"Search API Error: {e}. Falling back to text-only mode.")
        try:
            client = get_gemini_client()
            fallback_prompt = prompt + "\n\n【指示】検索機能が利用不可です。所属学会などの事実情報は、確実なもの以外は空文字にしてください。"
            response = client.models.generate_content(
                model=MODEL_NAME,
                contents=fallback_prompt,
            )
            return response.text
        except Exception as e2:
            return f"Gemini API Error: {e2}"
