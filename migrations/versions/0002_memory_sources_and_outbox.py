"""Add durable conversation memory, data sources, workflows, and event outbox.

Revision ID: 0002_memory_sources
Revises: 0001_platform
"""

import sqlalchemy as sa
from alembic import op

from app import models  # noqa: F401
from app.database import Base

revision = "0002_memory_sources"
down_revision = "0001_platform"
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())
    with op.batch_alter_table("task_events") as batch:
        batch.alter_column(
            "task_id",
            existing_type=sa.String(length=32),
            nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("task_events") as batch:
        batch.alter_column(
            "task_id",
            existing_type=sa.String(length=32),
            nullable=False,
        )
    for table in (
        "event_receipts",
        "event_outbox",
        "workflow_checkpoints",
        "workflow_definitions",
        "conversation_memory",
        "data_sources",
    ):
        Base.metadata.tables[table].drop(bind=op.get_bind(), checkfirst=True)
