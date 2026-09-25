# -*- coding: utf-8 -*-
"""
portal_cliente/entradas.py

Aba "Pedidos de Entrada" do portal do cliente (pedido do Hugo, 24/09/2026):
o embarcador anuncia a mercadoria que vai chegar no galpão (XML da NF-e de
remessa emitida por ele, ou planilha no modelo Fresh Log), o worker
enviar_entradas_stokki.py cria o recebimento (#PE) na Stokki pelo wizard
de XML múltiplo, o timer stokki-wms-recebimentos amarra o #PE a esta
entrada pela chave da NF-e, e o cliente acompanha até o galpão endereçar.

Spec: docs/superpowers/specs/2026-09-24-pedidos-de-entrada-portal-design.md

Tabelas (dados/dados.db):
  portal_entradas        -- uma por NF-e/remessa (chave_nfe única): dados da
                            nota, data prevista, status do ciclo, fila da
                            Stokki, ligação com wms_recebimentos.
  portal_entrada_itens   -- as linhas da nota/planilha como vieram.
  portal_clientes_envio  -- ganha entradas_ativo (só o piloto, D3).

Ciclo (portal_entradas.status), só anda pra frente:
  ANUNCIADO -> CHEGOU -> ENDERECADO | DIVERGENCIA ; CANCELADO (só a partir de ANUNCIADO)
Fila da Stokki (portal_entradas.stokki_status):
  NA_FILA -> ENVIANDO -> CRIADO | ERRO

O que este módulo NUNCA faz: escrever em wms_movimentos/wms_saldos. O
estoque entra quando o operador endereça no celular, como hoje.
"""
import hashlib
import json
import logging
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import envio_pedidos as ep  # noqa: E402

logger = logging.getLogger(__name__)

DIAS_LISTAGEM = 30
ORIGEM_XML = ep.ORIGEM_XML
ORIGEM_PLANILHA = ep.ORIGEM_PLANILHA

STATUS_ANUNCIADO = "ANUNCIADO"
STATUS_CHEGOU = "CHEGOU"
STATUS_ENDERECADO = "ENDERECADO"
STATUS_DIVERGENCIA = "DIVERGENCIA"
STATUS_CANCELADO = "CANCELADO"
STATUS_ABERTOS = (STATUS_ANUNCIADO, STATUS_CHEGOU)
ROTULOS_STATUS = {
    STATUS_ANUNCIADO: "Anunciado",
    STATUS_CHEGOU: "Chegou no galpão",
    STATUS_ENDERECADO: "Endereçado",
    STATUS_DIVERGENCIA: "Com divergência",
    STATUS_CANCELADO: "Cancelado",
}
STOKKI_NA_FILA = "NA_FILA"
STOKKI_ENVIANDO = "ENVIANDO"
STOKKI_CRIADO = "CRIADO"
STOKKI_ERRO = "ERRO"
ROTULOS_STOKKI = {
    STOKKI_NA_FILA: "Na fila pra Stokki",
    STOKKI_ENVIANDO: "Criando na Stokki",
    STOKKI_CRIADO: "Recebimento criado na Stokki",
    STOKKI_ERRO: "Erro na Stokki",
}

ErroEnvio = ep.ErroEnvio
_so_digitos = ep._so_digitos
_agora = ep._agora
formatar_documento = ep.formatar_documento


# ── Banco ──────────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS portal_entradas (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    cnpj_embarcador    TEXT NOT NULL,
    origem             TEXT NOT NULL,
    chave_nfe          TEXT NOT NULL UNIQUE,
    numero_nf          TEXT,
    serie              TEXT,
    emitida_em         TEXT,
    referencia         TEXT,
    data_prevista      TEXT NOT NULL,
    volumes            INTEGER,
    peso_kg            REAL,
    valor_nf           REAL,
    arquivo_path       TEXT NOT NULL,
    status             TEXT NOT NULL,
    stokki_status      TEXT NOT NULL DEFAULT 'NA_FILA',
    stokki_id          INTEGER,
    stokki_codigo      TEXT,
    stokki_erro        TEXT,
    stokki_tentativas  INTEGER NOT NULL DEFAULT 0,
    wms_recebimento_id INTEGER,
    observacoes        TEXT,
    enviado_por        TEXT,
    criado_em          TEXT NOT NULL,
    atualizado_em      TEXT NOT NULL,
    cancelado_em       TEXT,
    cancelado_por      TEXT,
    chamado_id         INTEGER
);
CREATE INDEX IF NOT EXISTS idx_portal_entradas_emb ON portal_entradas (cnpj_embarcador, criado_em);
CREATE INDEX IF NOT EXISTS idx_portal_entradas_status ON portal_entradas (status);
CREATE TABLE IF NOT EXISTS portal_entrada_itens (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entrada_id  INTEGER NOT NULL REFERENCES portal_entradas(id),
    linha       INTEGER NOT NULL,
    sku         TEXT NOT NULL,
    ean         TEXT NOT NULL DEFAULT '',
    descricao   TEXT NOT NULL DEFAULT '',
    quantidade  REAL NOT NULL,
    unidade     TEXT NOT NULL DEFAULT '',
    UNIQUE (entrada_id, linha)
);
"""


def garantir_tabelas(conn: sqlite3.Connection) -> None:
    """Idempotente. Chamada por conectar() e também pelo timer do WMS
    (que abre a conexão dele em wms_pedidos.conectar) antes de amarrar."""
    conn.executescript(_DDL)
    if conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'portal_clientes_envio'").fetchone():
        ep._garantir_colunas(conn, "portal_clientes_envio", {"entradas_ativo": "INTEGER NOT NULL DEFAULT 0"})
    conn.commit()


def conectar() -> sqlite3.Connection:
    """A conexão de envio_pedidos (cria portal_envios/portal_clientes_envio)
    mais as tabelas desta aba."""
    conn = ep.conectar()
    garantir_tabelas(conn)
    return conn


def _pasta_entradas() -> Path:
    # função (não constante) porque os testes trocam ep._RAIZ
    return ep._RAIZ / "dados" / "portal_entradas"


def cnpj_freshlog(config: dict | None) -> str:
    return _so_digitos(((config or {}).get("portal_entradas") or {}).get("cnpj_freshlog") or "")


# ── Leitura ────────────────────────────────────────────────────────────────────

def ler_nfe_entrada(conteudo: bytes, nome_arquivo: str = "") -> dict:
    """NF-e de remessa pra armazenagem (D1): aceita tpNF 0 ou 1; a
    validação de quem emitiu fica em validar_entrada."""
    nfe = ep.ler_nfe(conteudo, nome_arquivo, permitir_entrada=True)
    nfe["origem"] = ORIGEM_XML
    nfe["referencia"] = ""
    nfe["data_prevista"] = ""
    return nfe


def rotulo_entrada(e: dict) -> str:
    if e.get("numero_nf"):
        return f"NF {e['numero_nf']}"
    return f"Remessa {e.get('referencia') or '?'}"


def _quando_br(valor: str | None) -> str:
    if not valor:
        return ""
    try:
        return datetime.strptime(valor[:19], "%Y-%m-%d %H:%M:%S").strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return valor


def validar_entrada(conn: sqlite3.Connection, item: dict, cnpj_cliente: str, config: dict | None,
                    outras_empresas: dict[str, str] | None = None) -> dict:
    """{ok, erros[], avisos[], existente} -- as 5 regras da spec (7.1),
    menos a data prevista, que só existe na confirmação."""
    erros, avisos = [], []
    emb = _so_digitos(cnpj_cliente)
    if item.get("emitente_cnpj") != emb:
        do_grupo = (outras_empresas or {}).get(item.get("emitente_cnpj"))
        if do_grupo:
            erros.append(f"Essa nota é da {do_grupo} ({formatar_documento(item['emitente_cnpj'])}). "
                         f"Troque a empresa no seletor acima e envie de novo.")
        else:
            erros.append(f"O CNPJ emitente da nota ({formatar_documento(item.get('emitente_cnpj') or '')} · "
                         f"{item.get('emitente_nome') or ''}) não é o da sua empresa.")
    existente = conn.execute("SELECT id, status, criado_em, stokki_codigo FROM portal_entradas WHERE chave_nfe = ?",
                             (item["chave_nfe"],)).fetchone()
    if existente and existente["status"] != STATUS_CANCELADO:
        erros.append(f"{rotulo_entrada(item)} já anunciada em {_quando_br(existente['criado_em'])} "
                     f"({ROTULOS_STATUS.get(existente['status'], existente['status'])}"
                     + (f", {existente['stokki_codigo']}" if existente["stokki_codigo"] else "") + ").")
    elif existente:
        avisos.append("Já esteve anunciada e foi cancelada -- será reaberta.")
    fl = cnpj_freshlog(config)
    if fl and item.get("origem") == ORIGEM_XML and _so_digitos(item.get("destinatario_doc")) != fl:
        avisos.append(f"O destinatário da nota não é a Fresh Log ({item.get('destinatario_nome') or formatar_documento(item.get('destinatario_doc') or '')}). "
                      f"Confira se é mesmo uma remessa pro galpão.")
    try:
        cfg = ep.config_stokki_cliente(conn, emb, config)
    except ErroEnvio as e:
        erros.append(str(e))
        cfg = None
    if cfg is not None:
        erros_sku, avisos_sku = ep.validar_skus(conn, item, cfg)
        erros.extend(erros_sku)
        avisos.extend(avisos_sku)
    if not item.get("itens_lista"):
        erros.append("A nota não tem itens (<det>) -- não dá pra anunciar o que vai chegar.")
    return {"ok": not erros, "erros": erros, "avisos": avisos, "existente": dict(existente) if existente else None}


# ── Planilha de entrada ────────────────────────────────────────────────────────
# Modelo próprio: UMA LINHA POR ITEM; linhas com a mesma "Referência" formam
# uma remessa. Sem endereço/destinatário/valor: o destino é sempre o galpão.

COLUNAS_PLANILHA_ENTRADA: list[tuple[str, str, bool, tuple[str, ...], int, str]] = [
    ("referencia", "Referência (nº do pedido de compra/remessa)", True,
     ("referencia", "pedido", "remessa", "n pedido", "numero do pedido", "po", "ordem", "order"), 30, "REM-1001"),
    ("data_prevista", "Data prevista de chegada (DD/MM/AAAA)", True,
     ("data prevista", "data prevista de chegada", "chegada", "data de chegada", "previsao", "data"), 22, ""),
    ("sku", "SKU do produto", True, ("sku", "codigo", "codigo do produto", "cod produto", "produto", "ean", "gtin", "item", "cod"), 18, "NUU001FD"),
    ("quantidade", "Quantidade", True, ("quantidade", "qtd", "qtde", "qte", "quant"), 12, "10"),
    ("unidade", "Unidade (CX, UN...)", False, ("unidade", "un", "und", "embalagem"), 12, "CX"),
    ("numero_nf", "Nº da NF (opcional)", False, ("nf", "n nf", "numero nf", "numero da nf", "nota", "nota fiscal", "nfe"), 16, ""),
    ("volumes", "Volumes", False, ("volumes", "vol", "qtd volumes", "caixas"), 10, "3"),
    ("peso_kg", "Peso (kg)", False, ("peso", "peso kg", "peso (kg)", "peso bruto"), 10, "12,5"),
    ("observacoes", "Observações", False, ("observacoes", "observacao", "obs"), 30, ""),
]
_APELIDOS_ENTRADA: dict[str, str] = {}
for _c in COLUNAS_PLANILHA_ENTRADA:
    _APELIDOS_ENTRADA[ep._normalizar_texto(_c[1])] = _c[0]
    for _a in _c[3]:
        _APELIDOS_ENTRADA.setdefault(ep._normalizar_texto(_a), _c[0])
MAX_LINHAS_PLANILHA = 2000


def chave_planilha_entrada(cnpj_embarcador: str, referencia: str) -> str:
    """Chave sintética única por embarcador+referência (vai em chave_nfe só
    pra dedupe). Prefixo próprio pra nunca colidir com a de Envios."""
    ref = re.sub(r"[^A-Z0-9]+", "-", str(referencia or "").upper()).strip("-")[:40]
    digest = hashlib.sha1(f"entrada|{_so_digitos(cnpj_embarcador)}|{str(referencia or '').strip().upper()}".encode()).hexdigest()[:10]
    return f"PLANILHA-ENTRADA-{_so_digitos(cnpj_embarcador)}-{ref}-{digest}"


def _achar_cabecalho(linhas: list[list]) -> tuple[int, dict[int, str]]:
    melhor = (0, -1, {})
    for i, linha in enumerate(linhas[:15]):
        mapa = {}
        for j, v in enumerate(linha):
            chave = _APELIDOS_ENTRADA.get(ep._normalizar_texto(v))
            if chave and chave not in mapa.values():
                mapa[j] = chave
        if len(mapa) > melhor[0]:
            melhor = (len(mapa), i, mapa)
    if melhor[0] < 3:
        raise ErroEnvio("Não achei o cabeçalho da planilha -- use o modelo Fresh Log de entrada (botão \"Baixar modelo\") "
                        "e mantenha a primeira linha com os nomes das colunas.")
    return melhor[1], melhor[2]


def ler_planilha_entrada(conteudo: bytes, nome: str, cnpj_embarcador: str) -> tuple[list[dict], list[dict]]:
    """(remessas, rejeitados). Cada remessa tem o MESMO formato do dict de
    ler_nfe_entrada (origem, referencia, data_prevista, itens_lista, ...)."""
    linhas = ep._abrir_planilha(conteudo, nome)
    if not linhas:
        raise ErroEnvio(f"{nome}: a planilha está vazia.")
    i_cab, mapa = _achar_cabecalho(linhas)
    faltando = [c[1] for c in COLUNAS_PLANILHA_ENTRADA if c[2] and c[0] not in mapa.values()]
    if faltando:
        raise ErroEnvio(f"{nome}: faltam colunas obrigatórias no cabeçalho: {', '.join(faltando)}. "
                        f"Baixe o modelo Fresh Log de entrada pra conferir os nomes.")
    emb = _so_digitos(cnpj_embarcador)
    hoje = date.today()
    grupos: dict[str, dict] = {}
    erros_grupo: dict[str, list[str]] = {}
    rejeitados: list[dict] = []
    total = 0
    for n, linha in enumerate(linhas[i_cab + 1:], start=i_cab + 2):
        campos = {chave: (linha[j] if j < len(linha) else None) for j, chave in mapa.items()}
        if not any(ep._celula_texto(v) for v in campos.values()):
            continue
        total += 1
        if total > MAX_LINHAS_PLANILHA:
            raise ErroEnvio(f"{nome}: a planilha tem mais de {MAX_LINHAS_PLANILHA} linhas -- divida em arquivos menores.")
        ref = ep._celula_texto(campos.get("referencia"))
        if not ref:
            rejeitados.append({"arquivo": nome, "rotulo": f"linha {n}", "linha": n, "erro": "Sem a referência da remessa."})
            continue
        chave_grupo = ref.strip().upper()
        erros = erros_grupo.setdefault(chave_grupo, [])
        sku = ep._celula_texto(campos.get("sku"))
        try:
            qtd = ep._numero_br(campos.get("quantidade"))
        except ValueError:
            qtd = None
            erros.append(f"linha {n}: quantidade inválida ({ep._celula_texto(campos.get('quantidade'))}).")
        if not sku:
            erros.append(f"linha {n}: sem SKU.")
        elif qtd is None or qtd <= 0:
            erros.append(f"linha {n}: quantidade precisa ser maior que zero.")
        data_txt = ep._celula_texto(campos.get("data_prevista"))
        try:
            data_prev = ep._data_planilha(campos.get("data_prevista"))
        except ValueError:
            data_prev = None
            erros.append(f"linha {n}: data prevista inválida ({data_txt}) -- use DD/MM/AAAA.")
        g = grupos.get(chave_grupo)
        if g is None:
            if not data_prev and not data_txt:
                erros.append("Sem a data prevista de chegada (obrigatória).")
            elif data_prev and data_prev < hoje:
                erros.append(f"Data prevista {data_prev.strftime('%d/%m/%Y')} já passou.")
            volumes = peso = None
            try:
                volumes = ep._numero_br(campos.get("volumes"))
                peso = ep._numero_br(campos.get("peso_kg"))
            except ValueError:
                erros.append("Volumes ou peso com número inválido.")
            g = grupos[chave_grupo] = {
                "chave_nfe": chave_planilha_entrada(emb, ref), "origem": ORIGEM_PLANILHA, "referencia": ref[:60],
                "numero_nf": _so_digitos(campos.get("numero_nf"))[:20], "serie": "", "emitida_em": "",
                "emitente_cnpj": emb, "emitente_nome": "", "destinatario_doc": "", "destinatario_nome": "",
                "data_prevista": data_prev.isoformat() if data_prev else "",
                "volumes": int(volumes) if volumes else 0, "peso_kg": round(peso or 0.0, 3), "valor_nf": 0.0,
                "observacoes": ep._celula_texto(campos.get("observacoes"))[:500],
                "itens_lista": [], "linhas": [], "nome_arquivo": nome,
            }
        elif data_prev and g["data_prevista"] and data_prev.isoformat() != g["data_prevista"]:
            erros.append(f"linha {n}: data prevista diferente da primeira linha da remessa {ref}.")
        g["itens_lista"].append({"linha": len(g["itens_lista"]) + 1, "sku": sku, "ean": "",
                                 "descricao": "", "quantidade": qtd or 0,
                                 "unidade": ep._celula_texto(campos.get("unidade")).upper()[:10], "valor_unitario": 0.0})
        g["linhas"].append(n)

    pedidos = []
    for chave_grupo, g in grupos.items():
        erros = erros_grupo.get(chave_grupo) or []
        if not any(i["sku"] and i["quantidade"] > 0 for i in g["itens_lista"]):
            erros.append("Nenhum item válido (SKU + quantidade).")
        if erros:
            rejeitados.append({"arquivo": nome, "rotulo": f"Remessa {g['referencia']}", "linha": g["linhas"][0] if g["linhas"] else None,
                               "erro": " ".join(dict.fromkeys(erros))})
            continue
        g["itens"] = len(g["itens_lista"])
        if not g["volumes"]:
            g["volumes"] = 1
        pedidos.append(g)
    return pedidos, rejeitados


def gerar_modelo_planilha_entrada(nome_cliente: str = "") -> bytes:
    import io
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Entradas"
    cab = Font(bold=True, color="FFFFFF")
    fundo_ob, fundo_op = PatternFill("solid", fgColor="0EA575"), PatternFill("solid", fgColor="6B7280")
    for j, (chave, rotulo, obrig, _ap, larg, _ex) in enumerate(COLUNAS_PLANILHA_ENTRADA, start=1):
        c = ws.cell(row=1, column=j, value=rotulo)
        c.font, c.fill = cab, (fundo_ob if obrig else fundo_op)
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(j)].width = larg
    ws.row_dimensions[1].height = 34
    ws.freeze_panes = "A2"
    amanha = (date.today() + timedelta(days=1)).strftime("%d/%m/%Y")
    exemplos = [
        {c[0]: c[5] for c in COLUNAS_PLANILHA_ENTRADA} | {"data_prevista": amanha},
        {"referencia": "REM-1001", "sku": "SKU-B", "quantidade": "4", "unidade": "UN"},
        {"referencia": "REM-1002", "data_prevista": amanha, "sku": "NUU001FD", "quantidade": "2", "unidade": "CX", "volumes": "1"},
    ]
    for i, ex in enumerate(exemplos, start=2):
        for j, col in enumerate(COLUNAS_PLANILHA_ENTRADA, start=1):
            v = ex.get(col[0], "")
            if v != "":
                ws.cell(row=i, column=j, value=v).font = Font(italic=True, color="9CA3AF")
    wi = wb.create_sheet("Instruções")
    wi.column_dimensions["A"].width = 110
    texto = [
        f"Modelo Fresh Log de anúncio de mercadoria (Pedidos de Entrada){(' · ' + nome_cliente) if nome_cliente else ''}",
        "",
        "1. Cada LINHA é um ITEM (SKU + quantidade). Linhas com a mesma \"Referência\" formam uma remessa só.",
        "2. Colunas em verde são obrigatórias; em cinza, opcionais. Mantenha os nomes do cabeçalho (a ordem pode mudar).",
        "3. Data prevista de chegada em DD/MM/AAAA, hoje ou futura -- é ela que organiza a fila do galpão.",
        "4. SKU é o código do produto cadastrado na Fresh Log/Stokki. Quantidade maior que zero.",
        "5. Não precisa de endereço nem destinatário: a mercadoria vem sempre pro galpão da Fresh Log.",
        "6. Apague as linhas de exemplo (em cinza) antes de enviar. Limite: 2.000 linhas por arquivo.",
        "",
        "Ao subir a planilha no portal, você vê uma prévia por remessa, confirma, e o recebimento é criado na Stokki automaticamente.",
    ]
    for i, t in enumerate(texto, start=1):
        c = wi.cell(row=i, column=1, value=t)
        if i == 1:
            c.font = Font(bold=True, size=13)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
