"""Add configurable authentication headers to REST connectors.

Revision ID: 0004_rest_connector_auth
Revises: 0003_governance_ledger
"""

import sqlalchemy as sa
from alembic import op

revision = "0004_rest_connector_auth"
down_revision = "0003_governance_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("connectors")}
    with op.batch_alter_table("connectors") as batch:
        if "auth_header_name" not in columns:
            batch.add_column(
                sa.Column(
                    "auth_header_name",
                    sa.String(length=80),
                    nullable=False,
                    server_default="Authorization",
                )
            )
        if "auth_prefix" not in columns:
            batch.add_column(
                sa.Column(
                    "auth_prefix",
                    sa.String(length=80),
                    nullable=False,
                    server_default="Bearer ",
                )
            )


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("connectors")}
    with op.batch_alter_table("connectors") as batch:
        if "auth_prefix" in columns:
            batch.drop_column("auth_prefix")
        if "auth_header_name" in columns:
            batch.drop_column("auth_header_name")
