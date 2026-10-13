"""Bounded Executor HTTP client."""

import time
from typing import Any
from uuid import UUID

import httpx2
from pydantic import BaseModel, ValidationError

from app.integrations.executor.contracts import Detail, Receipt, Result


class ExecutorError(Exception):
    """Only a safe machine code is persisted, never remote response bodies."""


def require(condition: bool) -> None:
    if not condition:
        raise ExecutorError("EXECUTOR_CONTRACT_MISMATCH")


class ExecutorClient:
    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx2.BaseTransport | None = None,
        response_max_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        self.client = httpx2.Client(
            base_url=base_url.rstrip("/") + "/",
            timeout=10,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )
        self.response_max_bytes = response_max_bytes

    def close(self) -> None:
        self.client.close()

    def request[T: BaseModel](
        self,
        method: str,
        path: str,
        model: type[T],
        payload: dict[str, Any] | None = None,
    ) -> T:
        try:
            started = time.monotonic()
            with self.client.stream(method, path, json=payload) as response:
                if not 200 <= response.status_code < 300:
                    raise ExecutorError(
                        f"EXECUTOR_HTTP_{response.status_code}"
                    )
                body = bytearray()
                for chunk in response.iter_bytes():
                    if (
                        len(body) + len(chunk) > self.response_max_bytes
                        or time.monotonic() - started > 30
                    ):
                        raise ExecutorError("EXECUTOR_RESPONSE_LIMIT")
                    body.extend(chunk)
            return model.model_validate_json(bytes(body))
        except httpx2.HTTPError as exc:
            raise ExecutorError("EXECUTOR_UNREACHABLE") from exc
        except (ValidationError, ValueError) as exc:
            raise ExecutorError("EXECUTOR_INVALID_RESPONSE") from exc

    def submit(self, payload: dict[str, Any]) -> Receipt:
        return self.request("POST", "api/v1/executions", Receipt, payload)

    def detail(self, execution_id: UUID) -> Detail:
        return self.request("GET", f"api/v1/executions/{execution_id}", Detail)

    def result(self, execution_id: UUID) -> Result:
        return self.request(
            "GET", f"api/v1/executions/{execution_id}/result", Result
        )
