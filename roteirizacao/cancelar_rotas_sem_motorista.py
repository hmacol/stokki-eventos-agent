# -*- coding: utf-8 -*-
"""
cancelar_rotas_sem_motorista.py

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
    py -3.11 roteirizacao/cancelar_rotas_sem_motorista.py                # execução normal
    py -3.11 roteirizacao/cancelar_rotas_sem_motorista.py --modo-teste   # só lista, não cancela
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
from rotas_client import listar_rotas, cancelar_rota, atualizar_rota
from rascunhos_rota import reverter_por_vuupt_route_id
from mapa_util import extrair_servicos_da_rota
import tratativas

DIAS_ROTAS_PASSADAS = 14
STATUS_SERVICO_DEVOLVIVEL = {"not_assigned", "assigned", "accepted"}
STATUS_SERVICO_INICIADO = {"on_route", "arrived", "in_progress", "started"}
STATUS_ROTA_ENCERRADA = {"finished", "canceled"}

PADRAO_DATA_START_AT = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


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


def _mexeu_hoje(servicos: list[dict], hoje: date) -> bool:
    """Algum serviço da rota teve saída/chegada/conclusão HOJE (horário da
    Vuupt vem em UTC sem fuso -> converte pra data local)."""
    from nucleo.normalizacao import vuupt_para_local
    for s in servicos:
        for campo in ("started_at", "arrived_at", "completed_at"):
            local = vuupt_para_local(s.get(campo)) if s.get(campo) else None
            if local and local[:10] == hoje.isoformat():
                return True
    return False


def plano_devolucao(rota: dict, hoje: date | None = None) -> dict:
    """Regra pura do passo 2 pra UMA rota de dia anterior. Retorna
    {"acao": "nada"|"cancelar_rota"|"atualizar", "devolver": [serviços],
     "manter_ids": [ids na ordem], "iniciados": [codes]}.
    Rota que teve movimento HOJE ainda está rodando (rota longa, motorista
    atrasado): a carga das paradas pendentes está no caminhão -- não mexe,
    vai pro resumo como "em andamento" (revisão de 28/09)."""
    vazio = {"acao": "nada", "devolver": [], "manter_ids": [], "iniciados": []}
    if rota.get("status") in STATUS_ROTA_ENCERRADA:
        return vazio
    servicos = [s for s in extrair_servicos_da_rota(rota) if s.get("id")]
    if hoje and _mexeu_hoje(servicos, hoje):
        pendentes = [(s.get("code") or str(s["id"])).lstrip("#") for s in servicos
                     if (s.get("status") or "") in STATUS_SERVICO_DEVOLVIVEL | STATUS_SERVICO_INICIADO]
        return {**vazio, "iniciados": pendentes, "rodando_hoje": True}
    devolver = [s for s in servicos if (s.get("status") or "") in STATUS_SERVICO_DEVOLVIVEL]
    iniciados = [(s.get("code") or str(s["id"])).lstrip("#") for s in servicos
                 if (s.get("status") or "") in STATUS_SERVICO_INICIADO]
    if not devolver:
        return {**vazio, "iniciados": iniciados}
    ids_devolver = {s["id"] for s in devolver}
    manter = [s["id"] for s in servicos if s["id"] not in ids_devolver]
    return {"acao": "atualizar" if manter else "cancelar_rota", "devolver": devolver,
            "manter_ids": manter, "iniciados": iniciados}


def devolver_pendentes_de_rotas_passadas(token: str, hoje: date, modo_teste: bool,
                                         sem_app: set[int] | None = None) -> dict:
    """Passo 2 (ver docstring do módulo). Retorna o resumo pro e-mail.
    `sem_app`: agent_ids de motorista sem app (iPhone, BD_MOTORISTAS
    SEM_APP; Hugo, 08/10) -- a rota deles NUNCA é devolvida: o motorista
    fez a entrega mas nada foi registrado; a baixa é pela Torre."""
    sem_app = sem_app or set()
    prefixo = "[MODO TESTE] " if modo_teste else ""
    filtro = [
        {"field": "start_at", "operator": "gte",
         "value": (hoje - timedelta(days=DIAS_ROTAS_PASSADAS)).strftime("%Y-%m-%d") + " 00:00:00"},
        {"field": "start_at", "operator": "lt", "value": hoje.strftime("%Y-%m-%d") + " 00:00:00"},
    ]
    rotas = listar_rotas(token, include=["services"], filtro=filtro)
    resumo = {"devolvidos": [], "iniciados": [], "erros": [], "aguardando_baixa": []}
    ids_devolvidos = []
    for rota in rotas:
        data_rota = _data_inicio_rota(rota)
        if data_rota is None or data_rota >= hoje:
            continue
        if rota.get("agent_id") in sem_app:
            abertos = [s for s in extrair_servicos_da_rota(rota)
                       if s.get("id") and (s.get("status") or "") not in ("done", "canceled", "cancelled")]
            if rota.get("status") not in STATUS_ROTA_ENCERRADA and abertos:
                resumo["aguardando_baixa"].append(
                    f"{rota.get('name') or rota.get('id')} (agent {rota.get('agent_id')}, "
                    f"{data_rota:%d/%m}, {len(abertos)} pedido(s))")
            continue
        plano = plano_devolucao(rota, hoje)
        nome = rota.get("name") or f"rota {rota.get('id')}"
        rotulo = f"{nome}, ainda rodando hoje" if plano.get("rodando_hoje") else nome
        resumo["iniciados"] += [f"{c} ({rotulo})" for c in plano["iniciados"]]
        if plano["acao"] == "nada":
            continue
        codes = [(s.get("code") or str(s["id"])).lstrip("#") for s in plano["devolver"]]
        logger.info(f"{prefixo}Rota {rota.get('id')} '{nome}' ({data_rota}): devolvendo {codes} "
                    f"({plano['acao']}).")
        if modo_teste:
            resumo["devolvidos"] += codes
            continue
        try:
            if plano["acao"] == "cancelar_rota":
                cancelar_rota(token, rota["id"], services_action="unassign")
                reverter_por_vuupt_route_id(rota["id"])
            else:
                atualizar_rota(token, rota["id"], plano["manter_ids"])
        except Exception as e:
            logger.error(f"  FALHA ao devolver pedidos da rota {rota.get('id')} '{nome}': {e}")
            resumo["erros"].append(f"{nome} ({e})")
            continue
        resumo["devolvidos"] += codes
        ids_devolvidos += [s["id"] for s in plano["devolver"]]
        for s, code in zip(plano["devolver"], codes):
            try:
                tratativas.registrar_evento(
                    code, "ROTAS_PASSADAS", "DEVOLVIDO_AO_POOL", service_id=s["id"],
                    texto=f"Rota '{nome}' de {data_rota:%d/%m} não terminou -- pedido não iniciado "
                          f"voltou pro pool antes da roteirização das 16h.")
            except Exception as e:
                logger.warning(f"  Tratativa de {code} não registrada: {e}")

    # Espelho do núcleo já reflete a devolução (o pool das 16h pode ler de
    # lá). Best-effort: o timer de 15 min alcança se falhar.
    if ids_devolvidos:
        try:
            from vuupt_client import VuuptClient
            from nucleo.sincronizar_servicos_vuupt import ressincronizar_ids
            ressincronizar_ids(VuuptClient(token), ids_devolvidos)
        except Exception as e:
            logger.warning(f"Ressincronização do núcleo falhou (o timer alcança): {e}")
    return resumo


def main(modo_teste: bool = False):
    inicio = time.time()
    prefixo = "[MODO TESTE] " if modo_teste else ""
    logger.info(f"{prefixo}Devolução de pedidos de rotas passadas iniciada.")

    config = _carregar_config()
    token = config.get("vuupt_api", {}).get("token", "")
    resumo_etapas = {}

    # 05/10 (Hugo): o passo 1 (cancelar rota de HOJE sem motorista e
    # devolver os pedidos ao pool) saiu -- rota enviada à VUUPT fica,
    # mesmo sem motorista. Fica só o passo 2 (rotas de dias anteriores).
    try:
        from regras.preferencias_motoristas import agent_ids_sem_app
        sem_app = agent_ids_sem_app()
    except Exception as e:
        logger.warning(f"BD_MOTORISTAS (SEM_APP) ilegível -- segue sem exceção de motorista: {e}")
        sem_app = set()

    try:
        if not token:
            raise RuntimeError("config.yaml sem vuupt_api.token.")
        dev = devolver_pendentes_de_rotas_passadas(token, date.today(), modo_teste, sem_app)
        detalhe = f"{prefixo}{len(dev['devolvidos'])} pedido(s) devolvido(s) ao pool"
        if dev["devolvidos"]:
            detalhe += f": {', '.join(dev['devolvidos'])}"
        if dev["iniciados"]:
            detalhe += (f". CONFERIR (iniciados em rota antiga, não mexidos -- pode ter sido entregue "
                        f"sem baixa): {'; '.join(dev['iniciados'])}")
        if dev["aguardando_baixa"]:
            detalhe += (f". Rotas de motorista sem app aguardando baixa na Torre (não devolvidas): "
                        f"{'; '.join(dev['aguardando_baixa'])}")
        if dev["erros"]:
            detalhe += f". [ALERTA_DEVOLUCAO] {len(dev['erros'])} falha(s): {'; '.join(dev['erros'])}"
        resumo_etapas["Pedidos presos em rotas de dias anteriores"] = {
            "status": "erro" if dev["erros"] else "ok", "detalhe": detalhe + "."}
    except Exception as e:
        logger.exception(f"Erro ao devolver pedidos de rotas passadas: {e}")
        resumo_etapas["Pedidos presos em rotas de dias anteriores"] = {"status": "erro", "detalhe": str(e)}

    duracao = time.time() - inicio
    logger.info(f"{prefixo}Devolução de pedidos de rotas passadas finalizada em {duracao:.1f}s.")

    try:
        notificar_execucao(resumo_etapas, duracao, modo_teste, config)
    except Exception as e:
        logger.warning(f"Falha ao notificar execução (não afeta o resultado): {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Devolve ao pool os pedidos não iniciados de rotas de dias anteriores.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--modo-teste", action="store_true",
                        help="Só lista o que devolveria, não mexe em nada de verdade.")
    args = parser.parse_args()
    main(modo_teste=args.modo_teste)
