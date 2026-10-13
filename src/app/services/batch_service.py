"""Batch registration, partial updates, activation and skip history."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import and_, select, tuple_
from sqlalchemy.orm import Session, sessionmaker

from app.core.exceptions import ServiceError
from app.db.base import Actor, utcnow
from app.db.session import database_now
from app.domain.schedule_clock import EPSILON, ScheduleClock
from app.domain.scripts import ScriptType
from app.models import Batch, BatchConfig, Script, ScriptVersion, SkippedWindow
from app.schemas.batch import (
    BatchPage,
    BatchView,
    CreateBatch,
    RuntimeSpec,
    SkippedPage,
    SkippedView,
    UpdateBatch,
)
from app.schemas.common import audit_fields
from app.schemas.schedule import Interval, Policy, Schedule
from app.services.common import (
    before_cursor,
    check_revision,
    config_version,
    current_config,
    encode_cursor,
    owned_batch,
    owned_script,
    policy_of,
    schedule_of,
    script_version,
)
from app.services.idempotency import Idempotency, MutationResult
from app.services.runtime_catalog import RuntimeCatalog, check_runtime
from app.services.schedule_history import record_skipped


def batch_view(
    batch: Batch, config: BatchConfig, version: ScriptVersion
) -> BatchView:
    schedule = schedule_of(config)
    return BatchView(
        id=batch.id,
        name=batch.name,
        script_id=config.script_id,
        script_version_id=config.script_version_id,
        script_version_number=version.version_number,
        runtime=RuntimeSpec.model_validate(config.runtime),
        schedule=schedule,
        policy=policy_of(config),
        enabled=batch.enabled,
        next_run_at=batch.next_run_at,
        preview_runs=(
            ScheduleClock(schedule).preview(utcnow()) if schedule else []
        ),
        config_number=batch.config_number,
        revision=batch.revision,
        **audit_fields(batch),
    )


def merged_policy(current: Policy, patch: dict[str, Any]) -> Policy:
    """Apply a nested partial policy; the result must be valid as a whole."""
    merged = current.model_dump()
    for section, values in patch.items():
        merged[section] = merged[section] | values
    try:
        return Policy.model_validate(merged)
    except ValidationError as exc:
        raise ServiceError(
            422, "INVALID_POLICY", "Only PARALLEL allows max_concurrent > 1."
        ) from exc


def dump(schedule: Schedule | None) -> dict[str, Any] | None:
    return schedule.model_dump(mode="json") if schedule else None


def anchored(schedule: Schedule | None, now: datetime) -> Schedule | None:
    """Fill an omitted INTERVAL anchor: start_at, else when it is saved."""
    if schedule is None:
        return None
    definition = schedule.definition
    if isinstance(definition, Interval) and definition.anchor is None:
        fixed = definition.model_copy(
            update={"anchor": schedule.start_at or now}
        )
        return schedule.model_copy(update={"definition": fixed})
    return schedule


def script_type_of(session: Session, version: ScriptVersion) -> ScriptType:
    script = session.get(Script, version.script_id)
    assert script is not None
    return ScriptType(script.type)


def check_config_runtime(
    session: Session, config: BatchConfig, available: list[RuntimeSpec]
) -> None:
    version = config_version(session, config)
    check_runtime(
        RuntimeSpec.model_validate(config.runtime),
        script_type_of(session, version),
        available,
    )


class BatchService:
    def __init__(
        self, sessions: sessionmaker[Session], catalog: RuntimeCatalog
    ) -> None:
        self.sessions = sessions
        self.idempotency = Idempotency(sessions)
        # Read outside transactions: the lookup may call the Executor.
        self.catalog = catalog

    def resolve_version(
        self,
        session: Session,
        owner: str,
        script_id: UUID,
        version_number: int | None,
    ) -> ScriptVersion:
        """Pin an explicit version number, or the script's latest one."""
        script = owned_script(session, owner, script_id)
        return script_version(
            session, script.id, version_number or script.latest_version_number
        )

    def create_batch(
        self, owner: str, key: str, data: CreateBatch
    ) -> MutationResult:
        available = self.catalog.available()

        def action(session: Session) -> MutationResult:
            version = self.resolve_version(
                session, owner, data.script_id, data.script_version_number
            )
            check_runtime(
                data.runtime, script_type_of(session, version), available
            )
            actor = Actor.user(owner)
            batch = Batch(id=uuid4(), owner_id=owner, name=data.name)
            batch.stamp_created(actor)
            session.add(batch)
            session.flush()
            config = BatchConfig(
                batch_id=batch.id,
                number=1,
                script_id=version.script_id,
                script_version_id=version.id,
                runtime=data.runtime.model_dump(mode="json"),
                schedule=dump(anchored(data.schedule, batch.created_at)),
                policy=data.policy.model_dump(mode="json"),
            )
            config.stamp_created(actor, batch.created_at)
            session.add(config)
            session.flush()
            return 201, batch_view(batch, config, version).model_dump(
                mode="json"
            )

        return self.idempotency.run(
            owner,
            "batch:create",
            key,
            data.model_dump(mode="json"),
            action,
        )

    def update_batch(
        self,
        owner: str,
        batch_id: UUID,
        key: str,
        expected: int,
        data: UpdateBatch,
    ) -> MutationResult:
        fields = data.model_fields_set
        runtime_fields = {"runtime", "script_id", "script_version_number"}
        available = (
            self.catalog.available() if fields & runtime_fields else None
        )

        def action(session: Session) -> MutationResult:
            batch = owned_batch(session, owner, batch_id, lock=True)
            check_revision(batch.revision, expected)
            config = current_config(session, batch)
            now = database_now(session)
            if "name" in fields and data.name is not None:
                batch.name = data.name
            if fields - {"name"}:
                config = self.next_config(session, owner, batch, config, data)
                if available is not None:
                    check_config_runtime(session, config, available)
            batch.revision += 1
            batch.stamp_updated(Actor.user(owner), now)
            return 200, batch_view(
                batch, config, config_version(session, config)
            ).model_dump(mode="json")

        return self.idempotency.run(
            owner,
            f"batch:update:{batch_id}",
            key,
            {
                "expected_revision": expected,
                "data": data.model_dump(mode="json", exclude_unset=True),
            },
            action,
        )

    def next_config(
        self,
        session: Session,
        owner: str,
        batch: Batch,
        current: BatchConfig,
        data: UpdateBatch,
    ) -> BatchConfig:
        fields = data.model_fields_set
        script_id = data.script_id or current.script_id
        if "script_id" in fields or "script_version_number" in fields:
            version = self.resolve_version(
                session, owner, script_id, data.script_version_number
            )
        else:
            version = config_version(session, current)
        now = database_now(session)
        schedule = (
            anchored(data.schedule, now)
            if "schedule" in fields
            else schedule_of(current)
        )
        policy = policy_of(current)
        if data.policy is not None:
            policy = merged_policy(
                policy, data.policy.model_dump(exclude_none=True)
            )
        if batch.enabled:
            if schedule is None:
                raise ServiceError(
                    409, "SCHEDULE_REQUIRED", "Disable the batch first."
                )
            if "schedule" in fields:
                following = ScheduleClock(schedule).find(now)
                if following is None:
                    raise ServiceError(
                        422,
                        "NO_FUTURE_OCCURRENCE",
                        "Choose a schedule with a future occurrence.",
                    )
                if batch.next_run_at and batch.next_run_at <= now:
                    record_skipped(
                        session,
                        batch,
                        current,
                        batch.next_run_at,
                        now + EPSILON,
                        "SCHEDULE_REPLACED",
                        now,
                        Actor.user(owner),
                    )
                batch.next_run_at = following
        batch.config_number += 1
        config = BatchConfig(
            batch_id=batch.id,
            number=batch.config_number,
            script_id=version.script_id,
            script_version_id=version.id,
            runtime=(
                data.runtime.model_dump(mode="json")
                if data.runtime
                else current.runtime
            ),
            schedule=dump(schedule),
            policy=policy.model_dump(mode="json"),
        )
        config.stamp_created(Actor.user(owner), now)
        session.add(config)
        session.flush()
        return config

    def get_batch(self, owner: str, batch_id: UUID) -> BatchView:
        with self.sessions() as session:
            batch = owned_batch(session, owner, batch_id)
            config = current_config(session, batch)
            return batch_view(batch, config, config_version(session, config))

    def list_batches(
        self,
        owner: str,
        limit: int,
        cursor: str | None,
        script_id: UUID | None,
    ) -> BatchPage:
        query = (
            select(Batch, BatchConfig, ScriptVersion)
            .join(
                BatchConfig,
                and_(
                    BatchConfig.batch_id == Batch.id,
                    BatchConfig.number == Batch.config_number,
                ),
            )
            .join(
                ScriptVersion,
                ScriptVersion.id == BatchConfig.script_version_id,
            )
            .where(Batch.owner_id == owner)
        )
        if script_id is not None:
            query = query.where(BatchConfig.script_id == script_id)
        if cursor:
            query = query.where(
                before_cursor(Batch.created_at, Batch.id, cursor)
            )
        query = query.order_by(Batch.created_at.desc(), Batch.id.desc())
        with self.sessions() as session:
            rows = session.execute(query.limit(limit + 1)).all()
            items = [batch_view(b, c, v) for b, c, v in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            next_cursor = encode_cursor(items[-1].created_at, items[-1].id)
        return BatchPage(items=items, next_cursor=next_cursor)

    def set_enabled(
        self,
        owner: str,
        batch_id: UUID,
        key: str,
        expected: int,
        enabled: bool,
    ) -> MutationResult:
        available = self.catalog.available() if enabled else None

        def action(session: Session) -> MutationResult:
            batch = owned_batch(session, owner, batch_id, lock=True)
            check_revision(batch.revision, expected)
            config = current_config(session, batch)
            if batch.enabled != enabled:
                now = database_now(session)
                if enabled:
                    schedule = schedule_of(config)
                    if schedule is None:
                        raise ServiceError(
                            409,
                            "SCHEDULE_REQUIRED",
                            "Set a schedule before enabling.",
                        )
                    assert available is not None
                    check_config_runtime(session, config, available)
                    following = ScheduleClock(schedule).find(now)
                    if following is None:
                        raise ServiceError(
                            422,
                            "NO_FUTURE_OCCURRENCE",
                            "Choose a schedule with a future occurrence.",
                        )
                    batch.next_run_at = following
                else:
                    if batch.next_run_at and batch.next_run_at <= now:
                        record_skipped(
                            session,
                            batch,
                            config,
                            batch.next_run_at,
                            now + EPSILON,
                            "DISABLED",
                            now,
                            Actor.user(owner),
                        )
                    batch.next_run_at = None
                batch.enabled = enabled
                batch.revision += 1
                batch.stamp_updated(Actor.user(owner), now)
            return 200, batch_view(
                batch, config, config_version(session, config)
            ).model_dump(mode="json")

        return self.idempotency.run(
            owner,
            f"activation:{batch_id}",
            key,
            {"enabled": enabled, "expected_revision": expected},
            action,
        )

    def skipped(
        self,
        owner: str,
        batch_id: UUID,
        limit: int,
        cursor: UUID | None,
    ) -> SkippedPage:
        with self.sessions() as session:
            owned_batch(session, owner, batch_id)
            query = select(SkippedWindow).where(
                SkippedWindow.batch_id == batch_id
            )
            if cursor:
                last = session.scalar(query.where(SkippedWindow.id == cursor))
                if last is None:
                    raise ServiceError(
                        422, "INVALID_CURSOR", "Invalid cursor."
                    )
                query = query.where(
                    tuple_(SkippedWindow.created_at, SkippedWindow.id)
                    < tuple_(last.created_at, last.id)
                )
            rows = list(
                session.scalars(
                    query.order_by(
                        SkippedWindow.created_at.desc(),
                        SkippedWindow.id.desc(),
                    ).limit(limit + 1)
                )
            )
            items = [SkippedView.model_validate(row) for row in rows[:limit]]
            return SkippedPage(
                items=items,
                next_cursor=(items[-1].id if len(rows) > limit else None),
            )
