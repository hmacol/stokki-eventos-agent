"""Log estruturado: uma linha JSON por registro, em stderr (spec 5.5).
Campos extras do `extra=` sao achatados na linha."""
import contextvars
import json
import logging
import sys
from datetime import UTC, datetime

request_id_atual: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)

_CAMPOS_PADRAO = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


class FormatadorJson(logging.Formatter):
    def format(self, registro: logging.LogRecord) -> str:
        linha: dict[str, object] = {
            "hora": datetime.fromtimestamp(registro.created, tz=UTC).isoformat(),
            "nivel": registro.levelname,
            "logger": registro.name,
            "mensagem": registro.getMessage(),
        }
        rid = request_id_atual.get()
        if rid:
            linha["request_id"] = rid
        for chave, valor in registro.__dict__.items():
            if chave not in _CAMPOS_PADRAO and not chave.startswith("_"):
                linha[chave] = valor
        if registro.exc_info:
            linha["excecao"] = self.formatException(registro.exc_info)
        # ensure_ascii=False: acento sai legivel e nao estoura UnicodeEncodeError
        return json.dumps(linha, ensure_ascii=False, default=str)


def configurar_log(nivel: str) -> None:
    raiz = logging.getLogger()
    raiz.setLevel(nivel.upper())
    for h in list(raiz.handlers):
        raiz.removeHandler(h)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(FormatadorJson())
    raiz.addHandler(handler)
    logging.getLogger("uvicorn.access").disabled = True  # o middleware loga a requisicao
