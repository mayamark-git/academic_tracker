# Academic Tracker

Academic Tracker は、学術論文・研究者・学会を一元管理し、CiNii 連携と AI 要約を組み合わせて研究活動を支援する Streamlit アプリです。

## 主要機能

- 論文の収集と管理
  - CiNii からの論文検索・取得
  - DOI・抄録・PDF URL の自動取得
  - 既読 / 未読の切り替え
  - メモとキーワード管理

- PDF 管理と AI 要約
  - ローカル PDF の読み込み
  - PDF 本文からテキスト抽出
  - Gemini による先行研究レビュー要約

- 研究者プロファイル管理
  - 研究者の基本情報、所属、関連学会、メモの管理
  - AI による研究者情報の再取得と補完

- 学会マッピング
  - 研究者と学会の双方向同期
  - 関連学会の管理と検索リンク

- ローカル DB 管理
  - SQLite による論文・研究者・学会情報の保存
  - WAL モードを利用した柔軟なローカル運用

## 技術スタック

- Python 3
- Streamlit
- SQLite
- requests
- BeautifulSoup4
- pypdf
- python-dotenv
- Google GenAI SDK

## プロジェクト構成

- app.py: メインアプリケーション
- cron_fetcher.py: CiNii 連携と PDF 取得処理
- database.py: DB 接続ユーティリティ
- gemini_client.py: Gemini API 呼び出しの補助
- ui_components.py: UI 部品
- utils.py: 文字列サニタイズなどのヘルパー
- seed_papers.py: 初期データ投入用
- sync_local_pdfs.py: ローカル PDF 同期補助
- pdf_files/: ダウンロード済み PDF を保存するローカルフォルダ
- papers.db: SQLite データベース
- .env: ローカル環境変数（Git 管理対象外）

## セットアップ

1. Python 3.10 以上の環境を用意します。
2. 依存関係をインストールします。

```bash
pip install streamlit requests beautifulsoup4 python-dotenv pypdf google-genai
```

3. .env ファイルを作成し、必要な環境変数を設定します。

```bash
copy .env.example .env
```

4. アプリを起動します。

```bash
streamlit run app.py
```

## 環境変数

例として .env.example を用意しています。実際には次の値を入力してください。

```env
GEMINI_API_KEY=your_gemini_api_key
GOOGLE_API_KEY=your_gemini_api_key
CINII_APPID=your_cinii_appid
```

- GEMINI_API_KEY: Gemini API のキー
- GOOGLE_API_KEY: 互換用途のキー
- CINII_APPID: CiNii API 利用時の APP ID

## 公開前の注意

このプロジェクトでは、ローカルの秘密情報と生成物を GitHub に載せないようにしています。

対象外として扱うファイルは次のとおりです。

- .env
- papers.db
- papers.db-shm
- papers.db-wal
- pdf_files/
- __pycache__/
- .vscode/

## ライセンス

本リポジトリは個人利用の研究支援ツールとして管理しています。公開用途に応じて必要に応じてライセンスを追加してください。

## 補足

- 本アプリはローカル研究支援用途を前提にしています。
- PDF や DB は個人の研究データを含むため、GitHub への公開時は必ずローカルデータの扱いを確認してください。
