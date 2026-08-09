# -*- coding: utf-8 -*-
"""
atualizar_agendamentos_confirmados.py

Agente novo (pedido do Hugo, 02/08): depois que uma resposta de
agendamento é lida (ler_respostas_agendamento.py já processa a resposta
e marca status='RESPONDIDO' em agendamentos_pedido, com data/horário),
esse agente pega essas confirmações e ATUALIZA o serviço já existente
no VUUPT (scheduled_start/scheduled_end) — algo que faltava: o
pipeline.py só define esses campos na CRIAÇÃO do serviço; se a resposta
chegar depois (pedido já importado, parado em not_assigned esperando
roteirização), nada revisitava o serviço pra aplicar a data confirmada.

Usa PUT /services/{id} (vuupt_client.py::atualizar_servico, já
existente) — só os campos scheduled_start/scheduled_end, forma leve
(sem reenviar endereço/coordenadas).

Marca aplicado_vuupt_em em agendamentos_pedido depois de aplicar, pra
nunca reprocessar a mesma confirmação 2x.

COMO USAR:
    py -3.11 atualizar_agendamentos_confirmados.py
    py -3.11 atualizar_agendamentos_confirmados.py --modo-teste
"""
import argparse
import logging
import sqlite3
import sys
import time
from pathlib import Path

_RAIZ = Path(__file__).parent
sys.path.insert(0, str(_RAIZ))

# Força UTF-8 no stdout/stderr -- sem isso, no Windows o console usa a
# codepage padrão (não UTF-8), e acentos saem corrompidos (mojibake)
# tanto na tela quanto no log capturado (ex: pelo painel_agentes).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_RAIZ / "dados" / "atualizar_agendamentos_confirmados.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("atualizar_agendamentos_confirmados")

import yaml

from vuupt_client import VuuptClient, VuuptAPIError, _converter_data_para_iso
from notificar_execucao_agente import notificar_execucao

DB_PATH = _RAIZ / "dados" / "dados.db"


def _carregar_config() -> dict:
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _garantir_coluna_aplicado():
    """Adiciona a coluna aplicado_vuupt_em se ainda não existir (ALTER
    TABLE seguro pra rodar quantas vezes for preciso)."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("ALTER TABLE agendamentos_pedido ADD COLUMN aplicado_vuupt_em TEXT")
        conn.commit()
        logger.info("Coluna 'aplicado_vuupt_em' criada em agendamentos_pedido.")
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            raise
    conn.close()


def _buscar_confirmados_pendentes_de_aplicar() -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT pedido, data_agendada, horario_inicio_agendado, horario_fim_agendado "
        "FROM agendamentos_pedido "
        "WHERE status = 'RESPONDIDO' AND aplicado_vuupt_em IS NULL AND data_agendada IS NOT NULL"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _marcar_aplicado(pedido: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE agendamentos_pedido SET aplicado_vuupt_em = datetime('now','localtime') WHERE pedido = ?",
        (pedido,),
    )
    conn.commit()
    conn.close()


def main(modo_teste: bool = False):
    inicio = time.time()
    logger.info(f"{'[MODO TESTE] ' if modo_teste else ''}Atualização de agendamentos confirmados iniciada.")

    config = _carregar_config()
    token = config.get("vuupt_api", {}).get("token", "")

    resumo_etapas = {}

    try:
        _garantir_coluna_aplicado()
        vuupt = VuuptClient(token)

        confirmados = _buscar_confirmados_pendentes_de_aplicar()
        logger.info(f"{len(confirmados)} agendamento(s) confirmado(s) aguardando aplicar no VUUPT.")

        if not confirmados:
            resumo_etapas["Atualização de agendamentos"] = {"status": "ok", "detalhe": "Nenhum confirmado pendente."}
            return

        atualizados = 0
        falhas = 0
        for c in confirmados:
            pedido = c["pedido"]
            hora_inicio = c["horario_inicio_agendado"] or "08:00"
            hora_fim = c["horario_fim_agendado"] or "18:00"
            scheduled_start = _converter_data_para_iso(f"{c['data_agendada']} {hora_inicio}")
            scheduled_end = _converter_data_para_iso(f"{c['data_agendada']} {hora_fim}")

            if not scheduled_start:
                logger.warning(f"  {pedido}: data '{c['data_agendada']}' não pôde ser convertida -- pulando.")
                falhas += 1
                continue

            servico = vuupt.buscar_servico_por_code(pedido)
            if not servico:
                logger.warning(f"  {pedido}: serviço não encontrado no VUUPT -- pulando.")
                falhas += 1
                continue

            if modo_teste:
                logger.info(f"  [TESTE] {pedido}: aplicaria scheduled_start={scheduled_start}, "
                           f"scheduled_end={scheduled_end} (service_id={servico['id']})")
                atualizados += 1
                continue

            try:
                vuupt.atualizar_servico(servico["id"], {
                    "scheduled_start": scheduled_start,
                    "scheduled_end": scheduled_end,
                })
                _marcar_aplicado(pedido)
                logger.info(f"  {pedido}: agendamento aplicado no VUUPT ({scheduled_start} - {scheduled_end}).")
                atualizados += 1
            except VuuptAPIError as e:
                logger.error(f"  {pedido}: falha ao atualizar no VUUPT: {e}")
                falhas += 1

        prefixo_teste = "[Teste] " if modo_teste else ""
        resumo_etapas["Atualização de agendamentos"] = {
            "status": "ok" if falhas == 0 else "erro",
            "detalhe": f"{prefixo_teste}{atualizados} aplicado(s), {falhas} falha(s), "
                      f"de {len(confirmados)} confirmado(s) encontrado(s).",
        }

    except Exception as e:
        logger.exception(f"Erro na atualização de agendamentos confirmados: {e}")
        resumo_etapas["Atualização de agendamentos"] = {"status": "erro", "detalhe": str(e)}

    duracao = time.time() - inicio
    logger.info(f"Atualização de agendamentos confirmados finalizada em {duracao:.1f}s.")

    try:
        notificar_execucao(resumo_etapas, duracao, modo_teste, config)
    except Exception as e:
        logger.warning(f"Falha ao notificar execução (não afeta o resultado): {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Aplica no VUUPT os agendamentos já confirmados por e-mail")
    parser.add_argument("--modo-teste", action="store_true",
                        help="Mostra o que seria aplicado, sem chamar a API de verdade")
    args = parser.parse_args()
    main(modo_teste=args.modo_teste)
