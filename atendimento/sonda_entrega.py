# -*- coding: utf-8 -*-
"""
atendimento/sonda_entrega.py

UMA mensagem de teste pela Evolution API + leitura do resultado, com e-mail
do veredito. Existe pra medir se a trava 463 do WhatsApp ("reach-out
time-lock", ver banco.SUSPENSAO_463_HORAS_PADRAO) expirou -- SEM rajada:
cada envio a mais renova a trava, então é uma sonda por janela de silêncio,
nunca um loop.

Como lê o resultado: o webhook (app.py::webhook_evolution) grava
`mensagens.entregue_em` no messages.update DELIVERY_ACK/READ (✓✓) e marca
FALHOU no ERROR (463). A sonda só registra a mensagem e fica olhando a
própria linha no banco por --espera segundos.

Uso (manual):
    python atendimento/sonda_entrega.py --telefone +5511993725364
Agendamento pontual na VPS (timer transitório, some sozinho depois):
    systemd-run --on-calendar='2026-08-28 08:00:00' --timer-property=AccuracySec=1min \\
      --unit=atendimento-sonda-20260828 -p User=www-data -p WorkingDirectory=/opt/stokki-eventos \\
      /opt/stokki-eventos/venv/bin/python /opt/stokki-eventos/atendimento/sonda_entrega.py --telefone +5511993725364

Saída: 0 = ENTREGUE; 1 = qualquer outro veredito.
"""
import argparse
import logging
import sys
import time
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import yaml

import integracao_evolution
from atendimento import alertas, banco

logger = logging.getLogger("atendimento.sonda_entrega")


def _carregar_config() -> dict:
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def sondar(conn, cfg_evolution: dict, telefone: str, espera_s: int) -> tuple[str, str]:
    """Envia UMA mensagem e devolve (resultado, detalhe):
    ENTREGUE | RECUSADA | INDETERMINADA | FALHA_NO_POST."""
    contato = banco.buscar_ou_criar_contato(conn, telefone)
    conversa = banco.conversa_aberta_do_contato(conn, contato["id"]) or banco.abrir_conversa(conn, contato["id"])
    texto = f"Sonda da central Freshlog ({banco.datetime_agora_str()[:16]}) - pode ignorar."
    ok, evolution_id = integracao_evolution.enviar_texto(cfg_evolution, contato["telefone_e164"], texto)
    msg_id = banco.registrar_mensagem(
        conn, conversa["id"], "OUT", texto, evolution_message_id=evolution_id,
        status="ENVIADA" if ok else "PENDENTE",
    )
    if msg_id is None and evolution_id:
        # eco do webhook chegou antes e já inseriu a linha -- acha pelo id da Evolution
        linha = conn.execute("SELECT id FROM mensagens WHERE evolution_message_id = ?", (evolution_id,)).fetchone()
        msg_id = linha["id"] if linha else None
    if not ok:
        return "FALHA_NO_POST", "a Evolution API nem aceitou o envio (instância fora do ar ou desconectada?)"
    if msg_id is None:
        return "INDETERMINADA", f"mensagem enviada (keyId {evolution_id}) mas não achei a linha no banco pra acompanhar"

    fim = time.time() + espera_s
    while time.time() < fim:
        time.sleep(5)
        row = conn.execute("SELECT status, entregue_em FROM mensagens WHERE id = ?", (msg_id,)).fetchone()
        if row["entregue_em"]:
            return "ENTREGUE", f"✓✓ confirmado em {row['entregue_em']} (keyId {evolution_id})"
        if row["status"] == "FALHOU":
            return "RECUSADA", f"WhatsApp reportou ERROR (463) pra keyId {evolution_id} -- trava ainda vigente"
    return "INDETERMINADA", f"sem ✓✓ nem recusa em {espera_s}s (keyId {evolution_id}) -- conferir no celular"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sonda de entrega pelo WhatsApp (uma mensagem, sem rajada).")
    parser.add_argument("--telefone", required=True, help="E.164, ex: +5511993725364")
    parser.add_argument("--espera", type=int, default=90, help="segundos esperando ✓✓/463 (padrão 90)")
    parser.add_argument("--sem-email", action="store_true", help="não manda o e-mail do veredito")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        handlers=[logging.StreamHandler(),
                  logging.FileHandler(_RAIZ / "dados" / "atendimento_sonda_entrega.log", encoding="utf-8")],
    )
    config = _carregar_config()
    conn = banco.conectar()
    try:
        resultado, detalhe = sondar(conn, config.get("evolution_api", {}) or {}, args.telefone, args.espera)
    finally:
        conn.close()
    logger.info(f"Sonda {args.telefone}: {resultado} -- {detalhe}")
    if not args.sem_email:
        alertas.avisar_resultado_sonda(config, args.telefone, resultado, detalhe)
    return 0 if resultado == "ENTREGUE" else 1


if __name__ == "__main__":
    sys.exit(main())
