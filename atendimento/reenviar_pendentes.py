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

import email_utils
import integracao_evolution
from atendimento import banco

logger = logging.getLogger("atendimento.reenviar_pendentes")

_DESTINATARIO_PADRAO = "hugo@freshlogbr.com"


def _carregar_config() -> dict:
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _avisar_esgotado(config: dict, protocolo: str, telefone: str, corpo: str | None) -> None:
    destinatario = (config.get("atendimento", {}) or {}).get("email_alerta", _DESTINATARIO_PADRAO)
    conteudo = (
        f"<p>Uma mensagem não pôde ser entregue pelo WhatsApp depois de "
        f"{banco.MAX_TENTATIVAS_ENVIO} tentativas.</p>"
        f"<p><strong>Protocolo:</strong> {protocolo}<br>"
        f"<strong>Telefone:</strong> {telefone}<br>"
        f"<strong>Mensagem:</strong> {corpo or '(vazia)'}</p>"
        f"<p>Verifique a conexão em atendimento.freshhub.com.br/admin/whatsapp "
        f"e reenvie manualmente pela conversa se preciso.</p>"
    )
    email_utils.enviar_email(
        [destinatario], f"Atendimento: mensagem não entregue ({protocolo})",
        email_utils.envelope_html(conteudo, cor_acento=email_utils.COR_ERRO),
        config.get("email", {}),
    )


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
                _avisar_esgotado(config, msg["protocolo"], msg["telefone_e164"], msg["corpo"])

        logger.info(f"{len(pendentes)} pendente(s): {reenviadas} reenviada(s), {esgotadas} esgotada(s) (FALHOU).")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
