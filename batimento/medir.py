# -*- coding: utf-8 -*-
"""
batimento/medir.py

Primeiro passo do batimento (DOC_EXECUCAO_CLAUDE_BATIMENTO_PEDIDOS.md):
medicao SO LEITURA, rodada a mao na VPS, que lista TODOS os pedidos da
Stokki desde a data de corte, cruza com a Vuupt e com o banco e classifica
cada um em DESTINO / EM_ANDAMENTO / DIVERGENCIA. Sai um xlsx em
dados/batimento_medicao_<data>.xlsx com a conta fechada e um pedido por
linha, pra o Hugo ver o tamanho real do buraco antes de existir timer.

Nao grava em tabela nenhuma, nao escreve na Stokki nem na Vuupt.

Fontes:
  Stokki   listagem status=all ordenada por id desc, ate o id de corte
           (stokki/pedidos.py; trava stokki/sessao_uso.py obrigatoria)
  Vuupt    servicos done dos ultimos --dias-vuupt dias, com checklistAnswers
           (mesma leitura do verificar_entregues_nao_expedidos.py)
  Banco    nucleo_pedidos, nucleo_paradas/rotas, nucleo_comprovantes,
           expedicoes_processadas/falhas, pedidos_dedicados,
           insucessos_duplicados, insucessos_aguardando_resposta, vigia_pedidos

Execute (VPS, como www-data):
  sudo -u www-data venv/bin/python -m batimento.medir
  sudo -u www-data venv/bin/python -m batimento.medir --data-corte 2026-09-28 --dias-vuupt 30
  --id-minimo N  forca o id da Stokki a partir do qual listar (quando o
                 nucleo nao tem pedido criado desde a data de corte).
"""
import argparse
import json
import logging
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

_RAIZ = Path(__file__).resolve().parent.parent
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))

from batimento import regras  # noqa: E402
from stokki import pedidos as stokki_pedidos  # noqa: E402
from stokki import sessao_uso  # noqa: E402
from stokki.auth import StokkiSession  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("batimento.medir")

CONFIG_PATH = _RAIZ / "config.yaml"
DB_PATH = _RAIZ / "dados" / "dados.db"
TRANSPORTADORAS = _RAIZ / "dados" / "BD_TRANSPORTADORAS.xlsx"
VUUPT_BASE = "https://api.vuupt.com/api/v1/services"

DATA_CORTE_PADRAO = date(2026, 9, 28)   # Hugo, 04/10: "ultima semana"
DONO_TRAVA = "batimento-medicao"

_RE_SUFIXO = re.compile(r"-[RC]\d+$")
_RE_PS = re.compile(r"PS-(\d+)")
_RE_CNPJ = re.compile(r"\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}")


def _limpar(html) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(html or ""))).strip()


def base_do_codigo(codigo) -> str:
    """'#PS-1-R2' -> 'PS-1'. Reentrega e o mesmo pedido-base."""
    return _RE_SUFIXO.sub("", str(codigo or "").strip().lstrip("#").upper())


def numero_ps(codigo) -> int | None:
    m = _RE_PS.search(str(codigo or "").upper())
    return int(m.group(1)) if m else None


def _parse_utc(texto):
    if not texto:
        return None
    t = str(texto).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ── Corte ─────────────────────────────────────────────────────────────────────

def id_de_corte(conn: sqlite3.Connection, data_corte: date) -> int | None:
    """Maior numero PS que o nucleo viu nascer na Vuupt ANTES da data de
    corte, mais um. O id da Stokki e crescente, entao todo pedido acima dele
    foi lancado depois. O menor PS criado DESDE o corte nao serve: pedido
    antigo reimportado (ex.: PS-31190 criado na Vuupt em 29/09) puxa o
    corte pra semanas atras (medicao real de 04/10)."""
    rows = conn.execute("""
        SELECT codigo FROM nucleo_pedidos
        WHERE criado_em_provedor < ? AND codigo GLOB 'PS-*'
    """, (data_corte.strftime("%Y-%m-%d 00:00:00"),)).fetchall()
    numeros = [n for n in (numero_ps(r[0]) for r in rows) if n]
    return max(numeros) + 1 if numeros else None


# ── Stokki ────────────────────────────────────────────────────────────────────

# Medicao real de 04/10: na tabela, state="" traz so os status em aberto
# ("all" voltou sem linhas); expedido e cancelado tem filtro proprio
# (Sent=Enviado, Canceled=Cancelado).
STATUS_LISTAGEM = ("", "Sent", "Canceled")


def _iterar_desc(sessao: StokkiSession, status: str, id_minimo: int, por_pagina: int = 100):
    """Um status, id mais novo primeiro, ate passar de id_minimo.
    iterar_todos_pedidos nao ordena, entao pagina listar_pedidos direto."""
    pagina = 0
    while True:
        dados = stokki_pedidos.listar_pedidos(sessao, status=status, pagina=pagina, por_pagina=por_pagina,
                                              ordenar_coluna="1", ordenar_dir="desc")
        linhas = dados.get("aaData") or []
        if pagina == 0:
            total = dados.get("recordsFiltered") or dados.get("iTotalDisplayRecords") or 0
            logger.info(f"Stokki tabela state={status!r}: total filtrado {total}")
        if not linhas:
            return
        for linha in linhas:
            id_stokki = stokki_pedidos.extrair_id_da_linha(linha)
            if id_stokki is not None and id_stokki < id_minimo:
                return
            yield linha
        pagina += 1
        time.sleep(0.5)


def listar_stokki(sessao: StokkiSession, id_minimo: int) -> tuple[list[dict], dict]:
    """Todos os status, do mais novo pro mais velho, ate chegar em id_minimo.
    Devolve (linhas, contagem_por_status)."""
    contagem = {}
    try:
        contagem = stokki_pedidos.contar_pedidos(sessao, stokki_pedidos.STATUS_TODOS)
        logger.info(f"Stokki contagem por status: {json.dumps(contagem, ensure_ascii=False)}")
    except Exception as e:  # noqa: BLE001 -- a contagem e informativa
        logger.warning(f"Stokki: contar_pedidos falhou: {e}")

    linhas, vistos, ids = [], 0, set()
    for linha in (l for st in STATUS_LISTAGEM for l in _iterar_desc(sessao, st, id_minimo)):
        vistos += 1
        id_stokki = stokki_pedidos.extrair_id_da_linha(linha)
        if id_stokki is None or id_stokki in ids:
            continue
        ids.add(id_stokki)
        d = linha if isinstance(linha, dict) else {}
        linhas.append({
            "codigo": f"PS-{id_stokki}",
            "id_stokki": id_stokki,
            "embarcador": re.sub(r"#stkkc-\d+", "", _limpar(d.get("client"))).strip(),
            "transportadora": _limpar(d.get("carrier")),
            "destinatario": _limpar(d.get("destination"))[:120],
            "tipo": _limpar(d.get("type")),
            "data_saida": _limpar(d.get("expedition_date")),
            "status_stokki_bruto": _limpar(d.get("state")),
            "marcador": _limpar(d.get("marker")),
        })
    logger.info(f"Stokki: {len(linhas)} pedido(s) com id >= {id_minimo} ({vistos} linha(s) lidas).")
    return linhas, contagem


# ── Vuupt ─────────────────────────────────────────────────────────────────────

def listar_vuupt_done(token: str, dias: int) -> dict[str, dict]:
    """{codigo_base: {sucesso, insucesso, images_quantity, validated_at,
    completed_at, codes}} dos servicos done dos ultimos `dias`."""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    limite = datetime.now(timezone.utc) - timedelta(days=dias)
    por_base: dict[str, dict] = {}
    page = 1
    while True:
        for tentativa in range(5):
            resp = requests.get(VUUPT_BASE, headers=headers, timeout=60,
                                params={"page": page, "per_page": 100, "sort": "-completed_at",
                                        "include": "checklistAnswers"})
            if resp.status_code != 429:
                break
            espera = int(resp.headers.get("Retry-After") or 30 * (tentativa + 1))
            logger.warning(f"Vuupt 429 na pagina {page}; esperando {espera}s.")
            time.sleep(espera)
        resp.raise_for_status()
        body = resp.json()
        registros = body.get("data", [])
        if not registros:
            break
        parar = False
        for s in registros:
            dt = _parse_utc(s.get("completed_at"))
            if dt and dt < limite:
                parar = True
                break
            code = (s.get("code") or "").strip()
            if s.get("status") != "done" or not _RE_PS.search(code.upper()):
                continue
            base = base_do_codigo(code)
            cl = ((s.get("checklistAnswers") or {}).get("data") or [None])[0] or {}
            e = por_base.setdefault(base, {"sucesso": False, "insucesso": False, "images_quantity": 0,
                                           "validated_at": None, "completed_at": None, "codes": []})
            e["codes"].append(code.lstrip("#").upper())
            if s.get("status_done") == "success":
                e["sucesso"] = True
                e["images_quantity"] = max(e["images_quantity"], int(cl.get("images_quantity") or 0))
                e["validated_at"] = e["validated_at"] or cl.get("validated_at")
                if not e["completed_at"] or (s.get("completed_at") or "") > e["completed_at"]:
                    e["completed_at"] = s.get("completed_at")
            elif s.get("status_done") == "failed":
                e["insucesso"] = True
        if parar:
            break
        pag = body.get("meta", {}).get("pagination", {})
        if page >= pag.get("total_pages", page):
            break
        page += 1
        time.sleep(0.5)
    logger.info(f"Vuupt: {len(por_base)} pedido(s)-base com servico done em {dias} dias.")
    return por_base


# ── Banco ─────────────────────────────────────────────────────────────────────

def _tabela_existe(conn, nome) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (nome,)).fetchone() is not None


def ler_banco(conn: sqlite3.Connection, bases: set[str], agent_lalamove: int) -> dict[str, dict]:
    """Junta, por pedido-base, tudo que o banco sabe. Toda tabela e opcional
    (banco local pode nao ter todas)."""
    conn.row_factory = sqlite3.Row
    por_base: dict[str, dict] = {b: {} for b in bases}

    def pega(codigo):
        b = base_do_codigo(codigo)
        return por_base.get(b)

    # nucleo_pedidos: status, fluxo, servico vivo
    for r in conn.execute("""
            SELECT codigo, status, fluxo, status_provedor, status_done_provedor, excluido_em,
                   vuupt_route_id, qtd_checklists, criado_em_provedor
            FROM nucleo_pedidos WHERE codigo GLOB 'PS-*'"""):
        e = pega(r["codigo"])
        if e is None:
            continue
        e["servico_ja_existiu"] = True
        vivo = r["status"] != "CANCELADO" and not r["excluido_em"]
        e["servico_vivo"] = e.get("servico_vivo", False) or vivo
        # o status "mais recente" vale: a reentrega (-R1) manda sobre o original
        ordem = {"EM_ROTA": 4, "ABERTO": 3, "INSUCESSO": 2, "ENTREGUE": 5, "CANCELADO": 1}
        if "nucleo_status" not in e or (vivo and ordem.get(r["status"], 0) >= ordem.get(e["nucleo_status"], 0)):
            e["nucleo_status"] = r["status"]
        if r["status"] == "ENTREGUE":
            e["nucleo_entregue"] = True
        e["nucleo_fluxo"] = r["fluxo"] or e.get("nucleo_fluxo")
        e["nucleo_criado_em"] = min(filter(None, [e.get("nucleo_criado_em"), r["criado_em_provedor"]]), default=None)
        e.setdefault("route_ids", set())
        if r["vuupt_route_id"]:
            e["route_ids"].add(r["vuupt_route_id"])
    for e in por_base.values():
        if e.get("nucleo_entregue"):
            e["nucleo_status"] = "ENTREGUE"

    # paradas + rotas: entregue no app, rota Lalamove
    if _tabela_existe(conn, "nucleo_paradas"):
        for r in conn.execute("""
                SELECT p.id AS parada_id, p.codigo, p.situacao, r.motorista_nome, r.agent_id, r.provedor
                FROM nucleo_paradas p JOIN nucleo_rotas r ON r.id = p.rota_id
                WHERE p.codigo GLOB 'PS-*'"""):
            e = pega(r["codigo"])
            if e is None:
                continue
            e.setdefault("paradas", []).append(r["parada_id"])
            if r["situacao"] == "ENTREGUE":
                e["parada_entregue"] = True
            nome = (r["motorista_nome"] or "").strip().upper()
            if (agent_lalamove and r["agent_id"] == agent_lalamove) or nome.startswith("LALAMOVE"):
                e["lalamove"] = True
            if r["provedor"] == "APP":
                e["rota_app"] = True

    # comprovantes do app
    if _tabela_existe(conn, "nucleo_comprovantes"):
        for r in conn.execute("""
                SELECT p.codigo, c.tipo, c.resultado_validacao
                FROM nucleo_comprovantes c JOIN nucleo_paradas p ON p.id = c.parada_id
                WHERE c.tipo IN ('CANHOTO', 'ASSINATURA')"""):
            e = pega(r["codigo"])
            if e is not None:
                e["comprovante_app"] = True
                if r["resultado_validacao"] == "APROVADO":
                    e["comprovante_app_validado"] = True

    # expedicao
    if _tabela_existe(conn, "expedicoes_processadas"):
        for r in conn.execute("SELECT codigo_ps, canhoto_anexado, processado_em FROM expedicoes_processadas"):
            e = pega(r["codigo_ps"])
            if e is not None:
                e["expedido_por_nos"] = True
                e["expedicao_anexou_canhoto"] = e.get("expedicao_anexou_canhoto", False) or bool(r["canhoto_anexado"])
                e["expedido_em"] = r["processado_em"]
    if _tabela_existe(conn, "expedicoes_falhas"):
        for r in conn.execute("SELECT codigo_ps, motivo, tentativas FROM expedicoes_falhas"):
            e = pega(r["codigo_ps"])
            if e is not None:
                e["expedicao_falha"] = f"{r['tentativas']}x: {r['motivo']}"

    # dedicados
    if _tabela_existe(conn, "pedidos_dedicados"):
        for r in conn.execute("SELECT codigo_pedido FROM pedidos_dedicados WHERE removido_em IS NULL AND codigo_pedido IS NOT NULL"):
            e = pega(r["codigo_pedido"])
            if e is not None:
                e["dedicado"] = True

    # embarcador recusou reenvio
    if _tabela_existe(conn, "insucessos_duplicados"):
        for r in conn.execute("SELECT novo_code FROM insucessos_duplicados WHERE cancelado_em IS NOT NULL"):
            e = pega(r["novo_code"])
            if e is not None:
                e["embarcador_recusou"] = True
    if _tabela_existe(conn, "insucessos_aguardando_resposta"):
        for r in conn.execute("""SELECT code, resposta_texto FROM insucessos_aguardando_resposta
                                 WHERE status = 'RESPONDIDO' AND resposta_texto LIKE '%cancelar%'"""):
            e = pega(r["code"])
            if e is not None:
                e["embarcador_recusou"] = True

    # vigia
    if _tabela_existe(conn, "vigia_pedidos"):
        for r in conn.execute("SELECT codigo, estado, vencido FROM vigia_pedidos"):
            e = pega(r["codigo"])
            if e is not None:
                e["vigia_estado"] = r["estado"] + (" (VENCIDO)" if r["vencido"] else "")
    return por_base


# ── Transportadoras ───────────────────────────────────────────────────────────

def carregar_catalogo():
    try:
        from regras.transportadoras import CatalogoTransportadoras
        return CatalogoTransportadoras.carregar(TRANSPORTADORAS)
    except Exception as e:  # noqa: BLE001 -- sem planilha a medicao segue, sem tipo
        logger.warning(f"BD_TRANSPORTADORAS indisponivel ({e}); tipo da transportadora fica vazio.")
        return None


def tipo_transportadora(catalogo, nome: str) -> str | None:
    if not catalogo or not nome or nome.lower().startswith("não informado") or nome.lower().startswith("nao informado"):
        return None
    # A Stokki manda "TRANSFRIOS TRANSPORTES LTDA 80.654.387/0003-09": o nome
    # com o CNPJ colado nao casa no catalogo (medicao real de 04/10).
    m = _RE_CNPJ.search(nome)
    cnpj = re.sub(r"\D", "", m.group(0)) if m else ""
    try:
        return catalogo.resolver(_RE_CNPJ.sub("", nome).strip(), cnpj=cnpj).tipo
    except Exception:  # noqa: BLE001
        return None


# ── Montagem do fato e classificacao ─────────────────────────────────────────

def montar_fato(linha: dict, banco: dict, vuupt: dict, catalogo) -> dict:
    v = vuupt or {}
    if banco.get("comprovante_app"):
        canhoto_fonte, validado = "app", bool(banco.get("comprovante_app_validado"))
    elif v.get("images_quantity"):
        canhoto_fonte, validado = "vuupt_foto", bool(v.get("validated_at"))
    elif banco.get("expedicao_anexou_canhoto"):
        canhoto_fonte, validado = "expedicao_anexou", False
    else:
        canhoto_fonte, validado = None, False
    return {
        **linha,
        "transportadora_tipo": tipo_transportadora(catalogo, linha["transportadora"]),
        "nucleo_status": banco.get("nucleo_status"),
        "nucleo_fluxo": banco.get("nucleo_fluxo"),
        "servico_vivo": bool(banco.get("servico_vivo")),
        "servico_ja_existiu": bool(banco.get("servico_ja_existiu")),
        "vuupt_sucesso": bool(v.get("sucesso")) or bool(banco.get("parada_entregue")),
        "vuupt_insucesso": bool(v.get("insucesso")),
        "vuupt_completed_at": v.get("completed_at"),
        "canhoto_fonte": canhoto_fonte,
        "canhoto_validado": validado,
        "comprovante_redespacho": False,   # nao existe captura hoje (opcao A do Hugo)
        "comprovante_retirada": False,
        "comprovante_lalamove": False,     # pod_image da Lalamove nao e guardada
        "dedicado": bool(banco.get("dedicado")),
        "lalamove": bool(banco.get("lalamove")),
        "embarcador_recusou": bool(banco.get("embarcador_recusou")),
        "vigia_estado": banco.get("vigia_estado"),
        "expedido_por_nos": bool(banco.get("expedido_por_nos")),
        "expedido_em": banco.get("expedido_em"),
        "expedicao_falha": banco.get("expedicao_falha"),
        "nucleo_criado_em": banco.get("nucleo_criado_em"),
    }


def medir(linhas: list[dict], banco: dict[str, dict], vuupt: dict[str, dict], catalogo) -> tuple[list[dict], dict]:
    resultado, resumo = [], {"lancados": 0, regras.DESTINO: {}, regras.EM_ANDAMENTO: {}, regras.DIVERGENCIA: {},
                             "sem_classificar": 0}
    for linha in linhas:
        base = linha["codigo"]
        fato = montar_fato(linha, banco.get(base, {}), vuupt.get(base), catalogo)
        try:
            caixa, rotulo, ev = regras.classificar(fato)
        except Exception as e:  # noqa: BLE001 -- regra que estoura e furo na conta, nao derruba a medicao
            caixa, rotulo, ev = "SEM_CLASSIFICAR", type(e).__name__, [str(e)]
            resumo["sem_classificar"] += 1
        resumo["lancados"] += 1
        if caixa in resumo and isinstance(resumo[caixa], dict):
            resumo[caixa][rotulo] = resumo[caixa].get(rotulo, 0) + 1
        resultado.append({**fato, "caixa": caixa, "rotulo": rotulo, "evidencias": " | ".join(ev)})
    soma = sum(sum(v.values()) for k, v in resumo.items() if isinstance(v, dict)) + resumo["sem_classificar"]
    resumo["equacao_fecha"] = soma == resumo["lancados"]
    return resultado, resumo


# ── Saida ─────────────────────────────────────────────────────────────────────

COLUNAS = ["codigo", "embarcador", "destinatario", "transportadora", "transportadora_tipo", "data_saida",
           "status_stokki_bruto", "caixa", "rotulo", "evidencias", "nucleo_status", "nucleo_fluxo",
           "vigia_estado", "vuupt_completed_at", "canhoto_fonte", "canhoto_validado", "dedicado", "lalamove",
           "embarcador_recusou", "expedido_por_nos", "expedido_em", "expedicao_falha", "nucleo_criado_em",
           "marcador", "id_stokki"]


def gravar_xlsx(caminho: Path, linhas: list[dict], resumo: dict, contagem_stokki: dict, parametros: dict) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = "Resumo"
    ws.append(["Batimento de pedidos - medicao"])
    ws["A1"].font = Font(bold=True, size=13)
    for k, v in parametros.items():
        ws.append([k, str(v)])
    ws.append([])
    ws.append(["Lancados na Stokki", resumo["lancados"]])
    for caixa in (regras.DESTINO, regras.EM_ANDAMENTO, regras.DIVERGENCIA):
        total = sum(resumo[caixa].values())
        ws.append([caixa, total])
        ws.cell(row=ws.max_row, column=1).font = Font(bold=True)
        for rotulo, n in sorted(resumo[caixa].items(), key=lambda kv: -kv[1]):
            ws.append([f"    {rotulo}", n])
    ws.append(["SEM_CLASSIFICAR (erro de regra)", resumo["sem_classificar"]])
    ws.append(["Equacao fecha?", "SIM" if resumo["equacao_fecha"] else "NAO"])
    ws.column_dimensions["A"].width = 44
    ws.column_dimensions["B"].width = 24

    ws2 = wb.create_sheet("Pedidos")
    ws2.append(COLUNAS)
    for c in ws2[1]:
        c.font = Font(bold=True)
    for l in linhas:
        ws2.append([_celula(l.get(c)) for c in COLUNAS])
    ws2.freeze_panes = "B2"
    ws2.auto_filter.ref = ws2.dimensions

    ws3 = wb.create_sheet("Status Stokki")
    ws3.append(["status (como a Stokki devolve)", "quantidade"])
    if isinstance(contagem_stokki, dict):
        for k, v in contagem_stokki.items():
            ws3.append([str(k), _celula(v)])
    else:
        ws3.append([json.dumps(contagem_stokki, ensure_ascii=False), ""])

    caminho.parent.mkdir(parents=True, exist_ok=True)
    wb.save(caminho)


def _celula(v):
    if isinstance(v, (set, list, tuple)):
        return ", ".join(str(x) for x in v)
    if isinstance(v, (dict,)):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, bool):
        return "sim" if v else ""
    return v


# ── Main ──────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Medicao so leitura do batimento de pedidos.")
    ap.add_argument("--data-corte", default=DATA_CORTE_PADRAO.isoformat(), help="YYYY-MM-DD (padrao 2026-09-28)")
    ap.add_argument("--id-minimo", type=int, default=None, help="id da Stokki a partir do qual listar")
    ap.add_argument("--dias-vuupt", type=int, default=30)
    ap.add_argument("--saida", default=None, help="caminho do xlsx (padrao dados/batimento_medicao_<hoje>.xlsx)")
    ap.add_argument("--esperar-stokki", type=int, default=1800, help="segundos esperando a vez na Stokki")
    args = ap.parse_args(argv)

    data_corte = date.fromisoformat(args.data_corte)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    token = (config.get("vuupt_api") or {}).get("token", "")
    if not token:
        raise SystemExit("Token da Vuupt nao configurado (vuupt_api.token).")
    agent_lalamove = int((config.get("lalamove") or {}).get("agent_id_vuupt") or 0)

    conn = sqlite3.connect(DB_PATH)
    id_minimo = args.id_minimo or id_de_corte(conn, data_corte)
    if not id_minimo:
        raise SystemExit(f"Nucleo nao tem pedido criado desde {data_corte}; informe --id-minimo.")
    logger.info(f"Corte: pedidos com id Stokki >= {id_minimo} (data de corte {data_corte}).")

    if not sessao_uso.adquirir(DONO_TRAVA, ttl_segundos=1800, esperar_segundos=args.esperar_stokki):
        raise SystemExit(f"Stokki em uso por {sessao_uso.em_uso()}; tente mais tarde.")
    try:
        sessao = StokkiSession(config)
        linhas, contagem = listar_stokki(sessao, id_minimo)
    finally:
        sessao_uso.liberar(DONO_TRAVA)

    vuupt = listar_vuupt_done(token, args.dias_vuupt)
    bases = {l["codigo"] for l in linhas}
    banco = ler_banco(conn, bases, agent_lalamove)
    conn.close()
    catalogo = carregar_catalogo()

    resultado, resumo = medir(linhas, banco, vuupt, catalogo)

    logger.info(f"LANCADOS={resumo['lancados']} | "
                + " | ".join(f"{c}={sum(resumo[c].values())}" for c in (regras.DESTINO, regras.EM_ANDAMENTO, regras.DIVERGENCIA))
                + f" | sem_classificar={resumo['sem_classificar']} | equacao {'FECHA' if resumo['equacao_fecha'] else 'NAO FECHA'}")
    for caixa in (regras.DESTINO, regras.EM_ANDAMENTO, regras.DIVERGENCIA):
        for rotulo, n in sorted(resumo[caixa].items(), key=lambda kv: -kv[1]):
            logger.info(f"  {caixa:<13} {rotulo:<34} {n}")

    saida = Path(args.saida) if args.saida else _RAIZ / "dados" / f"batimento_medicao_{date.today().isoformat()}.xlsx"
    gravar_xlsx(saida, resultado, resumo, contagem,
                {"data_corte": data_corte, "id_minimo_stokki": id_minimo, "dias_vuupt": args.dias_vuupt,
                 "gerado_em": datetime.now().strftime("%Y-%m-%d %H:%M")})
    logger.info(f"Planilha: {saida}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
