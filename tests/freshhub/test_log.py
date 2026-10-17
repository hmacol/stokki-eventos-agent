import io
import json
import logging

from fastapi.testclient import TestClient

from freshhub.api.app import criar_app
from freshhub.config import Configuracao
from freshhub.log import configurar_log, request_id_atual


def _capturar() -> io.StringIO:
    configurar_log("INFO")
    buffer = io.StringIO()
    handler = logging.getLogger().handlers[0]
    handler.stream = buffer  # type: ignore[attr-defined]
    return buffer


def test_linha_json_com_campos_basicos():
    buffer = _capturar()
    logging.getLogger("teste.x").info("ola", extra={"pedido": "PS-1"})
    linha = json.loads(buffer.getvalue().strip().splitlines()[-1])
    assert linha["nivel"] == "INFO"
    assert linha["logger"] == "teste.x"
    assert linha["mensagem"] == "ola"
    assert linha["pedido"] == "PS-1"
    assert linha["hora"].endswith("+00:00")


def test_acento_nao_quebra_e_sai_legivel():
    buffer = _capturar()
    logging.getLogger("teste.y").warning("endereço não encontrado: São Paulo")
    linha = json.loads(buffer.getvalue().strip().splitlines()[-1])
    assert linha["mensagem"] == "endereço não encontrado: São Paulo"


def test_configurar_duas_vezes_nao_duplica_handler():
    configurar_log("INFO")
    configurar_log("DEBUG")
    assert len(logging.getLogger().handlers) == 1
    assert logging.getLogger().level == logging.DEBUG


def test_request_id_vai_pro_log_e_pro_cabecalho():
    cfg = Configuracao(ambiente="teste", database_url="postgresql+psycopg://u:s@127.0.0.1:1/x")  # type: ignore[arg-type]
    app = criar_app(cfg)  # reconfigura o log; capturar so depois
    buffer = _capturar()
    with TestClient(app) as cliente:
        r = cliente.get("/api/saude", headers={"X-Request-ID": "abc-123"})
    assert r.headers["X-Request-ID"] == "abc-123"
    linhas = [json.loads(linha) for linha in buffer.getvalue().strip().splitlines()]
    assert any(linha.get("request_id") == "abc-123" for linha in linhas)
    assert request_id_atual.get() is None
