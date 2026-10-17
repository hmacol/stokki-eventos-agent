import os
import subprocess
import sys

import pytest
from sqlalchemy import text

from freshhub.modelos.base import SCHEMAS

URL = os.environ.get("FH_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="FH_TEST_DATABASE_URL nao definida")


def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "FH_AMBIENTE": "teste", "FH_DATABASE_URL": URL or ""}
    return subprocess.run([sys.executable, "-m", "alembic", *args], env=env, capture_output=True, text=True)


def test_upgrade_head_cria_os_schemas(motor_teste):
    with motor_teste.connect() as c:
        existentes = {r[0] for r in c.execute(text("select schema_name from information_schema.schemata"))}
    assert set(SCHEMAS) <= existentes


def test_upgrade_duas_vezes_e_idempotente(motor_teste):
    r = _alembic("upgrade", "head")
    assert r.returncode == 0, r.stderr
    assert "Running upgrade" not in r.stderr + r.stdout


def test_downgrade_base_apaga_os_schemas_e_upgrade_recria(motor_teste):
    assert _alembic("downgrade", "base").returncode == 0
    with motor_teste.connect() as c:
        existentes = {r[0] for r in c.execute(text("select schema_name from information_schema.schemata"))}
    assert not (set(SCHEMAS) & existentes)
    assert _alembic("upgrade", "head").returncode == 0


def test_alembic_sem_database_url_da_mensagem_clara():
    env = {k: v for k, v in os.environ.items() if k != "FH_DATABASE_URL"}
    env["FH_AMBIENTE"] = "teste"
    r = subprocess.run([sys.executable, "-m", "alembic", "current"], env=env, capture_output=True, text=True)
    assert r.returncode != 0
    assert "FH_DATABASE_URL" in r.stderr
