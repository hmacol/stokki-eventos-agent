# -*- coding: utf-8 -*-
"""
painel_agentes/expedicao_volumes.py

Etiqueta de volume pra TODO pedido finalizado na Stokki (28/11/2026), cross incluido.
O operador digita ou bipa o PS, informa quantas caixas tem e as etiquetas 10x15 saem
pela fila de impressao (wms_fila_impressao, tipo VOLUME). Cada caixa tem um id unico
no QR (`FV|PS|n|total|id`): e ele que a bipagem do carregamento vai conferir depois.

Modulo de dados puro (sem Flask). Nunca chama a Stokki nem a Vuupt: le so o nosso
banco, e tabela de outro modulo que nao existir conta como fonte vazia.
O piloto do galpao (wms_volumes, QR de 4 campos) continua em wms_separacao.py.

Spec: docs/superpowers/specs/2026-11-28-etiqueta-volume-pedidos-stokki-design.md
"""
import re
import secrets
import sqlite3

import wms
import wms_fila_impressao
import wms_separacao

MAX_CAIXAS = 300
CONFIRMAR_ACIMA_DE = 99
_ALFABETO_ID = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"   # sem 0/O e 1/I: da pra ler e digitar
_TAMANHO_ID = 8

_DDL = (
    """
CREATE TABLE IF NOT EXISTS expedicao_volumes (
    id_caixa     TEXT PRIMARY KEY,          -- o <id> do QR
    codigo       TEXT NOT NULL,             -- PS-<n>, pedido base
    numero       INTEGER NOT NULL,
    total        INTEGER NOT NULL,
    impressao_id INTEGER,
    reimpressoes INTEGER NOT NULL DEFAULT 0,
    criado_em    TEXT NOT NULL,
    criado_por   TEXT NOT NULL DEFAULT '',
    anulado_em   TEXT,
    anulado_por  TEXT
)
""",
    """
CREATE INDEX IF NOT EXISTS idx_expedicao_volumes_codigo
    ON expedicao_volumes (codigo, anulado_em)
""",
)

STATUS_PRONTO_STOKKI = "Waiting for Carrier"   # "aguardando transportador" no retrato do pipeline

_RE_CODIGO = re.compile(r"^#?\s*(?:PS-?)?(\d+)(?:-R\d+)?$")


def garantir_tabela(conn) -> None:
    """Cria a tabela se nao existir (idempotente)."""
    for stmt in _DDL:
        conn.execute(stmt)


def codigo_base(texto) -> str:
    """'#ps-45352' / '45352' / 'PS-45352-R1' / o QR de uma etiqueta -> 'PS-45352'.
    Reentrega (-R) e o mesmo pedido na Stokki: a caixa e a mesma."""
    bruto = str(texto if texto is not None else "").strip().upper()
    if bruto.startswith("FV|"):
        partes = bruto.split("|")
        bruto = partes[1].strip() if len(partes) > 1 else ""
    m = _RE_CODIGO.match(bruto)
    if not m:
        raise wms.ErroWMS(f"Numero de pedido invalido: {bruto or 'vazio'}. Use o numero da Stokki (ex.: PS-45352).")
    return f"PS-{int(m.group(1))}"


def _linhas(conn, sql: str, params: tuple = ()) -> list[dict]:
    """Linhas como dict (nao depende de row_factory). Tabela/coluna de outro modulo que nao existe = []."""
    try:
        cur = conn.execute(sql, params)
        colunas = [c[0] for c in cur.description]
        return [dict(zip(colunas, r)) for r in cur.fetchall()]
    except sqlite3.OperationalError as e:
        if "no such table" in str(e).lower() or "no such column" in str(e).lower():
            return []
        raise


def _ler(conn, sql: str, params: tuple):
    """Primeira linha como dict, ou None."""
    linhas = _linhas(conn, sql, params)
    return linhas[0] if linhas else None


def dados_do_pedido(conn, codigo: str) -> dict:
    """O que vai na etiqueta e na tela, pelo codigo base. Ordem das fontes: nucleo_pedidos,
    wms_pedidos, vigia_stokki_abertos; cada uma so preenche o que ainda esta vazio."""
    d = {"codigo": codigo, "conhecido": False, "embarcador": "", "destinatario": "", "endereco": "",
         "cidade": "", "uf": "", "nf": "", "cancelado": False, "sugestao": None}

    def completar(campos: dict):
        d["conhecido"] = True
        for chave, valor in campos.items():
            if not d[chave] and valor:
                d[chave] = str(valor).strip()

    nucleo = _ler(conn, "SELECT destinatario_nome, endereco, remetente_nome, status FROM nucleo_pedidos "
                        "WHERE codigo = ?", (codigo,))
    if nucleo:
        completar({"destinatario": nucleo["destinatario_nome"], "endereco": nucleo["endereco"],
                   "embarcador": nucleo["remetente_nome"]})
        d["cancelado"] = str(nucleo["status"] or "").upper() == "CANCELADO"

    galpao = _ler(conn, "SELECT * FROM wms_pedidos WHERE upper(replace(codigo_ps, '#', '')) = ? "
                        "ORDER BY id DESC LIMIT 1", (codigo,))
    if galpao:
        completar({"embarcador": galpao.get("embarcador"), "destinatario": galpao.get("destinatario"),
                   "endereco": galpao.get("endereco_destino"), "cidade": galpao.get("cidade"),
                   "uf": galpao.get("uf"), "nf": galpao.get("nf")})

    retrato = _ler(conn, "SELECT embarcador, status_stokki FROM vigia_stokki_abertos WHERE codigo = ?", (codigo,))
    if retrato:
        completar({"embarcador": retrato["embarcador"]})
        # Hugo, 28/11: so sugere o total CONFERIDO, e so com a Stokki em aguardando transportador
        if retrato["status_stokki"] == STATUS_PRONTO_STOKKI:
            lido = _ler(conn, "SELECT volumes, fonte FROM volumes_conferidos WHERE id_pedido = ?",
                        (int(codigo.split("-")[1]),))
            if lido and lido["fonte"] == "conferencia" and lido["volumes"]:
                d["sugestao"] = int(lido["volumes"])
    return d


# ---------------------------------------------------------------------------
# Etiquetar
# ---------------------------------------------------------------------------

def conteudo_qr(codigo: str, numero: int, total: int, id_caixa: str) -> str:
    """Os 4 primeiros campos sao os do QR do piloto; o 5o e a identidade da caixa."""
    return f"FV|{codigo}|{int(numero)}|{int(total)}|{id_caixa}"


def _ativos(conn, codigo: str) -> list[dict]:
    return _linhas(conn, "SELECT * FROM expedicao_volumes WHERE codigo = ? AND anulado_em IS NULL ORDER BY numero",
                   (codigo,))


def _validar_total(total) -> int:
    if isinstance(total, int) and not isinstance(total, bool):
        n = total
    elif isinstance(total, str) and total.strip().isdigit():
        n = int(total.strip())
    else:
        n = 0
    if not 1 <= n <= MAX_CAIXAS:
        raise wms.ErroWMS(f"Quantidade de caixas deve ficar entre 1 e {MAX_CAIXAS}.")
    return n


def _novos_ids(conn, quantidade: int) -> list[str]:
    ids: list[str] = []
    while len(ids) < quantidade:
        novo = "".join(secrets.choice(_ALFABETO_ID) for _ in range(_TAMANHO_ID))
        if novo in ids or conn.execute("SELECT 1 FROM expedicao_volumes WHERE id_caixa = ?", (novo,)).fetchone():
            continue
        ids.append(novo)
    return ids


def situacao(conn, texto) -> dict:
    """O que a tela mostra ao bipar o pedido: os dados dele e, se ja foi etiquetado, o conjunto ativo."""
    codigo = codigo_base(texto)
    d = dados_do_pedido(conn, codigo)
    ativos = _ativos(conn, codigo)
    d["volumes"] = None
    if ativos:
        d["volumes"] = {"total": ativos[0]["total"], "criado_em": ativos[0]["criado_em"],
                        "criado_por": ativos[0]["criado_por"],
                        "reimpressoes": max(a["reimpressoes"] for a in ativos)}
    return d


def etiquetar(conn, texto, total, operador: dict | None, *, confirmado: bool = False) -> dict:
    """Cria as caixas 1..total do pedido e manda as etiquetas 10x15 pra fila.
    Mesmo total de novo = nada muda (repetido). Devolve {"confirmar": [...]} sem gravar
    quando falta o operador confirmar (muitas caixas, pedido cancelado, troca de total)."""
    codigo = codigo_base(texto)
    total = _validar_total(total)
    nome = wms_separacao._op(operador)[1]
    with wms_separacao._tx(conn):   # le dentro da transacao: toque duplo nao cria duas levas
        dados = dados_do_pedido(conn, codigo)
        ativos = _ativos(conn, codigo)
        if ativos and len(ativos) == total and ativos[0]["total"] == total:
            return {"codigo": codigo, "total": total, "impressao_id": ativos[0]["impressao_id"],
                    "repetido": True, "anuladas": 0, "conhecido": dados["conhecido"]}
        if not confirmado:
            avisos = []
            if total > CONFIRMAR_ACIMA_DE:
                avisos.append(f"Sao mesmo {total} caixas?")
            if dados["cancelado"]:
                avisos.append(f"{codigo} esta CANCELADO no nosso sistema. Etiquetar mesmo assim?")
            if ativos:
                n = len(ativos)
                avisos.append(f"{codigo} ja tem {n} etiquetas. As {n} antigas deixam de valer: "
                              f"troque todas pelas {total} novas.")
            if avisos:
                return {"confirmar": avisos}
        ts = wms.agora()
        if ativos:
            conn.execute("UPDATE expedicao_volumes SET anulado_em = ?, anulado_por = ? "
                         "WHERE codigo = ? AND anulado_em IS NULL", (ts, nome, codigo))
            # o trabalho antigo que ainda nao saiu da impressora nao deve mais sair
            trabalhos = sorted({a["impressao_id"] for a in ativos if a["impressao_id"]})
            if trabalhos:
                conn.execute("UPDATE wms_fila_impressao SET situacao = 'CANCELADO' WHERE situacao = 'PENDENTE' "
                             f"AND id IN ({','.join('?' * len(trabalhos))})", trabalhos)
        ids = _novos_ids(conn, total)
        itens = [conteudo_qr(codigo, n, total, ids[n - 1]) for n in range(1, total + 1)]
        impressao_id = wms_fila_impressao.enfileirar(
            conn, tipo="VOLUME", tamanho="10x15", itens=itens,
            titulo=f"{codigo} · {total} volume(s)", pedido_por=nome)
        conn.executemany(
            "INSERT INTO expedicao_volumes (id_caixa, codigo, numero, total, impressao_id, criado_em, criado_por) "
            "VALUES (?,?,?,?,?,?,?)",
            [(ids[n - 1], codigo, n, total, impressao_id, ts, nome) for n in range(1, total + 1)])
    return {"codigo": codigo, "total": total, "impressao_id": impressao_id, "repetido": False,
            "anuladas": len(ativos), "conhecido": dados["conhecido"]}


def reimprimir(conn, texto, operador: dict | None, numero=None) -> dict:
    """Manda de novo pra fila as etiquetas ativas (todas, ou so a caixa `numero`), com os mesmos QRs."""
    codigo = codigo_base(texto)
    nome = wms_separacao._op(operador)[1]
    with wms_separacao._tx(conn):
        ativos = _ativos(conn, codigo)
        if not ativos:
            raise wms.ErroWMS(f"{codigo} ainda nao tem etiqueta. Informe a quantidade de caixas e imprima.")
        alvo = ativos
        if numero not in (None, ""):
            try:
                n = int(numero)
            except (TypeError, ValueError):
                raise wms.ErroWMS("Numero da caixa invalido.")
            alvo = [a for a in ativos if a["numero"] == n]
            if not alvo:
                raise wms.ErroWMS(f"{codigo} nao tem a caixa {numero} (sao {len(ativos)} caixas).")
        itens = [conteudo_qr(codigo, a["numero"], a["total"], a["id_caixa"]) for a in alvo]
        impressao_id = wms_fila_impressao.enfileirar(
            conn, tipo="VOLUME", tamanho="10x15", itens=itens,
            titulo=f"{codigo} · reimpressao de {len(alvo)} volume(s)", pedido_por=nome)
        conn.executemany("UPDATE expedicao_volumes SET reimpressoes = reimpressoes + 1 WHERE id_caixa = ?",
                         [(a["id_caixa"],) for a in alvo])
    return {"codigo": codigo, "quantidade": len(alvo), "impressao_id": impressao_id}


def dados_da_etiqueta(conn, conteudo: str) -> dict | None:
    """FV|PS|n|total|id -> o que vai na arte (as mesmas chaves de wms_separacao.dados_do_volume).
    None se o texto nao for desse formato ou o id nao for daquele pedido. Caixa anulada nao imprime.
    Numero e total saem da linha gravada, nao do texto."""
    partes = str(conteudo or "").strip().split("|")
    if len(partes) != 5 or partes[0].upper() != "FV":
        return None
    caixa = _ler(conn, "SELECT * FROM expedicao_volumes WHERE id_caixa = ?", (partes[4].strip().upper(),))
    if not caixa or caixa["codigo"] != partes[1].strip().upper():
        return None
    if caixa["anulado_em"]:
        raise wms.ErroWMS(f"{caixa['codigo']}: etiqueta anulada (o total de caixas mudou). Imprima o trabalho novo.")
    p = dados_do_pedido(conn, caixa["codigo"])
    return {"codigo_ps": caixa["codigo"], "numero": caixa["numero"], "total": caixa["total"],
            "qr": conteudo_qr(caixa["codigo"], caixa["numero"], caixa["total"], caixa["id_caixa"]),
            "embarcador": p["embarcador"], "destinatario": p["destinatario"], "endereco": p["endereco"],
            "cidade": p["cidade"], "uf": p["uf"], "nf": p["nf"]}


def prontos_sem_etiqueta(conn, limite: int = 50) -> dict:
    """Pedidos em aguardando transportador na Stokki (retrato do pipeline, atrasado ate a proxima
    rodada) que ainda nao tem etiqueta nossa: total e os `limite` mais novos. So cobranca."""
    com_etiqueta = {r["codigo"] for r in _linhas(
        conn, "SELECT DISTINCT codigo FROM expedicao_volumes WHERE anulado_em IS NULL")}
    itens = []
    for r in _linhas(conn, "SELECT codigo, embarcador, primeira_vez_em FROM vigia_stokki_abertos "
                           "WHERE status_stokki = ? ORDER BY primeira_vez_em DESC, codigo", (STATUS_PRONTO_STOKKI,)):
        try:
            codigo = codigo_base(r["codigo"])
        except wms.ErroWMS:
            continue   # codigo que nao e um pedido so: fora da lista
        if codigo not in com_etiqueta:
            itens.append({"codigo": codigo, "embarcador": r["embarcador"] or "", "desde": r["primeira_vez_em"] or ""})
    return {"total": len(itens), "itens": itens[:max(0, int(limite))]}
