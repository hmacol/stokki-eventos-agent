# -*- coding: utf-8 -*-
"""
nucleo/marketplace_remoto.py

Cliente da página pública do marketplace (confirmacao_motoristas/app.py)
para a escolha feita pelo APP do motorista -- Hugo, 14/09/2026.

Por quê: a disputa de uma oferta tem que acontecer num lugar só. A página
pública já faz o claim atômico (motorista escolhe pelo link/CPF) e é ela
que a sincronização local consulta (roteirizacao/sincronizar_respostas_
confirmacao.py). Quando o app gravava a escolha só no banco local, a
página seguia com a oferta ABERTA e a reconciliação desfazia a escolha em
até 15 min. Agora o app escolhe/cancela LÁ primeiro e só espelha aqui.
"""
import logging

import requests

logger = logging.getLogger("nucleo.marketplace_remoto")


class SemConexaoMarketplace(Exception):
    """Página pública fora do ar / sem rede: a escolha NÃO acontece."""


class ClienteMarketplace:
    def __init__(self, url_base: str, sync_secret: str, timeout: float = 10.0):
        self.url_base = url_base.rstrip("/")
        self.sync_secret = sync_secret
        self.timeout = timeout

    @classmethod
    def do_config(cls, config: dict | None) -> "ClienteMarketplace | None":
        """None quando confirmacao_rotas não está configurado (dev/testes):
        aí a escolha fica só no banco local, como antes."""
        cfg = (config or {}).get("confirmacao_rotas") or {}
        if not cfg.get("url_base") or not cfg.get("sync_secret"):
            return None
        return cls(cfg["url_base"], cfg["sync_secret"])

    def _post(self, caminho: str, rascunho_id: int, agent_id: int) -> tuple[int, dict]:
        try:
            r = requests.post(f"{self.url_base}{caminho}", json={"rascunho_id": rascunho_id, "agent_id": agent_id},
                              headers={"X-Sync-Secret": self.sync_secret}, timeout=self.timeout)
        except requests.RequestException as e:
            logger.warning("marketplace %s rascunho=%s agent=%s: sem conexão (%s)", caminho, rascunho_id, agent_id, e)
            raise SemConexaoMarketplace(str(e)) from e
        try:
            corpo = r.json()
        except ValueError:
            corpo = {}
        if r.status_code >= 500 or r.status_code in (401, 403):
            logger.error("marketplace %s rascunho=%s: HTTP %s %s", caminho, rascunho_id, r.status_code, r.text[:200])
            raise SemConexaoMarketplace(f"HTTP {r.status_code}")
        return r.status_code, corpo

    def escolher(self, rascunho_id: int, agent_id: int) -> tuple[int, dict]:
        return self._post("/api/sync/ofertas/escolher", rascunho_id, agent_id)

    def cancelar(self, rascunho_id: int, agent_id: int) -> tuple[int, dict]:
        return self._post("/api/sync/ofertas/cancelar", rascunho_id, agent_id)
