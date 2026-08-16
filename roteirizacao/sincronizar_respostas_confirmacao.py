# -*- coding: utf-8 -*-
"""
roteirizacao/sincronizar_respostas_confirmacao.py

Puxa da VPS pública (confirmacao_motoristas/app.py) as respostas que os
motoristas já deram (confirmado/recusado) e aplica em
regras/confirmacao_rotas.py (local) -- pedido do Hugo, 16/08. Roda
solto, sem depender do horário de geração dos avisos
(avisar_motoristas_rotas.py --gerar-confirmacoes), pra refletir a
resposta do motorista em /planejamento assim que possível.

COMO USAR (agendado a cada 30 min no Agendador de Tarefas do Windows,
mesmo padrão dos demais agentes -- usar caminho completo do py.exe):
    py -3.11 roteirizacao/sincronizar_respostas_confirmacao.py
"""
import logging
import sys
from pathlib import Path

_RAIZ_LOCAL   = Path(__file__).parent
_RAIZ_PROJETO = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ_PROJETO))
sys.path.insert(0, str(_RAIZ_LOCAL))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_RAIZ_LOCAL / "dados" / "sincronizar_respostas_confirmacao.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("sincronizar_respostas_confirmacao")

import requests
import yaml

from regras import confirmacao_rotas


def _carregar_config() -> dict:
    with open(_RAIZ_PROJETO / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def main():
    config = _carregar_config().get("confirmacao_rotas", {})
    url_base = (config.get("url_base") or "").rstrip("/")
    sync_secret = config.get("sync_secret")
    if not url_base or not sync_secret:
        logger.warning("confirmacao_rotas.url_base/sync_secret não configurados em config.yaml -- nada a sincronizar.")
        return

    try:
        resp = requests.get(
            f"{url_base}/api/sync/respostas",
            headers={"X-Sync-Secret": sync_secret},
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning(f"Falha ao consultar respostas na VPS: {exc}")
        return

    respostas = resp.json().get("respostas", [])
    aplicadas = nao_encontradas = 0
    for resposta in respostas:
        ok = confirmacao_rotas.aplicar_resposta_remota(
            token=resposta["token"], status=resposta["status"],
            motivo_recusa=resposta.get("motivo_recusa"), respondido_em=resposta["respondido_em"],
        )
        if ok:
            aplicadas += 1
        else:
            nao_encontradas += 1

    logger.info(
        f"{len(respostas)} resposta(s) recebida(s) da VPS -- {aplicadas} aplicada(s) localmente"
        + (f", {nao_encontradas} sem correspondência local (dessincronia)" if nao_encontradas else "") + "."
    )


if __name__ == "__main__":
    main()
