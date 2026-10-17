"""Ambiente do Alembic: a URL vem de FH_DATABASE_URL (nunca do ini)."""
import sys
from collections.abc import MutableMapping
from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import engine_from_config, pool

from freshhub.config import ConfiguracaoInvalida, carregar_config
from freshhub.modelos.base import SCHEMAS, Base

try:
    _cfg = carregar_config()
except ConfiguracaoInvalida as e:
    print(f"alembic: {e}", file=sys.stderr)  # noqa: T201 - saida de linha de comando
    raise SystemExit(2) from e

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)  # loggers do alembic.ini: mostra "Running upgrade -> NNNN"
config.set_main_option("sqlalchemy.url", _cfg.database_url_texto)
target_metadata = Base.metadata


def _incluir(name: str | None, type_: str, parent_names: MutableMapping[Any, str | None]) -> bool:
    # autogenerate so enxerga os nossos schemas
    if type_ == "schema":
        return name in SCHEMAS
    return True


def run_migrations_online() -> None:
    conectavel = engine_from_config(
        config.get_section(config.config_ini_section) or {}, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    with conectavel.connect() as conexao:
        # alembic_version fica no schema public: precisa existir antes dos nossos schemas
        context.configure(
            connection=conexao,
            target_metadata=target_metadata,
            include_schemas=True,
            include_name=_incluir,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    raise SystemExit("modo offline nao e usado neste projeto")
run_migrations_online()
