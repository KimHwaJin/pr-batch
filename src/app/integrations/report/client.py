"""prj_re report client authenticated with short-lived service tokens.

Scheduled runs happen without a signed-in user, so pr-batch signs its own
token (iss=pr-batch) instead of relaying a user's prj_re bearer token.
"""

import time
from typing import Any

import httpx2
import jwt
from pydantic import ValidationError

from app.integrations.report.contracts import ReportJob


class ReportError(Exception):
    """Only a safe machine code is persisted, never remote bodies."""


class ReportClient:
    def __init__(
        self,
        base_url: str,
        secret: str,
        audience: str,
        *,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        self.client = httpx2.Client(
            base_url=base_url.rstrip("/") + "/",
            timeout=10,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )
        self.secret = secret
        self.audience = audience

    def close(self) -> None:
        self.client.close()

    def token(self, owner: str) -> str:
        now = int(time.time())
        claims = {
            "iss": "pr-batch",
            "aud": self.audience,
            "sub": owner,
            "scope": "report:write",
            "iat": now,
            "exp": now + 300,
        }
        return jwt.encode(claims, self.secret, algorithm="HS256")

    def call(
        self,
        method: str,
        path: str,
        owner: str,
        payload: dict[str, Any] | None = None,
    ) -> ReportJob:
        try:
            response = self.client.request(
                method,
                path,
                json=payload,
                headers={"Authorization": f"Bearer {self.token(owner)}"},
            )
        except httpx2.HTTPError as exc:
            raise ReportError("REPORT_UNREACHABLE") from exc
        if not 200 <= response.status_code < 300:
            raise ReportError(f"REPORT_HTTP_{response.status_code}")
        if len(response.content) > 1024 * 1024:
            raise ReportError("REPORT_RESPONSE_LIMIT")
        try:
            return ReportJob.model_validate_json(response.content)
        except (ValidationError, ValueError) as exc:
            raise ReportError("REPORT_INVALID_RESPONSE") from exc

    def request_report(self, owner: str, payload: dict[str, Any]) -> ReportJob:
        return self.call(
            "POST", "internal/v1/execution-reports", owner, payload
        )

    def report(self, owner: str, job_id: str) -> ReportJob:
        return self.call(
            "GET", f"internal/v1/execution-reports/{job_id}", owner
        )
