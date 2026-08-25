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

from datetime import date

from vuupt_client import VuuptClient, VuuptAPIError, _converter_data_para_iso
from notificar_execucao_agente import notificar_execucao
from roteirizacao.regioes_dia_fixo import ajustar_data_por_dia_fixo, nomes_dias
from roteirizacao.notificar_agendamento_dia_fixo import notificar_agendamentos_dia_fixo
from email_utils import notificacoes_automaticas_ativas

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
        ajustes_dia_fixo: list[dict] = []
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

            # Regras de dia fixo de entrega (pedido do Hugo, 13/08): a
            # data confirmada por e-mail entrava às cegas -- um pedido de
            # Sorocaba (só terças) confirmado pra sexta ficava pra sexta.
            # Se cair num dia sem entrega na região/galpão, empurra pra
            # próxima data válida e avisa o remetente (mesmo e-mail do
            # agendamento por dia fixo, com data original + ajustada).
            ajuste = None
            try:
                data_original = date.fromisoformat(scheduled_start[:10])
                data_final, regra = ajustar_data_por_dia_fixo(servico, data_original)
                if regra:
                    scheduled_start = data_final.isoformat() + scheduled_start[10:]
                    if scheduled_end:
                        scheduled_end = data_final.isoformat() + scheduled_end[10:]
                    logger.info(f"  {pedido}: data confirmada {data_original.strftime('%d/%m')} cai fora "
                               f"dos dias de '{regra['nome']}' ({nomes_dias(regra['dias'])}) -- "
                               f"ajustada pra {data_final.strftime('%d/%m/%Y')}.")
                    ajuste = {"servico": servico, "regiao": regra["nome"], "dias": regra["dias"],
                              "data": data_final, "data_original": data_original}
            except Exception as e:
                logger.warning(f"  {pedido}: falha ao validar dia fixo da data confirmada "
                               f"(segue com a data original): {e}")

            if modo_teste:
                logger.info(f"  [TESTE] {pedido}: aplicaria scheduled_start={scheduled_start}, "
                           f"scheduled_end={scheduled_end} (service_id={servico['id']})")
                if ajuste:
                    ajustes_dia_fixo.append(ajuste)
                atualizados += 1
                continue

            try:
                vuupt.atualizar_servico(servico["id"], {
                    "scheduled_start": scheduled_start,
                    "scheduled_end": scheduled_end,
                })
                _marcar_aplicado(pedido)
                if ajuste:
                    ajustes_dia_fixo.append(ajuste)
                logger.info(f"  {pedido}: agendamento aplicado no VUUPT ({scheduled_start} - {scheduled_end}).")
                atualizados += 1
            except VuuptAPIError as e:
                logger.error(f"  {pedido}: falha ao atualizar no VUUPT: {e}")
                falhas += 1

        # Avisa os remetentes das datas movidas pelo dia fixo (1 e-mail
        # por remetente). Falha aqui não desfaz as atualizações.
        if ajustes_dia_fixo and notificacoes_automaticas_ativas(config):
            try:
                resultado_aviso = notificar_agendamentos_dia_fixo(
                    ajustes_dia_fixo, config.get("email", {}), modo_teste=modo_teste)
                logger.info(f"Notificação de ajuste por dia fixo: {resultado_aviso}")
            except Exception as e:
                logger.warning(f"Falha ao notificar ajustes de dia fixo: {e}")

        prefixo_teste = "[Teste] " if modo_teste else ""
        detalhe_ajustes = f", {len(ajustes_dia_fixo)} ajustado(s) pro dia fixo" if ajustes_dia_fixo else ""
        resumo_etapas["Atualização de agendamentos"] = {
            "status": "ok" if falhas == 0 else "erro",
            "detalhe": f"{prefixo_teste}{atualizados} aplicado(s), {falhas} falha(s), "
                      f"de {len(confirmados)} confirmado(s) encontrado(s){detalhe_ajustes}.",
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
