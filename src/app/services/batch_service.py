"""Batch registration, partial updates, activation and skip history."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import and_, exists, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.exceptions import ServiceError
from app.core.security import Identity, Principal
from app.db.base import Actor
from app.db.session import database_now
from app.domain.run_state import ACTIVE, RunStatus
from app.domain.schedule_clock import EPSILON, ScheduleClock
from app.domain.scripts import ScriptType
from app.models import (
    Batch,
    BatchConfig,
    BatchRun,
    Script,
    ScriptVersion,
    SkippedWindow,
)
from app.schemas.batch import (
    BatchPage,
    BatchView,
    CreateBatch,
    DisabledCount,
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
    batch: Batch,
    config: BatchConfig,
    version: ScriptVersion,
    now: datetime,
) -> BatchView:
    """`now` is the database clock, like every other schedule decision."""
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
        tenant_id=batch.tenant_id,
        project_id=batch.project_id,
        enabled=batch.enabled,
        disabled_reason=batch.disabled_reason,
        disabled_at=batch.disabled_at,
        next_run_at=batch.next_run_at,
        preview_runs=(
            ScheduleClock(schedule).preview(now) if schedule else []
        ),
        config_number=batch.config_number,
        revision=batch.revision,
        **audit_fields(batch),
    )


def merged_policy(current: Policy, patch: dict[str, Any]) -> Policy:
    """Apply a nested partial policy; the result must be valid as a whole."""
    merged = current.model_dump()
    for section, values in patch.items():
        if (
            values.get("mode", merged[section]["mode"])
            != merged[section]["mode"]
        ):
            # A new mode starts from that mode's defaults.
            merged[section] = values
        else:
            merged[section] = merged[section] | values
    try:
        return Policy.model_validate(merged)
    except ValidationError as exc:
        problem = exc.errors()[0]
        field = ".".join(str(part) for part in problem["loc"][:3])
        raise ServiceError(
            422,
            "INVALID_POLICY",
            f"{field}: not valid for the selected mode ({problem['type']}).",
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


def stop_schedule(
    session: Session,
    batch: Batch,
    config: BatchConfig,
    now: datetime,
    reason: str,
    actor: Actor | None = None,
) -> None:
    """Clear the next occurrence, recording it if it was already due."""
    if batch.next_run_at and batch.next_run_at <= now:
        record_skipped(
            session,
            batch,
            config,
            batch.next_run_at,
            now + EPSILON,
            reason,
            now,
            actor or Actor.user(batch.owner_id),
        )
    batch.next_run_at = None


class BatchService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        catalog: RuntimeCatalog,
        executor_configured: bool = True,
    ) -> None:
        self.sessions = sessions
        self.idempotency = Idempotency(sessions)
        self.catalog = catalog
        # Without an Executor the background runtime never starts, so an
        # enabled batch would silently never run. Drafts need no Executor.
        self.executor_configured = executor_configured

    def resolve_version(
        self,
        session: Session,
        owner: Principal,
        script_id: UUID,
        version_number: int | None,
    ) -> ScriptVersion:
        """Pin an explicit version number, or the script's latest one.

        The share lock keeps the script from being deleted under the pin.
        """
        script = owned_script(session, owner, script_id, share=True)
        return script_version(
            session, script.id, version_number or script.latest_version_number
        )

    def create_batch(
        self, identity: Identity, key: str, data: CreateBatch
    ) -> MutationResult:
        owner = identity.owner
        # Header-derived values are part of the request a key replays
        # (the tenant already scopes the key).
        payload = {
            **data.model_dump(mode="json"),
            "project_id": identity.project_id,
        }
        if replayed := self.idempotency.replay(
            owner, "batch:create", key, payload
        ):
            return replayed
        available = self.catalog.available()

        def action(session: Session) -> MutationResult:
            version = self.resolve_version(
                session, owner, data.script_id, data.script_version_number
            )
            check_runtime(
                data.runtime, script_type_of(session, version), available
            )
            actor = Actor.user(owner.user_id)
            now = database_now(session)
            batch = Batch(
                id=uuid4(),
                tenant_id=owner.tenant,
                owner_id=owner.user_id,
                name=data.name,
                project_id=identity.project_id,
                client=identity.caller.client,
            )
            batch.stamp_created(actor, now)
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
            return 201, batch_view(batch, config, version, now).model_dump(
                mode="json"
            )

        return self.idempotency.run(
            owner, "batch:create", key, payload, action
        )

    def update_batch(
        self,
        identity: Identity,
        batch_id: UUID,
        key: str,
        expected: int,
        data: UpdateBatch,
    ) -> MutationResult:
        owner = identity.owner
        fields = data.model_fields_set
        operation = f"batch:update:{batch_id}"
        payload = {
            "expected_revision": expected,
            "data": data.model_dump(mode="json", exclude_unset=True),
        }
        if replayed := self.idempotency.replay(owner, operation, key, payload):
            return replayed
        runtime_fields = {"runtime", "script_id", "script_version_number"}
        available = (
            self.catalog.available() if fields & runtime_fields else None
        )

        def action(session: Session) -> MutationResult:
            batch = owned_batch(session, owner, batch_id, lock=True)
            check_revision(batch.revision, expected)
            config = current_config(session, batch)
            now = database_now(session)
            changed = set(fields)
            if not changed:
                return 200, batch_view(
                    batch, config, config_version(session, config), now
                ).model_dump(mode="json")
            if "name" in changed and data.name is not None:
                batch.name = data.name
            if changed - {"name"}:
                config = self.next_config(session, owner, batch, config, data)
                if available is not None:
                    check_config_runtime(session, config, available)
            batch.revision += 1
            batch.stamp_updated(Actor.user(owner.user_id), now)
            return 200, batch_view(
                batch, config, config_version(session, config), now
            ).model_dump(mode="json")

        return self.idempotency.run(owner, operation, key, payload, action)

    def next_config(
        self,
        session: Session,
        owner: Principal,
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
                        Actor.user(owner.user_id),
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
        config.stamp_created(Actor.user(owner.user_id), now)
        session.add(config)
        session.flush()
        return config

    def get_batch(self, owner: Principal, batch_id: UUID) -> BatchView:
        with self.sessions() as session:
            batch = owned_batch(session, owner, batch_id)
            config = current_config(session, batch)
            return batch_view(
                batch,
                config,
                config_version(session, config),
                database_now(session),
            )

    def list_batches(
        self,
        owner: Principal,
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
            .where(
                Batch.tenant_id == owner.tenant,
                Batch.owner_id == owner.user_id,
                Batch.deleted_at.is_(None),
            )
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
            now = database_now(session)
            items = [batch_view(b, c, v, now) for b, c, v in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            next_cursor = encode_cursor(items[-1].created_at, items[-1].id)
        return BatchPage(items=items, next_cursor=next_cursor)

    def set_enabled(
        self,
        identity: Identity,
        batch_id: UUID,
        key: str,
        expected: int,
        enabled: bool,
    ) -> MutationResult:
        owner = identity.owner
        operation = f"activation:{batch_id}"
        payload = {"enabled": enabled, "expected_revision": expected}
        if replayed := self.idempotency.replay(owner, operation, key, payload):
            return replayed
        available = self.catalog.available() if enabled else None

        def action(session: Session) -> MutationResult:
            batch = owned_batch(session, owner, batch_id, lock=True)
            if enabled and not self.executor_configured:
                raise ServiceError(
                    503, "EXECUTOR_NOT_CONFIGURED", "Configure Executor first."
                )
            check_revision(batch.revision, expected)
            config = current_config(session, batch)
            now = database_now(session)
            if batch.enabled != enabled:
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
                    batch.disabled_reason = None
                    batch.disabled_at = batch.disabled_by = None
                else:
                    stop_schedule(session, batch, config, now, "DISABLED")
                    batch.disabled_reason = "OWNER"
                    batch.disabled_at = now
                    batch.disabled_by = identity.caller.client
                batch.enabled = enabled
                batch.revision += 1
                batch.stamp_updated(Actor.user(owner.user_id), now)
            return 200, batch_view(
                batch, config, config_version(session, config), now
            ).model_dump(mode="json")

        return self.idempotency.run(owner, operation, key, payload, action)

    def disable_by_owner(
        self, owner: Principal, caller: str | None
    ) -> DisabledCount:
        """Stop a deactivated user's schedules in one tenant (repeatable).

        Running executions finish; runs still waiting for a slot are
        skipped, since nothing of them reached the Executor yet.
        """
        actor = Actor("SYSTEM", caller or "header-mode")
        with self.sessions.begin() as session:
            # Lock order everywhere: batch rows, then run rows.
            batches = list(
                session.scalars(
                    select(Batch)
                    .where(
                        Batch.tenant_id == owner.tenant,
                        Batch.owner_id == owner.user_id,
                        Batch.deleted_at.is_(None),
                    )
                    .order_by(Batch.id)
                    .with_for_update()
                )
            )
            now = database_now(session)
            disabled = 0
            for batch in batches:
                if not batch.enabled:
                    continue
                config = current_config(session, batch)
                stop_schedule(
                    session, batch, config, now, "OWNER_DEACTIVATED", actor
                )
                batch.enabled = False
                batch.disabled_reason = "USER_DEACTIVATED"
                batch.disabled_at = now
                batch.disabled_by = caller
                batch.revision += 1
                batch.stamp_updated(actor, now)
                disabled += 1
            if batches:
                waiting = session.scalars(
                    select(BatchRun)
                    .where(
                        BatchRun.batch_id.in_([b.id for b in batches]),
                        BatchRun.status == RunStatus.WAITING,
                    )
                    .with_for_update()
                )
                for run in waiting:
                    run.status = RunStatus.SKIPPED
                    run.skip_reason = "OWNER_DEACTIVATED"
                    run.completed_at = now
                    run.stamp_updated(actor, now)
        return DisabledCount(disabled=disabled)

    def delete_batch(
        self, owner: Principal, batch_id: UUID, key: str, expected: int
    ) -> MutationResult:
        def action(session: Session) -> MutationResult:
            # Admission (manual or scheduled) holds this lock too, so no
            # run can start between the check and the delete.
            batch = owned_batch(session, owner, batch_id, lock=True)
            check_revision(batch.revision, expected)
            active = exists().where(
                BatchRun.batch_id == batch_id, BatchRun.status.in_(ACTIVE)
            )
            if session.scalar(select(active)):
                raise ServiceError(
                    409,
                    "BATCH_HAS_ACTIVE_RUNS",
                    "Wait for or cancel its active runs first.",
                )
            now = database_now(session)
            if batch.enabled:
                config = current_config(session, batch)
                stop_schedule(session, batch, config, now, "DELETED")
                batch.enabled = False
            batch.revision += 1
            batch.stamp_deleted(Actor.user(owner.user_id), now)
            return 204, {}

        return self.idempotency.run(
            owner,
            f"batch:delete:{batch_id}",
            key,
            {"expected_revision": expected},
            action,
        )

    def skipped(
        self,
        owner: Principal,
        batch_id: UUID,
        limit: int,
        cursor: str | None,
    ) -> SkippedPage:
        with self.sessions() as session:
            owned_batch(session, owner, batch_id)
            query = select(SkippedWindow).where(
                SkippedWindow.batch_id == batch_id
            )
            if cursor:
                query = query.where(
                    before_cursor(
                        SkippedWindow.created_at, SkippedWindow.id, cursor
                    )
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
                next_cursor=(
                    encode_cursor(items[-1].created_at, items[-1].id)
                    if len(rows) > limit
                    else None
                ),
            )
