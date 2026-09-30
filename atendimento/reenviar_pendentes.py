# -*- coding: utf-8 -*-
"""
atendimento/reenviar_pendentes.py

Reenvia mensagens de WhatsApp que falharam ao sair pela Evolution API --
ver banco.py::mensagens_pendentes_para_retry/marcar_envio_sucesso/
marcar_envio_falha_ou_esgotado e a rota /api/conversas/<id>/responder em
app.py (grava status="PENDENTE" em vez de só devolver erro pro atendente).

Backoff exponencial com teto (30s até 15min, ver banco.py::MAX_TENTATIVAS_ENVIO);
depois de esgotar as tentativas a mensagem vira FALHOU (definitivo) e dispara
um e-mail de alerta -- uma vez só, não a cada tentativa.

COMO USAR (timer systemd na VPS a cada 1min, infra/atendimento-reenviar-pendentes.*):
    python atendimento/reenviar_pendentes.py

config.yaml:
    atendimento:
      email_alerta: "hugo@freshlogbr.com"   # opcional, default abaixo
    evolution_api: { ... }                  # ver integracao_evolution.py
    email: { ... }                          # ver email_utils.py
"""
import logging
import sys
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import yaml

import integracao_evolution
from atendimento import alertas, banco

logger = logging.getLogger("atendimento.reenviar_pendentes")


def _carregar_config() -> dict:
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        handlers=[logging.StreamHandler(),
                  logging.FileHandler(_RAIZ / "dados" / "atendimento_reenviar_pendentes.log", encoding="utf-8")],
    )

    config = _carregar_config()
    cfg_evolution = config.get("evolution_api", {}) or {}
    conn = banco.conectar()
    try:
        suspensao = banco.suspensao_envios(conn)
        if suspensao:
            # Disjuntor (ver banco.SUSPENSAO_463_HORAS_PADRAO): reenviar agora
            # só renovaria a trava do WhatsApp -- as pendentes esperam.
            logger.info(f"Envios automáticos suspensos até {suspensao['ate']} ({suspensao['motivo']}) -- nada reenviado.")
            return 0
        pendentes = banco.mensagens_pendentes_para_retry(conn)
        if not pendentes:
            logger.info("Nenhuma mensagem pendente.")
            return 0

        reenviadas = esgotadas = 0
        for msg in pendentes:
            sucesso, evolution_id = integracao_evolution.enviar_texto(
                cfg_evolution, msg["telefone_e164"], msg["corpo"] or "",
            )
            if sucesso:
                banco.marcar_envio_sucesso(conn, msg["id"], evolution_id)
                reenviadas += 1
                continue
            esgotou = banco.marcar_envio_falha_ou_esgotado(conn, msg["id"], msg["tentativas"])
            if esgotou:
                esgotadas += 1
                alertas.avisar_mensagem_nao_entregue(
                    config, msg["protocolo"], msg["telefone_e164"], msg["corpo"],
                    motivo=f"esgotou {banco.MAX_TENTATIVAS_ENVIO} tentativas (falha ao chamar a Evolution API)",
                )

        logger.info(f"{len(pendentes)} pendente(s): {reenviadas} reenviada(s), {esgotadas} esgotada(s) (FALHOU).")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
