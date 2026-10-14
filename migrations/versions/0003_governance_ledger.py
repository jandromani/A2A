"""Add agent identity lifecycle, usage ledger and tamper-evident audit chain.

Revision ID: 0003_governance_ledger
Revises: 0002_memory_sources
"""

import json
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy import insert, select, text

from app import models  # noqa: F401
from app.audit import audit_hash
from app.database import Base
from app.models import AuditChainState, ModelUsageLedger

revision = "0003_governance_ledger"
down_revision = "0002_memory_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    Base.metadata.create_all(bind=connection)

    inspector = sa.inspect(connection)
    outbox_columns = {column["name"] for column in inspector.get_columns("event_outbox")}
    if "external_id" not in outbox_columns:
        with op.batch_alter_table("event_outbox") as batch:
            batch.add_column(sa.Column("external_id", sa.String(length=300), nullable=True))
    if not _has_unique_constraint(connection, "event_outbox", "external_id"):
        with op.batch_alter_table("event_outbox") as batch:
            batch.create_unique_constraint("uq_event_outbox_external_id", ["external_id"])

    audit_columns = {column["name"] for column in inspector.get_columns("audit_events")}
    missing_audit_columns: set[str] = set()
    sequence_existed_before = "sequence" in audit_columns
    for name, column in (
        ("sequence", sa.Column("sequence", sa.Integer(), nullable=True)),
        ("previous_hash", sa.Column("previous_hash", sa.String(length=64), nullable=True)),
        ("entry_hash", sa.Column("entry_hash", sa.String(length=64), nullable=True)),
    ):
        if name not in audit_columns:
            missing_audit_columns.add(name)
            with op.batch_alter_table("audit_events") as batch:
                batch.add_column(column)

    raw_rows = (
        connection.execute(
            text(
                "SELECT id, actor, action, resource_type, resource_id, decision, details_json, "
                "sequence, previous_hash, entry_hash, created_at "
                "FROM audit_events ORDER BY created_at, id"
            )
        )
        .mappings()
        .all()
    )
    previous_hash = ""
    sequence = 0
    existing_chain_valid = not missing_audit_columns
    if existing_chain_valid:
        ordered_rows = sorted(raw_rows, key=lambda row: row["sequence"] or 0)
        for expected_sequence, raw in enumerate(ordered_rows, start=1):
            details = _json_value(raw["details_json"])
            created_at = _datetime_value(raw["created_at"])
            body = {
                "sequence": raw["sequence"],
                "id": raw["id"],
                "actor": raw["actor"],
                "action": raw["action"],
                "resource_type": raw["resource_type"],
                "resource_id": raw["resource_id"],
                "decision": raw["decision"],
                "details_json": details,
                "created_at": created_at,
            }
            if (
                raw["sequence"] != expected_sequence
                or raw["previous_hash"] != previous_hash
                or raw["entry_hash"] != audit_hash(body, previous_hash)
            ):
                existing_chain_valid = False
                break
            sequence = expected_sequence
            previous_hash = raw["entry_hash"]

    if raw_rows and not existing_chain_valid:
        if (
            sequence_existed_before
            and _has_unique_constraint(connection, "audit_events", "sequence")
            and len(raw_rows) > 1
        ):
            raise RuntimeError(
                "Existing audit sequence/hash fields are invalid; repair the chain "
                "before migration."
            )
        previous_hash = ""
        sequence = 0
        for raw in raw_rows:
            sequence += 1
            details = _json_value(raw["details_json"])
            created_at = _datetime_value(raw["created_at"])
            body = {
                "sequence": sequence,
                "id": raw["id"],
                "actor": raw["actor"],
                "action": raw["action"],
                "resource_type": raw["resource_type"],
                "resource_id": raw["resource_id"],
                "decision": raw["decision"],
                "details_json": details,
                "created_at": created_at,
            }
            current_hash = audit_hash(body, previous_hash)
            connection.execute(
                text(
                    "UPDATE audit_events SET sequence=:sequence, previous_hash=:previous_hash, "
                    "entry_hash=:entry_hash WHERE id=:id"
                ),
                {
                    "id": raw["id"],
                    "sequence": sequence,
                    "previous_hash": previous_hash,
                    "entry_hash": current_hash,
                },
            )
            previous_hash = current_hash

    if not _has_unique_constraint(connection, "audit_events", "sequence"):
        with op.batch_alter_table("audit_events") as batch:
            for name in missing_audit_columns:
                if name == "sequence":
                    batch.alter_column(name, existing_type=sa.Integer(), nullable=False)
                elif name in {"previous_hash", "entry_hash"}:
                    batch.alter_column(name, existing_type=sa.String(length=64), nullable=False)
            batch.create_unique_constraint("uq_audit_events_sequence", ["sequence"])

    chain_state = connection.execute(
        select(
            AuditChainState.__table__.c.last_sequence, AuditChainState.__table__.c.head_hash
        ).where(AuditChainState.__table__.c.id == "global")
    ).first()
    if chain_state and existing_chain_valid and raw_rows:
        if chain_state.last_sequence != sequence or chain_state.head_hash != previous_hash:
            raise RuntimeError("Existing audit chain head does not match its entries.")
    elif chain_state:
        connection.execute(
            AuditChainState.__table__.update()
            .where(AuditChainState.__table__.c.id == "global")
            .values(
                last_sequence=sequence,
                head_hash=previous_hash,
                updated_at=datetime.now(UTC),
            )
        )
    else:
        connection.execute(
            insert(AuditChainState.__table__).values(
                id="global",
                last_sequence=sequence,
                head_hash=previous_hash,
                updated_at=datetime.now(UTC),
            )
        )

    # Backfill the old task result snapshot once so the ledger spans existing use.
    task_rows = connection.execute(
        text("SELECT id, actor, result_json, created_at FROM tasks ORDER BY created_at, id")
    ).mappings()
    for task in task_rows:
        result = task["result_json"] or {}
        if isinstance(result, str):
            result = json.loads(result)
        if not isinstance(result, dict):
            continue
        created_at = task["created_at"]
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        legacy_calls = []
        primary_usage = result.get("model_usage")
        if isinstance(primary_usage, dict):
            legacy_calls.append(("primary", primary_usage))
        steps = result.get("steps")
        if isinstance(steps, dict):
            legacy_calls.extend(
                (str(step_id), step_result["model_usage"])
                for step_id, step_result in steps.items()
                if isinstance(step_result, dict)
                and isinstance(step_result.get("model_usage"), dict)
            )
        for call_id, usage in legacy_calls:
            if usage.get("attempted") is not True:
                continue
            call_key = f"{task['id']}:{call_id}"
            if connection.scalar(
                select(ModelUsageLedger.__table__.c.id).where(
                    ModelUsageLedger.__table__.c.call_key == call_key
                )
            ):
                continue
            connection.execute(
                insert(ModelUsageLedger.__table__).values(
                    call_key=call_key,
                    task_id=task["id"],
                    actor=task["actor"],
                    provider=str(usage.get("provider") or "unknown")[:80],
                    model=str(usage.get("model") or "unknown")[:180],
                    attempted=True,
                    succeeded=usage.get("succeeded") is True,
                    input_tokens=usage.get("input_tokens"),
                    output_tokens=usage.get("output_tokens"),
                    estimated_cost_usd=usage.get("estimated_cost_usd"),
                    error_code=usage.get("fallback_reason"),
                    created_at=created_at,
                )
            )

    if connection.dialect.name == "sqlite":
        connection.execute(
            text(
                "CREATE TRIGGER IF NOT EXISTS audit_events_no_update BEFORE UPDATE ON audit_events "
                "BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END"
            )
        )
        connection.execute(
            text(
                "CREATE TRIGGER IF NOT EXISTS audit_events_no_delete BEFORE DELETE ON audit_events "
                "BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END"
            )
        )
        connection.execute(
            text(
                "CREATE TRIGGER IF NOT EXISTS model_usage_no_update BEFORE UPDATE "
                "ON model_usage_ledger "
                "BEGIN SELECT RAISE(ABORT, 'model_usage_ledger is append-only'); END"
            )
        )
        connection.execute(
            text(
                "CREATE TRIGGER IF NOT EXISTS model_usage_no_delete BEFORE DELETE "
                "ON model_usage_ledger "
                "BEGIN SELECT RAISE(ABORT, 'model_usage_ledger is append-only'); END"
            )
        )
    elif connection.dialect.name == "postgresql":
        connection.execute(
            text(
                "CREATE OR REPLACE FUNCTION reject_audit_mutation() RETURNS trigger AS $$ "
                "BEGIN RAISE EXCEPTION 'audit_events is append-only'; END; $$ LANGUAGE plpgsql"
            )
        )
        connection.execute(text("DROP TRIGGER IF EXISTS audit_events_immutable ON audit_events"))
        connection.execute(
            text(
                "CREATE TRIGGER audit_events_immutable BEFORE UPDATE OR DELETE ON audit_events "
                "FOR EACH ROW EXECUTE FUNCTION reject_audit_mutation()"
            )
        )
        connection.execute(
            text("DROP TRIGGER IF EXISTS model_usage_immutable ON model_usage_ledger")
        )
        connection.execute(
            text(
                "CREATE TRIGGER model_usage_immutable BEFORE UPDATE OR DELETE "
                "ON model_usage_ledger "
                "FOR EACH ROW EXECUTE FUNCTION reject_audit_mutation()"
            )
        )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        connection.execute(text("DROP TRIGGER IF EXISTS audit_events_no_update"))
        connection.execute(text("DROP TRIGGER IF EXISTS audit_events_no_delete"))
        connection.execute(text("DROP TRIGGER IF EXISTS model_usage_no_update"))
        connection.execute(text("DROP TRIGGER IF EXISTS model_usage_no_delete"))
    elif connection.dialect.name == "postgresql":
        connection.execute(text("DROP TRIGGER IF EXISTS audit_events_immutable ON audit_events"))
        connection.execute(
            text("DROP TRIGGER IF EXISTS model_usage_immutable ON model_usage_ledger")
        )
        connection.execute(text("DROP FUNCTION IF EXISTS reject_audit_mutation()"))
    for table in ("model_usage_ledger", "agent_identities", "audit_chain_state"):
        Base.metadata.tables[table].drop(bind=connection, checkfirst=True)


def _has_unique_constraint(connection, table: str, column: str) -> bool:
    inspector = sa.inspect(connection)
    if any(
        column in constraint.get("column_names", [])
        for constraint in inspector.get_unique_constraints(table)
    ):
        return True
    return any(
        index.get("unique") and column in index.get("column_names", [])
        for index in inspector.get_indexes(table)
    )


def _json_value(value):
    if not value:
        return {}
    return json.loads(value) if isinstance(value, str) else value


def _datetime_value(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value
