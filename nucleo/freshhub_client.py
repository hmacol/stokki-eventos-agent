# -*- coding: utf-8 -*-
"""
nucleo/freshhub_client.py

Cliente da API do sistema novo (freshhub, repo C:\\freshhub; plano 1.2, ADR-0011 de la). O legado e o
braco do novo junto a Stokki e a Vuupt ate as Fases 2-3: le o pool (GET /api/pool), atende a fila de
replica (GET /api/replicacao/pendentes, POST /api/replicacao/{id}/resultado) e registra o que decide no
planejamento (POST /api/eventos).

Config (config.yaml da VPS, nunca no chat):
    freshhub_api:
      url: http://127.0.0.1:8100      # a API so aceita o token de loopback, sem Caddy
      token: fhs_...                   # criado por `freshhub.jobs.usuarios criar-token`; um por ambiente

Mesmo molde do vuupt_client: requests.Session com Bearer, timeout de 15 s e chamar_com_retry (429).
Qualquer falha de rede ou HTTP >= 400 vira ErroFreshhub: quem chama decide se cai pra Vuupt (pool) ou so loga.
O token nunca aparece em log nem em repr.
"""
import logging

import requests

from http_retry import chamar_com_retry

logger = logging.getLogger("nucleo.freshhub_client")

TIMEOUT = 15
ERRO_MAX = 2000  # POST /api/replicacao/{id}/resultado aceita ate 2000 caracteres de erro


class ErroFreshhub(RuntimeError):
    """API nova fora do ar, recusando o token ou devolvendo erro. `status` e o HTTP (None em falha de rede);
    `permanente` = 4xx: repetir a mesma chamada nao vai melhorar (validacao, token), entao quem guarda pra
    tentar depois deve descartar."""

    def __init__(self, mensagem: str, status: int | None = None):
        super().__init__(mensagem)
        self.status = status

    @property
    def permanente(self) -> bool:
        return self.status is not None and 400 <= self.status < 500


class FreshhubClient:
    def __init__(self, url: str, token: str, timeout: int = TIMEOUT):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.sessao = requests.Session()
        self.sessao.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})

    def __repr__(self) -> str:
        return f"FreshhubClient(url={self.url!r})"

    def _chamar(self, metodo: str, caminho: str, **kw):
        func = getattr(self.sessao, metodo)
        try:
            resp = chamar_com_retry(func, f"{self.url}{caminho}", timeout=self.timeout, **kw)
        except requests.RequestException as e:
            raise ErroFreshhub(f"{metodo.upper()} {caminho}: {type(e).__name__}: {e}") from e
        if resp.status_code >= 400:
            raise ErroFreshhub(f"{metodo.upper()} {caminho}: HTTP {resp.status_code} {(resp.text or '')[:200]}",
                               status=resp.status_code)
        return resp

    def obter_pool(self) -> dict:
        """Corpo inteiro de GET /api/pool: itens (formato do servico da Vuupt + estado/prontidao/marcas),
        dados_de (snapshot da ultima carga) e gerado_em."""
        return self._chamar("get", "/api/pool").json()

    def listar_pool(self) -> list[dict]:
        return list(self.obter_pool().get("itens") or [])

    def pendentes(self, destino: str, limite: int = 50) -> list[dict]:
        """Itens da fila de replica do destino; a API ja os marca como enviados a este token."""
        return self._chamar("get", "/api/replicacao/pendentes",
                            params={"destino": destino, "limite": limite}).json()

    def resultado(self, id: str, ok: bool, resultado: dict | None = None, erro: str | None = None) -> None:
        corpo = {"ok": ok, "resultado": resultado, "erro": erro[:ERRO_MAX] if erro else None}
        self._chamar("post", f"/api/replicacao/{id}/resultado", json=corpo)

    def arquivo(self, id: str) -> bytes:
        """O arquivo que a replica manda usar (plano 1.3: o XML original da NF-e que o portal novo guardou)."""
        return self._chamar("get", f"/api/replicacao/{id}/arquivo").content

    def eventos(self, lista: list[dict]) -> dict:
        """[{codigo, tipo, ocorrido_em (ISO com fuso), dados}] -> {aceitos, repetidos, desconhecidos}."""
        return self._chamar("post", "/api/eventos", json={"eventos": lista}).json()


def de_config(config: dict | None) -> FreshhubClient | None:
    """Cliente a partir de `freshhub_api` do config.yaml; None quando url ou token faltam (sombra desligada)."""
    secao = ((config or {}).get("freshhub_api") or {})
    url = str(secao.get("url") or "").strip()
    token = str(secao.get("token") or "").strip()
    if not url or not token:
        return None
    return FreshhubClient(url, token)
