# -*- coding: utf-8 -*-
"""
nucleo/relatorios_financeiro.py

Os dois relatórios que o financeiro usa hoje (exportados da Vuupt),
gerados a partir do NÚCLEO -- Etapa 3 do DOC_EXECUCAO_CLAUDE_SAIDA_VUUPT.md.
Decisão do Hugo (16/09): **cópia fiel** primeiro, mesmas colunas, mesma
ordem, mesmo formato. Discussão sobre o que cada coluna deveria ser vem
depois; aqui o objetivo é o financeiro não sentir diferença nenhuma.

    Relatório - Rotas     (77 colunas)
    Relatório - Serviços  (84 colunas)

De onde sai cada coisa: o núcleo guarda o payload BRUTO da Vuupt em
`nucleo_rotas.dados_json` e `nucleo_paradas.dados_json` (princípio 3 do
doc do app: "nunca perder campo que ainda não sabíamos que importava") --
é isso que permite reproduzir indicadores, custos e estatísticas sem
inventar conta nenhuma. Nome de agente/veículo/zona/remetente vem do
espelho de pedidos (include=customer,sender,zone) e dos cadastros em
`nucleo_cadastros_vuupt`.

O QUE AINDA SAI VAZIO (e por quê):
  - "Qtd. de anexos" e "Qtd. de checklists": o núcleo conta comprovante do
    app (nucleo_comprovantes); pra rota da Vuupt isso está na conta dela.
  - Colunas de custo da Vuupt: são o valor configurado LÁ (R$ 25,00 fixo na
    maioria das rotas; a tarifa real do motorista mora em
    regras/tarifa_motorista.py). Saem exatamente como a Vuupt mandou.

COMO USAR:
    python nucleo/relatorios_financeiro.py --de 2026-09-01 --ate 2026-09-16
    python nucleo/relatorios_financeiro.py --de ... --ate ... --pasta dados/relatorios
    python nucleo/relatorios_financeiro.py --atualizar-cadastros
    # conferência contra a exportação da Vuupt, coluna a coluna:
    python nucleo/relatorios_financeiro.py --de ... --ate ... \
        --comparar-rotas "Relatório - Rotas - Freshlog - 210980.xlsx" \
        --comparar-servicos "Relatório - Serviços - Freshlog - 210976.xlsx"
"""
import argparse
import json
import logging
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from nucleo import banco
from nucleo.normalizacao import vuupt_para_local

logger = logging.getLogger("nucleo.relatorios_financeiro")

URL_CONSULTA_ROTA = "https://app.freshhub.com.br/painel/consulta/rota/"

SITUACAO_ROTA = {"finished": "Finalizada", "canceled": "Cancelada", "cancelled": "Cancelada",
                 "assigned": "Atribuída", "not_started": "Não iniciada", "started": "Em andamento",
                 "accepted": "Atribuída", "done": "Finalizada"}
SITUACAO_SERVICO = {"done": "Finalizado", "canceled": "Cancelado", "cancelled": "Cancelado",
                    "assigned": "Atribuído", "accepted": "Aceito", "on_route": "Em deslocamento",
                    "arrived": "No local", "not_assigned": "Não atribuído"}
TIPO_SERVICO = {"delivery": "Entrega", "pickup": "Coleta"}
# Conferido contra a exportação de 16/09 (1.211 serviços): 0=Não,
# 1=baixa precisão, 2=alta precisão, 3=dados insuficientes, nulo=vazio.
FORA_DO_RAIO = {0: "Não", 1: "Sim (baixa precisão)", 2: "Sim (alta precisão)", 3: "dados insuficientes"}


# ── Formatação (igual à exportação da Vuupt) ──────────────────────────────────

def _dt(bruto, com_segundos: bool = False) -> str:
    """Carimbo da Vuupt (UTC sem fuso) -> 'DD/MM/AAAA HH:MM[:SS]' local."""
    local = vuupt_para_local(bruto)
    if not local or len(local) < 16:
        return ""
    try:
        d = datetime.strptime(local, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ""
    return d.strftime("%d/%m/%Y %H:%M:%S" if com_segundos else "%d/%m/%Y %H:%M")


def _dt_local(valor) -> str:
    """Carimbo que JÁ está em hora local (colunas do núcleo)."""
    if not valor or len(str(valor)) < 16:
        return ""
    try:
        return datetime.strptime(str(valor)[:19], "%Y-%m-%d %H:%M:%S").strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return ""


def _num(valor, casas: int = 2) -> str:
    if valor in (None, ""):
        return ""
    try:
        return f"{float(valor):,.{casas}f}".replace(",", "@").replace(".", ",").replace("@", ".")
    except (TypeError, ValueError):
        return ""


def _km(metros) -> str:
    """A Vuupt manda metros; o relatório mostra km com 1 casa."""
    if metros in (None, ""):
        return ""
    try:
        return f"{float(metros) / 1000:.1f}".replace(".", ",")
    except (TypeError, ValueError):
        return ""


def _dur(segundos) -> str:
    """Segundos -> 'HH:MM:SS'. Passa de 24 h sem virar (o relatório mostra
    '26:47:39' em rota que ficou aberta de um dia pro outro)."""
    if segundos in (None, ""):
        return ""
    try:
        total = int(float(segundos))
    except (TypeError, ValueError):
        return ""
    sinal = "-" if total < 0 else ""
    total = abs(total)
    return f"{sinal}{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def _pct(valor) -> str:
    """Percentual. Zero sai VAZIO -- é o que a exportação da Vuupt faz
    (rota cancelada vem com todas as estatísticas em branco)."""
    if valor in (None, "") or float(valor or 0) == 0:
        return ""
    try:
        return f"{int(round(float(valor)))}%"
    except (TypeError, ValueError):
        return ""


def _dim(valor) -> str:
    """Dimensão/ocupação: zero também sai vazio na exportação da Vuupt."""
    if valor in (None, "") or float(valor or 0) == 0:
        return ""
    return _num(valor, 3)


def _dur0(valor) -> str:
    """Duração que a Vuupt SEMPRE imprime (sem valor vira 00:00:00)."""
    return _dur(valor if valor is not None else 0)


def _num0(valor) -> str:
    """Custo que a Vuupt SEMPRE imprime (sem valor vira 0,00)."""
    return _num(valor if valor is not None else 0)


def _texto(valor) -> str:
    return "" if valor is None else str(valor)


def _ler_json(bruto) -> dict:
    try:
        d = json.loads(bruto) if bruto else {}
    except (TypeError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


# ── Cadastros (usuário e veículo da Vuupt) ────────────────────────────────────

def _garantir_cadastros(conn: sqlite3.Connection):
    conn.execute("""CREATE TABLE IF NOT EXISTS nucleo_cadastros_vuupt (
                        tipo          TEXT NOT NULL,
                        id            INTEGER NOT NULL,
                        nome          TEXT,
                        dados_json    TEXT,
                        atualizado_em TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                        PRIMARY KEY (tipo, id))""")


def atualizar_cadastros(conn: sqlite3.Connection, token: str) -> dict:
    """Usuários (95) e veículos (150) da Vuupt: nome do agente e placa. São
    listas pequenas e quase paradas -- puxadas sob demanda, não a cada
    relatório."""
    import requests
    from http_retry import chamar_com_retry

    _garantir_cadastros(conn)
    cabecalhos = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    stats = {}
    for tipo, url in (("usuario", "https://api.vuupt.com/api/v1/users"),
                      ("veiculo", "https://api.vuupt.com/api/v1/vehicles")):
        pagina, total = 1, 0
        while True:
            resp = chamar_com_retry(requests.get, url, headers=cabecalhos,
                                    params={"per_page": 100, "page": pagina}, timeout=60)
            resp.raise_for_status()
            corpo = resp.json()
            for item in corpo.get("data", []):
                conn.execute("""INSERT INTO nucleo_cadastros_vuupt (tipo, id, nome, dados_json, atualizado_em)
                                VALUES (?, ?, ?, ?, ?)
                                ON CONFLICT(tipo, id) DO UPDATE SET nome = excluded.nome,
                                    dados_json = excluded.dados_json, atualizado_em = excluded.atualizado_em""",
                             (tipo, item.get("id"), item.get("name") or item.get("license_plate"),
                              json.dumps(item, ensure_ascii=False), banco.agora()))
                total += 1
            paginacao = (corpo.get("meta") or {}).get("pagination") or {}
            if pagina >= (paginacao.get("total_pages") or 1):
                break
            pagina += 1
        stats[tipo] = total
    conn.commit()
    return stats


class Cadastros:
    """Mapas id -> nome/placa, lidos uma vez por execução."""

    def __init__(self, conn: sqlite3.Connection):
        _garantir_cadastros(conn)
        self.usuarios, self.veiculos, self.placas = {}, {}, {}
        for linha in conn.execute("SELECT tipo, id, nome, dados_json FROM nucleo_cadastros_vuupt"):
            if linha["tipo"] == "usuario":
                self.usuarios[linha["id"]] = linha["nome"]
            else:
                dados = _ler_json(linha["dados_json"])
                self.veiculos[linha["id"]] = dados
                self.placas[linha["id"]] = dados.get("license_plate") or linha["nome"]
        # Placa também pela planilha de motoristas (é de onde a nossa
        # operação tira placa hoje) -- usada quando o veículo não está na
        # lista da Vuupt.
        self.motoristas = {}

    def usuario(self, id_usuario) -> str:
        return _texto(self.usuarios.get(id_usuario))

    def placa(self, vehicle_id) -> str:
        return _texto(self.placas.get(vehicle_id))

    def veiculo(self, vehicle_id) -> dict:
        return self.veiculos.get(vehicle_id) or {}


# ── Relatório de ROTAS ────────────────────────────────────────────────────────

COLUNAS_ROTAS = [
    "#", "Nome da Rota", "Origem", "Situação", "Local de início", "Local de fim", "Agente", "Veículo",
    "Criado em", "Criado por", "Cancelado em", "Cancelado por",
    "Indicadores gerais (previsão) - Início", "Indicadores gerais (previsão) - Fim",
    "Indicadores gerais (previsão) - Número de serviços", "Indicadores gerais (previsão) - Distância (km)",
    "Indicadores gerais (previsão) - Tempo", "Indicadores gerais (previsão) - Tempo de espera",
    "Indicadores gerais (previsão) - Duração", "Indicadores gerais (previsão) - Tempo em pausa",
    "Indicadores gerais (previsão) - Tempo total",
    "Indicadores gerais (realizado) - Início", "Indicadores gerais (realizado) - Fim",
    "Indicadores gerais (realizado) - Número de serviços", "Indicadores gerais (realizado) - Distância (km)",
    "Indicadores gerais (realizado) - Tempo", "Indicadores gerais (realizado) - Tempo de espera",
    "Indicadores gerais (realizado) - Duração", "Indicadores gerais (realizado) - Tempo total",
    "Dimensões (veículo) - Volume (m³)", "Dimensões (veículo) - Peso (kg)", "Dimensões (veículo) - Quantidade (Qtde)",
    "Ocupação (inicial) - Volume (m³)", "Ocupação (inicial) - Peso (kg)", "Ocupação (inicial) - Quantidade (Qtde)",
    "Ocupação (atual) - Volume (m³)", "Ocupação (atual) - Peso (kg)", "Ocupação (atual) - Quantidade (Qtde)",
    "Custos (previsão) - Por agente - Por rota", "Custos (previsão) - Por agente - Por serviço",
    "Custos (previsão) - Por agente - Por tempo", "Custos (previsão) - Por agente - Por distância",
    "Custos (previsão) - Por agente - Total",
    "Custos (previsão) - Por veículo - Por rota", "Custos (previsão) - Por veículo - Por serviço",
    "Custos (previsão) - Por veículo - Por tempo", "Custos (previsão) - Por veículo - Por distância",
    "Custos (previsão) - Por veículo - Total",
    "Custos (previsão) - Por serviço - Fixo", "Custos (previsão) - Por serviço - Por tempo",
    "Custos (previsão) - Por serviço - Total", "Custos (previsão) - Custo total",
    "Custos (realizado) - Por agente - Por rota", "Custos (realizado) - Por agente - Por serviço",
    "Custos (realizado) - Por agente - Por tempo", "Custos (realizado) - Por agente - Por distância",
    "Custos (realizado) - Por agente - Total",
    "Custos (realizado) - Por veículo - Por rota", "Custos (realizado) - Por veículo - Por serviço",
    "Custos (realizado) - Por veículo - Por tempo", "Custos (realizado) - Por veículo - Por distância",
    "Custos (realizado) - Por veículo - Total",
    "Custos (realizado) - Por serviço - Fixo", "Custos (realizado) - Por serviço - Por tempo",
    "Custos (realizado) - Por serviço - Total", "Custos (realizado) - Custo total",
    "Estatísticas - Aderência", "Estatísticas - Serviços realizados", "Estatísticas - Distância",
    "Estatísticas - Tempo de deslocamento", "Estatísticas - Tempo total", "Estatísticas - Tempo no local",
    "Estatísticas - Custo total", "Estatísticas - Volume (m³)", "Estatísticas - Peso (kg)",
    "Estatísticas - Quantidade (Qtde)", "Estatísticas - Link para mapa",
]


def linha_rota(rota: sqlite3.Row, cad: Cadastros, realizado: dict | None = None) -> dict:
    b = _ler_json(rota["dados_json"])          # payload bruto da rota na Vuupt
    r = realizado or {}                        # agregado das paradas (quando a Vuupt não fechou a rota)
    veiculo = cad.veiculo(rota["vehicle_id"])
    prev, feito = "prevision_initial_", "done_"

    def custo(prefixo: str, quem: str, parte: str) -> str:
        return _num0(b.get(f"{prefixo}cost_per_{quem}_{parte}"))

    linha = {
        "#": _texto(rota["vuupt_route_id"] or rota["id"]),
        "Nome da Rota": _texto(rota["nome"]),
        "Origem": {"manual": "Manual"}.get((b.get("source") or "").lower(), _texto(b.get("source")) or "Manual"),
        "Situação": SITUACAO_ROTA.get((rota["status_provedor"] or "").lower(), ""),
        "Local de início": "Freshlog",
        "Local de fim": "Freshlog",
        "Agente": cad.usuario(rota["agent_id"]) or _texto(rota["motorista_nome"]),
        "Veículo": cad.placa(rota["vehicle_id"]),
        "Criado em": _dt(b.get("created_at")),
        "Criado por": cad.usuario(b.get("created_by_id")),
        "Cancelado em": _dt(b.get("canceled_at")),
        "Cancelado por": cad.usuario(b.get("canceled_by_user_id")),
        "Indicadores gerais (previsão) - Início": _dt(b.get("start_at")),
        "Indicadores gerais (previsão) - Fim": _dt(b.get(f"{prev}finish_at")),
        "Indicadores gerais (previsão) - Número de serviços": _texto(b.get(f"{prev}number_services")),
        "Indicadores gerais (previsão) - Distância (km)": _km(b.get(f"{prev}trip_distance")),
        "Indicadores gerais (previsão) - Tempo": _dur0(b.get(f"{prev}trip_time")),
        "Indicadores gerais (previsão) - Tempo de espera": _dur0(b.get(f"{prev}waiting_time")),
        "Indicadores gerais (previsão) - Duração": _dur0(b.get(f"{prev}duration_time")),
        "Indicadores gerais (previsão) - Tempo em pausa": _dur0(b.get(f"{prev}break_time")),
        "Indicadores gerais (previsão) - Tempo total": _dur0(b.get(f"{prev}total_time")),
        "Indicadores gerais (realizado) - Início": _dt(b.get(f"{feito}started_at")),
        "Indicadores gerais (realizado) - Fim": _dt(b.get(f"{feito}finished_at")),
        "Indicadores gerais (realizado) - Número de serviços": _texto(b.get(f"{feito}number_services")),
        "Indicadores gerais (realizado) - Distância (km)": _km(b.get(f"{feito}trip_distance") or None),
        "Indicadores gerais (realizado) - Tempo": _dur0(b.get(f"{feito}trip_time")),
        "Indicadores gerais (realizado) - Tempo de espera": _dur0(b.get(f"{feito}waiting_time")),
        "Indicadores gerais (realizado) - Duração": _dur0(b.get(f"{feito}duration_time")),
        "Indicadores gerais (realizado) - Tempo total": _dur0(b.get(f"{feito}total_time")),
        # Veículo cadastrado imprime até o zero; sem veículo, tudo vazio.
        "Dimensões (veículo) - Volume (m³)": _num(veiculo.get("dimension_1") or 0, 3) if veiculo else "",
        "Dimensões (veículo) - Peso (kg)": _num(veiculo.get("dimension_2") or 0, 3) if veiculo else "",
        "Dimensões (veículo) - Quantidade (Qtde)": _num(veiculo.get("dimension_3") or 0, 3) if veiculo else "",
        "Ocupação (inicial) - Volume (m³)": _dim(b.get("start_dimension_1")),
        "Ocupação (inicial) - Peso (kg)": _dim(b.get("start_dimension_2")),
        "Ocupação (inicial) - Quantidade (Qtde)": _dim(b.get("start_dimension_3")),
        "Ocupação (atual) - Volume (m³)": _dim(b.get("current_dimension_1")),
        "Ocupação (atual) - Peso (kg)": _dim(b.get("current_dimension_2")),
        "Ocupação (atual) - Quantidade (Qtde)": _dim(b.get("current_dimension_3")),
        "Estatísticas - Aderência": _pct(b.get("stat_adherence")),
        "Estatísticas - Serviços realizados": _pct(b.get("stat_percentage_conclusion")),
        "Estatísticas - Distância": _pct(b.get("stat_percentage_distance")),
        "Estatísticas - Tempo de deslocamento": _pct(b.get("stat_percentage_trip_time")),
        "Estatísticas - Tempo total": _pct(b.get("stat_percentage_time_total")),
        "Estatísticas - Tempo no local": _pct(b.get("stat_percentage_time_on_place")),
        "Estatísticas - Custo total": _pct(b.get("stat_percentage_cost_total")),
        "Estatísticas - Volume (m³)": _pct(b.get("stat_percentage_dimension_1")),
        "Estatísticas - Peso (kg)": _pct(b.get("stat_percentage_dimension_2")),
        "Estatísticas - Quantidade (Qtde)": _pct(b.get("stat_percentage_dimension_3")),
        # O link da Vuupt é ASSINADO por ela (.../public-map?signature=...);
        # não dá pra reproduzir de fora. Aqui vai o link da nossa tela de
        # consulta da rota, que mostra a mesma coisa pra quem confere.
        "Estatísticas - Link para mapa": f"{URL_CONSULTA_ROTA}{rota['id']}",
    }
    for rotulo, prefixo in (("previsão", prev), ("realizado", feito)):
        for quem, partes in (("agent", ("per_route", "per_service", "per_time", "per_distance", "total")),
                             ("vehicle", ("per_route", "per_service", "per_time", "per_distance", "total")),
                             ("service", ("fixed", "per_time", "total"))):
            for parte in partes:
                nome_parte = {"per_route": "Por rota", "per_service": "Por serviço", "per_time": "Por tempo",
                              "per_distance": "Por distância", "total": "Total", "fixed": "Fixo"}[parte]
                nome_quem = {"agent": "Por agente", "vehicle": "Por veículo", "service": "Por serviço"}[quem]
                linha[f"Custos ({rotulo}) - {nome_quem} - {nome_parte}"] = custo(prefixo, quem, parte)
        linha[f"Custos ({rotulo}) - Custo total"] = _num0(b.get(f"{prefixo}cost_total"))
    return linha


def gerar_rotas(conn: sqlite3.Connection, de: str, ate: str, cad: Cadastros,
                por: str = "criacao") -> list[dict]:
    """`por='criacao'` é o recorte da exportação da Vuupt (filtro "Criado
    em"): rota montada na véspera às 22h entra no dia em que foi criada,
    não no dia em que rodou. `por='rota'` usa o dia da operação, que é o
    que a gente usa no resto do painel."""
    linhas = []
    for rota in conn.execute("""SELECT * FROM nucleo_rotas
                                WHERE data_rota BETWEEN date(?, '-3 day') AND date(?, '+3 day')
                                ORDER BY data_rota, start_at, id""", (de, ate)).fetchall():
        if "replica_de" in (rota["dados_json"] or ""):
            continue                      # rota de teste do piloto, não é operação
        agregado = conn.execute("""SELECT MIN(started_at) inicio, MAX(completed_at) fim,
                                          COUNT(*) FILTER (WHERE situacao IN ('ENTREGUE','PARCIAL','INSUCESSO')) concluidos
                                     FROM nucleo_paradas WHERE rota_id = ?""", (rota["id"],)).fetchone()
        linha = linha_rota(rota, cad, dict(agregado) if agregado else None)
        referencia = (linha["Criado em"] if por == "criacao" else "")
        dia = (f"{referencia[6:10]}-{referencia[3:5]}-{referencia[0:2]}" if referencia else rota["data_rota"])
        if de <= dia <= ate:
            linhas.append(linha)
    return linhas


# ── Relatório de SERVIÇOS ─────────────────────────────────────────────────────

COLUNAS_SERVICOS = [
    "Serviço #", "Código", "Título", "Tipo", "Código de rastreio", "Remetente", "Destinatário", "Endereço",
    "Latitude", "Longitude", "Agente", "Agendado para - Início", "Agendado para - Fim", "Situação",
    "Situação - Finalizado", "Motivo - Sem sucesso", "Fora do raio", "Rastreado em", "Criado por",
    "Atribuído por", "Cancelado por", "Qtd. de anexos", "Qtd. de checklists",
    "Rota - #", "Rota - Nome", "Rota - Situação", "Rota - Veículo",
    "Rota - Sequência prevista", "Rota - Sequência realizada",
    "Horários (previsão inicial) - Início", "Horários (previsão inicial) - Chegada",
    "Horários (previsão inicial) - Espera", "Horários (previsão inicial) - Duração",
    "Horários (previsão inicial) - Fim",
    "Horários (previsão atual) - Início", "Horários (previsão atual) - Chegada",
    "Horários (previsão atual) - Espera", "Horários (previsão atual) - Duração", "Horários (previsão atual) - Fim",
    "Horários (execução) - Criado", "Horários (execução) - Atribuído ao Agente",
    "Horários (execução) - Aceito pelo Agente", "Horários (execução) - Iniciado em",
    "Horários (execução) - Chegou ao local", "Horários (execução) - Concluído",
    "Horários (execução) - Cancelado em",
    "Estatísticas - Tempo de Atribuição", "Estatísticas - Tempo de Aceitação (Agente)",
    "Estatísticas - Tempo de Início (Agente)", "Estatísticas - Tempo de Deslocamento (Agente)",
    "Estatísticas - Tempo de Conclusão (Agente)", "Estatísticas - Distância Percorrida (km)",
    "Dimensões - Volume (m³)", "Dimensões - Peso (kg)", "Dimensões - Quantidade (Qtde)",
    "Campos extra - Ocorrências", "Campos extra - Custo Frete", "Campos extra - Custo TDE",
    "Campos extra - Custo Adicionais",
    "Custos (previsão) - Por serviço - Fixo", "Custos (previsão) - Por serviço - Por tempo",
    "Custos (previsão) - Por serviço - Total",
    "Custos (previsão) - Por veículo - Por serviço", "Custos (previsão) - Por veículo - Por tempo",
    "Custos (previsão) - Por veículo - Por distância", "Custos (previsão) - Por veículo - Total",
    "Custos (previsão) - Por agente - Por serviço", "Custos (previsão) - Por agente - Por tempo",
    "Custos (previsão) - Por agente - Por distância", "Custos (previsão) - Por agente - Total",
    "Custos (previsão) - Custo total",
    "Custos (realizado) - Por serviço - Fixo", "Custos (realizado) - Por serviço - Por tempo",
    "Custos (realizado) - Por serviço - Total",
    "Custos (realizado) - Por veículo - Por serviço", "Custos (realizado) - Por veículo - Por tempo",
    "Custos (realizado) - Por veículo - Por distância", "Custos (realizado) - Por veículo - Total",
    "Custos (realizado) - Por agente - Por serviço", "Custos (realizado) - Por agente - Por tempo",
    "Custos (realizado) - Por agente - Por distância", "Custos (realizado) - Por agente - Total",
    "Custos (realizado) - Custo total", "Zona - Nome",
]

_MOTIVOS = {}


def _motivo_texto(conn: sqlite3.Connection, failed_reason_id) -> str:
    if failed_reason_id is None:
        return ""
    if not _MOTIVOS:
        try:
            for linha in conn.execute("SELECT vuupt_failed_reason_id, motivo_texto FROM motivos_ocorrencia"):
                if linha[0] is not None:
                    _MOTIVOS[int(linha[0])] = linha[1]
        except sqlite3.Error:
            pass
    return _texto(_MOTIVOS.get(int(failed_reason_id)))


def linha_servico(pedido: sqlite3.Row, servico: dict, parada: dict, rota: sqlite3.Row | None,
                  conn: sqlite3.Connection, cad: Cadastros) -> dict:
    rs = parada.get("route_service") or {}
    em_aberto = ((rota["status_provedor"] if rota else "") or "").lower() in ("not_started", "assigned")
    linha = {
        "Serviço #": _texto(servico.get("id") or pedido["vuupt_service_id"]),
        "Código": _texto(servico.get("code") or pedido["codigo"]),
        "Título": _texto(servico.get("title") or pedido["titulo"]),
        "Tipo": TIPO_SERVICO.get((servico.get("type") or pedido["tipo"] or "").lower(), ""),
        "Código de rastreio": _texto(servico.get("tracking_code")),
        "Remetente": _texto(pedido["remetente_nome"]),
        "Destinatário": _texto(pedido["destinatario_nome"]),
        "Endereço": _texto(servico.get("address") or pedido["endereco"]),
        "Latitude": _texto(servico.get("latitude") or pedido["latitude"]),
        "Longitude": _texto(servico.get("longitude") or pedido["longitude"]),
        "Agente": _texto(cad.usuario(servico.get("driver_id") or pedido["driver_id"])
                         or (rota["motorista_nome"] if rota else "")),
        "Agendado para - Início": _dt(servico.get("scheduled_start"), com_segundos=True),
        "Agendado para - Fim": _dt(servico.get("scheduled_end"), com_segundos=True),
        "Situação": SITUACAO_SERVICO.get((servico.get("status") or "").lower(), ""),
        "Situação - Finalizado": {"success": "Sucesso", "failed": "Sem sucesso"}.get(servico.get("status_done"), ""),
        "Motivo - Sem sucesso": _motivo_texto(conn, servico.get("failed_reason_id")),
        "Fora do raio": FORA_DO_RAIO.get(servico.get("outside_radius"), ""),
        "Rastreado em": _dt(servico.get("tracked_by_customer_at"), com_segundos=True),
        "Criado por": cad.usuario(servico.get("created_by_id")),
        "Atribuído por": "",          # a exportação da Vuupt não preenche (só atribuição manual)
        "Cancelado por": cad.usuario(servico.get("canceled_by_id")),
        "Qtd. de anexos": _texto(pedido["qtd_anexos"] if pedido["qtd_anexos"] is not None else ""),
        "Qtd. de checklists": _texto(pedido["qtd_checklists"] if pedido["qtd_checklists"] is not None else ""),
        "Rota - #": _texto(servico.get("route_id") or (rota["vuupt_route_id"] if rota else "")),
        "Rota - Nome": _texto(rota["nome"] if rota else ""),
        "Rota - Situação": SITUACAO_ROTA.get(((rota["status_provedor"] if rota else "") or "").lower(), ""),
        "Rota - Veículo": cad.placa(rota["vehicle_id"]) if rota else "",
        "Rota - Sequência prevista": _texto(rs.get("prevision_sequence") or servico.get("route_sequence")),
        "Rota - Sequência realizada": _texto(rs.get("done_sequence")),
        "Horários (previsão inicial) - Início": _dt(rs.get("prevision_initial_start_at"), com_segundos=True),
        "Horários (previsão inicial) - Chegada": _dt(rs.get("prevision_initial_arrive_at"), com_segundos=True),
        "Horários (previsão inicial) - Espera": _texto(rs.get("prevision_initial_waiting_time")),
        "Horários (previsão inicial) - Duração": _texto(rs.get("prevision_initial_duration_time")),
        "Horários (previsão inicial) - Fim": _dt(rs.get("prevision_initial_end_at"), com_segundos=True),
        # "Previsão atual" só sai enquanto a rota não rodou (conferido: as 37
        # linhas preenchidas na exportação são todas de rota não iniciada ou
        # atribuída). Rota finalizada/cancelada vai em branco.
        "Horários (previsão atual) - Início": _dt(rs.get("prevision_start_at"), com_segundos=True) if em_aberto else "",
        "Horários (previsão atual) - Chegada": _dt(rs.get("prevision_arrive_at"), com_segundos=True) if em_aberto else "",
        "Horários (previsão atual) - Espera": _texto(rs.get("prevision_waiting_time")) if em_aberto else "",
        "Horários (previsão atual) - Duração": _texto(rs.get("prevision_duration_time")) if em_aberto else "",
        "Horários (previsão atual) - Fim": _dt(rs.get("prevision_end_at"), com_segundos=True) if em_aberto else "",
        "Horários (execução) - Criado": _dt(servico.get("created_at"), com_segundos=True),
        "Horários (execução) - Atribuído ao Agente": _dt(servico.get("assigned_at"), com_segundos=True),
        "Horários (execução) - Aceito pelo Agente": _dt(servico.get("accepted_at"), com_segundos=True),
        "Horários (execução) - Iniciado em": _dt(servico.get("started_at"), com_segundos=True),
        "Horários (execução) - Chegou ao local": _dt(servico.get("arrived_at"), com_segundos=True),
        "Horários (execução) - Concluído": _dt(servico.get("completed_at"), com_segundos=True),
        "Horários (execução) - Cancelado em": _dt(servico.get("canceled_at"), com_segundos=True),
        "Estatísticas - Tempo de Atribuição": _texto(servico.get("stat_time_until_assigned")),
        "Estatísticas - Tempo de Aceitação (Agente)": _texto(servico.get("stat_time_until_accepted")),
        "Estatísticas - Tempo de Início (Agente)": _texto(servico.get("stat_time_until_on_route")),
        "Estatísticas - Tempo de Deslocamento (Agente)": _texto(servico.get("stat_time_until_arrived")),
        "Estatísticas - Tempo de Conclusão (Agente)": _texto(servico.get("stat_time_until_done")),
        "Estatísticas - Distância Percorrida (km)": _texto(servico.get("route_distance")),
        "Dimensões - Volume (m³)": _num(servico.get("dimension_1") or 0, 3),
        "Dimensões - Peso (kg)": _num(servico.get("dimension_2") or 0, 3),
        "Dimensões - Quantidade (Qtde)": _num(servico.get("dimension_3") or pedido["caixas"] or 0, 3),
        "Campos extra - Ocorrências": "",
        "Campos extra - Custo Frete": "",
        "Campos extra - Custo TDE": "",
        "Campos extra - Custo Adicionais": "",
        "Zona - Nome": _texto(pedido["zona"]),
    }
    for rotulo, prefixo in (("previsão", "prevision_initial_"), ("realizado", "done_")):
        for nome_quem, quem, partes in (("Por serviço", "service", (("Fixo", "fixed"), ("Por tempo", "per_time"),
                                                                    ("Total", "total"))),
                                        ("Por veículo", "vehicle", (("Por serviço", "per_service"),
                                                                    ("Por tempo", "per_time"),
                                                                    ("Por distância", "per_distance"),
                                                                    ("Total", "total"))),
                                        ("Por agente", "agent", (("Por serviço", "per_service"),
                                                                 ("Por tempo", "per_time"),
                                                                 ("Por distância", "per_distance"),
                                                                 ("Total", "total")))):
            for nome_parte, parte in partes:
                # Só o que a VUUPT põe no PRÓPRIO serviço -- o custo da rota
                # não é rateado aqui (conferido: no relatório dela o total
                # por agente do serviço é 0,00 mesmo em rota com custo).
                chave = f"{prefixo}cost_per_{quem}_{parte}"
                linha[f"Custos ({rotulo}) - {nome_quem} - {nome_parte}"] = _num0(
                    rs.get(chave) if rs.get(chave) is not None else servico.get(chave))
        chave_total = f"{prefixo}cost_total"
        linha[f"Custos ({rotulo}) - Custo total"] = _num0(
            rs.get(chave_total) if rs.get(chave_total) is not None else servico.get(chave_total))
    return linha


def gerar_servicos(conn: sqlite3.Connection, de: str, ate: str, cad: Cadastros) -> list[dict]:
    """Uma linha por SERVIÇO criado no período -- é o recorte da exportação
    da Vuupt (filtro "Horários (execução) - Criado").

    Por SERVIÇO, não por pedido: quando o mesmo código é recriado na Vuupt
    (reimportação, reentrega), existem dois serviços e o relatório dela traz
    os dois. `nucleo_pedidos` guarda uma linha por CÓDIGO (com o serviço mais
    recente), então a lista sai da união dele com `nucleo_paradas`, que tem o
    payload de cada serviço na rota em que entrou."""
    rotas = {r["id"]: r for r in conn.execute("SELECT * FROM nucleo_rotas")}
    pedidos_por_service, pedidos_por_codigo = {}, {}
    for pedido in conn.execute("SELECT * FROM nucleo_pedidos"):
        pedidos_por_codigo[pedido["codigo"]] = pedido
        if pedido["vuupt_service_id"]:
            pedidos_por_service[pedido["vuupt_service_id"]] = pedido

    # Serviço -> melhor payload e a parada/rota em que ele está.
    servicos: dict[int, dict] = {}
    for linha in conn.execute("""SELECT p.service_id, p.dados_json, p.codigo, p.rota_id, r.vuupt_route_id, r.data_rota
                                   FROM nucleo_paradas p JOIN nucleo_rotas r ON r.id = p.rota_id
                                  WHERE p.service_id IS NOT NULL AND r.provedor = 'VUUPT'
                                  ORDER BY r.data_rota, p.id"""):
        bruto = _ler_json(linha["dados_json"])
        atual = servicos.get(linha["service_id"])
        # Rota em que o serviço está AGORA manda; senão fica a mais recente.
        if atual and bruto.get("route_id") and bruto["route_id"] != linha["vuupt_route_id"]:
            continue
        servicos[linha["service_id"]] = {"servico": bruto, "parada": bruto, "rota": rotas.get(linha["rota_id"]),
                                         "codigo": linha["codigo"]}
    for service_id, pedido in pedidos_por_service.items():
        bruto = _ler_json(pedido["dados_json"]).get("service") or {}
        if service_id in servicos:
            # O pedido tem o payload mais fresco (sync de 15 min); a parada
            # tem o route_service (sequência e previsões da rota).
            if bruto:
                servicos[service_id]["servico"] = {**servicos[service_id]["servico"], **bruto}
        elif bruto:
            servicos[service_id] = {"servico": bruto, "parada": {}, "rota": None, "codigo": pedido["codigo"]}

    linhas = []
    for service_id, dados in servicos.items():
        servico = dados["servico"]
        pedido = pedidos_por_service.get(service_id) or pedidos_por_codigo.get(dados["codigo"])
        if pedido is None:
            continue
        criado = vuupt_para_local(servico.get("created_at")) or _texto(pedido["criado_em_provedor"])
        if not criado or not (de <= criado[:10] <= ate):
            continue
        linhas.append(linha_servico(pedido, servico, dados["parada"], dados["rota"], conn, cad))
    linhas.sort(key=lambda l: (l["Horários (execução) - Criado"][6:10], l["Horários (execução) - Criado"][3:5],
                               l["Horários (execução) - Criado"][0:2], l["Horários (execução) - Criado"][11:]))
    return linhas


# ── Escrita e conferência ─────────────────────────────────────────────────────

def escrever_xlsx(caminho: Path, colunas: list[str], linhas: list[dict]):
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(colunas)
    for linha in linhas:
        ws.append([linha.get(c, "") for c in colunas])
    caminho.parent.mkdir(parents=True, exist_ok=True)
    wb.save(caminho)
    return caminho


def _ler_xlsx(caminho: Path) -> tuple[list[str], list[dict]]:
    import openpyxl

    wb = openpyxl.load_workbook(caminho, read_only=True, data_only=True)
    linhas = list(wb.active.iter_rows(values_only=True))
    wb.close()
    colunas = [str(c or "") for c in linhas[0]]
    return colunas, [{c: ("" if v is None else str(v)) for c, v in zip(colunas, r)} for r in linhas[1:]]


def comparar(caminho_vuupt: Path, colunas: list[str], nossas: list[dict], chave: str, exemplos: int = 3) -> dict:
    """Confere linha a linha contra a exportação da Vuupt, casando pela
    chave (nº da rota / nº do serviço). Devolve o resumo por coluna."""
    _, deles = _ler_xlsx(caminho_vuupt)
    por_chave_deles = {l.get(chave, ""): l for l in deles}
    por_chave_nossas = {l.get(chave, ""): l for l in nossas}
    so_deles = sorted(set(por_chave_deles) - set(por_chave_nossas))
    so_nossas = sorted(set(por_chave_nossas) - set(por_chave_deles))
    comuns = sorted(set(por_chave_deles) & set(por_chave_nossas))
    diferencas = {}
    for c in colunas:
        divergentes = []
        for k in comuns:
            a, b = (por_chave_deles[k].get(c) or "").strip(), (por_chave_nossas[k].get(c) or "").strip()
            if a != b:
                divergentes.append({"chave": k, "vuupt": a[:40], "nosso": b[:40]})
        if divergentes:
            diferencas[c] = {"linhas": len(divergentes), "exemplos": divergentes[:exemplos]}
    celulas = len(comuns) * len(colunas)
    divergentes = sum(d["linhas"] for d in diferencas.values())
    return {"celulas": celulas, "celulas_iguais": celulas - divergentes,
            "linhas_vuupt": len(deles), "linhas_nossas": len(nossas), "comuns": len(comuns),
            "so_na_vuupt": so_deles[:20], "so_nossas": so_nossas[:20],
            "colunas_iguais": len(colunas) - len(diferencas), "colunas": len(colunas), "diferencas": diferencas}


def imprimir_comparacao(titulo: str, resultado: dict, exemplos: int = 3):
    print(f"\n=== {titulo} ===")
    print(f"linhas: Vuupt {resultado['linhas_vuupt']} | nossas {resultado['linhas_nossas']} | "
          f"em comum {resultado['comuns']}")
    if resultado["so_na_vuupt"]:
        print(f"  só na Vuupt ({len(resultado['so_na_vuupt'])}): {resultado['so_na_vuupt'][:8]}")
    if resultado["so_nossas"]:
        print(f"  só nossas ({len(resultado['so_nossas'])}): {resultado['so_nossas'][:8]}")
    iguais, total = resultado["celulas_iguais"], resultado["celulas"]
    print(f"colunas idênticas: {resultado['colunas_iguais']}/{resultado['colunas']} | "
          f"células idênticas: {iguais}/{total} ({100 * iguais / total:.2f}%)" if total else "")
    for coluna, info in sorted(resultado["diferencas"].items(), key=lambda kv: -kv[1]["linhas"]):
        print(f"  {coluna[:58]:58s} {info['linhas']:5d} linha(s)")
        for ex in info["exemplos"][:exemplos]:
            print(f"      {ex['chave']}: vuupt={ex['vuupt']!r} nosso={ex['nosso']!r}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Relatórios do financeiro (cópia fiel) a partir do núcleo.")
    hoje = date.today()
    parser.add_argument("--de", default=(hoje - timedelta(days=15)).isoformat())
    parser.add_argument("--ate", default=hoje.isoformat())
    parser.add_argument("--pasta", default=str(_RAIZ / "dados" / "relatorios"))
    parser.add_argument("--atualizar-cadastros", action="store_true",
                        help="puxa usuários e veículos da Vuupt (nome do agente, placa)")
    parser.add_argument("--comparar-rotas", help="xlsx exportado da Vuupt pra conferir")
    parser.add_argument("--comparar-servicos", help="xlsx exportado da Vuupt pra conferir")
    parser.add_argument("--exemplos", type=int, default=3)
    parser.add_argument("--filtro-rotas", choices=["criacao", "rota"], default="criacao",
                        help="recorte do relatório de rotas: data de criação (como a Vuupt) ou dia da rota")
    parser.add_argument("--db")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    conn = banco.conectar(Path(args.db) if args.db else None)
    try:
        if args.atualizar_cadastros:
            import yaml
            caminho = next((c for c in (_RAIZ / "config.yaml", Path.cwd() / "config.yaml") if c.exists()), None)
            config = yaml.safe_load(caminho.read_text(encoding="utf-8")) if caminho else {}
            logger.info("Cadastros atualizados: %s", atualizar_cadastros(conn, config["vuupt_api"]["token"]))

        cad = Cadastros(conn)
        rotas = gerar_rotas(conn, args.de, args.ate, cad, por=args.filtro_rotas)
        servicos = gerar_servicos(conn, args.de, args.ate, cad)
        pasta = Path(args.pasta)
        arq_rotas = escrever_xlsx(pasta / f"Relatorio - Rotas - {args.de}_a_{args.ate}.xlsx", COLUNAS_ROTAS, rotas)
        arq_serv = escrever_xlsx(pasta / f"Relatorio - Servicos - {args.de}_a_{args.ate}.xlsx",
                                 COLUNAS_SERVICOS, servicos)
        logger.info(f"{len(rotas)} rota(s) -> {arq_rotas}")
        logger.info(f"{len(servicos)} serviço(s) -> {arq_serv}")

        if args.comparar_rotas:
            imprimir_comparacao("ROTAS", comparar(Path(args.comparar_rotas), COLUNAS_ROTAS, rotas, "#"), args.exemplos)
        if args.comparar_servicos:
            imprimir_comparacao("SERVIÇOS", comparar(Path(args.comparar_servicos), COLUNAS_SERVICOS, servicos,
                                                     "Serviço #"), args.exemplos)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
