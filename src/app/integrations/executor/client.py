"""Bounded Executor HTTP client."""

import json
import re
import time
from typing import Any
from uuid import UUID

import httpx2
from pydantic import BaseModel, ValidationError

from app.integrations.executor.contracts import (
    CommandAck,
    Detail,
    Receipt,
    Result,
)

ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,45}$")
ERROR_BODY_MAX_BYTES = 16 * 1024
# The Executor checked the request and created nothing (validation runs
# before the idempotency lookup, POST /executions spec section 4-5).
SUBMIT_REJECTIONS = {
    422: None,  # any code
    409: {"INVALID_STATE_TRANSITION"},
}


class ExecutorError(Exception):
    """Only a safe machine code is persisted, never remote response bodies.

    ``status`` and ``code`` (the Executor's ``error.code``, when it sent a
    well-formed one) let callers tell rejections from outages.
    """

    def __init__(
        self, message: str, status: int | None = None, code: str | None = None
    ) -> None:
        super().__init__(message)
        self.status, self.code = status, code

    @property
    def submit_rejected(self) -> bool:
        """A definite refusal of a submission, not a lost or failed call."""
        if self.status not in SUBMIT_REJECTIONS or self.code is None:
            return False
        codes = SUBMIT_REJECTIONS[self.status]
        return codes is None or self.code in codes


def error_code(body: bytes) -> str | None:
    try:
        code = json.loads(body)["error"]["code"]
    except (ValueError, KeyError, TypeError):
        return None
    return code if isinstance(code, str) and ERROR_CODE.match(code) else None


def require(condition: bool) -> None:
    if not condition:
        raise ExecutorError("EXECUTOR_CONTRACT_MISMATCH")


class ExecutorClient:
    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        transport: httpx2.BaseTransport | None = None,
        response_max_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        self.client = httpx2.Client(
            base_url=base_url.rstrip("/") + "/",
            timeout=10,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
            headers={"X-Api-Key": api_key} if api_key else None,
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
        params: dict[str, str | int] | None = None,
        headers: dict[str, str] | None = None,
    ) -> T:
        try:
            started = time.monotonic()
            with self.client.stream(
                method, path, json=payload, params=params, headers=headers
            ) as response:
                if not 200 <= response.status_code < 300:
                    status = response.status_code
                    code = error_code(self.read_error(response, started))
                    raise ExecutorError(
                        f"EXECUTOR_HTTP_{status}"
                        + (f":{code}" if code else ""),
                        status,
                        code,
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

    @staticmethod
    def read_error(response: httpx2.Response, started: float) -> bytes:
        body = bytearray()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if (
                len(body) > ERROR_BODY_MAX_BYTES
                or time.monotonic() - started > 30
            ):
                return b""
        return bytes(body)

    # `identity` is the run's identity headers (identity_headers): the
    # Executor scopes every execution call to that user and tenant.

    def submit(
        self, payload: dict[str, Any], identity: dict[str, str]
    ) -> Receipt:
        return self.request(
            "POST", "api/v1/executions", Receipt, payload, headers=identity
        )

    def detail(self, execution_id: UUID, identity: dict[str, str]) -> Detail:
        return self.request(
            "GET",
            f"api/v1/executions/{execution_id}",
            Detail,
            headers=identity,
        )

    def result(self, execution_id: UUID, identity: dict[str, str]) -> Result:
        return self.request(
            "GET",
            f"api/v1/executions/{execution_id}/result",
            Result,
            headers=identity,
        )

    def cancel(
        self,
        execution_id: UUID,
        key: str,
        actor: dict[str, str],
        reason: str,
        identity: dict[str, str],
    ) -> bool:
        """Request cancellation; False when Executor reports it too late.

        The actor is always sent: without it the Executor keeps the
        submitter in updated_by and the canceller is lost.
        """
        try:
            self.request(
                "POST",
                f"api/v1/executions/{execution_id}/cancel",
                CommandAck,
                {"idempotency_key": key, "reason": reason, "actor": actor},
                headers=identity,
            )
        except ExecutorError as exc:
            # Only a terminal execution; an idempotency conflict is a bug.
            if exc.status == 409 and exc.code == "INVALID_STATE_TRANSITION":
                return False
            raise
        return True
