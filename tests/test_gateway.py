from __future__ import annotations

import base64
import io
import os
import tempfile
import time
import unittest
from pathlib import Path

import httpx
import pymupdf
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.config import get_settings


class FakeUpstream:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, object]]] = []

    async def post(self, url: str, json: dict[str, object]) -> httpx.Response:
        self.requests.append((url, json))
        request = httpx.Request("POST", url)
        return httpx.Response(200, json={"id": "upstream-result", "object": "response"}, request=request)

    async def aclose(self) -> None:
        pass


class GatewayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.environment = {
            "VLLM_MODEL": "test-model",
            "VLLM_BASE_URL": "http://vllm.test/v1",
            "VLLM_API_KEY": "upstream-key",
            "GATEWAY_API_KEY": "gateway-key",
            "GATEWAY_DATA_DIR": self.temporary.name,
        }
        self.original_environment = {key: os.environ.get(key) for key in self.environment}
        os.environ.update(self.environment)
        get_settings.cache_clear()
        self.client_context = TestClient(app)
        self.client = self.client_context.__enter__()
        self.upstream = FakeUpstream()
        self.client.app.state.http_client = self.upstream
        self.headers = {"Authorization": "Bearer gateway-key"}

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        get_settings.cache_clear()
        for key, value in self.original_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def test_authentication_and_health(self) -> None:
        self.assertEqual(self.client.get("/health").status_code, 200)
        response = self.client.get("/v1/files")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "invalid_api_key")

    def test_file_lifecycle_and_chat_expansion(self) -> None:
        created = self._upload_pdf("The verification code is 4821.")
        file_id = created["id"]
        current = self._wait_until_processed(file_id)
        self.assertEqual(current["status"], "processed")

        response = self.client.post(
            "/v1/chat/completions",
            headers=self.headers,
            json={
                "model": "test-model",
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "file", "file": {"file_id": file_id}},
                        {"type": "text", "text": "Read the code."},
                    ],
                }],
                "temperature": 0.25,
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = self.upstream.requests[-1][1]
        self.assertEqual(payload["temperature"], 0.25)
        content = payload["messages"][1]["content"]  # type: ignore[index]
        self.assertIn("4821", content[0]["text"])
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/webp;base64,"))

        deleted = self.client.delete(f"/v1/files/{file_id}", headers=self.headers)
        self.assertTrue(deleted.json()["deleted"])
        self.assertEqual(self.client.get(f"/v1/files/{file_id}", headers=self.headers).status_code, 404)

    def test_responses_file_data_is_temporary(self) -> None:
        encoded = base64.b64encode(self._pdf_bytes("Temporary document text")).decode("ascii")
        response = self.client.post(
            "/v1/responses",
            headers=self.headers,
            json={
                "model": "test-model",
                "input": [{
                    "role": "user",
                    "content": [
                        {"type": "input_file", "filename": "inline.pdf", "file_data": encoded},
                        {"type": "input_text", "text": "Summarize it."},
                    ],
                }],
                "max_output_tokens": 64,
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = self.upstream.requests[-1][1]
        message = payload["input"][0]  # type: ignore[index]
        self.assertEqual(message["type"], "message")
        self.assertIn("Temporary document text", message["content"][0]["text"])
        self.assertEqual(message["content"][1]["detail"], "auto")
        self.assertEqual(list((Path(self.temporary.name) / "work").iterdir()), [])

    def _upload_pdf(self, text: str) -> dict[str, object]:
        response = self.client.post(
            "/v1/files",
            headers=self.headers,
            files={"file": ("sample.pdf", io.BytesIO(self._pdf_bytes(text)), "application/pdf")},
            data={"purpose": "user_data"},
        )
        self.assertEqual(response.status_code, 200)
        return response.json()

    def _wait_until_processed(self, file_id: object) -> dict[str, object]:
        for _ in range(100):
            response = self.client.get(f"/v1/files/{file_id}", headers=self.headers)
            body = response.json()
            if body["status"] != "uploaded":
                return body
            time.sleep(0.05)
        self.fail("File conversion did not finish")

    @staticmethod
    def _pdf_bytes(text: str) -> bytes:
        document = pymupdf.open()
        page = document.new_page()
        page.insert_text((72, 72), text)
        content = document.tobytes()
        document.close()
        return content


if __name__ == "__main__":
    unittest.main()