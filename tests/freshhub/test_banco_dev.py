import os

import pytest
from sqlalchemy import create_engine, text

URL = os.environ.get("FH_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="FH_TEST_DATABASE_URL nao definida")


def test_postgres_de_teste_responde():
    motor = create_engine(URL or "", pool_pre_ping=True)
    with motor.connect() as conexao:
        versao = conexao.execute(text("select version()")).scalar_one()
        assert versao.startswith("PostgreSQL 16")
        assert conexao.execute(text("select current_database()")).scalar_one() == "freshhub_test"
