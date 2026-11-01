"""Batch registration, partial updates, activation and skip history."""

import logging
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
    reset_schedule_failures,
    schedule_of,
    script_version,
)
from app.services.idempotency import Idempotency, MutationResult
from app.services.runtime_catalog import RuntimeCatalog, check_runtime
from app.services.schedule_history import record_skipped

logger = logging.getLogger(__name__)


def batch_view(
    batch: Batch,
    config: BatchConfig,
    version: ScriptVersion,
    now: datetime,
    enable_blocker: ServiceError | None,
) -> BatchView:
    """`now` is the database clock, like every other schedule decision.

    `enable_blocker` is what enabling would be refused with now (see
    BatchService.enable_blocker); it is reported only while disabled.
    """
    schedule = schedule_of(config)
    blocked = None if batch.enabled else enable_blocker
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
        can_enable=not batch.enabled and blocked is None,
        can_disable=batch.enabled,
        enable_blocked_reason=blocked.code if blocked else None,
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
    reset_schedule_failures(batch)


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
            return 201, self.view(
                session, batch, config, version, now
            ).model_dump(mode="json")

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
                return 200, self.view(
                    session,
                    batch,
                    config,
                    config_version(session, config),
                    now,
                ).model_dump(mode="json")
            if "name" in changed and data.name is not None:
                batch.name = data.name
            if changed - {"name"}:
                config = self.next_config(session, owner, batch, config, data)
                if available is not None:
                    check_config_runtime(session, config, available)
            batch.revision += 1
            batch.stamp_updated(Actor.user(owner.user_id), now)
            return 200, self.view(
                session, batch, config, config_version(session, config), now
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
                reset_schedule_failures(batch)
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

    def enable_blocker(
        self, config: BatchConfig, script_type: ScriptType, now: datetime
    ) -> ServiceError | None:
        """The error enabling would be refused with now, else None.

        Enabling raises it and batch views report its code
        (enable_blocked_reason), so the two never disagree. All checks are
        local: settings, the stored config and the database clock.
        """
        if not self.executor_configured:
            return ServiceError(
                503, "EXECUTOR_NOT_CONFIGURED", "Configure Executor first."
            )
        schedule = schedule_of(config)
        if schedule is None:
            return ServiceError(
                409, "SCHEDULE_REQUIRED", "Set a schedule before enabling."
            )
        try:
            check_runtime(
                RuntimeSpec.model_validate(config.runtime),
                script_type,
                self.catalog.available(),
            )
        except ServiceError as error:
            return error
        if ScheduleClock(schedule).find(now) is None:
            return ServiceError(
                422,
                "NO_FUTURE_OCCURRENCE",
                "Choose a schedule with a future occurrence.",
            )
        return None

    def view(
        self,
        session: Session,
        batch: Batch,
        config: BatchConfig,
        version: ScriptVersion,
        now: datetime,
    ) -> BatchView:
        blocker = self.enable_blocker(
            config, script_type_of(session, version), now
        )
        return batch_view(batch, config, version, now, blocker)

    def get_batch(self, owner: Principal, batch_id: UUID) -> BatchView:
        with self.sessions() as session:
            batch = owned_batch(session, owner, batch_id)
            config = current_config(session, batch)
            return self.view(
                session,
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
            select(Batch, BatchConfig, ScriptVersion, Script.type)
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
            .join(Script, Script.id == ScriptVersion.script_id)
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
            items = [
                batch_view(
                    b, c, v, now, self.enable_blocker(c, ScriptType(t), now)
                )
                for b, c, v, t in rows[:limit]
            ]
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
        if enabled:
            self.catalog.available()  # unconfigured: refuse before any lookup

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
                    blocker = self.enable_blocker(
                        config,
                        script_type_of(
                            session, config_version(session, config)
                        ),
                        now,
                    )
                    if blocker is not None:
                        raise blocker
                    schedule = schedule_of(config)
                    assert schedule is not None
                    following = ScheduleClock(schedule).find(now)
                    assert following is not None
                    batch.next_run_at = following
                    reset_schedule_failures(batch)
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
            return 200, self.view(
                session, batch, config, config_version(session, config), now
            ).model_dump(mode="json")

        return self.idempotency.run(owner, operation, key, payload, action)

    def disable_by_owner(
        self,
        owner_id: str,
        tenants: frozenset[str] | None,
        caller: str | None,
    ) -> DisabledCount:
        """Stop a deactivated user's schedules (repeatable) in `tenants`
        (None: every tenant).

        Running executions finish; runs still waiting for a slot are
        skipped, since nothing of them reached the Executor yet. Batches
        already off are left as they are.
        """
        scope = [Batch.owner_id == owner_id]
        if tenants is not None:
            scope.append(Batch.tenant_id.in_(sorted(tenants)))
        result = self.stop_batches(
            scope,
            "USER_DEACTIVATED",
            "OWNER_DEACTIVATED",
            caller,
            mark_disabled=False,
        )
        logger.info(
            "Batches disabled by owner client=%s tenants=%s owner=%s "
            "disabled=%d",
            caller or "header-mode",
            "*" if tenants is None else ",".join(sorted(tenants)),
            owner_id,
            result.disabled,
        )
        return result

    def disable_by_tenant(
        self, tenant: str, caller: str | None
    ) -> DisabledCount:
        """Stop every schedule of a revoked tenant (repeatable).

        Like disable_by_owner, and batches already off are marked too, so
        no batch of the tenant can be run manually until enabled again.
        `disabled` counts batches that were on.
        """
        result = self.stop_batches(
            [Batch.tenant_id == tenant],
            "TENANT_REVOKED",
            "TENANT_REVOKED",
            caller,
            mark_disabled=True,
        )
        logger.info(
            "Batches disabled by tenant client=%s tenant=%s disabled=%d",
            caller or "header-mode",
            tenant,
            result.disabled,
        )
        return result

    def stop_batches(
        self,
        scope: list[Any],
        disabled_reason: str,
        skip_reason: str,
        caller: str | None,
        *,
        mark_disabled: bool,
    ) -> DisabledCount:
        actor = Actor("SYSTEM", caller or "header-mode")
        with self.sessions.begin() as session:
            # Lock order everywhere: batch rows, then run rows.
            batches = list(
                session.scalars(
                    select(Batch)
                    .where(*scope, Batch.deleted_at.is_(None))
                    .order_by(Batch.id)
                    .with_for_update()
                )
            )
            now = database_now(session)
            disabled = 0
            for batch in batches:
                if batch.enabled:
                    config = current_config(session, batch)
                    stop_schedule(
                        session, batch, config, now, skip_reason, actor
                    )
                    batch.enabled = False
                    batch.disabled_at = now
                    disabled += 1
                elif (
                    not mark_disabled
                    or batch.disabled_reason == disabled_reason
                ):
                    continue
                else:
                    # Already off: keep when it went off, change the reason.
                    batch.disabled_at = batch.disabled_at or now
                batch.disabled_reason = disabled_reason
                batch.disabled_by = caller
                batch.revision += 1
                batch.stamp_updated(actor, now)
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
                    run.skip_reason = skip_reason
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
