"""Fabrica da aplicacao FastAPI."""
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

from freshhub import __version__
from freshhub.api.saude import roteador as roteador_saude
from freshhub.config import Configuracao, carregar_config
from freshhub.log import configurar_log, request_id_atual
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

    @app.middleware("http")
    async def _request_id(
        request: Request, chamar_proximo: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        token = request_id_atual.set(rid)
        inicio = time.perf_counter()
        try:
            resposta = await chamar_proximo(request)
        finally:
            request_id_atual.reset(token)
        resposta.headers["X-Request-ID"] = rid
        # request_id explicito no extra porque o reset ja aconteceu
        logging.getLogger("freshhub.api").info(
            "requisicao",
            extra={
                "request_id": rid,
                "metodo": request.method,
                "caminho": request.url.path,
                "status": resposta.status_code,
                "ms": round((time.perf_counter() - inicio) * 1000, 1),
            },
        )
        return resposta

    app.include_router(roteador_saude)
    return app
