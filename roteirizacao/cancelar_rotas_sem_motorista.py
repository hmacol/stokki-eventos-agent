# -*- coding: utf-8 -*-
"""
cancelar_rotas_sem_motorista.py

28/10 (Hugo): SÓ AVISA. Às 15h45 lista as rotas de dias anteriores
(últimos DIAS_ROTAS_PASSADAS dias, não encerradas) que ainda têm pedido
sem baixa e manda no grupo ALERTA LOGISTICA
(notificar_whatsapp.avisar_rotas_sem_baixa) + o resumo de sempre por
e-mail. Nenhum pedido em rota volta pro pool sozinho: só se alguém
cancelar a rota ou o pedido na mão. O nome do arquivo e da unit ficou
pelo histórico; o texto abaixo é de como era antes.

Caso que motivou (28/10): a #1 de 20/10 do Hedgar (47 pedidos, entrega
na Transfrios) foi devolvida às 15h45 de 21/10 sem ninguém ter dado baixa,
e os pedidos ficaram uma semana voltando pro rascunho.

--- Histórico ---

Rotina diária de proteção (15h45 desde 05/10, antes da rodada das 16h;
ver infra/stokki-cancelar-rotas-sem-motorista.timer) -- pedido do Hugo,
19/08, depois de achar o PS-36801 "preso" numa rota de véspera sem
motorista que nunca foi cancelada.

05/10 (Hugo): o PASSO 1 abaixo (cancelar rota de HOJE sem motorista)
FOI DESLIGADO -- rota enviada à VUUPT sem motorista não é mais
cancelada. Só roda o PASSO 2. O texto do passo 1 fica como histórico.

Por quê: enviar uma rota sem motorista atribuído é fluxo normal e
suportado (rotas_client.criar_rota permite agent_id=None, pra
atribuição manual depois -- 01/08). O problema é quando NINGUÉM atribui
motorista até o fim do dia: a rota fica esquecida na VUUPT pra sempre.
Os pedidos dela aparecem como "not_assigned" no pool (parecem
disponíveis), mas a VUUPT recusa colocá-los em qualquer rota nova
("já está em outra rota", dado desatualizado -- ver
rotas_client.criar_rota_removendo_conflitos) porque o service ainda
referencia o route_id antigo por baixo do status. Sem essa rotina, os
pedidos ficam invisivelmente inutilizáveis até alguém descobrir e
cancelar a rota manualmente.

Elegível pra cancelamento (mesmo critério de segurança de
reprocessar_rotas.py, restrito a HOJE em vez de hoje+futuro):
  - status != "canceled" (já cancelada, nada a fazer)
  - start_at é HOJE (rota de dia futuro pode ainda ganhar motorista
    até lá -- não é candidata ainda)
  - agent_id é None (rota com motorista atribuído NUNCA é tocada,
    mesmo que o motorista ainda não tenha aceitado/iniciado)

cancelar_rota() sempre com services_action="unassign" -- pedidos
voltam pra not_assigned DE VERDADE (limpa o route_id de referência),
liberando pro pool/roteirização normal do dia seguinte. Se a rota
cancelada tinha um rascunho local (enviada pelo painel de
planejamento), reverter_por_vuupt_route_id sincroniza o rascunho de
volta pra RASCUNHO -- sem isso ele ficaria "ENVIADO fantasma" na tela,
apontando pra uma rota que não existe mais.

PASSO 2 (Hugo, 28/09: "devolve antes da rodada das 18h"): pedido preso
em rota de DIA ANTERIOR que nunca terminou volta pro pool. Olha as rotas
dos últimos DIAS_ROTAS_PASSADAS dias (antes de hoje) e, em cada uma:
  - serviço ainda não iniciado (not_assigned/assigned/accepted) sai da
    rota -- é pedido que nunca saiu do galpão;
  - serviço INICIADO (on_route/arrived) NÃO é mexido: o motorista foi até
    o cliente e pode ter entregue sem dar baixa. Vai pro resumo como
    alerta, pra alguém conferir canhoto/motorista.
Se tudo o que sobrou na rota é devolvível e nada foi concluído, cancela a
rota com services_action=unassign (mesmo caminho do passo 1); senão
reescreve a rota só com o que fica (mesmo PUT do "Excluir da Rota" da
Torre). Rota 'finished'/'canceled' não é tocada.

08/10 (Hugo): rota de motorista SEM_APP (iPhone, coluna do BD_MOTORISTAS)
NÃO é devolvida -- ele fez a rota mas nada foi registrado. Vai pro resumo
como "aguardando baixa"; a baixa é pela Torre (nucleo/baixa_sem_app.py).

COMO USAR:
    py -3.11 roteirizacao/cancelar_rotas_sem_motorista.py                # execução normal (avisa)
    py -3.11 roteirizacao/cancelar_rotas_sem_motorista.py --modo-teste   # só loga o aviso, não envia
"""
import argparse
import logging
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

_RAIZ_LOCAL = Path(__file__).parent
_RAIZ_PROJETO = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ_PROJETO))
sys.path.insert(0, str(_RAIZ_LOCAL))
sys.path.insert(0, str(_RAIZ_PROJETO / "painel_agentes"))
(_RAIZ_LOCAL / "dados").mkdir(parents=True, exist_ok=True)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_RAIZ_LOCAL / "dados" / "cancelar_rotas_sem_motorista.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("cancelar_rotas_sem_motorista")

import yaml

from notificar_execucao_agente import notificar_execucao
from rotas_client import listar_rotas
from mapa_util import extrair_servicos_da_rota
from nucleo.sincronizar_vuupt import _nomes_motoristas

DIAS_ROTAS_PASSADAS = 14
STATUS_SERVICO_FECHADO = {"done", "canceled", "cancelled"}
STATUS_SERVICO_INICIADO = {"on_route", "arrived", "in_progress", "started"}
STATUS_ROTA_ENCERRADA = {"finished", "canceled"}

PADRAO_DATA_START_AT = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
PADRAO_NUMERO_ROTA = re.compile(r"#\s*\d+")


def _carregar_config() -> dict:
    with open(_RAIZ_PROJETO / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _data_inicio_rota(rota: dict) -> date | None:
    """Extrai AAAA-MM-DD de start_at. None se ausente/irreconhecível --
    nesse caso a rota NÃO é candidata (não dá pra confirmar que é hoje
    sem essa data)."""
    start_at = rota.get("start_at")
    if not start_at:
        return None
    match = PADRAO_DATA_START_AT.search(str(start_at))
    if not match:
        return None
    ano, mes, dia = match.groups()
    try:
        return date(int(ano), int(mes), int(dia))
    except ValueError:
        return None


def rotas_sem_baixa(rotas: list[dict], hoje: date, nomes: dict[int, str]) -> list[dict]:
    """Rotas de dia ANTERIOR, não encerradas, com pedido ainda aberto (nem
    done nem cancelado). Não mexe em nada: é só a lista do aviso.
    Cada item: {"rota_id", "numero", "nome", "data", "motorista",
    "pendentes", "iniciados"}; ordenado por data e número."""
    resultado = []
    for rota in rotas:
        data_rota = _data_inicio_rota(rota)
        if data_rota is None or data_rota >= hoje or rota.get("status") in STATUS_ROTA_ENCERRADA:
            continue
        abertos = [s for s in extrair_servicos_da_rota(rota)
                   if s.get("id") and (s.get("status") or "") not in STATUS_SERVICO_FECHADO]
        if not abertos:
            continue
        nome = rota.get("name") or f"rota {rota.get('id')}"
        numero = PADRAO_NUMERO_ROTA.search(nome)
        resultado.append({
            "rota_id": rota.get("id"), "numero": numero.group(0).replace(" ", "") if numero else nome,
            "nome": nome, "data": data_rota, "motorista": nomes.get(rota.get("agent_id")),
            "pendentes": len(abertos),
            "iniciados": sum(1 for s in abertos if s.get("status") in STATUS_SERVICO_INICIADO),
        })

    def _ordem(item):
        n = re.search(r"\d+", item["numero"])
        return item["data"], int(n.group(0)) if n else 0, item["nome"]
    return sorted(resultado, key=_ordem)


def main(modo_teste: bool = False, hoje: date | None = None):
    inicio = time.time()
    prefixo = "[MODO TESTE] " if modo_teste else ""
    hoje = hoje or date.today()
    logger.info(f"{prefixo}Aviso de rotas sem baixa iniciado.")

    config = _carregar_config()
    token = config.get("vuupt_api", {}).get("token", "")
    resumo_etapas = {}
    etapa = "Pedidos sem baixa em rotas de dias anteriores"

    try:
        if not token:
            raise RuntimeError("config.yaml sem vuupt_api.token.")
        filtro = [
            {"field": "start_at", "operator": "gte",
             "value": (hoje - timedelta(days=DIAS_ROTAS_PASSADAS)).strftime("%Y-%m-%d") + " 00:00:00"},
            {"field": "start_at", "operator": "lt", "value": hoje.strftime("%Y-%m-%d") + " 00:00:00"},
        ]
        rotas = rotas_sem_baixa(listar_rotas(token, include=["services"], filtro=filtro), hoje,
                                _nomes_motoristas(config))
        for r in rotas:
            logger.info(f"  {r['nome']} ({r['data']:%d/%m}, {r['motorista'] or 'sem motorista'}): "
                        f"{r['pendentes']} pedido(s) sem baixa, {r['iniciados']} em andamento.")
        detalhe = (f"{prefixo}{len(rotas)} rota(s) de dias anteriores com pedido sem baixa -- nada devolvido "
                   f"ao pool (só cancelando na mão)")
        if rotas:
            detalhe += ": " + "; ".join(f"{r['nome']} ({r['motorista'] or 'sem motorista'}, "
                                        f"{r['pendentes']} pedido(s))" for r in rotas)
        from notificar_whatsapp import avisar_rotas_sem_baixa
        situacao = avisar_rotas_sem_baixa(rotas, config, modo_teste=modo_teste)
        resumo_etapas[etapa] = {"status": "ok", "detalhe": f"{detalhe}. WhatsApp: {situacao}."}
    except Exception as e:
        logger.exception(f"Erro ao levantar rotas sem baixa: {e}")
        resumo_etapas[etapa] = {"status": "erro", "detalhe": str(e)}

    duracao = time.time() - inicio
    logger.info(f"{prefixo}Aviso de rotas sem baixa finalizado em {duracao:.1f}s.")

    try:
        notificar_execucao(resumo_etapas, duracao, modo_teste, config)
    except Exception as e:
        logger.warning(f"Falha ao notificar execução (não afeta o resultado): {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Avisa as rotas de dias anteriores com pedido sem baixa (não devolve nada).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--modo-teste", action="store_true",
                        help="Só loga o aviso, não manda WhatsApp.")
    args = parser.parse_args()
    main(modo_teste=args.modo_teste)
