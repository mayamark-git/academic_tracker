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

- 自分の論文を起点にした関連文献探索
  - 基準論文PDFから研究課題・キーワード・概念・研究者を抽出し、検索前に編集
  - CiNiiから書誌情報を先に取得し、内容と研究者の関連度（A〜D）・判定理由を表示
  - 選択した最大10件のみ、公開PDFの取得と全文ベースのAI要約を実行
  - 候補検索結果は専用履歴に保存し、選択した文献だけ既存の論文一覧に統合

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
- research_discovery.py: 関連文献探索のデータ整形・判定ヘルパー
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

## 開発・テスト

ユニットテストとオフラインのワークフロー統合テストは `python -m unittest discover -v` で実行できます。テストでは一時ディレクトリまたはインメモリSQLiteを使い、CiNiiなどの外部API・手元の `papers.db`・PDFには触れません。`test_workflows.py` ではDB初期化、CiNii同期の部分失敗、候補履歴の保存、選択候補の既存論文への統合までを通して確認します。

アプリのDB接続先は `ACADEMIC_TRACKER_DB_PATH` で変更できます。未設定時は従来どおり作業ディレクトリの `papers.db` を使用します。アプリ全体の接続先が変わるため、検証時は本番・個人用DBではなく、専用の別ファイルを指定してください。

DBスキーマを変更する場合は、アプリ起動時に繰り返し実行しても安全な初期化・移行処理に追加し、既存のローカルDBと空のテストDBの両方で動作を確認してください。列の追加は既存データを保持する形にし、個人のDBやPDFをテストデータとして使わないでください。

CiNii同期は一部の検索条件に失敗しても取得できた論文を保存し、失敗条件と追加件数を画面に表示します。外部サービス側の一時エラーでは、同じ操作を再実行できます。PDF取得ではHTTPエラーやPDF以外の応答を明示し、不完全なダウンロードファイルを完成ファイルとして保存しません。

論文一覧画面の「データのバックアップとエクスポート」から、SQLiteデータベース全体の整合性を保ったバックアップと論文一覧CSVをダウンロードできます。DBバックアップは研究者・学会・設定なども含みますが、PDF本体は含みません。必要な場合は `pdf_files/` も別途バックアップしてください。ダウンロードファイルには研究メモ等が含まれるため、安全な場所に保管してください。

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
- UNPAYWALL_EMAIL: Unpaywall API の利用に必要な連絡先メールアドレス（有効なアドレスを設定）

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

## 自分の論文から関連文献を探す

「自分の論文から探す」画面で基準論文のPDFを登録します。PDFの抽出テキストはキーワード・概念・研究者の分析のため Gemini API に送信されます。CiNii候補のタイトル・著者・抄録も関連度判定のため Gemini API に送信されます。送信を避けたい文書ではこの機能を使用しないでください。

抽出結果を確認・編集してからCiNii検索を行います。検索条件はキーワード/概念と研究者をそれぞれ最大5件選択でき、各条件から最大30件を取得して重複をまとめます。評価対象は最大100件です。検索時点ではPDFをダウンロードせず、A〜Dの関連度と理由を確認します。PDF取得対象として選択できるのは高関連度のA・Bのみで、1回に最大10件です。PDF直リンクが得られない場合は書誌情報を候補履歴に残し、PDFの代わりに出版社等のリンクから確認してください。

全文要約はPDFからテキストを抽出して分割処理します。文字情報を抽出できないスキャン画像PDFにはOCR処理を行わないため、OCR済みPDFを用意してください。PDF取得とGeminiの利用には、各サービスの利用条件・料金・レート制限が適用されます。

## ライセンス

本リポジトリは個人利用の研究支援ツールとして管理しています。公開用途に応じて必要に応じてライセンスを追加してください。

## 補足

- 本アプリはローカル研究支援用途を前提にしています。
- PDF や DB は個人の研究データを含むため、GitHub への公開時は必ずローカルデータの扱いを確認してください。
