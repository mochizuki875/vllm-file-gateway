# vLLM File Gateway

PDF、PPTX、DOCX、XLSXを画像とテキストへ変換し、OpenAI互換のFiles、Chat Completions、Responses APIからvLLMへ送信するGatewayです。

## 起動

Python 3.13、LibreOffice、Noto Sans CJK、Carlitoが必要です。開発コンテナでは次を実行します。

```bash
./.venv/bin/pip install -r requirements.txt
./.venv/bin/uvicorn gateway.app:app --host 0.0.0.0 --port 8080
```

接続設定は`.env`から読み込みます。現在の`.env`には指定されたvLLM接続先が設定されています。クライアントはGatewayへ`Authorization: Bearer dummy`を送ります。

```dotenv
VLLM_MODEL=gemma-4-26B-A4B
VLLM_BASE_URL=http://192.168.2.174:8000/v1
VLLM_API_KEY=dummy
GATEWAY_API_KEY=dummy
GATEWAY_DATA_DIR=./gateway-data
MAX_DOCUMENT_IMAGES=4
```

## 利用例

```bash
curl http://localhost:8080/v1/files \
  -H 'Authorization: Bearer dummy' \
  -F purpose=user_data \
  -F file=@document.pdf
```

アップロードは非同期変換を開始し、最初は`status: "uploaded"`を返します。応答の`id`を使い、`status`が`processed`になるまで状態を取得します。

```bash
curl http://localhost:8080/v1/files/file_... \
  -H 'Authorization: Bearer dummy' | jq .
```

`status`が`processed`になった後、Responses APIで参照します。

```bash
curl http://localhost:8080/v1/responses \
  -H 'Authorization: Bearer dummy' \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "gemma-4-26B-A4B",
    "input": [{
      "role": "user",
      "content": [
        {"type": "input_file", "file_id": "file_..."},
        {"type": "input_text", "text": "この文書を要約してください。"}
      ]
    }]
  }'
```

Chat Completionsではユーザーメッセージのcontentに`{"type":"file","file":{"file_id":"file_..."}}`を指定します。Responsesは`file_data`と公開HTTPSの`file_url`、Chat Completionsは`file_data`にも対応します。

### OpenAI Python SDK

[samples/openai_file_response.py](samples/openai_file_response.py)は、ファイルのアップロード、変換完了待ち、Responses APIの実行、ファイル削除までを行います。Responses APIが失敗した場合も`finally`でファイルを削除します。

```bash
./.venv/bin/python samples/openai_file_response.py document.pdf \
  --prompt 'この文書の重要な点を3つ挙げてください。'
```

接続先を変更する場合は、OpenAI SDK用の環境変数を指定します。

```bash
OPENAI_BASE_URL=http://localhost:8080/v1 \
OPENAI_API_KEY=dummy \
OPENAI_MODEL=gemma-4-26B-A4B \
./.venv/bin/python samples/openai_file_response.py document.pdf
```

## テスト

```bash
./.venv/bin/python -m unittest discover -v
```

詳細な制約と段階的な拡張方針は[DESIGN.md](DESIGN.md)を参照してください。