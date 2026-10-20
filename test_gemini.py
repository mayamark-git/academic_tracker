import os
from dotenv import load_dotenv
from google import genai

def run_gemini_smoke_test():
    load_dotenv()
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEYが設定されていません。")

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model="gemini-3.6-flash",
        contents="こんにちは。1文で自己紹介してください。",
    )
    print("=== 素のテキスト生成: 成功 ===")
    print(response.text)


if __name__ == "__main__":
    run_gemini_smoke_test()