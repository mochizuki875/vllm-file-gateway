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

from gateway.main import app
from gateway.config import Settings, get_settings


class FakeUpstream:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, object]]] = []
        self.request_headers: list[dict[str, str]] = []
        self.passthrough_requests: list[dict[str, object]] = []

    async def post(self, url: str, json: dict[str, object], headers: dict[str, str]) -> httpx.Response:
        self.requests.append((url, json))
        self.request_headers.append(headers)
        request = httpx.Request("POST", url)
        return httpx.Response(200, json={"id": "upstream-result", "object": "response"}, request=request)

    async def request(
        self,
        method: str,
        url: str,
        params: list[tuple[str, str]],
        content: bytes,
        headers: dict[str, str],
    ) -> httpx.Response:
        self.passthrough_requests.append(
            {"method": method, "url": url, "params": params, "content": content, "headers": headers}
        )
        request = httpx.Request(method, url)
        return httpx.Response(
            201,
            content=b'{"object":"passthrough"}',
            headers={"content-type": "application/json", "x-vllm-request-id": "upstream-1"},
            request=request,
        )

    async def aclose(self) -> None:
        pass


class GatewayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.environment = {
            "VLLM_MODEL": "test-model",
            "VLLM_BASE_URL": "http://vllm.test/v1",
            "GATEWAY_DATA_DIR": self.temporary.name,
        }
        self.original_environment = {key: os.environ.get(key) for key in self.environment}
        os.environ.update(self.environment)
        get_settings.cache_clear()
        self.client_context = TestClient(app)
        self.client = self.client_context.__enter__()
        self.upstream = FakeUpstream()
        self.client.app.state.http_client = self.upstream
        self.headers = {"Authorization": "Bearer client-key"}

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        get_settings.cache_clear()
        for key, value in self.original_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def test_optional_authentication_and_health(self) -> None:
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.get("/v1/files").status_code, 200)
        self.assertEqual(
            self.client.get("/v1/files", headers={"Authorization": "Bearer unverified-key"}).status_code,
            200,
        )

        response = self.client.post(
            "/v1/responses",
            json={"model": "test-model", "input": "hello"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.upstream.request_headers[-1], {})

    def test_required_gateway_authentication_uses_upstream_key(self) -> None:
        settings = self.client.app.state.settings
        settings.gateway_auth_required = True
        settings.gateway_api_key = "gateway-key"
        settings.vllm_api_key = "upstream-key"

        missing = self.client.get("/v1/files")
        invalid = self.client.get("/v1/files", headers={"Authorization": "Bearer wrong-key"})
        valid = self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer gateway-key"},
            json={"model": "test-model", "input": "hello"},
        )
        passthrough = self.client.get(
            "/v1/models",
            headers={"Authorization": "Bearer gateway-key"},
        )

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(invalid.status_code, 401)
        self.assertEqual(valid.status_code, 200)
        self.assertEqual(self.upstream.request_headers[-1]["Authorization"], "Bearer upstream-key")
        self.assertEqual(passthrough.status_code, 201)
        passthrough_headers = httpx.Headers(self.upstream.passthrough_requests[-1]["headers"])  # type: ignore[arg-type]
        self.assertEqual(passthrough_headers["authorization"], "Bearer upstream-key")

    def test_required_gateway_authentication_requires_both_keys(self) -> None:
        with self.assertRaisesRegex(ValueError, "GATEWAY_API_KEY and VLLM_API_KEY"):
            Settings(
                vllm_model="test-model",
                vllm_base_url="http://vllm.test/v1",
                gateway_auth_required=True,
                gateway_api_key=None,
                vllm_api_key=None,
            )

    def test_file_lifecycle_and_chat_expansion(self) -> None:
        with self.assertLogs("uvicorn.error", level="INFO") as captured:
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

            deleted = self.client.delete(f"/v1/files/{file_id}", headers=self.headers)

        logs = "\n".join(captured.output)
        self.assertIn("File accepted", logs)
        self.assertIn("Document conversion completed", logs)
        self.assertIn("Model request completed", logs)
        self.assertIn("File deleted", logs)
        self.assertNotIn("client-key", logs)
        self.assertNotIn("4821", logs)

        self.assertEqual(response.status_code, 200)
        payload = self.upstream.requests[-1][1]
        self.assertEqual(self.upstream.request_headers[-1]["Authorization"], "Bearer client-key")
        self.assertEqual(payload["temperature"], 0.25)
        content = payload["messages"][1]["content"]  # type: ignore[index]
        self.assertIn("4821", content[0]["text"])
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))

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

    def test_document_page_limit_uses_setting(self) -> None:
        self.client.app.state.settings.max_document_pages = 1
        encoded = base64.b64encode(self._pdf_bytes("Too many pages", page_count=2)).decode("ascii")

        response = self.client.post(
            "/v1/responses",
            headers=self.headers,
            json={
                "model": "test-model",
                "input": [{
                    "role": "user",
                    "content": [{"type": "input_file", "filename": "pages.pdf", "file_data": encoded}],
                }],
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "too_many_pages")
        self.assertIn("1-page limit", response.json()["error"]["message"])

    def test_unknown_v1_api_is_passed_through(self) -> None:
        response = self.client.post(
            "/v1/embeddings?encoding_format=float&tag=one&tag=two",
            headers={**self.headers, "Content-Type": "application/json", "X-Request-ID": "client-1"},
            content=b'{"model":"embedding-model","input":"hello"}',
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json(), {"object": "passthrough"})
        self.assertEqual(response.headers["x-vllm-request-id"], "upstream-1")
        forwarded = self.upstream.passthrough_requests[-1]
        self.assertEqual(forwarded["method"], "POST")
        self.assertEqual(forwarded["url"], "http://vllm.test/v1/embeddings")
        self.assertEqual(forwarded["params"], [("encoding_format", "float"), ("tag", "one"), ("tag", "two")])
        self.assertEqual(forwarded["content"], b'{"model":"embedding-model","input":"hello"}')
        forwarded_headers = httpx.Headers(forwarded["headers"])  # type: ignore[arg-type]
        self.assertEqual(forwarded_headers["x-request-id"], "client-1")
        self.assertEqual(forwarded_headers["authorization"], "Bearer client-key")

    def test_unknown_files_method_is_not_passed_through(self) -> None:
        response = self.client.patch("/v1/files/file_unknown", headers=self.headers, content=b"{}")

        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.json()["error"]["code"], "method_not_allowed")
        self.assertEqual(self.upstream.passthrough_requests, [])

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
    def _pdf_bytes(text: str, page_count: int = 1) -> bytes:
        document = pymupdf.open()
        for _ in range(page_count):
            page = document.new_page()
            page.insert_text((72, 72), text)
        content = document.tobytes()
        document.close()
        return content


if __name__ == "__main__":
    unittest.main()