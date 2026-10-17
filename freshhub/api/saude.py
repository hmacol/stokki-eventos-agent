"""Rota de saude: a unica que fala com o banco sem passar por servicos
(excecao declarada no import-linter)."""
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from freshhub import __version__

logger = logging.getLogger(__name__)
roteador = APIRouter()


@roteador.get("/api/saude")
def saude(request: Request) -> JSONResponse:
    config = request.app.state.config
    motor = request.app.state.motor
    banco = "ok"
    try:
        with motor.connect() as conexao:
            conexao.execute(text("select 1"))
    except Exception as e:  # noqa: BLE001 - qualquer falha de banco vira 'falha', sem a URL
        # So o TIPO da excecao vai pro log: a mensagem do psycopg traz host e usuario
        banco = "falha"
        logger.warning("saude: banco indisponivel (%s)", type(e).__name__)
    corpo = {
        "status": "ok" if banco == "ok" else "degradado",
        "versao": __version__,
        "ambiente": config.ambiente,
        "banco": banco,
    }
    return JSONResponse(corpo, status_code=200 if banco == "ok" else 503)
