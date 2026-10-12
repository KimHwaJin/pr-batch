"""Bounded Executor HTTP client and metadata-only response contracts."""

import time
from typing import Any, Literal
from uuid import UUID

import httpx2
from pydantic import AwareDatetime, BaseModel, Field, ValidationError

ExecutionStatus = Literal[
    "QUEUED",
    "DISPATCHED",
    "RUNNING",
    "WAITING_FOR_OPERATION",
    "FINALIZING",
    "CANCEL_REQUESTED",
    "CANCELLED",
    "SUCCEEDED",
    "FAILED",
]


class State(BaseModel):
    status: ExecutionStatus
    version: int = Field(ge=0)


class StepReceipt(BaseModel):
    sequence: int = Field(ge=0)
    step_id: UUID


class OperationReceipt(BaseModel):
    operation_id: UUID
    steps: list[StepReceipt]


class Receipt(BaseModel):
    execution_id: UUID
    operation: OperationReceipt
    state: State


class Lifecycle(BaseModel):
    started_at: AwareDatetime | None
    finished_at: AwareDatetime | None


class Context(BaseModel):
    user_id: str
    task_id: str


class Failure(BaseModel):
    type: str = Field(max_length=128)


class Retry(BaseModel):
    strategy: str
    count: int = Field(ge=0)
    from_sequence: int | None
    retained_runtime_session_until: AwareDatetime | None


class Projection(BaseModel):
    status: Literal["NOT_STARTED", "PENDING", "SUCCEEDED", "FAILED"]


class Workspace(BaseModel):
    notebook_projection: Projection


class Detail(BaseModel):
    execution_id: UUID
    state: State
    context: Context
    lifecycle: Lifecycle
    failure: Failure | None
    retry: Retry
    workspace: Workspace


class ResultRef(BaseModel):
    storage: Literal["SHARED_PV"]
    execution_id: UUID
    step_id: UUID
    attempt_id: UUID
    fencing_token: int = Field(ge=0)
    relative_path: str = Field(max_length=4096)
    checksum_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    size_bytes: int = Field(ge=0)
    complete: bool
    representation_count: int = Field(ge=0)
    total_size_bytes: int = Field(ge=0)


class StepResult(BaseModel):
    status: str = Field(max_length=32)
    result_ref: ResultRef | None


class ResultStep(StepReceipt):
    result: StepResult


class ResultOperation(BaseModel):
    operation_id: UUID
    steps: list[ResultStep]


class ResultExecution(BaseModel):
    execution_id: UUID
    state: State


class Producer(BaseModel):
    execution_id: UUID


class Artifact(BaseModel):
    artifact_id: UUID
    type: str = Field(max_length=32)
    status: Literal["AVAILABLE", "INCOMPLETE", "DELETED"]
    produced_by: Producer


class Attempt(BaseModel):
    attempt_id: UUID
    execution_id: UUID
    attempt_number: int
    lifecycle: Lifecycle


class Result(BaseModel):
    execution: ResultExecution
    operations: list[ResultOperation]
    attempts: list[Attempt]
    artifacts: list[Artifact]


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
