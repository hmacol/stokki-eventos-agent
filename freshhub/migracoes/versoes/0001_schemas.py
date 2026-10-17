"""Schemas por modulo de dominio (spec 2.1).

Revisao: 0001
Anterior: None
Data: 2026-10-17
"""
from alembic import op

from freshhub.modelos.base import SCHEMAS

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    for schema in SCHEMAS:
        op.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')


def downgrade() -> None:
    for schema in reversed(SCHEMAS):
        op.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
