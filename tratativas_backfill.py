# -*- coding: utf-8 -*-
"""
tratativas_backfill.py

Backfill ÚNICO do histórico de tratativas (tratativas.py) a partir do que já
existia ANTES desse módulo -- pedido do Hugo, 14/08: "tudo o que for tratado
em outros dias já entram nesse histórico". As duas fontes históricas reais
(as únicas com conteúdo de tratativa de verdade, ver levantamento em
tratativas.py):

  - insucessos_aguardando_resposta (insucesso_entrega/fingerprint_aguardando_
    resposta.py) -> gera AVISO_ENVIADO (sempre) e RESPOSTA_RECEBIDA (se já
    respondido). Decisão granular (cancelar/reagendar/manter) não existia
    nesse histórico antigo -- só o booleano duplicado_apos_resposta, então a
    decisão aqui vira só "mantida/duplicada" ou "cancelada".
  - torre_excecoes_tratadas (painel_agentes/torre_controle.py) -> gera
    EXCECAO_TRATADA pras exceções do tipo "Insucesso" (id "insucesso:<code>";
    as demais não têm um pedido único por trás, ficam de fora).

Idempotente: usa tratativas.ja_registrado(pedido_code, evento) antes de cada
INSERT -- rodar de novo não duplica nada.

Uso: py -3.11 tratativas_backfill.py
"""
import logging
import sqlite3
import sys
from pathlib import Path

_RAIZ = Path(__file__).parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "insucesso_entrega"))

import tratativas
from motivos_falha import texto_do_motivo

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

DB_PATH = _RAIZ / "dados" / "dados.db"


def _decisao_legada(duplicado_apos_resposta) -> str:
    return "mantida/duplicada" if duplicado_apos_resposta else "cancelada"


def backfill_insucessos_aguardando_resposta() -> tuple[int, int]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM insucessos_aguardando_resposta").fetchall()
    conn.close()

    avisos = respostas = 0
    for r in rows:
        if not r["code"]:
            continue
        motivo_texto = texto_do_motivo(r["failed_reason_id"])

        if not tratativas.ja_registrado(r["code"], "AVISO_ENVIADO"):
            tratativas.registrar_evento(
                r["code"], "INSUCESSO_ENTREGA", "AVISO_ENVIADO",
                service_id=r["service_id"], motivo_id=r["failed_reason_id"],
                motivo_texto=motivo_texto, quando=r["primeira_notificacao_em"],
            )
            avisos += 1

        if (r["status"] or "").upper() == "RESPONDIDO" and r["respondido_em"]:
            if not tratativas.ja_registrado(r["code"], "RESPOSTA_RECEBIDA"):
                tratativas.registrar_evento(
                    r["code"], "INSUCESSO_ENTREGA", "RESPOSTA_RECEBIDA",
                    service_id=r["service_id"], motivo_id=r["failed_reason_id"],
                    motivo_texto=motivo_texto,
                    decisao=_decisao_legada(r["duplicado_apos_resposta"]),
                    texto=r["resposta_texto"], quando=r["respondido_em"],
                )
                respostas += 1

    return avisos, respostas


def backfill_torre_excecoes_tratadas() -> int:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM torre_excecoes_tratadas").fetchall()
    conn.close()

    tratadas = 0
    for r in rows:
        excecao_id = r["id"] or ""
        if not excecao_id.startswith("insucesso:"):
            continue
        pedido_code = excecao_id.split(":", 1)[1]
        if not pedido_code:
            continue
        if tratativas.ja_registrado(pedido_code, "EXCECAO_TRATADA"):
            continue
        tratativas.registrar_evento(
            pedido_code, "TORRE", "EXCECAO_TRATADA",
            texto=r["motivo"], quando=r["tratado_em"],
        )
        tratadas += 1

    return tratadas


def main():
    if not DB_PATH.exists():
        logger.warning(f"Banco não encontrado em {DB_PATH} -- nada a fazer.")
        return
    avisos, respostas = backfill_insucessos_aguardando_resposta()
    tratadas = backfill_torre_excecoes_tratadas()
    logger.info(
        f"Backfill concluído: {avisos} aviso(s), {respostas} resposta(s), "
        f"{tratadas} exceção(ões) tratada(s) da Torre."
    )


if __name__ == "__main__":
    main()
