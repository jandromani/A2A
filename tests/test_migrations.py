import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import sessionmaker

from app.audit import verify_audit_chain
from app.models import AuditRecord, ModelUsageLedger


def test_legacy_sqlite_upgrade_adds_governance_columns_and_backfills(tmp_path):
    database = tmp_path / "legacy.db"
    database_url = f"sqlite:///{database}"
    env = {**os.environ, "DATABASE_URL": database_url}
    root = Path(__file__).resolve().parents[1]
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "0002_memory_sources"],
        check=True,
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )

    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute(
        "INSERT INTO tasks (id, scenario, title, request_text, status, actor, context_id, "
        "payload_json, result_json, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "legacy-task",
            "knowledge",
            "Legacy task",
            "Historic request",
            "completed",
            "operator-1",
            None,
            "{}",
            json.dumps(
                {
                    "model_usage": {
                        "attempted": True,
                        "succeeded": True,
                        "provider": "legacy-provider",
                        "model": "legacy-model",
                        "input_tokens": 11,
                        "output_tokens": 7,
                    },
                    "steps": {
                        "draft": {
                            "model_usage": {
                                "attempted": True,
                                "succeeded": True,
                                "provider": "legacy-provider",
                                "model": "legacy-model",
                                "input_tokens": 3,
                                "output_tokens": 2,
                            }
                        }
                    },
                }
            ),
            "2024-03-04T12:00:00+00:00",
            "2024-03-04T12:00:00+00:00",
        ),
    )
    # Recreate the prior table shapes: earlier migration files used live ORM metadata,
    # so this models databases created by releases before the governance migration.
    connection.execute("ALTER TABLE audit_events RENAME TO audit_events_new")
    connection.execute(
        "CREATE TABLE audit_events (id VARCHAR(32) PRIMARY KEY, actor VARCHAR(200) NOT NULL, "
        "action VARCHAR(120) NOT NULL, resource_type VARCHAR(80) NOT NULL, "
        "resource_id VARCHAR(120), decision VARCHAR(40) NOT NULL, details_json JSON NOT NULL, "
        "created_at DATETIME NOT NULL)"
    )
    connection.execute(
        "INSERT INTO audit_events (id, actor, action, resource_type, resource_id, decision, "
        "details_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "legacy-audit",
            "operator-1",
            "legacy.action",
            "task",
            "legacy-task",
            "allowed",
            "{}",
            "2024-03-04T12:00:00+00:00",
        ),
    )
    connection.execute("DROP TABLE audit_events_new")
    connection.execute("DELETE FROM audit_chain_state")
    connection.execute("ALTER TABLE event_outbox RENAME TO event_outbox_new")
    connection.execute(
        "CREATE TABLE event_outbox (id VARCHAR(32) PRIMARY KEY, "
        "task_event_id VARCHAR(32) NOT NULL, "
        "topic VARCHAR(120) NOT NULL, aggregate_id VARCHAR(120), payload_json JSON NOT NULL, "
        "status VARCHAR(24) NOT NULL, attempts INTEGER NOT NULL, "
        "next_attempt_at DATETIME NOT NULL, "
        "last_error TEXT, created_at DATETIME NOT NULL, published_at DATETIME, "
        "processed_at DATETIME, "
        "FOREIGN KEY(task_event_id) REFERENCES task_events(id) ON DELETE CASCADE)"
    )
    connection.execute("DROP TABLE event_outbox_new")
    connection.commit()
    connection.close()

    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=True,
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )

    engine = create_engine(database_url)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        assert verify_audit_chain(session)["valid"] is True
        audit = session.scalar(select(AuditRecord).where(AuditRecord.id == "legacy-audit"))
        assert audit.sequence == 1
        assert audit.entry_hash
        calls = session.scalars(select(ModelUsageLedger).order_by(ModelUsageLedger.call_key)).all()
        assert [call.call_key for call in calls] == ["legacy-task:draft", "legacy-task:primary"]
        assert all(call.created_at.year == 2024 for call in calls)
    schema = inspect(engine)
    assert "external_id" in {column["name"] for column in schema.get_columns("event_outbox")}
    assert any(
        "external_id" in constraint.get("column_names", [])
        for constraint in schema.get_unique_constraints("event_outbox")
    )
    engine.dispose()
