# -*- coding: utf-8 -*-
"""
Aula do portal (/aula, publica, 06/11): videos curtos de treinamento do
cliente. Os mp4/jpg e o manifesto aula.json sao gerados por aula_portal/gravar.py
e copiados pra PASTA_AULA; a pagina so mostra e so entrega o que esta no
manifesto.
"""
import json
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
PASTA_AULA = _RAIZ / "dados" / "aula"


def carregar_manifesto(pasta: Path | None = None, liberadas: frozenset | set = frozenset()) -> list[dict]:
    """Videos do manifesto em ordem de numero; lista vazia se o arquivo nao
    existe ou nao e um JSON valido (a pagina mostra "sendo preparados").
    Video com "requer" (tela oculta do cliente, ex.: "cotacao") so entra se
    essa tela estiver em `liberadas`."""
    pasta = pasta or PASTA_AULA
    try:
        dados = json.loads((pasta / "aula.json").read_text(encoding="utf-8"))
        videos = [v for v in dados.get("videos", []) if isinstance(v, dict) and v.get("video")
                  and (not v.get("requer") or v["requer"] in liberadas)]
        return sorted(videos, key=lambda v: int(v.get("numero") or 0))
    except (OSError, ValueError, AttributeError, TypeError):
        return []


def arquivo_permitido(nome: str, videos: list[dict]) -> bool:
    """So nomes exatos de video/capa do manifesto (nada de caminho)."""
    return any(nome in (v.get("video"), v.get("capa")) for v in videos)
