import os
import time

import pytest
from fastapi.testclient import TestClient

from freshhub import __version__
from freshhub.api.app import criar_app
from freshhub.config import Configuracao

URL = os.environ.get("FH_TEST_DATABASE_URL")


def _config(url: str) -> Configuracao:
    return Configuracao(ambiente="teste", database_url=url)  # type: ignore[arg-type]


@pytest.mark.skipif(not URL, reason="FH_TEST_DATABASE_URL nao definida")
def test_saude_ok_com_banco(motor_teste):
    with TestClient(criar_app(_config(URL or ""))) as cliente:
        r = cliente.get("/api/saude")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "versao": __version__, "ambiente": "teste", "banco": "ok"}


def test_saude_503_sem_banco_e_sem_vazar_segredo(caplog):
    url = "postgresql+psycopg://freshhub:SENHA-SECRETA@127.0.0.1:1/nao_existe"
    inicio = time.perf_counter()
    with TestClient(criar_app(_config(url))) as cliente:
        r = cliente.get("/api/saude")
    # banco fora nao pode prender a rota de saude (no Windows a conexao recusada leva ~65 s sem timeout)
    assert time.perf_counter() - inicio < 15
    assert r.status_code == 503
    assert r.json()["banco"] == "falha"
    assert r.json()["status"] == "degradado"
    assert "SENHA-SECRETA" not in r.text
    assert "SENHA-SECRETA" not in caplog.text


def test_rota_inexistente_e_404_json():
    url = "postgresql+psycopg://u:s@127.0.0.1:1/x"
    with TestClient(criar_app(_config(url))) as cliente:
        r = cliente.get("/api/nao-existe")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")
