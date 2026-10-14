"""Create the initial agent platform schema.

Revision ID: 0001_platform
Revises:
"""

from alembic import op
from sqlalchemy import text

from app import models  # noqa: F401
from app.database import Base

revision = "0001_platform"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.create_all(bind=connection)


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
