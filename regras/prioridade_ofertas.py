# -*- coding: utf-8 -*-
"""
regras/prioridade_ofertas.py

Priorização dos motoristas elegíveis de uma oferta do marketplace de
rotas (pedido do Hugo, 03/09): em vez de todo elegível ver e poder
pegar a rota no mesmo instante ("quem clicar primeiro leva"), a
oferta abre em ONDAS -- a 1ª onda enxerga a rota na hora, a 2ª só
alguns minutos depois, e assim por diante, até todo mundo enxergar.
Ninguém deixa de ser elegível (a regra de "mesma oportunidade a
todos", de 22/08, continua valendo); quem tem prioridade só ganha
vantagem de TEMPO.

Ordem de prioridade (menor tupla = vê primeiro):
  1. Rodízio de SP, "leitura B" confirmada pelo Hugo em 03/09: numa
     rota FORA do Centro Expandido, o motorista cuja placa está
     restrita naquele dia vem primeiro -- essas rotas são as únicas
     que ele consegue fazer hoje, enquanto quem está com a placa
     liberada ainda pode pegar as rotas do centro. (Numa rota DENTRO
     da área, o motorista restrito já nem é elegível -- trava rígida
     de roteirizacao/alocacao_motoristas.py -- então esse critério
     não separa ninguém ali.) Mesmo espírito da ordenação por escassez
     de 20/08 (contar_motoristas_elegiveis): gastar primeiro quem
     tem menos opções.
  2. Menos rotas nos últimos 7 dias (janela curta).
  3. Menos rotas nos últimos 30 dias (janela longa).
  4. agent_id, só pra ordem estável (mesmo desempate de
     selecionar_motorista_equitativo).

Fonte da contagem histórica (contar_rotas_recentes): nucleo_rotas
(nucleo/banco.py -- espelho da Vuupt sincronizado a cada 30 min na
VPS, enxerga inclusive rota criada fora do planejamento) MAIS os
rascunhos com motorista já definido que a Vuupt ainda não conhece
(rascunhos_rota, status RASCUNHO/OFERTADA/ENVIADO com agent_id --
inclui as rotas de HOJE já alocadas/escolhidas, senão dois motoristas
com o mesmo histórico ficariam empatados mesmo que um deles já tenha
pegado uma rota hoje). Rota que existe nos dois lugares conta 1 vez
(dedup por rascunho_id/vuupt_route_id). Rota CANCELADA/DESCARTADA não
conta. Tabela ausente (banco novo, teste) = contagem zero, nunca
derruba a publicação.

Config (config.yaml, seção `marketplace_rotas`, todos opcionais):
    tamanho_onda:       3    # motoristas por onda (0 = desliga as ondas: todos veem na hora)
    intervalo_minutos:  15   # espera entre uma onda e a seguinte
    max_ondas:          3    # a última onda recebe todo o resto
    janela_curta_dias:  7
    janela_longa_dias:  30

Timestamps de liberação (`visivel_a_partir_de`) sempre em UTC, formato
'YYYY-MM-DDTHH:MM:SSZ' -- comparados como string tanto na VPS
(confirmacao_motoristas/app.py) quanto no app do motorista
(nucleo/operacao.py), então independem do fuso configurado em cada
máquina (a VPS e a máquina local nunca tiveram garantia de estar no
mesmo fuso).
"""
import logging
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from regras.preferencias_motoristas import MotoristaPreferencias

logger = logging.getLogger(__name__)

_RAIZ = Path(__file__).parent.parent
_DB_PATH = _RAIZ / "dados" / "dados.db"
# roteirizacao/ não é pacote (sem __init__.py) -- mesmo padrão de
# regras/resumo_oferta.py pra alcançar rodizio_sp.py.
sys.path.insert(0, str(_RAIZ / "roteirizacao"))

FORMATO_UTC = "%Y-%m-%dT%H:%M:%SZ"

PADRAO_CONFIG = {
    "tamanho_onda": 3,
    "intervalo_minutos": 15,
    "max_ondas": 3,
    "janela_curta_dias": 7,
    "janela_longa_dias": 30,
}


@dataclass
class ElegivelPriorizado:
    motorista: MotoristaPreferencias
    onda: int                      # 0 = vê na hora
    visivel_a_partir_de: str       # UTC, FORMATO_UTC
    rotas_curta: int               # rotas na janela curta (7d por padrão)
    rotas_longa: int               # rotas na janela longa (30d por padrão)
    prioridade_rodizio: bool       # placa restrita hoje numa rota fora do Centro Expandido
    vagas_restantes: int           # quantas rotas ele ainda pode pegar nesse dia (MAX_ROTAS_DIA - já alocadas)

    def para_json(self) -> dict:
        """Formato gravado em ofertas_rota.agent_ids_elegiveis (e
        empurrado pra VPS): mantém as 3 chaves antigas (agent_id,
        telefone_ultimos4, cpf) e acrescenta as de priorização."""
        m = self.motorista
        digitos = "".join(c for c in (m.telefone or "") if c.isdigit())
        return {
            "agent_id": m.agent_id,
            "telefone_ultimos4": digitos[-4:] if len(digitos) >= 4 else None,
            "cpf": m.cpf,
            "onda": self.onda,
            "visivel_a_partir_de": self.visivel_a_partir_de,
            "vagas_restantes": self.vagas_restantes,
            "rotas_curta": self.rotas_curta,
            "rotas_longa": self.rotas_longa,
            "prioridade_rodizio": self.prioridade_rodizio,
        }


def agora_utc() -> datetime:
    return datetime.now(timezone.utc)


def formatar_utc(momento: datetime) -> str:
    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=timezone.utc)
    return momento.astimezone(timezone.utc).strftime(FORMATO_UTC)


def agora_utc_str() -> str:
    return formatar_utc(agora_utc())


def carregar_config(config: dict | None) -> dict:
    """Mescla `config['marketplace_rotas']` (config.yaml) com os
    padrões -- valor inválido/ausente cai no padrão, nunca quebra."""
    bruto = (config or {}).get("marketplace_rotas") or {}
    saida = dict(PADRAO_CONFIG)
    for chave, padrao in PADRAO_CONFIG.items():
        try:
            valor = int(bruto.get(chave, padrao))
        except (TypeError, ValueError):
            logger.warning(f"marketplace_rotas.{chave} inválido ({bruto.get(chave)!r}) -- usando padrão {padrao}.")
            valor = padrao
        saida[chave] = max(0, valor)
    return saida


def _tem_tabela(conn: sqlite3.Connection, nome: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (nome,)).fetchone() is not None


def contar_rotas_recentes(data_alvo: date, dias: int, conn: sqlite3.Connection | None = None) -> dict[int, int]:
    """{agent_id: quantidade de rotas} na janela [data_alvo - dias, data_alvo],
    inclusive o próprio dia (ver docstring do módulo). Abre a conexão em
    dados/dados.db se `conn` não vier (teste passa a própria)."""
    inicio = (data_alvo - timedelta(days=dias)).isoformat()
    fim = data_alvo.isoformat()
    fechar = conn is None
    if conn is None:
        if not _DB_PATH.exists():
            return {}
        conn = sqlite3.connect(_DB_PATH)
    try:
        contagem: dict[int, int] = {}
        vistos_rascunho: set[int] = set()
        vistos_vuupt: set[int] = set()

        if _tem_tabela(conn, "nucleo_rotas"):
            for agent_id, rascunho_id, vuupt_route_id in conn.execute(
                "SELECT agent_id, rascunho_id, vuupt_route_id FROM nucleo_rotas "
                "WHERE agent_id IS NOT NULL AND status != 'CANCELADA' AND data_rota BETWEEN ? AND ?",
                (inicio, fim),
            ):
                contagem[agent_id] = contagem.get(agent_id, 0) + 1
                if rascunho_id is not None:
                    vistos_rascunho.add(rascunho_id)
                if vuupt_route_id is not None:
                    vistos_vuupt.add(vuupt_route_id)

        if _tem_tabela(conn, "rascunhos_rota"):
            for rid, agent_id, vuupt_route_id in conn.execute(
                "SELECT id, agent_id, vuupt_route_id FROM rascunhos_rota "
                "WHERE agent_id IS NOT NULL AND status IN ('RASCUNHO', 'OFERTADA', 'ENVIADO') "
                "AND data_alvo BETWEEN ? AND ?",
                (inicio, fim),
            ):
                if rid in vistos_rascunho or (vuupt_route_id is not None and vuupt_route_id in vistos_vuupt):
                    continue
                contagem[agent_id] = contagem.get(agent_id, 0) + 1
        return contagem
    finally:
        if fechar:
            conn.close()


def priorizar(
    elegiveis: list[MotoristaPreferencias],
    data_alvo: date,
    rota_em_area_rodizio: bool,
    contagem_alocacoes_dia: dict[int, int],
    config: dict | None = None,
    agora: datetime | None = None,
    contagem_curta: dict[int, int] | None = None,
    contagem_longa: dict[int, int] | None = None,
) -> list[ElegivelPriorizado]:
    """Ordena os elegíveis e distribui em ondas. `contagem_curta`/
    `contagem_longa` só são passadas em teste -- em produção vêm de
    contar_rotas_recentes. Retorna a lista já na ordem de prioridade."""
    from rodizio_sp import placa_restrita_no_dia

    cfg = carregar_config(config)
    agora = agora or agora_utc()
    if contagem_curta is None:
        contagem_curta = contar_rotas_recentes(data_alvo, cfg["janela_curta_dias"])
    if contagem_longa is None:
        contagem_longa = contar_rotas_recentes(data_alvo, cfg["janela_longa_dias"])

    dia_semana = data_alvo.weekday()

    def _prioridade_rodizio(m: MotoristaPreferencias) -> bool:
        return (not rota_em_area_rodizio) and placa_restrita_no_dia(m.placa, dia_semana)

    ordenados = sorted(
        elegiveis,
        key=lambda m: (
            0 if _prioridade_rodizio(m) else 1,
            contagem_curta.get(m.agent_id, 0),
            contagem_longa.get(m.agent_id, 0),
            m.agent_id,
        ),
    )

    tamanho_onda = cfg["tamanho_onda"]
    max_ondas = cfg["max_ondas"]
    ondas_ligadas = tamanho_onda > 0 and max_ondas > 1 and cfg["intervalo_minutos"] > 0

    saida = []
    for posicao, m in enumerate(ordenados):
        onda = min(posicao // tamanho_onda, max_ondas - 1) if ondas_ligadas else 0
        visivel = agora + timedelta(minutes=onda * cfg["intervalo_minutos"])
        saida.append(ElegivelPriorizado(
            motorista=m,
            onda=onda,
            visivel_a_partir_de=formatar_utc(visivel),
            rotas_curta=contagem_curta.get(m.agent_id, 0),
            rotas_longa=contagem_longa.get(m.agent_id, 0),
            prioridade_rodizio=_prioridade_rodizio(m),
            vagas_restantes=max(1, m.max_rotas_dia - contagem_alocacoes_dia.get(m.agent_id, 0)),
        ))
    return saida


def resumo_ondas(priorizados: list[ElegivelPriorizado]) -> dict[int, int]:
    """{onda: quantidade} -- pro log/retorno do endpoint de publicação."""
    resumo: dict[int, int] = {}
    for p in priorizados:
        resumo[p.onda] = resumo.get(p.onda, 0) + 1
    return dict(sorted(resumo.items()))
