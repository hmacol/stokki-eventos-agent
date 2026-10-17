"""Fixtures do pacote novo. Precisa de FH_TEST_DATABASE_URL (Postgres de
teste, Task 4). Migra base -> head uma vez por sessao de teste; cada
teste roda numa transacao que volta no fim."""
import os
import subprocess
import sys
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from freshhub.modelos.sessao import criar_motor

URL_TESTE = os.environ.get("FH_TEST_DATABASE_URL")


def banco_e_de_teste(url: str) -> bool:
    """So banco cujo nome termina em _test pode receber downgrade base
    (DROP SCHEMA CASCADE): protege dev, hml e prod de um erro de copia."""
    nome = url.rsplit("/", 1)[-1].split("?", 1)[0] if "/" in url.split("://", 1)[-1] else ""
    return nome.endswith("_test")


def _alembic(*args: str) -> None:
    env = {**os.environ, "FH_AMBIENTE": "teste", "FH_DATABASE_URL": URL_TESTE or ""}
    subprocess.run([sys.executable, "-m", "alembic", *args], check=True, env=env)


@pytest.fixture(scope="session")
def motor_teste() -> Iterator[Engine]:
    if not URL_TESTE:
        pytest.skip("FH_TEST_DATABASE_URL nao definida")
    if not banco_e_de_teste(URL_TESTE):
        pytest.exit("FH_TEST_DATABASE_URL precisa apontar pra um banco *_test (a fixture apaga schemas)", 3)
    _alembic("downgrade", "base")
    _alembic("upgrade", "head")
    motor = criar_motor(URL_TESTE)
    yield motor
    motor.dispose()


@pytest.fixture
def sessao(motor_teste: Engine) -> Iterator[Session]:
    conexao = motor_teste.connect()
    transacao = conexao.begin()
    sessao = Session(bind=conexao, join_transaction_mode="create_savepoint")
    try:
        yield sessao
    finally:
        sessao.close()
        transacao.rollback()
        conexao.close()
