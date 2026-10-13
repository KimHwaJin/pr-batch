"""Proposed prj_re report API contract (asynchronous job)."""

from typing import Literal

from pydantic import BaseModel, Field


class ReportArtifact(BaseModel):
    type: str = Field(max_length=32)
    artifact_id: str = Field(max_length=128)


class ReportJob(BaseModel):
    report_job_id: str = Field(min_length=1, max_length=128)
    status: Literal["PENDING", "RUNNING", "SUCCEEDED", "FAILED"]
    artifacts: list[ReportArtifact] = Field(default_factory=list)
    error_code: str | None = Field(default=None, max_length=64)
