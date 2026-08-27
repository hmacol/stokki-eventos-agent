# -*- coding: utf-8 -*-
"""
atendimento/monitorar_saude_evolution.py

Checa se a sessão do WhatsApp (Evolution API) continua conectada e avisa
por e-mail só na transição (edge-triggered, ver
banco.py::atualizar_estado_evolution) -- sem isso, a única forma de saber
que a sessão caiu era alguém abrir /admin/whatsapp e clicar em "Atualizar".

Qualquer falha ao consultar a API (rede fora, container caído) também conta
como desconectado -- não é só o campo "state" da resposta.

COMO USAR (timer systemd na VPS a cada 5min, infra/atendimento-monitorar-saude-evolution.*):
    python atendimento/monitorar_saude_evolution.py

config.yaml: mesmas seções de reenviar_pendentes.py (atendimento.email_alerta,
evolution_api, email).
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

logger = logging.getLogger("atendimento.monitorar_saude_evolution")

_DESTINATARIO_PADRAO = "hugo@freshlogbr.com"


def _carregar_config() -> dict:
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _checar_conectado(cfg_evolution: dict) -> bool:
    try:
        resp = integracao_evolution.status_instancia(cfg_evolution)
        return (resp.get("instance") or {}).get("state") == "open"
    except Exception as exc:
        logger.warning(f"Falha ao consultar status da Evolution API: {exc}")
        return False


def _avisar_transicao(config: dict, conectado: bool) -> None:
    destinatario = (config.get("atendimento", {}) or {}).get("email_alerta", _DESTINATARIO_PADRAO)
    if conectado:
        assunto = "Atendimento: WhatsApp reconectado"
        conteudo = "<p>A sessão do WhatsApp (Evolution API) voltou a ficar conectada.</p>"
        cor = email_utils.COR_ACENTO
    else:
        assunto = "Atendimento: WhatsApp desconectado"
        conteudo = (
            "<p>A sessão do WhatsApp (Evolution API) caiu ou está inacessível -- "
            "novas mensagens não estão sendo enviadas.</p>"
            "<p>Verifique em atendimento.freshhub.com.br/admin/whatsapp e "
            "reparear o número se necessário.</p>"
        )
        cor = email_utils.COR_ERRO
    email_utils.enviar_email([destinatario], assunto, email_utils.envelope_html(conteudo, cor_acento=cor),
                              config.get("email", {}))


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        handlers=[logging.StreamHandler(),
                  logging.FileHandler(_RAIZ / "dados" / "atendimento_monitorar_saude_evolution.log", encoding="utf-8")],
    )

    config = _carregar_config()
    cfg_evolution = config.get("evolution_api", {}) or {}
    conectado = _checar_conectado(cfg_evolution)

    conn = banco.conectar()
    try:
        mudou = banco.atualizar_estado_evolution(conn, conectado)
    finally:
        conn.close()

    if mudou:
        logger.info(f"Estado da sessão mudou: conectado={conectado} -- enviando e-mail de alerta.")
        _avisar_transicao(config, conectado)
    else:
        logger.info(f"Sem mudança: conectado={conectado}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
