# Academic Tracker Backend (Cloud Run + Firestore)

FastAPI で書いたバックエンドです。Cloud Run で動かし、Firebase Admin SDK 経由で Firestore を読み書きします。

## 認証と秘密情報

- Firestore への接続は Application Default Credentials を使います。Cloud Run 上では、サービスに割り当てたサービスアカウントの権限で接続します。
- **サービスアカウントの鍵ファイルは使いません。** コード・イメージ・環境変数に鍵や API キーを置きません。
- 初期設定は認証必須（`--no-allow-unauthenticated`）です。呼び出し側には Cloud Run 起動元（`roles/run.invoker`）の権限が必要です。

## API

| メソッド | パス | 内容 |
|---|---|---|
| GET | `/healthz` | プロセスの生存確認（Firestore に触れない） |
| GET | `/readyz` | Firestore への疎通確認 |
| GET / POST | `/{collection}` | 一覧（`limit` 1〜500）／作成 |
| GET / PATCH / DELETE | `/{collection}/{id}` | 取得／部分更新／削除 |

`collection` は `papers`（キー: `title`）、`researchers`（`name`）、`societies`（`name`）、`keywords`（`keyword`）です。ドキュメント ID はキーの正規化ハッシュで、同じキーの作成は 409 を返します。キー自体は PATCH で変更できません。

## ローカルでのテスト

Firestore には接続せず、メモリ上のストアで検証します。

```bash
cd backend
pip install -r requirements.txt
python -m unittest -v test_backend
```

## デプロイ手順

`PROJECT_ID` は自分のプロジェクト ID に置き換えます。

```bash
gcloud config set project PROJECT_ID
gcloud services enable run.googleapis.com firestore.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com

# Firestore（ネイティブモード、未作成の場合のみ）
gcloud firestore databases create --location=asia-northeast1

# 実行用サービスアカウントと最小権限
gcloud iam service-accounts create academic-tracker-api
gcloud projects add-iam-policy-binding PROJECT_ID \
  --member="serviceAccount:academic-tracker-api@PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/datastore.user"

# デプロイ（リポジトリのルートで実行）
gcloud run deploy academic-tracker-api \
  --source backend \
  --region asia-northeast1 \
  --service-account academic-tracker-api@PROJECT_ID.iam.gserviceaccount.com \
  --no-allow-unauthenticated
```

## 動作確認

```bash
URL=$(gcloud run services describe academic-tracker-api --region asia-northeast1 --format="value(status.url)")
TOKEN=$(gcloud auth print-identity-token)
curl -H "Authorization: Bearer $TOKEN" "$URL/readyz"
curl -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"title":"テスト論文"}' "$URL/papers"
```

## 現状の範囲

- 既存の Streamlit アプリ（SQLite 利用）はこのバックエンドに接続していません。ローカル利用は従来どおりです。
- Gemini 呼び出し、CiNii 同期、PDF 処理はバックエンドに移していません。
- 既存の SQLite データの移行は未実装です。
