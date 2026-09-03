from __future__ import annotations


class GatewayError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        param: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.param = param

    def body(self) -> dict[str, object]:
        return {
            "error": {
                "message": self.message,
                "type": "invalid_request_error",
                "param": self.param,
                "code": self.code,
            }
        }
