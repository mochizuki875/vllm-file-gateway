# vLLM File Gateway 設計

## 1. 文書の目的

本書は、vLLM File Gateway `0.1.0`の現在の設計と実装境界を説明する。

Gatewayは、PDFおよびOffice文書をテキストとページ画像へ変換し、OpenAI互換のFiles API、Responses API、Chat Completions APIを通じてvLLMのマルチモーダルモデルへ入力する。

本書でいうOpenAI互換は、対応するHTTPエンドポイントと入力形式に関する互換性を指す。OpenAIのホスト型サービス、内部の文書変換、トークン使用量、回答品質との同一性は保証しない。

## 2. 設計原則

1. vLLMはモデル推論だけを担当し、ファイル保存はGateway、画像変換は外部ライブラリが担当する。
2. Gatewayの`file_id`をvLLMへ渡さず、抽出テキストとdata URL画像へ展開する。
3. Files APIの変換は非同期で実行し、APIワーカーを長時間占有しない。
4. `file_data`と`file_url`はリクエスト中だけ保持し、処理終了時に削除する。
5. 保存ファイルは作成から6時間で失効させる。
6. Authorizationから導出したテナントIDを、すべての保存ファイル参照に使用する。
7. モデル名、vLLM接続先、認証モード、データ保存先は環境変数で設定する。
8. 単一Gatewayインスタンスとローカル永続領域を運用単位とする。

## 3. 対応範囲

### 3.1 文書形式

| 拡張子 | メディアタイプ | ページ画像 | テキスト |
| --- | --- | --- | --- |
| `.pdf` | `application/pdf` | PyMuPDFで直接描画 | PyMuPDFでページ単位に抽出 |
| `.pptx` | PowerPoint Open XML | LibreOfficeでPDF化して描画 | python-pptxでスライド単位に抽出 |
| `.docx` | Word Open XML | LibreOfficeでPDF化して描画 | python-docxで段落と表を抽出 |
| `.xlsx` | Excel Open XML | LibreOfficeでPDF化して描画 | openpyxlでシート単位に抽出 |

暗号化PDF、旧Office形式、マクロ付き形式、その他の拡張子は対応対象外である。Office文書の動画、アニメーション、マクロは実行しない。

### 3.2 API

| メソッド | パス | 動作 |
| --- | --- | --- |
| `GET` | `/health` | プロセスのヘルス状態を返す |
| `POST` | `/v1/files` | ファイルを保存して変換をキューへ追加する |
| `GET` | `/v1/files` | 呼び出し元が所有するファイルを一覧する |
| `GET` | `/v1/files/{file_id}` | Fileオブジェクトを返す |
| `GET` | `/v1/files/{file_id}/content` | 保存した元ファイルを返す |
| `DELETE` | `/v1/files/{file_id}` | ファイルと派生データを削除する |
| `POST` | `/v1/responses` | ファイル入力を展開してvLLM Responses APIへ転送する |
| `POST` | `/v1/chat/completions` | ファイル入力を展開してvLLM Chat Completions APIへ転送する |
| 各種 | `/v1/*` | 上記以外のAPIをvLLMへ透過転送する |

Responses APIとChat Completions APIは同期応答だけを提供する。`stream: true`および`previous_response_id`には対応しない。

### 3.3 ファイル入力

Responses APIは次の`input_file`ソースに対応する。

- Files APIが発行した`file_id`
- `filename`とbase64またはdata URL形式の`file_data`
- 公開HTTPS URLの`file_url`

Chat Completions APIは、ユーザーメッセージ内の`type: "file"`で次のソースに対応する。

- Files APIが発行した`file_id`
- `filename`とbase64またはdata URL形式の`file_data`

一つのファイル参照には、対応するソースを一つだけ指定できる。Chat Completions APIには独自の`file_url`形式を追加しない。

## 4. システム構成

```mermaid
flowchart LR
  Client[OpenAI SDK / HTTP Client]
  API[FastAPI Gateway]
  Queue[Conversion Queue]
  Converter[External document-image-renderer]
  Storage[(SQLite + Filesystem)]
  VLLM[vLLM]

  Client -->|Files / Responses / Chat| API
  API --> Queue
  Queue --> Converter
  Converter --> Storage
  API --> Storage
  API -->|Text + data URL images| VLLM
  VLLM --> API
  API --> Client
```

### 4.1 モジュール責務

| モジュール | 責務 |
| --- | --- |
| `gateway.main` | FastAPIエンドポイント、認証、入力展開、vLLMへの転送 |
| `gateway.service` | ファイルライフサイクル、変換キュー、一時入力の解決、保持期限処理 |
| `gateway.converter` | 形式別テキスト抽出、外部ライブラリの呼び出し、マニフェストの生成 |
| `gateway.database` | SQLiteスキーマ、所有者を含むファイル検索、状態更新 |
| `gateway.config` | `.env`と環境変数の読み込み、設定値の検証 |
| `gateway.errors` | GatewayエラーのOpenAI形式への変換 |
| 外部`document-image-renderer` | OfficeからPDFへの変換、PDF描画、画像保存 |

Gatewayは起動時にSQLiteスキーマを作成し、変換ワーカーと削除ワーカーを開始する。
変換キューはプロセス内の`asyncio.Queue`であり、文書変換は`asyncio.to_thread`を使ってワーカースレッドで実行する。
外部ライブラリはOfficeからPDFへの変換時に、LibreOfficeをタイムアウト付き子プロセスとして起動する。

## 5. ファイルライフサイクル

### 5.1 状態

内部状態と外部Fileオブジェクトの対応は次のとおりである。

| 内部状態 | APIの`status` | 意味 |
| --- | --- | --- |
| `uploaded` | `uploaded` | 保存済み、変換待ち |
| `processing` | `uploaded` | 変換中 |
| `processed` | `processed` | 推論で参照可能 |
| `failed` | `error` | 変換失敗 |
| `deleted` | 非公開 | 論理削除済み |

```mermaid
stateDiagram-v2
  [*] --> uploaded
  uploaded --> processing
  processing --> processed
  processing --> failed
  uploaded --> deleted
  processing --> deleted
  processed --> deleted
  failed --> deleted
```

変換前または変換中のファイルを推論で参照すると`409 file_not_ready`を返す。変換に失敗したファイルは`422 file_processing_failed`を返す。

### 5.2 アップロード

`POST /v1/files`は`multipart/form-data`を受け付ける。`purpose`は`user_data`だけに対応する。`expires_after`を指定する場合は次の値だけを受け付ける。

```json
{"anchor":"created_at","seconds":21600}
```

APIは受信データを最大1 MiB単位で読み、合計サイズを検査する。ファイル名にはベース名だけを使用し、保存ディレクトリにはテナントIDとランダムな`file_id`を使用する。

### 5.3 非同期変換

ファイル保存とデータベース登録が完了すると、`file_id`を変換キューへ追加する。変換ワーカーは状態を`processing`へ変更し、変換成果物が揃った後に`processed`へ変更する。例外が発生した場合は`failed`へ変更し、内部エラー文字列を最大1,000文字でデータベースへ保存する。

起動時には、有効期限内で`uploaded`または`processing`のレコードをキューへ再登録する。キューは単一プロセス内にあるため、複数インスタンス間では共有されない。

### 5.4 保持期限と削除

Files APIで保存したデータの有効期限は、`created_at + 21,600秒`である。削除ワーカーは30秒間隔で期限超過レコードを検索し、論理削除したうえでファイルディレクトリを物理削除する。

削除APIも同じ処理を行う。論理削除後または失効後のファイルは、取得、一覧、内容取得、推論参照の対象にならない。

推論中のファイルに対するリースは実装していない。推論と削除が同時に発生した場合、読み込みとの競合が発生する可能性がある。

## 6. 文書変換

画像変換には外部の[document-image-renderer](https://github.com/mochizuki875/document-image-renderer)を使用する。
Gatewayは`RenderOptions`で出力形式、DPI、LibreOfficeのタイムアウトを指定し、`render_document`を呼び出す。
Gateway固有の入力検証、テキスト抽出、manifest生成は外部ライブラリへ含めない。

### 6.1 入力検証

許可する拡張子は`.pdf`、`.pptx`、`.docx`、`.xlsx`である。PDFは先頭の`%PDF-`、Office文書は先頭のZIPローカルファイルヘッダー`PK\x03\x04`を確認する。

この検証は拡張子と基本シグネチャの不一致を検出するものであり、Office ZIP内部構造の完全な検証やマルウェア判定は行わない。

### 6.2 テキスト抽出

- PDFは各ページのテキスト層を抽出する。
- PPTXは各スライド上のテキストフレームを文書順に連結する。
- DOCXは本文段落と表セルを一つのテキストブロックへ連結する。
- XLSXはワークシートごとにセル値をタブ区切りで出力する。数式は計算せず、数式文字列を使用する。

Office文書のテキストブロック数と、LibreOfficeが生成するPDFページ数は一致しない場合がある。対応するテキストブロックがないページでは、空のテキストを使用する。

### 6.3 ページ画像

PDFまたはLibreOfficeが生成したPDFを、外部ライブラリが150 DPIのRGB画像として描画し、PNGとして保存する。透過は使用しない。

変換可能な文書は最大20生成画像である。外部ライブラリによる描画後に上限を検査し、超過した場合は生成画像を削除して変換を失敗させる。

Microsoft OfficeとLibreOfficeではレイアウト、改ページ、フォント置換が異なる場合がある。GatewayはMicrosoft Officeとの画素単位の一致を保証しない。

### 6.4 変換マニフェスト

変換結果は`manifest.json`へ保存する。

```json
{
  "schema_version": 1,
  "converter_version": "2026.09.0",
  "source": {
    "media_type": "application/pdf",
    "sha256": "..."
  },
  "documents": [
    {
      "name": "source.pdf",
      "pages": [
        {
          "page_number": 1,
          "image_path": "source-page-0001.png",
          "text_path": "page-0001.txt",
          "width": 1448,
          "height": 2048,
          "media_type": "image/png",
          "sha256": "..."
        }
      ]
    }
  ],
  "warnings": []
}
```

マニフェスト内の成果物パスは、`derived`ディレクトリからの相対パスである。

## 7. 推論入力の組み立て

### 7.1 ファイル解決

`file_id`は、Authorizationから導出したテナントIDと組み合わせてSQLiteから検索する。別のAuthorizationが所有するID、削除済みID、失効済みIDはすべて`404 file_not_found`として扱う。Authorizationがない場合は共通の匿名テナントIDを使用する。

`file_data`は厳密なbase64検証を行い、`GATEWAY_DATA_DIR/work`配下の一時ディレクトリへ保存して同期変換する。`file_url`も同じ一時領域へダウンロードして同期変換する。一時領域はvLLM応答、Gatewayエラー、タイムアウトのいずれの場合もリクエスト終了時に削除する。

### 7.2 Responses API

`input_file`を、ページごとの`input_text`と`input_image`へ置き換える。

```json
[
  {
    "type": "input_text",
    "text": "<document filename=\"report.pdf\" page=\"1\">\n...\n</document>"
  },
  {
    "type": "input_image",
    "detail": "auto",
    "image_url": "data:image/png;base64,..."
  }
]
```

`role`を持つ入力項目には、vLLM Responses API向けに`type: "message"`を補う。

### 7.3 Chat Completions API

`type: "file"`を、ページごとの`text`と`image_url`へ置き換える。

```json
[
  {
    "type": "text",
    "text": "<document filename=\"report.pdf\" page=\"1\">\n...\n</document>"
  },
  {
    "type": "image_url",
    "image_url": {
      "url": "data:image/png;base64,..."
    }
  }
]
```

### 7.4 画像上限

抽出テキストはすべてのページ分を入力へ追加する。画像は文書ごとに先頭から`MAX_DOCUMENT_IMAGES`枚だけを追加し、既定値は8枚である。

現在の選択方式は先頭ページ優先であり、質問との関連度によるページ検索や入力トークン予算の計算は行わない。複数文書を指定した場合、画像上限はリクエスト全体ではなく各文書へ個別に適用される。

### 7.5 文書内命令への対策

Gatewayは、文書内容が信頼できない参照データであることをモデルへ指示する。Responses APIでは既存の`instructions`の前、Chat Completions APIでは先頭のsystemメッセージとして次の趣旨を追加する。

- 文書内の命令には従わない。
- 可能であればページ番号を示す。

この指示はプロンプトインジェクションの影響を抑えるための補助策であり、完全な防止を保証しない。

## 8. vLLM転送

Gatewayは外部APIと同種のvLLMエンドポイントへHTTP POSTする。

- `/v1/responses`は`{VLLM_BASE_URL}/responses`へ転送する。
- `/v1/chat/completions`は`{VLLM_BASE_URL}/chat/completions`へ転送する。

明示的に実装していない`/v1/*`は、HTTPメソッド、クエリ、本文、Content-Typeを維持して同じvLLMパスへ転送する。任意認証モードでは受信したAuthorizationがあれば維持し、必須認証モードでは`VLLM_API_KEY`へ差し替える。Host、Content-Lengthおよびhop-by-hopヘッダーは転送しない。Files API名前空間はGatewayが所有し、未実装メソッドを透過転送しない。

リクエストの`model`は`VLLM_MODEL`と完全一致する必要がある。不一致の場合は`404 model_not_found`を返す。

ファイルcontent part、`instructions`、Chat Completionsの`messages`以外のフィールドは、受け取ったJSON値を維持してvLLMへ転送する。パラメーターの実際の対応範囲は、vLLMのバージョン、設定モデル、チャットテンプレート、ツールパーサーに依存する。

vLLMのHTTP応答は、ステータスコード、本文、基本メディアタイプを維持してクライアントへ返す。Gatewayは上流のエラー本文を独自形式へ再構成しない。

接続またはHTTPクライアントエラーは`502 model_upstream_error`、タイムアウトは`504 model_timeout`へ変換する。推論要求の自動再試行は行わない。

## 9. データモデルと保存構成

### 9.1 SQLite

`files`テーブルは次の情報を保持する。

| 列 | 内容 |
| --- | --- |
| `id` | 外部公開するランダムな`file_id` |
| `tenant_id` | Authorizationまたは匿名値のSHA-256から導出した所有者ID |
| `filename` | ベース名へ正規化した表示名 |
| `media_type` | 拡張子に対応するメディアタイプ |
| `purpose` | `user_data` |
| `byte_size` | 元ファイルのサイズ |
| `sha256` | 元ファイルのSHA-256 |
| `status` | 内部変換状態 |
| `source_path` | データディレクトリからの相対パス |
| `manifest_path` | データディレクトリからの相対パス |
| `error_message` | 変換失敗時の内部メッセージ |
| `created_at` | Unix時刻 |
| `expires_at` | 作成から6時間後のUnix時刻 |
| `deleted_at` | 論理削除時刻 |

### 9.2 ファイルシステム

```text
GATEWAY_DATA_DIR/
├── gateway.db
├── files/
│   └── <tenant-id>/
│       └── <file-id>/
│           ├── source.<ext>
│           ├── manifest.json
│           └── derived/
│               ├── source-page-0001.png
│               └── page-0001.txt
└── work/
```

`work`には`file_data`と`file_url`用の一時ディレクトリを作成する。Files APIで保存するデータとSQLiteは、同じ永続ボリュームに配置する必要がある。

## 10. Authorization転送とテナント分離

`GATEWAY_AUTH_REQUIRED=false`では、`/v1`以下のAuthorizationは任意である。Gatewayは値を照合せず、vLLMへ転送するAPIでは受信したAuthorizationを維持する。Authorizationがない場合は認証ヘッダーを追加せず、APIキーを要求するかどうかと、その認証はvLLMが決定する。

`GATEWAY_AUTH_REQUIRED=true`では、すべての`/v1`リクエストに`GATEWAY_API_KEY`をBearerトークンとして要求し、Gatewayで定時間比較する。vLLMへ転送する際はAuthorizationを`VLLM_API_KEY`へ差し替える。両キーが未設定の場合はGatewayの起動を拒否する。

テナントIDは受信Bearer値からSHA-256先頭32文字を導出し、ファイルの作成、検索、一覧、削除に使用する。任意モードでAuthorizationがない場合は、空文字列から導出した共通の匿名テナントIDを使用する。

`GET /health`は認証を要求せず、Gatewayプロセスの稼働だけを示す。SQLite、LibreOffice、vLLMへの接続性は検査しない。

## 11. URL取得の制約

`file_url`には次の制約を適用する。

- HTTPSのみ
- 443番ポートのみ
- URL内のユーザー名とパスワードを禁止
- DNS解決結果がすべてグローバルIPアドレスであること
- 最大3回のリダイレクト
- リダイレクト先にも同じURL検証を適用
- 環境のHTTPプロキシ設定を使用しない
- 応答をストリーム受信し、ファイルサイズ上限を適用
- CookieやAuthorizationヘッダーを取得先へ送らない

DNS検証後の接続先IP固定は行っていないため、DNS rebindingを完全には防止しない。公開環境では、URL取得をネットワークレベルでも制限された別サービスへ分離することが望ましい。

## 12. 設定

| 環境変数 | 既定値 | 制約 |
| --- | --- | --- |
| `VLLM_MODEL` | なし | 1文字以上、必須 |
| `VLLM_BASE_URL` | なし | `/v1`で終わるHTTPまたはHTTPS URL、必須 |
| `GATEWAY_AUTH_REQUIRED` | `false` | GatewayでAPIキーを検証するか |
| `GATEWAY_API_KEY` | なし | 必須認証モードのクライアントキー |
| `VLLM_API_KEY` | なし | 必須認証モードでvLLMへ送るキー |
| `GATEWAY_DATA_DIR` | `gateway-data` | SQLiteと文書データの保存先 |
| `FILE_TTL_SECONDS` | `21600` | 現在は`21600`だけを許可 |
| `MAX_FILE_BYTES` | `52428800` | ファイル入力の最大バイト数 |
| `MAX_DOCUMENT_PAGES` | `20` | 文書ごとに変換可能なページ数 |
| `MAX_DOCUMENT_IMAGES` | `8` | 文書ごとにvLLMへ送る画像数 |
| `REQUEST_TIMEOUT_SECONDS` | `300` | vLLM要求のタイムアウト秒数 |

秘密値を含む`.env`はリポジトリへ含めない。

## 13. エラー

Gatewayが生成するエラーは次の形式で返す。

```json
{
  "error": {
    "message": "The file is still being processed.",
    "type": "invalid_request_error",
    "param": "input[0].content[0].file_id",
    "code": "file_not_ready"
  }
}
```

代表的なコードは次のとおりである。

| HTTP | コード | 条件 |
| ---: | --- | --- |
| 400 | `unsupported_file_type` | 拡張子または基本シグネチャが非対応 |
| 400 | `file_too_large` | ファイルサイズ上限超過 |
| 400 | `invalid_file_data` | base64、data URL、ファイル名が不正 |
| 400 | `invalid_file_url` | URLまたは取得結果が許可条件外 |
| 400 | `too_many_pages` | 一時入力のページ上限超過 |
| 400 | `unsupported_feature` | ストリーミングまたは会話状態を要求 |
| 404 | `file_not_found` | ファイルが存在しないか参照不可 |
| 404 | `model_not_found` | モデル名が設定値と不一致 |
| 409 | `file_not_ready` | 保存ファイルを変換中 |
| 422 | `file_processing_failed` | 保存ファイルの変換失敗 |
| 502 | `model_upstream_error` | vLLMへ接続できない |
| 504 | `model_timeout` | vLLM要求がタイムアウト |

Files APIのバックグラウンド変換でページ上限を超えた場合、Fileオブジェクトは`status: "failed"`になる。一時入力の同期変換では`400 too_many_pages`を返す。

## 14. セキュリティ境界

現在の実装には次の対策が含まれる。

- テナントIDを条件に含めたファイル参照
- ファイル名からディレクトリ部分を除去
- 拡張子と基本シグネチャの検証
- ファイルサイズ、ページ数、URL取得条件の制限
- LibreOfficeの一時ユーザープロファイルと実行タイムアウト
- 文書データをモデル命令と区別する追加指示
- 6時間後の論理削除と物理削除
- クライアントのAuthorizationを外部ファイルURLへ転送しないHTTPクライアント分離

現在の実装には次の機能を含まない。

- 文書パーサー全体のコンテナまたはプロセス隔離
- LibreOffice子プロセスのネットワーク遮断、CPU・メモリ・ディスククォータ
- Office ZIP内部構造、展開後サイズ、圧縮率の詳細検査
- マルウェアスキャン
- 保存時暗号化
- TLS終端
- レート制限と利用者別クォータ
- DNS検証後の接続先IP固定
- 監査ログ、メトリクス、分散トレース

このため、Gatewayを信頼できない利用者へ直接公開する構成は想定しない。公開ネットワークで使用する場合は、TLS、追加認証、リクエストサイズ制限、レート制限、ネットワーク分離を備えたリバースプロキシまたはAPI Gatewayの内側に配置し、文書変換処理を別の隔離環境へ分離する必要がある。

## 15. 可用性と運用上の制約

- SQLite、ローカルファイルシステム、プロセス内キューを使用するため、単一インスタンスで運用する。
- 複数インスタンス間で状態、ファイル、変換キューは共有されない。
- データディレクトリを永続化しない場合、再起動時に保存ファイルを失う。
- データベースのスキーマ移行機構はなく、起動時に`create_all`を実行する。
- 変換ワーカーは一つであり、大きな文書が後続処理を待たせる可能性がある。
- 変換失敗の自動再試行は行わない。
- ファイル内容とメタデータをまたぐトランザクションはない。
- データのバックアップと復元はGatewayの機能として提供しない。
- vLLMの可用性は`/health`へ反映されない。

## 16. 既知の互換性制約

- `stream: true`は`400 unsupported_feature`を返す。
- `previous_response_id`は`400 unsupported_feature`を返す。
- Responses APIの文字列形式`input`にはファイルを追加できない。
- Chat Completions APIのファイルは配列形式の`content`内でだけ展開する。
- 入力JSONは厳密なOpenAIスキーマでは検証せず、必要な部分を実行時に検査する。
- ファイル以外の標準パラメーターはベストエフォートで転送する。
- vLLM固有の非対応パラメーターは、上流のステータスと本文のまま返る場合がある。
- Responses APIでは画像の`detail`を常に`auto`としてvLLMへ送る。
- 文書ごとの画像上限を超えたページもテキストは送るため、長い文書ではコンテキスト上限を超える可能性がある。
- PDF化後のページ数制限により、入力上のスライド数、シート数、論理ページ数と受理可否が一致しない場合がある。

## 17. テスト方針

自動テストは、外部vLLMをモックして次の境界を確認する。

- 任意Authorizationの転送、匿名ファイルスコープ、ヘルスチェック
- Files APIの作成、状態遷移、取得、内容取得、一覧、削除
- 所有者トークンによるファイル分離
- Chat Completions APIの`file_id`展開
- Responses APIの`file_data`変換と一時領域削除
- vLLMへ送るテキスト、data URL画像、文書内命令対策

変換結果に影響するLibreOffice、フォント、PyMuPDF、Pillowを更新する場合は、PDF、PPTX、DOCX、XLSXの代表文書でページ数、画像寸法、抽出テキスト、視覚差分を確認する。特定モデルの回答品質やGPU上限はGatewayの単体テスト対象ではなく、利用するvLLM構成ごとに確認する。

## 18. 拡張時の設計境界

現在の単一インスタンス構成を維持したまま追加できる機能は、入力検証の強化、変換成果物の改善、エラー正規化、観測性の追加である。

次の機能は保存方式または実行境界の再設計を必要とする。

- 複数Gatewayインスタンスによる水平スケール
- 分散変換ワーカー
- 複数APIキーと利用者管理
- ストリーミング中の確実なキャンセル
- `previous_response_id`を使う会話状態
- 大規模文書の検索とページ選択
- 文書パーサーの強いサンドボックス

拡張時も、vLLMへ元ファイルやGatewayの`file_id`を渡さず、Gatewayが文書参照をモデル入力へ変換する責務境界を維持する。