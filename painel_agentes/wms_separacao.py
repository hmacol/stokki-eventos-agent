"""
Separacao em onda, distribuicao, conferencia cega e volumes do galpao (17/11/2026).

Spec: docs/superpowers/specs/2026-11-01-galpao-separacao-conferencia-etiquetagem-design.md
Plano: docs/superpowers/plans/2026-11-17-galpao-separacao-conferencia-volumes.md

Modulo de dados puro (sem Flask). Regras que valem pra tudo aqui:
- Cada bipe e uma transacao curta (BEGIN IMMEDIATE + commit): o dados.db e
  compartilhado com o painel e todos os agentes.
- Todo bipe carrega o uuid do aparelho; repetir a chamada devolve o mesmo
  resultado e nunca conta duas vezes.
- Separar NAO gera movimento (C6 da spec): a caixa continua no saldo da posicao
  ate a baixa da expedicao. O que muda e a situacao da caixa e a reserva do
  pedido, que passa a apontar (origem MANUAL) pra posicao/lote da caixa bipada,
  pra baixa tirar o saldo de onde a caixa realmente saiu.
- A necessidade vem dos ITENS do pedido (wms_pedido_itens), nao das reservas:
  reserva e sugestao de onde buscar, e pode estar parcial por falta de saldo.
- Nada aqui escreve na Stokki.
"""
import json
from contextlib import contextmanager
from pathlib import Path

import wms
import wms_caixas
import wms_fila_impressao
from wms import ErroWMS

EPS = 0.0004
ONDA_VIVA = ("ABERTA", "EM_SEPARACAO")
EXECUCOES = ("", "NA_ONDA", "SEPARANDO", "SEPARADO", "CONFERINDO", "AGUARDANDO_DECISAO", "CONFERIDO", "EMBALADO")
ETAPAS_VIVAS = ("SEPARADA", "DISTRIBUIDA", "CONFERIDA")
MAX_VOLUMES = 99

_DDL = (
    """
CREATE TABLE IF NOT EXISTS wms_ondas (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    embarcador_id INTEGER NOT NULL,
    embarcador    TEXT NOT NULL DEFAULT '',
    situacao      TEXT NOT NULL DEFAULT 'ABERTA',   -- ABERTA | EM_SEPARACAO | CONCLUIDA | CANCELADA
    criada_por    TEXT NOT NULL DEFAULT '',
    criada_em     TEXT NOT NULL,
    iniciada_em   TEXT NOT NULL DEFAULT '',
    concluida_em  TEXT NOT NULL DEFAULT ''
)
""",
    """
CREATE TABLE IF NOT EXISTS wms_onda_pedidos (
    onda_id   INTEGER NOT NULL,
    pedido_id INTEGER NOT NULL,
    PRIMARY KEY (onda_id, pedido_id)
)
""",
    """
CREATE TABLE IF NOT EXISTS wms_separacao_itens (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid                 TEXT NOT NULL UNIQUE,        -- uuid do bipe de separar
    dist_uuid            TEXT,                        -- uuid do bipe de distribuir
    onda_id              INTEGER NOT NULL,
    pedido_id            INTEGER,                     -- NULL ate distribuir
    serial               TEXT NOT NULL,               -- caixa de origem
    produto_id           INTEGER NOT NULL,
    posicao              TEXT NOT NULL DEFAULT '',    -- de onde a caixa saiu (chave da reserva e da baixa)
    lote                 TEXT NOT NULL DEFAULT '',
    validade             TEXT NOT NULL DEFAULT '',
    qtd_un               REAL NOT NULL,
    caixa_inteira        INTEGER NOT NULL DEFAULT 1,
    etapa                TEXT NOT NULL DEFAULT 'SEPARADA',  -- SEPARADA | DISTRIBUIDA | CONFERIDA | EXPEDIDA | DEVOLVIDA
    separado_por_id      TEXT NOT NULL DEFAULT '',
    separado_por_nome    TEXT NOT NULL DEFAULT '',
    distribuido_por_id   TEXT NOT NULL DEFAULT '',
    distribuido_por_nome TEXT NOT NULL DEFAULT '',
    criado_em            TEXT NOT NULL,
    atualizado_em        TEXT NOT NULL
)
""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_wms_sep_dist_uuid ON wms_separacao_itens(dist_uuid) WHERE dist_uuid IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_wms_sep_onda ON wms_separacao_itens(onda_id, etapa)",
    "CREATE INDEX IF NOT EXISTS idx_wms_sep_pedido ON wms_separacao_itens(pedido_id, etapa)",
    "CREATE INDEX IF NOT EXISTS idx_wms_sep_serial ON wms_separacao_itens(serial)",
    """
CREATE TABLE IF NOT EXISTS wms_conferencias (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    pedido_id     INTEGER NOT NULL,
    operador_id   TEXT NOT NULL DEFAULT '',
    operador_nome TEXT NOT NULL DEFAULT '',
    iniciada_em   TEXT NOT NULL,
    fechada_em    TEXT NOT NULL DEFAULT '',
    resultado     TEXT NOT NULL DEFAULT '',   -- '' (aberta) | OK | FALTA | ABANDONADA
    detalhe       TEXT NOT NULL DEFAULT ''
)
""",
    """
CREATE TABLE IF NOT EXISTS wms_conferencia_bipes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid           TEXT NOT NULL UNIQUE,
    conferencia_id INTEGER NOT NULL,
    serial         TEXT NOT NULL DEFAULT '',   -- caixa inteira; '' = quantidade solta pelo codigo do produto
    produto_id     INTEGER NOT NULL,
    qtd_un         REAL NOT NULL,
    criado_em      TEXT NOT NULL
)
""",
    "CREATE INDEX IF NOT EXISTS idx_wms_conf_bipes ON wms_conferencia_bipes(conferencia_id)",
    """
CREATE TABLE IF NOT EXISTS wms_decisoes_falta (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    pedido_id      INTEGER NOT NULL,
    conferencia_id INTEGER,
    decisao        TEXT NOT NULL,              -- VOLTAR | PARCIAL
    motivo         TEXT NOT NULL,
    por            TEXT NOT NULL DEFAULT '',
    em             TEXT NOT NULL
)
""",
    """
CREATE TABLE IF NOT EXISTS wms_volumes (
    pedido_id    INTEGER NOT NULL,
    numero       INTEGER NOT NULL,
    total        INTEGER NOT NULL,
    impressao_id INTEGER,
    reimpressoes INTEGER NOT NULL DEFAULT 0,
    criado_em    TEXT NOT NULL,
    PRIMARY KEY (pedido_id, numero)
)
""",
)

# colunas que entraram depois da primeira versao das tabelas (migracao aditiva)
_COLUNAS_EXTRAS = {
    "wms_pedidos": {"execucao": "TEXT NOT NULL DEFAULT ''", "onda_id": "INTEGER",
                    "embarcador_id": "TEXT NOT NULL DEFAULT ''"},
    # a reserva MANUAL criada pra este item: a baixa consome o item pela reserva dele
    "wms_separacao_itens": {"reserva_id": "INTEGER"},
}


def garantir_tabelas(conn) -> None:
    for sql in _DDL:
        conn.execute(sql)
    for tabela, colunas in _COLUNAS_EXTRAS.items():
        existentes = {r[1] for r in conn.execute(f"PRAGMA table_info({tabela})")}
        for nome, tipo in colunas.items():
            if existentes and nome not in existentes:
                conn.execute(f"ALTER TABLE {tabela} ADD COLUMN {nome} {tipo}")
    conn.commit()


def conectar(caminho: Path | None = None):
    import wms_pedidos
    conn = wms_pedidos.conectar(caminho)
    wms_caixas.garantir_tabelas(conn)
    garantir_tabelas(conn)
    return conn


# ---------------------------------------------------------------------------
# Apoio
# ---------------------------------------------------------------------------

@contextmanager
def _tx(conn):
    """Transacao curta de escrita. Fecha o que estiver pendente antes de abrir."""
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _op(operador: dict | None) -> tuple[str, str]:
    operador = operador or {}
    return str(operador.get("id") or ""), str(operador.get("nome") or "")


def _exigir_uuid(uuid) -> str:
    uuid = str(uuid or "").strip()
    if len(uuid) < 8:
        raise ErroWMS("Identificador do bipe (uuid) obrigatorio.")
    return uuid


def _r(valor) -> float:
    return round(float(valor or 0), 3)


def _pedido(conn, pedido_id: int) -> dict:
    row = conn.execute("SELECT * FROM wms_pedidos WHERE id = ?", (int(pedido_id),)).fetchone()
    if not row:
        raise ErroWMS("Pedido nao encontrado.")
    return dict(row)


def _onda(conn, onda_id: int) -> dict:
    row = conn.execute("SELECT * FROM wms_ondas WHERE id = ?", (int(onda_id),)).fetchone()
    if not row:
        raise ErroWMS("Onda nao encontrada.")
    return dict(row)


def _pedidos_da_onda(conn, onda_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT p.* FROM wms_onda_pedidos op JOIN wms_pedidos p ON p.id = op.pedido_id "
        "WHERE op.onda_id = ? ORDER BY p.codigo_ps", (int(onda_id),)).fetchall()
    return [dict(r) for r in rows]


def necessidade_do_pedido(conn, pedido_id: int) -> dict[int, float]:
    """{produto_id: qtd_un} que o pedido pede AGORA (linhas vivas e resolvidas).
    Pedido cancelado ou ja baixado nao pede mais nada."""
    p = conn.execute("SELECT estado_reserva FROM wms_pedidos WHERE id = ?", (int(pedido_id),)).fetchone()
    if p and p[0] in ("CANCELADO", "BAIXADO"):
        return {}
    rows = conn.execute(
        "SELECT produto_id, SUM(qtd_un) AS q FROM wms_pedido_itens WHERE pedido_id = ? AND removido_em = '' "
        "AND produto_id IS NOT NULL AND qtd_un IS NOT NULL GROUP BY produto_id", (int(pedido_id),)).fetchall()
    return {int(r["produto_id"]): _r(r["q"]) for r in rows}


def _faltas_da_onda(conn, onda_id: int) -> tuple[dict, dict]:
    """
    (falta por produto na onda, falta por pedido {pedido_id: {produto: falta}}).
    Desconta o que cada pedido JA tem atribuido, de qualquer onda (pedido que voltou
    da conferencia so pede o que falta), e o que esta separado nesta onda sem pedido.
    """
    por_pedido, total = {}, {}
    for p in _pedidos_da_onda(conn, onda_id):
        nec = necessidade_do_pedido(conn, p["id"])
        atr = _atribuido_ao_pedido(conn, p["id"])
        faltas = {prod: _r(q - atr.get(prod, 0.0)) for prod, q in nec.items() if q - atr.get(prod, 0.0) > EPS}
        por_pedido[p["id"]] = faltas
        for prod, f in faltas.items():
            total[prod] = _r(total.get(prod, 0.0) + f)
    soltas = _somas(conn, "onda_id = ? AND pedido_id IS NULL", (int(onda_id),))
    for prod, q in soltas.items():
        total[prod] = _r(total.get(prod, 0.0) - q)
    return total, por_pedido


def _somas(conn, where: str, args: tuple) -> dict[int, float]:
    marcas = ",".join("?" * len(ETAPAS_VIVAS))
    rows = conn.execute(
        f"SELECT produto_id, SUM(qtd_un) AS q FROM wms_separacao_itens WHERE {where} "
        f"AND etapa IN ({marcas}) GROUP BY produto_id", (*args, *ETAPAS_VIVAS)).fetchall()
    return {int(r["produto_id"]): _r(r["q"]) for r in rows}


def _separado_da_onda(conn, onda_id: int) -> dict[int, float]:
    return _somas(conn, "onda_id = ?", (int(onda_id),))


def _atribuido_ao_pedido(conn, pedido_id: int) -> dict[int, float]:
    return _somas(conn, "pedido_id = ?", (int(pedido_id),))


def _produto(conn, produto_id: int) -> dict:
    return wms.obter_produto(conn, int(produto_id)) or {"id": produto_id, "sku": "", "descricao": ""}


def _rotulo(produto: dict) -> str:
    return f"{produto.get('sku') or ''} {produto.get('descricao') or ''}".strip()


# ---------------------------------------------------------------------------
# Data de entrega (regra do Hugo, 17/11): agendamento; senao a expedicao ja
# ajustada pelo dia da regiao (gravada pelo sync em data_entrega_calc).
# ---------------------------------------------------------------------------

def data_entrega_do_pedido(conn, pedido: dict) -> tuple[str, str]:
    """(AAAA-MM-DD ou '', 'agendamento' | 'expedicao' | '')."""
    codigo = str(pedido.get("codigo_ps") or "").replace("#", "").strip().upper()
    if codigo:
        try:
            row = conn.execute(
                "SELECT agendamento_inicio FROM nucleo_pedidos "
                "WHERE upper(replace(codigo, '#', '')) = ? AND COALESCE(agendamento_inicio, '') <> '' "
                "ORDER BY id DESC LIMIT 1", (codigo,)).fetchone()
        except Exception:  # noqa: BLE001 -- banco sem o nucleo (teste, bancada): vale a expedicao
            row = None
        if row and len(str(row[0])) >= 10:
            return str(row[0])[:10], "agendamento"
    calc = str(pedido.get("data_entrega_calc") or "")
    return (calc, "expedicao") if calc else ("", "")


# ---------------------------------------------------------------------------
# Ondas
# ---------------------------------------------------------------------------

def pedidos_para_onda(conn, embarcador_id: int, data_entrega: str | None = None) -> list[dict]:
    """Pedidos do embarcador que ainda nao entraram em onda. data_entrega='sem' traz os sem data."""
    rows = conn.execute(
        "SELECT * FROM wms_pedidos WHERE embarcador_id = ? AND COALESCE(execucao, '') = '' "
        "AND estado_reserva NOT IN ('BAIXADO', 'CANCELADO') ORDER BY codigo_ps", (str(int(embarcador_id)),)).fetchall()
    saida = []
    for row in rows:
        p = dict(row)
        data, fonte = data_entrega_do_pedido(conn, p)
        if data_entrega == "sem" and data:
            continue
        if data_entrega and data_entrega != "sem" and data != data_entrega:
            continue
        itens = conn.execute(
            "SELECT COUNT(*) AS linhas, COALESCE(SUM(qtd_un), 0) AS un, COALESCE(SUM(falta_un), 0) AS falta, "
            "SUM(CASE WHEN produto_id IS NULL OR qtd_un IS NULL THEN 1 ELSE 0 END) AS sem_cadastro "
            "FROM wms_pedido_itens WHERE pedido_id = ? AND removido_em = ''", (p["id"],)).fetchone()
        saida.append({
            "id": p["id"], "codigo_ps": p["codigo_ps"], "situacao": p["situacao"],
            "estado_reserva": p["estado_reserva"], "destinatario": p.get("destinatario") or "",
            "cidade": p.get("cidade") or "", "uf": p.get("uf") or "",
            "data_entrega": data, "fonte_data": fonte, "regiao": p.get("regiao_dia_fixo") or "",
            "linhas": int(itens["linhas"] or 0), "un": _r(itens["un"]), "falta_un": _r(itens["falta"]),
            "linhas_sem_cadastro": int(itens["sem_cadastro"] or 0),
        })
    return saida


def datas_para_onda(conn, embarcador_id: int) -> list[dict]:
    """[{data, pedidos}] dos pedidos fora de onda, pra tela escolher o dia ('' = sem data)."""
    contagem: dict[str, int] = {}
    for p in pedidos_para_onda(conn, embarcador_id):
        contagem[p["data_entrega"]] = contagem.get(p["data_entrega"], 0) + 1
    return [{"data": d, "pedidos": n} for d, n in sorted(contagem.items(), key=lambda x: (x[0] == "", x[0]))]


def montar_onda(conn, embarcador_id: int, pedido_ids: list, criada_por: str) -> int:
    ids = sorted({int(i) for i in (pedido_ids or [])})
    if not ids:
        raise ErroWMS("Escolha ao menos um pedido para a onda.")
    with _tx(conn):
        nome = ""
        for pid in ids:
            p = _pedido(conn, pid)
            if str(p.get("embarcador_id") or "") != str(int(embarcador_id)):
                raise ErroWMS(f"{p['codigo_ps']} e de outro embarcador: a onda e de um embarcador so.")
            if p.get("execucao"):
                raise ErroWMS(f"{p['codigo_ps']} ja esta em separacao ou conferencia.")
            if p["estado_reserva"] in ("BAIXADO", "CANCELADO"):
                raise ErroWMS(f"{p['codigo_ps']} esta {p['estado_reserva'].lower()}.")
            if not necessidade_do_pedido(conn, pid):
                raise ErroWMS(f"{p['codigo_ps']} nao tem item com produto cadastrado: nada a separar.")
            nome = nome or p["embarcador"]
        ts = wms.agora()
        cur = conn.execute(
            "INSERT INTO wms_ondas (embarcador_id, embarcador, situacao, criada_por, criada_em) "
            "VALUES (?, ?, 'ABERTA', ?, ?)", (int(embarcador_id), nome, str(criada_por or "")[:80], ts))
        onda_id = cur.lastrowid
        for pid in ids:
            conn.execute("INSERT INTO wms_onda_pedidos (onda_id, pedido_id) VALUES (?, ?)", (onda_id, pid))
            conn.execute("UPDATE wms_pedidos SET execucao = 'NA_ONDA', onda_id = ?, atualizado_em = ? WHERE id = ?",
                         (onda_id, ts, pid))
    return onda_id


def iniciar_onda(conn, onda_id: int, operador: dict | None = None) -> dict:
    with _tx(conn):
        onda = _onda(conn, onda_id)
        if onda["situacao"] == "EM_SEPARACAO":
            return onda
        if onda["situacao"] != "ABERTA":
            raise ErroWMS(f"Onda {onda_id} esta {onda['situacao'].lower()}.")
        ts = wms.agora()
        conn.execute("UPDATE wms_ondas SET situacao = 'EM_SEPARACAO', iniciada_em = ? WHERE id = ?", (ts, onda_id))
        conn.execute("UPDATE wms_pedidos SET execucao = 'SEPARANDO', atualizado_em = ? "
                     "WHERE onda_id = ? AND execucao = 'NA_ONDA'", (ts, onda_id))
    return _onda(conn, onda_id)


def onda_de_um_pedido(conn, pedido_id: int, operador: dict | None = None) -> int:
    """Pedido a pedido = onda de um pedido, que o separador comeca sozinho (C9)."""
    p = _pedido(conn, pedido_id)
    onda_id = montar_onda(conn, int(p.get("embarcador_id") or 0), [pedido_id], _op(operador)[1])
    iniciar_onda(conn, onda_id, operador)
    return onda_id


def cancelar_onda(conn, onda_id: int, por: str = "") -> None:
    with _tx(conn):
        onda = _onda(conn, onda_id)
        if onda["situacao"] not in ONDA_VIVA:
            raise ErroWMS(f"Onda {onda_id} esta {onda['situacao'].lower()}.")
        if _separado_da_onda(conn, onda_id):
            raise ErroWMS("A onda ja tem caixa separada: devolva as caixas antes de cancelar.")
        ts = wms.agora()
        conn.execute("UPDATE wms_ondas SET situacao = 'CANCELADA', concluida_em = ? WHERE id = ?", (ts, onda_id))
        conn.execute("UPDATE wms_pedidos SET execucao = '', onda_id = NULL, atualizado_em = ? WHERE onda_id = ?",
                     (ts, onda_id))


def ondas_vivas(conn, embarcadores=None) -> list[dict]:
    rows = conn.execute("SELECT * FROM wms_ondas WHERE situacao IN ('ABERTA', 'EM_SEPARACAO') ORDER BY id").fetchall()
    saida = []
    for row in rows:
        o = dict(row)
        if embarcadores is not None and o["embarcador_id"] not in embarcadores:
            continue
        pedidos = _pedidos_da_onda(conn, o["id"])
        o["pedidos"] = [p["codigo_ps"] for p in pedidos]
        saida.append(o)
    return saida


def detalhe_onda(conn, onda_id: int) -> dict:
    """Onda + o que falta por produto + a caminhada sugerida (reservas FEFO ainda nao bipadas)
    + os itens ja bipados (pra tela poder devolver um bipe errado)."""
    onda = _onda(conn, onda_id)
    pedidos = _pedidos_da_onda(conn, onda_id)
    pedido_un: dict[int, float] = {}
    atribuido_un: dict[int, float] = {}
    por_pedido = []
    for p in pedidos:
        nec = necessidade_do_pedido(conn, p["id"])
        atr = _atribuido_ao_pedido(conn, p["id"])
        for prod, q in nec.items():
            pedido_un[prod] = _r(pedido_un.get(prod, 0) + q)
        for prod, q in atr.items():
            atribuido_un[prod] = _r(atribuido_un.get(prod, 0) + q)
        por_pedido.append({"id": p["id"], "codigo_ps": p["codigo_ps"], "execucao": p.get("execucao") or "",
                           "destinatario": p.get("destinatario") or "",
                           "pedido_un": _r(sum(nec.values())), "atribuido_un": _r(sum(atr.values()))})
    soltas = _somas(conn, "onda_id = ? AND pedido_id IS NULL", (int(onda_id),))
    produtos = []
    for prod in sorted(set(pedido_un) | set(atribuido_un) | set(soltas)):
        info = _produto(conn, prod)
        produtos.append({"produto_id": prod, "sku": info.get("sku") or "", "descricao": info.get("descricao") or "",
                         "ean": info.get("ean") or "", "pedido_un": pedido_un.get(prod, 0.0),
                         "separado_un": _r(atribuido_un.get(prod, 0.0) + soltas.get(prod, 0.0)),
                         "distribuido_un": atribuido_un.get(prod, 0.0)})
    caminhada, itens = [], []
    if pedidos:
        ids = tuple(p["id"] for p in pedidos)
        marcas = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT posicao, produto_id, lote, validade, SUM(quantidade_un) AS q FROM wms_reservas "
            f"WHERE pedido_id IN ({marcas}) AND estado = 'ATIVA' AND origem = 'FEFO' "
            f"GROUP BY posicao, produto_id, lote, validade ORDER BY posicao, produto_id, validade", ids).fetchall()
        for r in rows:
            info = _produto(conn, r["produto_id"])
            caminhada.append({"posicao": r["posicao"], "produto_id": r["produto_id"], "sku": info.get("sku") or "",
                              "descricao": info.get("descricao") or "", "lote": r["lote"],
                              "validade": r["validade"], "qtd_un": _r(r["q"])})
        codigos = {p["id"]: p["codigo_ps"] for p in pedidos}
        rows = conn.execute(
            f"SELECT * FROM wms_separacao_itens WHERE etapa IN ('SEPARADA', 'DISTRIBUIDA') "
            f"AND (pedido_id IN ({marcas}) OR (onda_id = ? AND pedido_id IS NULL)) ORDER BY id DESC",
            (*ids, int(onda_id))).fetchall()
        for it in rows:
            info = _produto(conn, it["produto_id"])
            itens.append({"id": it["id"], "serial": it["serial"], "sku": info.get("sku") or "",
                          "descricao": info.get("descricao") or "", "qtd_un": _r(it["qtd_un"]),
                          "caixa_inteira": bool(it["caixa_inteira"]), "lote": it["lote"], "validade": it["validade"],
                          "codigo_ps": codigos.get(it["pedido_id"], ""), "separado_por": it["separado_por_nome"]})
    return {"onda": onda, "pedidos": por_pedido, "produtos": produtos, "caminhada": caminhada, "itens": itens,
            "a_distribuir": sum(1 for i in itens if not i["codigo_ps"])}


# ---------------------------------------------------------------------------
# Reserva do pedido aponta pra caixa bipada
# ---------------------------------------------------------------------------

def _reservar_manual(conn, pedido_id: int, produto_id: int, posicao: str, lote: str, validade: str,
                     qtd: float) -> int | None:
    """
    Troca `qtd` da reserva sugerida (FEFO) do pedido por UMA reserva MANUAL na chave da caixa
    bipada e devolve o id dela (None se o pedido nao tem mais linha desse produto). Uma reserva
    por item bipado: a baixa consome o item pela reserva dele, sem adivinhar caixa.
    Roda dentro da transacao de quem chama.
    """
    ts = wms.agora()
    restante = _r(qtd)
    fefo = conn.execute(
        "SELECT * FROM wms_reservas WHERE pedido_id = ? AND produto_id = ? AND estado = 'ATIVA' AND origem = 'FEFO' "
        "ORDER BY (posicao = ? AND lote = ? AND validade = ?) DESC, id",
        (pedido_id, produto_id, posicao, lote, validade)).fetchall()
    linha = None
    for r in fefo:
        if restante <= EPS:
            break
        tira = min(_r(r["quantidade_un"]), restante)
        sobra = _r(r["quantidade_un"] - tira)
        if sobra <= EPS:
            conn.execute("UPDATE wms_reservas SET estado = 'CANCELADA', atualizado_em = ? WHERE id = ?", (ts, r["id"]))
        else:
            conn.execute("UPDATE wms_reservas SET quantidade_un = ?, atualizado_em = ? WHERE id = ?",
                         (sobra, ts, r["id"]))
        linha = linha or r["item_id"]
        restante = _r(restante - tira)
    if linha is None:
        item = conn.execute(
            "SELECT id FROM wms_pedido_itens WHERE pedido_id = ? AND produto_id = ? AND removido_em = '' "
            "ORDER BY linha LIMIT 1", (pedido_id, produto_id)).fetchone()
        linha = item["id"] if item else None
    if linha is None:
        return None
    cur = conn.execute(
        "INSERT INTO wms_reservas (pedido_id, item_id, produto_id, posicao, lote, validade, quantidade_un, "
        "estado, origem, criado_em, atualizado_em) VALUES (?,?,?,?,?,?,?, 'ATIVA', 'MANUAL', ?, ?)",
        (pedido_id, linha, produto_id, posicao, lote, validade, _r(qtd), ts, ts))
    reserva_id = cur.lastrowid
    # A caixa saiu da prateleira: se a chave ficou reservada alem do saldo, solta a
    # sugestao FEFO de OUTROS pedidos ali (a proxima rodada do sync realoca).
    saldo = wms._saldo_atual(conn, posicao, produto_id, lote, validade)
    reservado = conn.execute(
        "SELECT COALESCE(SUM(quantidade_un), 0) FROM wms_reservas WHERE produto_id = ? AND posicao = ? "
        "AND lote = ? AND validade = ? AND estado = 'ATIVA'", (produto_id, posicao, lote, validade)).fetchone()[0]
    excesso = _r(reservado) - _r(saldo)
    if excesso > EPS:
        outros = conn.execute(
            "SELECT id, quantidade_un FROM wms_reservas WHERE produto_id = ? AND posicao = ? AND lote = ? "
            "AND validade = ? AND estado = 'ATIVA' AND origem = 'FEFO' AND pedido_id <> ? ORDER BY id DESC",
            (produto_id, posicao, lote, validade, pedido_id)).fetchall()
        for r in outros:
            if excesso <= EPS:
                break
            conn.execute("UPDATE wms_reservas SET estado = 'CANCELADA', atualizado_em = ? WHERE id = ?", (ts, r["id"]))
            excesso = _r(excesso - r["quantidade_un"])
    return reserva_id


def _desfazer_reserva_manual(conn, item: dict, qtd: float | None = None) -> None:
    """Tira da reserva MANUAL do item a quantidade que deixou de ser do pedido (tudo, por padrao)."""
    if not item.get("reserva_id"):
        return
    r = conn.execute("SELECT quantidade_un, estado FROM wms_reservas WHERE id = ?", (item["reserva_id"],)).fetchone()
    if not r or r["estado"] != "ATIVA":
        return
    ts = wms.agora()
    sobra = _r(r["quantidade_un"] - _r(item["qtd_un"] if qtd is None else qtd))
    if sobra <= EPS:
        conn.execute("UPDATE wms_reservas SET estado = 'CANCELADA', atualizado_em = ? WHERE id = ?",
                     (ts, item["reserva_id"]))
    else:
        conn.execute("UPDATE wms_reservas SET quantidade_un = ?, atualizado_em = ? WHERE id = ?",
                     (sobra, ts, item["reserva_id"]))


def _soltar_caixa(conn, serial: str, de: str, operador, motivo: str, posicao: str, chave: str) -> None:
    """Caixa que saiu da prateleira e nao tem mais nada vivo separado dela fica DEVOLVIDA ate
    alguem guardar (C13). Caixa que nunca saiu do lugar (so cederam unidades) nao muda."""
    marcas = ",".join("?" * len(ETAPAS_VIVAS))
    vivos = conn.execute(f"SELECT COUNT(*) FROM wms_separacao_itens WHERE serial = ? AND etapa IN ({marcas})",
                         (serial, *ETAPAS_VIVAS)).fetchone()[0]
    if vivos:
        return
    cur = conn.execute("UPDATE wms_caixas SET situacao = 'DEVOLVIDA', atualizado_em = ? WHERE serial = ? "
                       "AND situacao IN ('SEPARADA', 'DISTRIBUIDA', 'CONFERIDA')", (wms.agora(), serial))
    if cur.rowcount:
        _evento_caixa(conn, serial, de, "DEVOLVIDA", chave, operador, motivo, posicao)


def _devolver(conn, item: dict, operador, motivo: str) -> None:
    """Item deixa de valer pro pedido/onda: etapa DEVOLVIDA, reserva solta, caixa a guardar."""
    conn.execute("UPDATE wms_separacao_itens SET etapa = 'DEVOLVIDA', atualizado_em = ? WHERE id = ?",
                 (wms.agora(), item["id"]))
    _desfazer_reserva_manual(conn, item)
    _soltar_caixa(conn, item["serial"], item["etapa"], operador, motivo, item["posicao"], f"sep-dev-{item['id']}")


def _evento_caixa(conn, serial: str, de: str, para: str, uuid: str, operador, observacao: str, posicao=None) -> None:
    op_id, op_nome = _op(operador)
    conn.execute(
        "INSERT OR IGNORE INTO wms_caixa_eventos (uuid, serial, de, para, posicao, operador_id, operador_nome, "
        "observacao, criado_em) VALUES (?,?,?,?,?,?,?,?,?)",
        (uuid, serial, de, para, posicao, op_id, op_nome, observacao[:500], wms.agora()))


def _atribuir(conn, item: dict, pedido: dict, operador, dist_uuid: str) -> None:
    """Item da onda passa a ser do pedido. Dentro da transacao de quem chama."""
    op_id, op_nome = _op(operador)
    ts = wms.agora()
    reserva_id = _reservar_manual(conn, pedido["id"], item["produto_id"], item["posicao"], item["lote"],
                                  item["validade"], item["qtd_un"])
    conn.execute(
        "UPDATE wms_separacao_itens SET pedido_id = ?, etapa = 'DISTRIBUIDA', dist_uuid = ?, reserva_id = ?, "
        "distribuido_por_id = ?, distribuido_por_nome = ?, atualizado_em = ? WHERE id = ?",
        (pedido["id"], dist_uuid, reserva_id, op_id, op_nome, ts, item["id"]))
    if item["caixa_inteira"]:
        cur = conn.execute("UPDATE wms_caixas SET situacao = 'DISTRIBUIDA', atualizado_em = ? "
                           "WHERE serial = ? AND situacao = 'SEPARADA'", (ts, item["serial"]))
        if cur.rowcount:
            _evento_caixa(conn, item["serial"], "SEPARADA", "DISTRIBUIDA", f"sep-dist-{dist_uuid}", operador,
                          f"Distribuida para {pedido['codigo_ps']}", item["posicao"])


# ---------------------------------------------------------------------------
# Separar
# ---------------------------------------------------------------------------

def _resposta_separar(conn, item: dict, repetido: bool) -> dict:
    produto = _produto(conn, item["produto_id"])
    falta = _faltas_da_onda(conn, item["onda_id"])[0].get(item["produto_id"], 0.0)
    codigo_ps = ""
    if item["pedido_id"]:
        codigo_ps = _pedido(conn, item["pedido_id"])["codigo_ps"]
    return {"item_id": item["id"], "serial": item["serial"], "produto_id": item["produto_id"],
            "produto": _rotulo(produto), "qtd_un": _r(item["qtd_un"]), "caixa_inteira": bool(item["caixa_inteira"]),
            "lote": item["lote"], "validade": item["validade"],
            "codigo_ps": codigo_ps, "falta_un": max(_r(falta), 0.0), "repetido": repetido}


def separar_caixa(conn, onda_id: int, serial: str, *, qtd_un: float | None = None, uuid: str,
                  operador: dict | None = None) -> dict:
    """
    O separador bipou a caixa na prateleira. qtd_un=None leva a caixa inteira;
    menor que a caixa registra a parte retirada e a caixa fica onde esta.
    Onda de um pedido ja distribui (devolve codigo_ps).
    """
    uuid = _exigir_uuid(uuid)
    ja = conn.execute("SELECT * FROM wms_separacao_itens WHERE uuid = ?", (uuid,)).fetchone()
    if ja:
        return _resposta_separar(conn, dict(ja), True)
    serial = str(serial or "").strip().upper()
    op_id, op_nome = _op(operador)
    with _tx(conn):
        onda = _onda(conn, onda_id)
        if onda["situacao"] != "EM_SEPARACAO":
            raise ErroWMS(f"Onda {onda_id} nao esta em separacao ({onda['situacao'].lower()}).")
        caixa = wms_caixas.obter_caixa(conn, serial)
        if not caixa:
            raise ErroWMS(f"Caixa {serial} nao existe.")
        produto = caixa.get("produto") or {}
        if produto.get("embarcador_id") != onda["embarcador_id"]:
            raise ErroWMS(f"Caixa {serial} e de outro embarcador ({produto.get('embarcador') or '?'}).")
        if caixa["situacao"] in ("SEPARADA", "DISTRIBUIDA", "CONFERIDA"):
            raise ErroWMS(f"Caixa {serial} ja foi separada.")
        if caixa["situacao"] == "IMPRESSA":
            raise ErroWMS(f"Caixa {serial} ainda nao foi enderecada: bipe no Inventario ou no Receber primeiro.")
        # DEVOLVIDA entra de novo direto: ela nunca saiu do saldo da posicao (C13)
        if caixa["situacao"] not in ("EM_ESTOQUE", "ABERTA", "DEVOLVIDA"):
            raise ErroWMS(f"Caixa {serial} esta {caixa['situacao'].lower()}: nao pode ser separada.")
        pedidos = _pedidos_da_onda(conn, onda_id)
        nec = 0.0
        for p in pedidos:
            nec += necessidade_do_pedido(conn, p["id"]).get(caixa["produto_id"], 0.0)
        falta = _r(_faltas_da_onda(conn, onda_id)[0].get(caixa["produto_id"], 0.0))
        if nec <= EPS:
            raise ErroWMS(f"A onda nao pede este produto ({_rotulo(produto)}).")
        if falta <= EPS:
            raise ErroWMS(f"A onda ja tem tudo deste produto ({_rotulo(produto)}).")
        # o que ja foi prometido desta caixa em partes e ainda nao saiu na baixa
        marcas = ",".join("?" * len(ETAPAS_VIVAS))
        prometido = conn.execute(
            f"SELECT COALESCE(SUM(qtd_un), 0) FROM wms_separacao_itens WHERE serial = ? AND etapa IN ({marcas})",
            (serial, *ETAPAS_VIVAS)).fetchone()[0]
        livre = _r(caixa["qtd_un"] - prometido)
        if livre <= EPS:
            raise ErroWMS(f"Caixa {serial} nao tem mais unidade livre.")
        if qtd_un in (None, ""):
            q = livre
            if q > falta + EPS:
                raise ErroWMS(f"A onda so precisa de {falta:g} UN e a caixa tem {q:g}: informe quanto tirou.")
        else:
            try:
                q = _r(qtd_un)
            except (TypeError, ValueError):
                raise ErroWMS("Quantidade invalida.")
            if q <= EPS:
                raise ErroWMS("Quantidade deve ser maior que zero.")
            if q > livre + EPS:
                raise ErroWMS(f"A caixa {serial} so tem {livre:g} UN livre.")
            if q > falta + EPS:
                raise ErroWMS(f"A onda so precisa de {falta:g} UN deste produto.")
        inteira = abs(q - _r(caixa["qtd_un"])) <= EPS and _r(prometido) <= EPS
        ts = wms.agora()
        if inteira:
            cur = conn.execute(
                "UPDATE wms_caixas SET situacao = 'SEPARADA', atualizado_em = ? "
                "WHERE serial = ? AND situacao IN ('EM_ESTOQUE', 'ABERTA', 'DEVOLVIDA')", (ts, serial))
            if cur.rowcount != 1:
                raise ErroWMS(f"Caixa {serial} acabou de ser separada em outro aparelho.")
            _evento_caixa(conn, serial, caixa["situacao"], "SEPARADA", f"sep-{uuid}", operador,
                          f"Separada na onda {onda_id}", caixa["posicao"])
        cur = conn.execute(
            "INSERT INTO wms_separacao_itens (uuid, onda_id, serial, produto_id, posicao, lote, validade, qtd_un, "
            "caixa_inteira, etapa, separado_por_id, separado_por_nome, criado_em, atualizado_em) "
            "VALUES (?,?,?,?,?,?,?,?,?, 'SEPARADA', ?,?,?,?)",
            (uuid, onda_id, serial, caixa["produto_id"], caixa["posicao"] or "", caixa["lote"] or "",
             caixa["validade"] or "", q, 1 if inteira else 0, op_id, op_nome, ts, ts))
        item = dict(conn.execute("SELECT * FROM wms_separacao_itens WHERE id = ?", (cur.lastrowid,)).fetchone())
        if len(pedidos) == 1:
            _atribuir(conn, item, pedidos[0], operador, f"auto-{uuid}")
    item = dict(conn.execute("SELECT * FROM wms_separacao_itens WHERE uuid = ?", (uuid,)).fetchone())
    return _resposta_separar(conn, item, False)


def devolver_item(conn, item_id: int, *, operador: dict | None = None, motivo: str = "") -> dict:
    """Desfaz um bipe de separacao enquanto o pedido ainda esta separando. Caixa inteira fica
    DEVOLVIDA ate alguem guardar ou bipar de novo (C13); parte de caixa so libera a quantidade."""
    with _tx(conn):
        row = conn.execute("SELECT * FROM wms_separacao_itens WHERE id = ?", (int(item_id),)).fetchone()
        if not row:
            raise ErroWMS("Item de separacao nao encontrado.")
        item = dict(row)
        if item["etapa"] not in ("SEPARADA", "DISTRIBUIDA"):
            raise ErroWMS(f"Item ja esta {item['etapa'].lower()}: nao pode ser devolvido.")
        if item["pedido_id"]:
            p = _pedido(conn, item["pedido_id"])
            if (p.get("execucao") or "") not in ("SEPARANDO", ""):
                raise ErroWMS(f"{p['codigo_ps']} ja saiu da separacao: quem desfaz agora e o escritorio (voltar).")
        _devolver(conn, item, operador, f"Devolvida da onda {item['onda_id']}. {motivo}".strip())
    return {"item_id": item["id"], "serial": item["serial"], "caixa_inteira": bool(item["caixa_inteira"])}


# ---------------------------------------------------------------------------
# Distribuir
# ---------------------------------------------------------------------------

def _resposta_distribuir(conn, item: dict, repetido: bool, sobra_un: float = 0.0) -> dict:
    pedido = _pedido(conn, item["pedido_id"])
    nec = necessidade_do_pedido(conn, pedido["id"])
    atr = _atribuido_ao_pedido(conn, pedido["id"])
    restam = _r(sum(max(q - atr.get(prod, 0.0), 0.0) for prod, q in nec.items()))
    return {"item_id": item["id"], "serial": item["serial"], "pedido_id": pedido["id"],
            "codigo_ps": pedido["codigo_ps"], "destinatario": pedido.get("destinatario") or "",
            "qtd_un": _r(item["qtd_un"]), "restam_do_pedido": restam, "sobra_un": _r(sobra_un),
            "produto": _rotulo(_produto(conn, item["produto_id"])), "repetido": repetido}


def distribuir_caixa(conn, onda_id: int, codigo: str, *, qtd_un: float | None = None, uuid: str,
                     operador: dict | None = None) -> dict:
    """
    O separador bipa cada caixa separada e a tela diz de qual pedido ela e.
    `codigo` e o serial da caixa (inteira ou de onde saiu a parte) ou o EAN/SKU
    do produto (parte solta). Se nenhum pedido precisa da quantidade inteira,
    vai o que o pedido precisa e o resto continua a distribuir (sobra_un).
    """
    uuid = _exigir_uuid(uuid)
    ja = conn.execute("SELECT * FROM wms_separacao_itens WHERE dist_uuid = ?", (uuid,)).fetchone()
    if ja:
        return _resposta_distribuir(conn, dict(ja), True)
    codigo = str(codigo or "").strip().upper()
    with _tx(conn):
        onda = _onda(conn, onda_id)
        if onda["situacao"] != "EM_SEPARACAO":
            raise ErroWMS(f"Onda {onda_id} nao esta em separacao ({onda['situacao'].lower()}).")
        row = conn.execute(
            "SELECT * FROM wms_separacao_itens WHERE onda_id = ? AND serial = ? AND etapa = 'SEPARADA' "
            "AND pedido_id IS NULL ORDER BY caixa_inteira DESC, id LIMIT 1", (onda_id, codigo)).fetchone()
        if not row:
            produtos = [p["id"] for p in wms.buscar_por_codigo(conn, codigo)] if codigo else []
            if produtos:
                marcas = ",".join("?" * len(produtos))
                row = conn.execute(
                    f"SELECT * FROM wms_separacao_itens WHERE onda_id = ? AND produto_id IN ({marcas}) "
                    f"AND etapa = 'SEPARADA' AND pedido_id IS NULL AND caixa_inteira = 0 ORDER BY id LIMIT 1",
                    (onda_id, *produtos)).fetchone()
        if not row:
            outra = conn.execute(
                "SELECT etapa FROM wms_separacao_itens WHERE onda_id = ? AND serial = ? ORDER BY id DESC LIMIT 1",
                (onda_id, codigo)).fetchone()
            if outra and outra["etapa"] in ("DISTRIBUIDA", "CONFERIDA"):
                raise ErroWMS(f"Caixa {codigo} ja foi distribuida.")
            raise ErroWMS(f"{codigo} nao foi separado nesta onda.")
        item = dict(row)
        q = _r(item["qtd_un"])
        if qtd_un not in (None, ""):
            try:
                pedida = _r(qtd_un)
            except (TypeError, ValueError):
                raise ErroWMS("Quantidade invalida.")
            if pedida <= EPS or pedida > q + EPS:
                raise ErroWMS(f"Quantidade deve ficar entre 0 e {q:g} UN.")
            q = pedida
        faltas = _faltas_da_onda(conn, onda_id)[1]
        candidatos = [(p, faltas[p["id"]][item["produto_id"]]) for p in _pedidos_da_onda(conn, onda_id)
                      if faltas.get(p["id"], {}).get(item["produto_id"], 0.0) > EPS]
        if not candidatos:
            raise ErroWMS("Nenhum pedido da onda precisa mais deste produto: devolva a caixa (botao Devolver).")
        pedido, falta = next(((p, f) for p, f in candidatos if f + EPS >= q), candidatos[0])
        vai = min(q, falta)
        sobra = _r(item["qtd_un"] - vai)
        ts = wms.agora()
        if sobra > EPS:
            # divide: esta linha fica com o que vai pro pedido, uma linha nova guarda o resto a distribuir
            conn.execute("UPDATE wms_separacao_itens SET qtd_un = ?, caixa_inteira = 0, atualizado_em = ? WHERE id = ?",
                         (vai, ts, item["id"]))
            conn.execute(
                "INSERT INTO wms_separacao_itens (uuid, onda_id, serial, produto_id, posicao, lote, validade, qtd_un, "
                "caixa_inteira, etapa, separado_por_id, separado_por_nome, criado_em, atualizado_em) "
                "VALUES (?,?,?,?,?,?,?,?,0, 'SEPARADA', ?,?,?,?)",
                (f"resto-{uuid}", onda_id, item["serial"], item["produto_id"], item["posicao"], item["lote"],
                 item["validade"], sobra, item["separado_por_id"], item["separado_por_nome"], ts, ts))
            item["qtd_un"], item["caixa_inteira"] = vai, 0
        _atribuir(conn, item, pedido, operador, uuid)
    item = dict(conn.execute("SELECT * FROM wms_separacao_itens WHERE dist_uuid = ?", (uuid,)).fetchone())
    return _resposta_distribuir(conn, item, False, sobra)


def fechar_separacao(conn, onda_id: int, operador: dict | None = None, mesmo_faltando: bool = False) -> dict:
    """
    Pedido com tudo distribuido vira SEPARADO. Com mesmo_faltando, o incompleto que tem ALGO
    separado tambem segue (a falta aparece na conferencia) e o que nao tem NADA sai da onda e
    volta pra lista de pedidos sem onda: pedido vazio nao vai pra conferencia.
    Onda fecha quando nenhum pedido ficou separando.
    """
    with _tx(conn):
        onda = _onda(conn, onda_id)
        if onda["situacao"] != "EM_SEPARACAO":
            raise ErroWMS(f"Onda {onda_id} nao esta em separacao ({onda['situacao'].lower()}).")
        soltas = conn.execute(
            "SELECT COUNT(*) FROM wms_separacao_itens WHERE onda_id = ? AND etapa = 'SEPARADA' AND pedido_id IS NULL",
            (onda_id,)).fetchone()[0]
        if soltas:
            raise ErroWMS(f"Ha {soltas} caixa(s) separada(s) sem pedido: distribua ou devolva antes de fechar.")
        ts = wms.agora()
        prontos, faltando, sem_nada = [], [], []
        faltas = _faltas_da_onda(conn, onda_id)[1]
        for p in _pedidos_da_onda(conn, onda_id):
            if p.get("execucao") != "SEPARANDO":
                continue
            falta = _r(sum(faltas.get(p["id"], {}).values()))
            tem_algo = bool(_atribuido_ao_pedido(conn, p["id"]))
            if falta > EPS:
                faltando.append({"codigo_ps": p["codigo_ps"], "falta_un": falta})
                if not mesmo_faltando:
                    continue
                if not tem_algo:
                    conn.execute("DELETE FROM wms_onda_pedidos WHERE onda_id = ? AND pedido_id = ?", (onda_id, p["id"]))
                    conn.execute("UPDATE wms_pedidos SET execucao = '', onda_id = NULL, atualizado_em = ? WHERE id = ?",
                                 (ts, p["id"]))
                    sem_nada.append(p["codigo_ps"])
                    continue
            elif not tem_algo:
                # nao pede mais nada (cancelado, baixado ou esvaziado na Stokki): sai da onda
                conn.execute("DELETE FROM wms_onda_pedidos WHERE onda_id = ? AND pedido_id = ?", (onda_id, p["id"]))
                conn.execute("UPDATE wms_pedidos SET execucao = '', onda_id = NULL, atualizado_em = ? WHERE id = ?",
                             (ts, p["id"]))
                sem_nada.append(p["codigo_ps"])
                continue
            conn.execute("UPDATE wms_pedidos SET execucao = 'SEPARADO', atualizado_em = ? WHERE id = ?", (ts, p["id"]))
            prontos.append(p["codigo_ps"])
        resta = conn.execute("SELECT COUNT(*) FROM wms_pedidos WHERE onda_id = ? AND execucao = 'SEPARANDO'",
                             (onda_id,)).fetchone()[0]
        if not resta:
            conn.execute("UPDATE wms_ondas SET situacao = 'CONCLUIDA', concluida_em = ? WHERE id = ?", (ts, onda_id))
    return {"prontos": prontos, "faltando": faltando, "sem_nada": sem_nada, "onda_concluida": not resta}


# ---------------------------------------------------------------------------
# Conferencia cega
# ---------------------------------------------------------------------------

def pedidos_para_conferir(conn, embarcadores=None) -> list[dict]:
    """So o que o conferente pode ver: PS, destinatario, embarcador. Nada de itens."""
    rows = conn.execute("SELECT * FROM wms_pedidos WHERE execucao IN ('SEPARADO', 'CONFERINDO') "
                        "ORDER BY codigo_ps").fetchall()
    saida = []
    for row in rows:
        p = dict(row)
        if embarcadores is not None and int(p.get("embarcador_id") or 0) not in embarcadores:
            continue
        aberta = conn.execute("SELECT operador_nome FROM wms_conferencias WHERE pedido_id = ? AND resultado = ''",
                              (p["id"],)).fetchone()
        saida.append({"id": p["id"], "codigo_ps": p["codigo_ps"], "embarcador": p["embarcador"],
                      "destinatario": p.get("destinatario") or "", "cidade": p.get("cidade") or "",
                      "conferindo_por": aberta["operador_nome"] if aberta else ""})
    return saida


def iniciar_conferencia(conn, pedido_id: int, operador: dict | None) -> dict:
    op_id, op_nome = _op(operador)
    if not op_id:
        raise ErroWMS("Operador obrigatorio na conferencia.")
    with _tx(conn):
        p = _pedido(conn, pedido_id)
        if p["estado_reserva"] == "CANCELADO":
            raise ErroWMS(f"{p['codigo_ps']} foi cancelado.")
        aberta = conn.execute("SELECT * FROM wms_conferencias WHERE pedido_id = ? AND resultado = ''",
                              (p["id"],)).fetchone()
        if aberta:
            if aberta["operador_id"] != op_id:
                raise ErroWMS(f"{p['codigo_ps']} ja esta sendo conferido por {aberta['operador_nome']}.")
            conferencia_id = aberta["id"]
        else:
            if p.get("execucao") != "SEPARADO":
                raise ErroWMS(f"{p['codigo_ps']} nao esta pronto para conferir.")
            mexeu = conn.execute(
                "SELECT 1 FROM wms_separacao_itens WHERE pedido_id = ? AND etapa <> 'DEVOLVIDA' "
                "AND (separado_por_id = ? OR distribuido_por_id = ?) LIMIT 1", (p["id"], op_id, op_id)).fetchone()
            if mexeu:
                raise ErroWMS("Quem separou ou distribuiu o pedido nao pode conferir: chame outra pessoa.")
            ts = wms.agora()
            cur = conn.execute(
                "INSERT INTO wms_conferencias (pedido_id, operador_id, operador_nome, iniciada_em) VALUES (?,?,?,?)",
                (p["id"], op_id, op_nome, ts))
            conferencia_id = cur.lastrowid
            conn.execute("UPDATE wms_pedidos SET execucao = 'CONFERINDO', atualizado_em = ? WHERE id = ?",
                         (ts, p["id"]))
    return _estado_conferencia(conn, conferencia_id)


def _estado_conferencia(conn, conferencia_id: int) -> dict:
    c = conn.execute("SELECT * FROM wms_conferencias WHERE id = ?", (int(conferencia_id),)).fetchone()
    if not c:
        raise ErroWMS("Conferencia nao encontrada.")
    p = _pedido(conn, c["pedido_id"])
    n = conn.execute("SELECT COUNT(*), SUM(serial <> '') FROM wms_conferencia_bipes WHERE conferencia_id = ?",
                     (c["id"],)).fetchone()
    return {"conferencia_id": c["id"], "pedido_id": p["id"], "codigo_ps": p["codigo_ps"],
            "destinatario": p.get("destinatario") or "", "cidade": p.get("cidade") or "",
            "embarcador": p["embarcador"], "bipes": int(n[0] or 0), "caixas_bipadas": int(n[1] or 0),
            "resultado": c["resultado"]}


def _conferencia_aberta(conn, conferencia_id: int, operador: dict | None = None) -> dict:
    """Conferencia ainda aberta. Com `operador`, so quem abriu pode bipar ou fechar: sem
    isso quem separou o pedido bipava (ou fechava) a conferencia de outra pessoa pelo id e
    a regra de "outra pessoa confere" virava enfeite (revisao de seguranca, 17/11)."""
    c = conn.execute("SELECT * FROM wms_conferencias WHERE id = ?", (int(conferencia_id),)).fetchone()
    if not c:
        raise ErroWMS("Conferencia nao encontrada.")
    if c["resultado"]:
        raise ErroWMS("Esta conferencia ja foi fechada.")
    if operador is not None and _op(operador)[0] != c["operador_id"]:
        raise ErroWMS(f"Esta conferencia e de {c['operador_nome']}: so quem abriu pode bipar ou fechar.")
    return dict(c)


def conferir_bipe(conn, conferencia_id: int, codigo: str, *, qtd_un: float | None = None, uuid: str,
                  operador: dict | None = None) -> dict:
    """
    Bipe do conferente. So entra o que foi separado PARA este pedido: a etiqueta de uma caixa
    inteira dele, ou o codigo do produto + quantidade para as unidades soltas (ate o total de
    unidades soltas que a separacao registrou). A resposta nunca diz o que era esperado.
    """
    uuid = _exigir_uuid(uuid)
    ja = conn.execute("SELECT conferencia_id FROM wms_conferencia_bipes WHERE uuid = ?", (uuid,)).fetchone()
    if ja and ja["conferencia_id"] == int(conferencia_id):
        return {**_estado_conferencia(conn, conferencia_id), "repetido": True}
    codigo = str(codigo or "").strip().upper()
    with _tx(conn):
        c = _conferencia_aberta(conn, conferencia_id, operador)
        p = _pedido(conn, c["pedido_id"])
        if p["estado_reserva"] == "CANCELADO":
            raise ErroWMS(f"{p['codigo_ps']} foi cancelado: pare a conferencia.")
        ts = wms.agora()
        inteira = conn.execute(
            "SELECT * FROM wms_separacao_itens WHERE serial = ? AND caixa_inteira = 1 "
            "AND etapa IN ('SEPARADA', 'DISTRIBUIDA', 'CONFERIDA') ORDER BY id DESC LIMIT 1", (codigo,)).fetchone()
        if inteira:
            if inteira["pedido_id"] != p["id"] or inteira["etapa"] != "DISTRIBUIDA":
                raise ErroWMS(f"Caixa {codigo} nao e deste pedido.")
            if conn.execute("SELECT 1 FROM wms_conferencia_bipes WHERE conferencia_id = ? AND serial = ?",
                            (c["id"], codigo)).fetchone():
                raise ErroWMS(f"Caixa {codigo} ja foi conferida.")
            conn.execute(
                "INSERT INTO wms_conferencia_bipes (uuid, conferencia_id, serial, produto_id, qtd_un, criado_em) "
                "VALUES (?,?,?,?,?,?)", (uuid, c["id"], codigo, inteira["produto_id"], _r(inteira["qtd_un"]), ts))
        else:
            caixa = wms_caixas.obter_caixa(conn, codigo) if codigo else None
            if caixa:
                produtos = [caixa["produto_id"]]
            else:
                produtos = [x["id"] for x in wms.buscar_por_codigo(conn, codigo)] if codigo else []
            if not produtos:
                raise ErroWMS(f"Codigo {codigo or '(vazio)'} nao reconhecido.")
            if len(produtos) > 1:
                raise ErroWMS(f"Codigo {codigo} esta em mais de um produto.")
            soltas = conn.execute(
                "SELECT COALESCE(SUM(qtd_un), 0) FROM wms_separacao_itens WHERE pedido_id = ? AND produto_id = ? "
                "AND caixa_inteira = 0 AND etapa = 'DISTRIBUIDA'", (p["id"], produtos[0])).fetchone()[0]
            if _r(soltas) <= EPS:
                if caixa:
                    raise ErroWMS(f"Caixa {codigo} nao e deste pedido.")
                raise ErroWMS("Este pedido nao tem unidade solta deste produto: bipe a etiqueta da caixa.")
            if qtd_un in (None, ""):
                raise ErroWMS("Informe a quantidade de unidades soltas deste produto.")
            try:
                q = _r(qtd_un)
            except (TypeError, ValueError):
                raise ErroWMS("Quantidade invalida.")
            if q <= EPS or q > wms_caixas.MAX_QTD_UN:
                raise ErroWMS("Quantidade invalida.")
            contado = conn.execute(
                "SELECT COALESCE(SUM(qtd_un), 0) FROM wms_conferencia_bipes WHERE conferencia_id = ? "
                "AND produto_id = ? AND serial = ''", (c["id"], produtos[0])).fetchone()[0]
            if _r(contado) + q > _r(soltas) + EPS:
                raise ErroWMS("Quantidade nao bate com as unidades soltas separadas pra este pedido: "
                              "reconte; se sobrou produto, chame o escritorio.")
            conn.execute(
                "INSERT INTO wms_conferencia_bipes (uuid, conferencia_id, serial, produto_id, qtd_un, criado_em) "
                "VALUES (?,?,'',?,?,?)", (uuid, c["id"], produtos[0], q, ts))
    return {**_estado_conferencia(conn, conferencia_id), "repetido": False}


def fechar_conferencia(conn, conferencia_id: int, operador: dict | None = None) -> dict:
    """
    Compara o bipado com o que o pedido pede AGORA. So fecha OK se bater exatamente e o pedido
    nao tiver linha sem cadastro; faltou, sobrou ou tem linha que o sistema nao sabe conferir:
    AGUARDANDO_DECISAO, e o escritorio decide. No OK, so o que foi bipado segue pro pedido
    (o que estava separado e nao foi bipado e devolvido) -- e isso que a baixa vai tirar.
    """
    with _tx(conn):
        c = _conferencia_aberta(conn, conferencia_id, operador)
        p = _pedido(conn, c["pedido_id"])
        if p["estado_reserva"] == "CANCELADO":
            raise ErroWMS(f"{p['codigo_ps']} foi cancelado: nao ha o que fechar.")
        esperado = necessidade_do_pedido(conn, p["id"])
        rows = conn.execute("SELECT produto_id, SUM(qtd_un) AS q FROM wms_conferencia_bipes "
                            "WHERE conferencia_id = ? GROUP BY produto_id", (c["id"],)).fetchall()
        conferido = {int(r["produto_id"]): _r(r["q"]) for r in rows}
        faltas, sobras = [], []
        for prod in sorted(set(esperado) | set(conferido)):
            e, k = esperado.get(prod, 0.0), conferido.get(prod, 0.0)
            if abs(e - k) <= EPS:
                continue
            info = _produto(conn, prod)
            linha = {"produto_id": prod, "sku": info.get("sku") or "", "descricao": info.get("descricao") or "",
                     "esperado_un": e, "conferido_un": k}
            (faltas if k < e else sobras).append(linha)
        sem_cadastro = [
            {"linha": r["linha"], "sku": r["sku"], "descricao": r["descricao"], "qtd": r["qtd_embalagem"],
             "motivo": r["motivo_pendencia"]}
            for r in conn.execute(
                "SELECT linha, sku, descricao, qtd_embalagem, motivo_pendencia FROM wms_pedido_itens "
                "WHERE pedido_id = ? AND removido_em = '' AND (produto_id IS NULL OR qtd_un IS NULL) ORDER BY linha",
                (p["id"],)).fetchall()]
        ts = wms.agora()
        resultado = "OK" if not (faltas or sobras or sem_cadastro) else "FALTA"
        detalhe = {"faltas": faltas, "sobras": sobras, "sem_cadastro": sem_cadastro}
        conn.execute("UPDATE wms_conferencias SET fechada_em = ?, resultado = ?, detalhe = ? WHERE id = ?",
                     (ts, resultado, json.dumps(detalhe, ensure_ascii=False), c["id"]))
        if resultado == "OK":
            _aplicar_conferencia(conn, p, c["id"], finalizar=True)
        else:
            conn.execute("UPDATE wms_pedidos SET execucao = 'AGUARDANDO_DECISAO', atualizado_em = ? WHERE id = ?",
                         (ts, p["id"]))
    return {"resultado": resultado, "codigo_ps": p["codigo_ps"], **detalhe}


def abandonar_conferencia(conn, conferencia_id: int, operador: dict | None = None, por: str = "") -> dict:
    """Larga uma conferencia aberta (bipe errado, conferente foi embora): os bipes dela deixam de
    valer e o pedido volta pra fila de conferencia. `operador` = quem abriu; sem ele, e o escritorio."""
    with _tx(conn):
        c = _conferencia_aberta(conn, conferencia_id, operador)
        ts = wms.agora()
        conn.execute("UPDATE wms_conferencias SET fechada_em = ?, resultado = 'ABANDONADA', detalhe = ? WHERE id = ?",
                     (ts, json.dumps({"por": por or _op(operador)[1]}, ensure_ascii=False), c["id"]))
        conn.execute("UPDATE wms_pedidos SET execucao = 'SEPARADO', atualizado_em = ? "
                     "WHERE id = ? AND execucao = 'CONFERINDO'", (ts, c["pedido_id"]))
    return {"codigo_ps": _pedido(conn, c["pedido_id"])["codigo_ps"]}


def conferencia_aberta_do_pedido(conn, pedido_id: int) -> int | None:
    row = conn.execute("SELECT id FROM wms_conferencias WHERE pedido_id = ? AND resultado = ''",
                       (int(pedido_id),)).fetchone()
    return row["id"] if row else None


def _aplicar_conferencia(conn, pedido: dict, conferencia_id: int | None, *, finalizar: bool,
                         operador=None) -> None:
    """
    Deixa atribuido ao pedido SO o que a conferencia bipou (e nada alem do que ele pede agora);
    o resto e devolvido, com a reserva solta e a caixa a guardar. E o que impede a baixa de tirar
    do estoque caixa que o conferente nao achou. finalizar=True marca o que ficou como CONFERIDA
    e o pedido como CONFERIDO; False (voltar pra separacao) deixa como DISTRIBUIDA.
    Dentro da transacao de quem chama.
    """
    ts = wms.agora()
    seriais, solto = set(), {}
    if conferencia_id:
        for b in conn.execute("SELECT serial, produto_id, qtd_un FROM wms_conferencia_bipes WHERE conferencia_id = ?",
                              (conferencia_id,)).fetchall():
            if b["serial"]:
                seriais.add(b["serial"])
            else:
                solto[b["produto_id"]] = _r(solto.get(b["produto_id"], 0.0) + b["qtd_un"])
    itens = conn.execute("SELECT * FROM wms_separacao_itens WHERE pedido_id = ? AND etapa = 'DISTRIBUIDA' ORDER BY id",
                         (pedido["id"],)).fetchall()
    for row in itens:
        it = dict(row)
        q = _r(it["qtd_un"])
        if it["caixa_inteira"]:
            cobre = q if it["serial"] in seriais else 0.0
        else:
            cobre = min(q, solto.get(it["produto_id"], 0.0))
            solto[it["produto_id"]] = _r(solto.get(it["produto_id"], 0.0) - cobre)
        if cobre <= EPS:
            _devolver(conn, it, operador, f"Nao conferida no {pedido['codigo_ps']}")
        elif cobre < q - EPS:
            _desfazer_reserva_manual(conn, it, qtd=_r(q - cobre))
            conn.execute("UPDATE wms_separacao_itens SET qtd_un = ?, atualizado_em = ? WHERE id = ?",
                         (cobre, ts, it["id"]))
    # nada alem do que o pedido pede agora (linha que saiu ou diminuiu na Stokki depois de separar)
    nec = necessidade_do_pedido(conn, pedido["id"])
    for prod, tem in _atribuido_ao_pedido(conn, pedido["id"]).items():
        excesso = _r(tem - nec.get(prod, 0.0))
        if excesso <= EPS:
            continue
        for row in conn.execute("SELECT * FROM wms_separacao_itens WHERE pedido_id = ? AND produto_id = ? "
                                "AND etapa = 'DISTRIBUIDA' ORDER BY id DESC", (pedido["id"], prod)).fetchall():
            if excesso <= EPS:
                break
            it = dict(row)
            q = _r(it["qtd_un"])
            if q <= excesso + EPS:
                _devolver(conn, it, operador, f"A mais no {pedido['codigo_ps']}")
                excesso = _r(excesso - q)
            elif not it["caixa_inteira"]:
                _desfazer_reserva_manual(conn, it, qtd=excesso)
                conn.execute("UPDATE wms_separacao_itens SET qtd_un = ?, atualizado_em = ? WHERE id = ?",
                             (_r(q - excesso), ts, it["id"]))
                excesso = 0.0
    if not finalizar:
        return
    for row in conn.execute("SELECT * FROM wms_separacao_itens WHERE pedido_id = ? AND etapa = 'DISTRIBUIDA'",
                            (pedido["id"],)).fetchall():
        conn.execute("UPDATE wms_separacao_itens SET etapa = 'CONFERIDA', atualizado_em = ? WHERE id = ?",
                     (ts, row["id"]))
        if row["caixa_inteira"]:
            cur = conn.execute("UPDATE wms_caixas SET situacao = 'CONFERIDA', atualizado_em = ? "
                               "WHERE serial = ? AND situacao = 'DISTRIBUIDA'", (ts, row["serial"]))
            if cur.rowcount:
                _evento_caixa(conn, row["serial"], "DISTRIBUIDA", "CONFERIDA", f"sep-conf-{row['id']}", operador,
                              f"Conferida no {pedido['codigo_ps']}", row["posicao"])
    conn.execute("UPDATE wms_pedidos SET execucao = 'CONFERIDO', atualizado_em = ? WHERE id = ?", (ts, pedido["id"]))


def faltas_pendentes(conn) -> list[dict]:
    rows = conn.execute("SELECT * FROM wms_pedidos WHERE execucao = 'AGUARDANDO_DECISAO' ORDER BY codigo_ps").fetchall()
    saida = []
    for row in rows:
        p = dict(row)
        c = conn.execute("SELECT * FROM wms_conferencias WHERE pedido_id = ? AND resultado = 'FALTA' "
                         "ORDER BY id DESC LIMIT 1", (p["id"],)).fetchone()
        detalhe = {}
        if c and c["detalhe"]:
            try:
                detalhe = json.loads(c["detalhe"])
            except ValueError:
                detalhe = {}
        saida.append({"pedido_id": p["id"], "codigo_ps": p["codigo_ps"], "embarcador": p["embarcador"],
                      "destinatario": p.get("destinatario") or "",
                      "conferente": c["operador_nome"] if c else "", "fechada_em": c["fechada_em"] if c else "",
                      "faltas": detalhe.get("faltas") or [], "sobras": detalhe.get("sobras") or [],
                      "sem_cadastro": detalhe.get("sem_cadastro") or []})
    return saida


def decidir_falta(conn, pedido_id: int, decisao: str, motivo: str, por: str) -> dict:
    """
    VOLTAR reabre a separacao (onda nova de um pedido, que so pede o que falta); PARCIAL segue
    so com o que foi conferido. Nos dois casos o que estava separado e NAO foi bipado na
    conferencia e devolvido: nao sai na baixa.
    """
    decisao = str(decisao or "").strip().upper()
    motivo = " ".join(str(motivo or "").split())
    if decisao not in ("VOLTAR", "PARCIAL"):
        raise ErroWMS("Decisao deve ser VOLTAR ou PARCIAL.")
    if len(motivo) < 3:
        raise ErroWMS("Informe o motivo da decisao.")
    with _tx(conn):
        p = _pedido(conn, pedido_id)
        if p.get("execucao") != "AGUARDANDO_DECISAO":
            raise ErroWMS(f"{p['codigo_ps']} nao esta esperando decisao.")
        c = conn.execute("SELECT id FROM wms_conferencias WHERE pedido_id = ? AND resultado = 'FALTA' "
                         "ORDER BY id DESC LIMIT 1", (p["id"],)).fetchone()
        conferencia_id = c["id"] if c else None
        ts = wms.agora()
        conn.execute("INSERT INTO wms_decisoes_falta (pedido_id, conferencia_id, decisao, motivo, por, em) "
                     "VALUES (?,?,?,?,?,?)", (p["id"], conferencia_id, decisao, motivo[:500], str(por or "")[:80], ts))
        onda_id = None
        if decisao == "VOLTAR":
            _aplicar_conferencia(conn, p, conferencia_id, finalizar=False)
            cur = conn.execute(
                "INSERT INTO wms_ondas (embarcador_id, embarcador, situacao, criada_por, criada_em, iniciada_em) "
                "VALUES (?, ?, 'EM_SEPARACAO', ?, ?, ?)",
                (int(p.get("embarcador_id") or 0), p["embarcador"], str(por or "")[:80], ts, ts))
            onda_id = cur.lastrowid
            conn.execute("INSERT INTO wms_onda_pedidos (onda_id, pedido_id) VALUES (?, ?)", (onda_id, p["id"]))
            conn.execute("UPDATE wms_pedidos SET execucao = 'SEPARANDO', onda_id = ?, atualizado_em = ? WHERE id = ?",
                         (onda_id, ts, p["id"]))
        else:
            # so o conferido sai: solta a sugestao FEFO que sobrou (o que faltou nao sera baixado)
            conn.execute("UPDATE wms_reservas SET estado = 'CANCELADA', atualizado_em = ? "
                         "WHERE pedido_id = ? AND estado = 'ATIVA' AND origem = 'FEFO'", (ts, p["id"]))
            _aplicar_conferencia(conn, p, conferencia_id, finalizar=True)
    return {"codigo_ps": p["codigo_ps"], "decisao": decisao, "onda_id": onda_id}


# ---------------------------------------------------------------------------
# Volumes
# ---------------------------------------------------------------------------

def conteudo_qr_volume(codigo_ps: str, numero: int, total: int) -> str:
    return f"FV|{str(codigo_ps or '').replace('#', '').strip().upper()}|{int(numero)}|{int(total)}"


def pedidos_para_volumes(conn, embarcadores=None) -> list[dict]:
    rows = conn.execute("SELECT * FROM wms_pedidos WHERE execucao IN ('CONFERIDO', 'EMBALADO') "
                        "AND estado_reserva <> 'BAIXADO' ORDER BY execucao, codigo_ps").fetchall()
    saida = []
    for row in rows:
        p = dict(row)
        if embarcadores is not None and int(p.get("embarcador_id") or 0) not in embarcadores:
            continue
        total = conn.execute("SELECT MAX(total) FROM wms_volumes WHERE pedido_id = ?", (p["id"],)).fetchone()[0]
        saida.append({"id": p["id"], "codigo_ps": p["codigo_ps"], "embarcador": p["embarcador"],
                      "destinatario": p.get("destinatario") or "", "execucao": p["execucao"],
                      "volumes": int(total or 0)})
    return saida


def _enfileirar_volumes(conn, pedido: dict, numeros: list[int], total: int, por: str) -> int:
    itens = [conteudo_qr_volume(pedido["codigo_ps"], n, total) for n in numeros]
    return wms_fila_impressao.enfileirar(
        conn, tipo="VOLUME", tamanho="10x15", itens=itens,
        titulo=f"{pedido['codigo_ps']} · {len(numeros)} volume(s)", pedido_por=str(por or ""))


def fechar_volumes(conn, pedido_id: int, total: int, operador: dict | None = None) -> dict:
    """O operador diz quantos volumes o pedido tem: cria 1..N, manda as etiquetas 10x15 pra fila
    e o pedido fica EMBALADO. Repetir com o mesmo total devolve a mesma impressao."""
    try:
        total = int(total)
    except (TypeError, ValueError):
        raise ErroWMS("Quantidade de volumes invalida.")
    if not 1 <= total <= MAX_VOLUMES:
        raise ErroWMS(f"Quantidade de volumes deve ficar entre 1 e {MAX_VOLUMES}.")
    with _tx(conn):
        p = _pedido(conn, pedido_id)
        if p.get("execucao") not in ("CONFERIDO", "EMBALADO"):
            raise ErroWMS(f"{p['codigo_ps']} ainda nao foi conferido.")
        atuais = conn.execute("SELECT numero, total, impressao_id FROM wms_volumes WHERE pedido_id = ? ORDER BY numero",
                              (p["id"],)).fetchall()
        if atuais and atuais[0]["total"] == total and len(atuais) == total:
            return {"codigo_ps": p["codigo_ps"], "total": total, "impressao_id": atuais[0]["impressao_id"],
                    "repetido": True}
        ts = wms.agora()
        conn.execute("DELETE FROM wms_volumes WHERE pedido_id = ?", (p["id"],))
        impressao_id = _enfileirar_volumes(conn, p, list(range(1, total + 1)), total, _op(operador)[1])
        for n in range(1, total + 1):
            conn.execute("INSERT INTO wms_volumes (pedido_id, numero, total, impressao_id, criado_em) "
                         "VALUES (?,?,?,?,?)", (p["id"], n, total, impressao_id, ts))
        conn.execute("UPDATE wms_pedidos SET execucao = 'EMBALADO', atualizado_em = ? WHERE id = ?", (ts, p["id"]))
    return {"codigo_ps": p["codigo_ps"], "total": total, "impressao_id": impressao_id, "repetido": False}


def reimprimir_volume(conn, pedido_id: int, numero: int, por: str = "") -> dict:
    with _tx(conn):
        p = _pedido(conn, pedido_id)
        v = conn.execute("SELECT * FROM wms_volumes WHERE pedido_id = ? AND numero = ?",
                         (p["id"], int(numero))).fetchone()
        if not v:
            raise ErroWMS(f"{p['codigo_ps']} nao tem o volume {numero}.")
        impressao_id = _enfileirar_volumes(conn, p, [v["numero"]], v["total"], por)
        conn.execute("UPDATE wms_volumes SET reimpressoes = reimpressoes + 1 WHERE pedido_id = ? AND numero = ?",
                     (p["id"], v["numero"]))
    return {"codigo_ps": p["codigo_ps"], "numero": int(numero), "impressao_id": impressao_id}


def dados_do_volume(conn, conteudo: str) -> dict | None:
    """FV|PS|n|total -> o que vai na etiqueta (ou None se o texto nao for de volume)."""
    partes = str(conteudo or "").strip().split("|")
    if len(partes) != 4 or partes[0].upper() != "FV":
        return None
    try:
        numero, total = int(partes[2]), int(partes[3])
    except ValueError:
        return None
    row = conn.execute("SELECT * FROM wms_pedidos WHERE upper(replace(codigo_ps, '#', '')) = ? ORDER BY id DESC LIMIT 1",
                       (partes[1].upper(),)).fetchone()
    p = dict(row) if row else {}
    return {"codigo_ps": partes[1].upper(), "numero": numero, "total": total, "qr": conteudo.strip(),
            "embarcador": p.get("embarcador") or "", "destinatario": p.get("destinatario") or "",
            "endereco": p.get("endereco_destino") or "", "cidade": p.get("cidade") or "", "uf": p.get("uf") or "",
            "nf": p.get("nf") or ""}


# ---------------------------------------------------------------------------
# Acompanhamento (escritorio)
# ---------------------------------------------------------------------------

def andamento(conn, embarcador_id: int | None = None, limite: int = 200) -> list[dict]:
    sql = "SELECT * FROM wms_pedidos WHERE COALESCE(execucao, '') <> ''"
    args: list = []
    if embarcador_id:
        sql += " AND embarcador_id = ?"
        args.append(str(int(embarcador_id)))
    sql += " ORDER BY atualizado_em DESC LIMIT ?"
    args.append(int(limite))
    saida = []
    for row in conn.execute(sql, args).fetchall():
        p = dict(row)
        nomes = conn.execute(
            "SELECT GROUP_CONCAT(DISTINCT separado_por_nome) AS sep, COUNT(*) AS itens, MIN(criado_em) AS inicio "
            "FROM wms_separacao_itens WHERE pedido_id = ? AND etapa <> 'DEVOLVIDA'", (p["id"],)).fetchone()
        conf = conn.execute("SELECT operador_nome, resultado, fechada_em FROM wms_conferencias WHERE pedido_id = ? "
                            "ORDER BY id DESC LIMIT 1", (p["id"],)).fetchone()
        volumes = conn.execute("SELECT MAX(total) FROM wms_volumes WHERE pedido_id = ?", (p["id"],)).fetchone()[0]
        data, fonte = data_entrega_do_pedido(conn, p)
        saida.append({
            "pedido_id": p["id"], "codigo_ps": p["codigo_ps"], "embarcador": p["embarcador"],
            "destinatario": p.get("destinatario") or "", "execucao": p["execucao"], "onda_id": p.get("onda_id"),
            "situacao_stokki": p["situacao"], "estado_reserva": p["estado_reserva"],
            "data_entrega": data, "fonte_data": fonte,
            "separado_por": nomes["sep"] or "", "itens_separados": int(nomes["itens"] or 0),
            "inicio_separacao": nomes["inicio"] or "",
            "conferente": conf["operador_nome"] if conf else "", "conferencia": conf["resultado"] if conf else "",
            "conferido_em": conf["fechada_em"] if conf else "", "volumes": int(volumes or 0),
            "atualizado_em": p["atualizado_em"],
        })
    return saida


# ---------------------------------------------------------------------------
# Ganchos chamados por wms_pedidos (baixa e cancelamento)
# ---------------------------------------------------------------------------

def ao_cancelar_pedido(conn, pedido_id: int) -> int:
    """Pedido cancelado (ou reservas liberadas): o que estava separado pra ele e devolvido e o
    pedido sai do fluxo e da onda. NAO faz commit (roda na transacao de quem chama)."""
    # Banco que nunca abriu a separacao (o sync conecta so com wms_pedidos): nada a devolver,
    # e criar tabela aqui fecharia a transacao de quem chamou.
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'wms_separacao_itens'").fetchone():
        return 0
    ts = wms.agora()
    itens = conn.execute("SELECT * FROM wms_separacao_itens WHERE pedido_id = ? "
                         "AND etapa IN ('SEPARADA', 'DISTRIBUIDA', 'CONFERIDA')", (int(pedido_id),)).fetchall()
    for it in itens:
        _devolver(conn, dict(it), None, "Pedido cancelado: guardar a caixa")
    conn.execute("UPDATE wms_conferencias SET resultado = 'ABANDONADA', fechada_em = ? "
                 "WHERE pedido_id = ? AND resultado = ''", (ts, int(pedido_id)))
    ondas = [r["onda_id"] for r in conn.execute(
        "SELECT op.onda_id FROM wms_onda_pedidos op JOIN wms_ondas o ON o.id = op.onda_id "
        "WHERE op.pedido_id = ? AND o.situacao IN ('ABERTA', 'EM_SEPARACAO')", (int(pedido_id),)).fetchall()]
    for onda_id in ondas:
        conn.execute("DELETE FROM wms_onda_pedidos WHERE onda_id = ? AND pedido_id = ?", (onda_id, int(pedido_id)))
        if not conn.execute("SELECT 1 FROM wms_onda_pedidos WHERE onda_id = ?", (onda_id,)).fetchone():
            # onda ficou sem pedido: o que estava separado sem dono volta, e ela encerra
            for it in conn.execute("SELECT * FROM wms_separacao_itens WHERE onda_id = ? AND etapa = 'SEPARADA' "
                                   "AND pedido_id IS NULL", (onda_id,)).fetchall():
                _devolver(conn, dict(it), None, "Onda sem pedido: guardar a caixa")
            conn.execute("UPDATE wms_ondas SET situacao = 'CANCELADA', concluida_em = ? WHERE id = ?", (ts, onda_id))
    conn.execute("UPDATE wms_pedidos SET execucao = '', onda_id = NULL WHERE id = ? AND COALESCE(execucao, '') <> ''",
                 (int(pedido_id),))
    return len(itens)


def consumir_do_pedido(conn, *, pedido_id: int, reserva_id: int, movimento_uuid: str, qtd_un: float) -> float:
    """
    Baixa da expedicao de UMA reserva do pedido: consome os itens separados que essa reserva
    representa (cada item bipado tem a reserva MANUAL dele). Caixa inteira vira EXPEDIDA; parte
    de caixa desconta a quantidade da caixa de origem. Devolve quanto da reserva nao tinha item
    (reserva FEFO de pedido que nao passou pela separacao): isso segue pelo caminho antigo
    (wms_caixas.consumir_caixas). NAO faz commit.
    """
    restante = _r(qtd_un)
    itens = conn.execute(
        "SELECT * FROM wms_separacao_itens WHERE pedido_id = ? AND reserva_id = ? "
        "AND etapa IN ('SEPARADA', 'DISTRIBUIDA', 'CONFERIDA') ORDER BY id", (int(pedido_id), int(reserva_id))).fetchall()
    ts = wms.agora()
    marcas = ",".join("?" * len(ETAPAS_VIVAS))
    for it in itens:
        q = _r(it["qtd_un"])
        conn.execute("UPDATE wms_separacao_itens SET etapa = 'EXPEDIDA', atualizado_em = ? WHERE id = ?",
                     (ts, it["id"]))
        caixa = conn.execute("SELECT situacao, qtd_un FROM wms_caixas WHERE serial = ?", (it["serial"],)).fetchone()
        if caixa:
            sobra = _r(caixa["qtd_un"] - q)
            if sobra <= EPS:
                # qtd_un da caixa expedida fica como o que saiu (mesma convencao de consumir_caixas)
                para = "EXPEDIDA"
                conn.execute("UPDATE wms_caixas SET situacao = 'EXPEDIDA', atualizado_em = ? WHERE serial = ?",
                             (ts, it["serial"]))
            else:
                outros = conn.execute(
                    f"SELECT COUNT(*) FROM wms_separacao_itens WHERE serial = ? AND etapa IN ({marcas})",
                    (it["serial"], *ETAPAS_VIVAS)).fetchone()[0]
                if caixa["situacao"] in ("EM_ESTOQUE", "ABERTA"):
                    para = "ABERTA"          # nunca saiu da prateleira: so cedeu unidades
                elif outros:
                    para = caixa["situacao"]  # ainda tem parte dela esperando outro pedido
                else:
                    para = "DEVOLVIDA"       # saiu da prateleira e sobrou: alguem precisa guardar
                conn.execute("UPDATE wms_caixas SET situacao = ?, qtd_un = ?, atualizado_em = ? WHERE serial = ?",
                             (para, sobra, ts, it["serial"]))
            # movimento_uuid fica de fora de proposito: consumir_caixas usa "ja existe evento
            # com este movimento" como marca de ja consumido, e ainda roda pro que sobrar.
            conn.execute(
                "INSERT OR IGNORE INTO wms_caixa_eventos (uuid, serial, de, para, posicao, observacao, criado_em) "
                "VALUES (?,?,?,?,?,?,?)",
                (f"{movimento_uuid}-sep-{it['id']}", it["serial"], caixa["situacao"], para, it["posicao"],
                 f"Baixa da expedicao (separado, mov {movimento_uuid}): {q:g} UN", ts))
        restante = _r(restante - q)
    return max(restante, 0.0)


