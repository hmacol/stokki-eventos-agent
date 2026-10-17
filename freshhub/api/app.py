"""Fabrica da aplicacao FastAPI."""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from freshhub import __version__
from freshhub.api.saude import roteador as roteador_saude
from freshhub.config import Configuracao, carregar_config
from freshhub.log import configurar_log
from freshhub.modelos.sessao import criar_motor


def criar_app(config: Configuracao | None = None) -> FastAPI:
    cfg = config or carregar_config()
    configurar_log(cfg.log_nivel)

    @asynccontextmanager
    async def ciclo(app: FastAPI) -> AsyncIterator[None]:
        app.state.config = cfg
        app.state.motor = criar_motor(cfg.database_url_texto)
        yield
        app.state.motor.dispose()

    app = FastAPI(
        title="Fresh Hub",
        version=__version__,
        lifespan=ciclo,
        docs_url="/api/docs" if cfg.ambiente != "prod" else None,
        openapi_url="/api/openapi.json",
    )
    app.include_router(roteador_saude)
    return app
