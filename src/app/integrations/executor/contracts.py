"""Metadata-only Executor response contracts."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field

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


class CommandAck(BaseModel):
    execution_id: UUID
    state: State
