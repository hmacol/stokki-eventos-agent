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

# colunas que este modulo acrescenta em wms_pedidos (migracao aditiva)
_COLUNAS_PEDIDO = {"execucao": "TEXT NOT NULL DEFAULT ''", "onda_id": "INTEGER",
                   "embarcador_id": "TEXT NOT NULL DEFAULT ''"}


def garantir_tabelas(conn) -> None:
    for sql in _DDL:
        conn.execute(sql)
    existentes = {r[1] for r in conn.execute("PRAGMA table_info(wms_pedidos)")}
    for nome, tipo in _COLUNAS_PEDIDO.items():
        if existentes and nome not in existentes:
            conn.execute(f"ALTER TABLE wms_pedidos ADD COLUMN {nome} {tipo}")
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
    """{produto_id: qtd_un} que o pedido pede AGORA (linhas vivas e resolvidas)."""
    rows = conn.execute(
        "SELECT produto_id, SUM(qtd_un) AS q FROM wms_pedido_itens WHERE pedido_id = ? AND removido_em = '' "
        "AND produto_id IS NOT NULL AND qtd_un IS NOT NULL GROUP BY produto_id", (int(pedido_id),)).fetchall()
    return {int(r["produto_id"]): _r(r["q"]) for r in rows}


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
    """Onda + o que falta por produto + a caminhada sugerida (reservas FEFO ainda nao bipadas)."""
    onda = _onda(conn, onda_id)
    pedidos = _pedidos_da_onda(conn, onda_id)
    pedido_un: dict[int, float] = {}
    por_pedido = []
    for p in pedidos:
        nec = necessidade_do_pedido(conn, p["id"])
        atr = _atribuido_ao_pedido(conn, p["id"])
        for prod, q in nec.items():
            pedido_un[prod] = _r(pedido_un.get(prod, 0) + q)
        por_pedido.append({"id": p["id"], "codigo_ps": p["codigo_ps"], "execucao": p.get("execucao") or "",
                           "destinatario": p.get("destinatario") or "",
                           "pedido_un": _r(sum(nec.values())), "atribuido_un": _r(sum(atr.values()))})
    separado = _separado_da_onda(conn, onda_id)
    distribuido = _somas(conn, "onda_id = ? AND pedido_id IS NOT NULL", (int(onda_id),))
    produtos = []
    for prod, q in sorted(pedido_un.items()):
        info = _produto(conn, prod)
        produtos.append({"produto_id": prod, "sku": info.get("sku") or "", "descricao": info.get("descricao") or "",
                         "ean": info.get("ean") or "", "pedido_un": q, "separado_un": separado.get(prod, 0.0),
                         "distribuido_un": distribuido.get(prod, 0.0)})
    caminhada = []
    if pedidos:
        marcas = ",".join("?" * len(pedidos))
        rows = conn.execute(
            f"SELECT posicao, produto_id, lote, validade, SUM(quantidade_un) AS q FROM wms_reservas "
            f"WHERE pedido_id IN ({marcas}) AND estado = 'ATIVA' AND origem = 'FEFO' "
            f"GROUP BY posicao, produto_id, lote, validade ORDER BY posicao, produto_id, validade",
            tuple(p["id"] for p in pedidos)).fetchall()
        for r in rows:
            info = _produto(conn, r["produto_id"])
            caminhada.append({"posicao": r["posicao"], "produto_id": r["produto_id"], "sku": info.get("sku") or "",
                              "descricao": info.get("descricao") or "", "lote": r["lote"],
                              "validade": r["validade"], "qtd_un": _r(r["q"])})
    soltas = conn.execute(
        "SELECT COUNT(*) FROM wms_separacao_itens WHERE onda_id = ? AND etapa = 'SEPARADA' AND pedido_id IS NULL",
        (int(onda_id),)).fetchone()[0]
    return {"onda": onda, "pedidos": por_pedido, "produtos": produtos, "caminhada": caminhada,
            "a_distribuir": int(soltas)}


# ---------------------------------------------------------------------------
# Reserva do pedido aponta pra caixa bipada
# ---------------------------------------------------------------------------

def _reservar_manual(conn, pedido_id: int, produto_id: int, posicao: str, lote: str, validade: str, qtd: float) -> None:
    """Troca `qtd` da reserva sugerida (FEFO) do pedido por reserva MANUAL na chave da caixa bipada.
    Roda dentro da transacao de quem chama."""
    ts = wms.agora()
    restante = _r(qtd)
    fefo = conn.execute(
        "SELECT * FROM wms_reservas WHERE pedido_id = ? AND produto_id = ? AND estado = 'ATIVA' AND origem = 'FEFO' "
        "ORDER BY (posicao = ? AND lote = ? AND validade = ?) DESC, id",
        (pedido_id, produto_id, posicao, lote, validade)).fetchall()
    partes = []  # (item_id, qtd)
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
        partes.append((r["item_id"], tira))
        restante = _r(restante - tira)
    if restante > EPS:
        item = conn.execute(
            "SELECT id FROM wms_pedido_itens WHERE pedido_id = ? AND produto_id = ? AND removido_em = '' "
            "ORDER BY linha LIMIT 1", (pedido_id, produto_id)).fetchone()
        if item:
            partes.append((item["id"], restante))
    for item_id, q in partes:
        conn.execute(
            "INSERT INTO wms_reservas (pedido_id, item_id, produto_id, posicao, lote, validade, quantidade_un, "
            "estado, origem, criado_em, atualizado_em) VALUES (?,?,?,?,?,?,?, 'ATIVA', 'MANUAL', ?, ?)",
            (pedido_id, item_id, produto_id, posicao, lote, validade, _r(q), ts, ts))
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


def _desfazer_reserva_manual(conn, pedido_id: int, item: dict) -> None:
    """Item que saiu do pedido (devolvido): tira `qtd` da reserva MANUAL daquela chave."""
    ts = wms.agora()
    restante = _r(item["qtd_un"])
    rows = conn.execute(
        "SELECT id, quantidade_un FROM wms_reservas WHERE pedido_id = ? AND produto_id = ? AND posicao = ? "
        "AND lote = ? AND validade = ? AND estado = 'ATIVA' AND origem = 'MANUAL' ORDER BY id DESC",
        (pedido_id, item["produto_id"], item["posicao"], item["lote"], item["validade"])).fetchall()
    for r in rows:
        if restante <= EPS:
            break
        tira = min(_r(r["quantidade_un"]), restante)
        sobra = _r(r["quantidade_un"] - tira)
        if sobra <= EPS:
            conn.execute("UPDATE wms_reservas SET estado = 'CANCELADA', atualizado_em = ? WHERE id = ?", (ts, r["id"]))
        else:
            conn.execute("UPDATE wms_reservas SET quantidade_un = ?, atualizado_em = ? WHERE id = ?",
                         (sobra, ts, r["id"]))
        restante = _r(restante - tira)


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
    conn.execute(
        "UPDATE wms_separacao_itens SET pedido_id = ?, etapa = 'DISTRIBUIDA', dist_uuid = ?, "
        "distribuido_por_id = ?, distribuido_por_nome = ?, atualizado_em = ? WHERE id = ?",
        (pedido["id"], dist_uuid, op_id, op_nome, ts, item["id"]))
    if item["caixa_inteira"]:
        cur = conn.execute("UPDATE wms_caixas SET situacao = 'DISTRIBUIDA', atualizado_em = ? "
                           "WHERE serial = ? AND situacao = 'SEPARADA'", (ts, item["serial"]))
        if cur.rowcount:
            _evento_caixa(conn, item["serial"], "SEPARADA", "DISTRIBUIDA", f"sep-dist-{dist_uuid}", operador,
                          f"Distribuida para {pedido['codigo_ps']}", item["posicao"])
    _reservar_manual(conn, pedido["id"], item["produto_id"], item["posicao"], item["lote"], item["validade"],
                     item["qtd_un"])


# ---------------------------------------------------------------------------
# Separar
# ---------------------------------------------------------------------------

def _resposta_separar(conn, item: dict, repetido: bool) -> dict:
    produto = _produto(conn, item["produto_id"])
    nec = {}
    for p in _pedidos_da_onda(conn, item["onda_id"]):
        for prod, q in necessidade_do_pedido(conn, p["id"]).items():
            nec[prod] = _r(nec.get(prod, 0) + q)
    separado = _separado_da_onda(conn, item["onda_id"]).get(item["produto_id"], 0.0)
    codigo_ps = ""
    if item["pedido_id"]:
        codigo_ps = _pedido(conn, item["pedido_id"])["codigo_ps"]
    return {"item_id": item["id"], "serial": item["serial"], "produto_id": item["produto_id"],
            "produto": _rotulo(produto), "qtd_un": _r(item["qtd_un"]), "caixa_inteira": bool(item["caixa_inteira"]),
            "codigo_ps": codigo_ps, "falta_un": max(_r(nec.get(item["produto_id"], 0) - separado), 0.0),
            "repetido": repetido}


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
        if caixa["situacao"] == "DEVOLVIDA":
            raise ErroWMS(f"Caixa {serial} esta devolvida: guarde numa posicao (Mover) antes de separar.")
        if caixa["situacao"] not in ("EM_ESTOQUE", "ABERTA"):
            raise ErroWMS(f"Caixa {serial} esta {caixa['situacao'].lower()}: nao pode ser separada.")
        pedidos = _pedidos_da_onda(conn, onda_id)
        nec = 0.0
        for p in pedidos:
            nec += necessidade_do_pedido(conn, p["id"]).get(caixa["produto_id"], 0.0)
        falta = _r(nec - _separado_da_onda(conn, onda_id).get(caixa["produto_id"], 0.0))
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
                "WHERE serial = ? AND situacao IN ('EM_ESTOQUE', 'ABERTA')", (ts, serial))
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
    """Desfaz um bipe de separacao que ainda nao foi conferido. Caixa inteira fica DEVOLVIDA
    ate alguem bipar a posicao onde guardou (C13); parte de caixa so libera a quantidade."""
    with _tx(conn):
        row = conn.execute("SELECT * FROM wms_separacao_itens WHERE id = ?", (int(item_id),)).fetchone()
        if not row:
            raise ErroWMS("Item de separacao nao encontrado.")
        item = dict(row)
        if item["etapa"] not in ("SEPARADA", "DISTRIBUIDA"):
            raise ErroWMS(f"Item ja esta {item['etapa'].lower()}: nao pode ser devolvido.")
        ts = wms.agora()
        conn.execute("UPDATE wms_separacao_itens SET etapa = 'DEVOLVIDA', atualizado_em = ? WHERE id = ?",
                     (ts, item["id"]))
        if item["pedido_id"]:
            _desfazer_reserva_manual(conn, item["pedido_id"], item)
        if item["caixa_inteira"]:
            cur = conn.execute("UPDATE wms_caixas SET situacao = 'DEVOLVIDA', atualizado_em = ? "
                               "WHERE serial = ? AND situacao IN ('SEPARADA', 'DISTRIBUIDA')", (ts, item["serial"]))
            if cur.rowcount:
                _evento_caixa(conn, item["serial"], item["etapa"], "DEVOLVIDA", f"sep-dev-{item['id']}", operador,
                              f"Devolvida da onda {item['onda_id']}. {motivo}".strip(), item["posicao"])
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
        candidatos = []
        for p in _pedidos_da_onda(conn, onda_id):
            falta = _r(necessidade_do_pedido(conn, p["id"]).get(item["produto_id"], 0.0)
                       - _atribuido_ao_pedido(conn, p["id"]).get(item["produto_id"], 0.0))
            if falta > EPS:
                candidatos.append((p, falta))
        if not candidatos:
            raise ErroWMS("Nenhum pedido da onda precisa mais deste produto: devolva a caixa.")
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
    """Pedido com tudo distribuido vira SEPARADO. Com mesmo_faltando, os incompletos tambem
    seguem (a falta aparece na conferencia). Onda fecha quando nenhum pedido ficou separando."""
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
        prontos, faltando = [], []
        for p in _pedidos_da_onda(conn, onda_id):
            if p.get("execucao") != "SEPARANDO":
                continue
            nec = necessidade_do_pedido(conn, p["id"])
            atr = _atribuido_ao_pedido(conn, p["id"])
            falta = _r(sum(max(q - atr.get(prod, 0.0), 0.0) for prod, q in nec.items()))
            if falta > EPS:
                faltando.append({"codigo_ps": p["codigo_ps"], "falta_un": falta})
                if not mesmo_faltando:
                    continue
            conn.execute("UPDATE wms_pedidos SET execucao = 'SEPARADO', atualizado_em = ? WHERE id = ?", (ts, p["id"]))
            prontos.append(p["codigo_ps"])
        resta = conn.execute("SELECT COUNT(*) FROM wms_pedidos WHERE onda_id = ? AND execucao = 'SEPARANDO'",
                             (onda_id,)).fetchone()[0]
        if not resta:
            conn.execute("UPDATE wms_ondas SET situacao = 'CONCLUIDA', concluida_em = ? WHERE id = ?", (ts, onda_id))
    return {"prontos": prontos, "faltando": faltando, "onda_concluida": not resta}


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
    """Bipe do conferente. Serial de caixa inteira do pedido, ou codigo do produto + quantidade
    (parte de caixa). A resposta nunca diz o que era esperado."""
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
        item = conn.execute(
            "SELECT * FROM wms_separacao_itens WHERE serial = ? AND caixa_inteira = 1 "
            "AND etapa IN ('SEPARADA', 'DISTRIBUIDA', 'CONFERIDA') ORDER BY id DESC LIMIT 1", (codigo,)).fetchone()
        if item:
            if item["pedido_id"] != p["id"]:
                raise ErroWMS(f"Caixa {codigo} nao e deste pedido.")
            if conn.execute("SELECT 1 FROM wms_conferencia_bipes WHERE conferencia_id = ? AND serial = ?",
                            (c["id"], codigo)).fetchone():
                raise ErroWMS(f"Caixa {codigo} ja foi conferida.")
            conn.execute(
                "INSERT INTO wms_conferencia_bipes (uuid, conferencia_id, serial, produto_id, qtd_un, criado_em) "
                "VALUES (?,?,?,?,?,?)", (uuid, c["id"], codigo, item["produto_id"], _r(item["qtd_un"]), ts))
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
            if qtd_un in (None, ""):
                if caixa:
                    raise ErroWMS(f"Caixa {codigo} nao esta inteira neste pedido: informe a quantidade.")
                raise ErroWMS("Informe a quantidade deste produto.")
            try:
                q = _r(qtd_un)
            except (TypeError, ValueError):
                raise ErroWMS("Quantidade invalida.")
            if q <= EPS or q > wms_caixas.MAX_QTD_UN:
                raise ErroWMS("Quantidade invalida.")
            conn.execute(
                "INSERT INTO wms_conferencia_bipes (uuid, conferencia_id, serial, produto_id, qtd_un, criado_em) "
                "VALUES (?,?,'',?,?,?)", (uuid, c["id"], produtos[0], q, ts))
    return {**_estado_conferencia(conn, conferencia_id), "repetido": False}


def fechar_conferencia(conn, conferencia_id: int, operador: dict | None = None) -> dict:
    """Compara o bipado com o que o pedido pede AGORA. Bateu (ou sobrou): CONFERIDO.
    Faltou: AGUARDANDO_DECISAO, e o escritorio decide."""
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
        ts = wms.agora()
        resultado = "FALTA" if faltas else "OK"
        conn.execute("UPDATE wms_conferencias SET fechada_em = ?, resultado = ?, detalhe = ? WHERE id = ?",
                     (ts, resultado, json.dumps({"faltas": faltas, "sobras": sobras}, ensure_ascii=False), c["id"]))
        if faltas:
            conn.execute("UPDATE wms_pedidos SET execucao = 'AGUARDANDO_DECISAO', atualizado_em = ? WHERE id = ?",
                         (ts, p["id"]))
        else:
            _marcar_conferido(conn, p, ts)
    return {"resultado": resultado, "faltas": faltas, "sobras": sobras, "codigo_ps": p["codigo_ps"]}


def _marcar_conferido(conn, pedido: dict, ts: str) -> None:
    conn.execute("UPDATE wms_pedidos SET execucao = 'CONFERIDO', atualizado_em = ? WHERE id = ?", (ts, pedido["id"]))
    itens = conn.execute("SELECT * FROM wms_separacao_itens WHERE pedido_id = ? AND etapa = 'DISTRIBUIDA'",
                         (pedido["id"],)).fetchall()
    for it in itens:
        conn.execute("UPDATE wms_separacao_itens SET etapa = 'CONFERIDA', atualizado_em = ? WHERE id = ?",
                     (ts, it["id"]))
        if it["caixa_inteira"]:
            cur = conn.execute("UPDATE wms_caixas SET situacao = 'CONFERIDA', atualizado_em = ? "
                               "WHERE serial = ? AND situacao = 'DISTRIBUIDA'", (ts, it["serial"]))
            if cur.rowcount:
                _evento_caixa(conn, it["serial"], "DISTRIBUIDA", "CONFERIDA", f"sep-conf-{it['id']}", None,
                              f"Conferida no {pedido['codigo_ps']}", it["posicao"])


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
                      "faltas": detalhe.get("faltas") or [], "sobras": detalhe.get("sobras") or []})
    return saida


def decidir_falta(conn, pedido_id: int, decisao: str, motivo: str, por: str) -> dict:
    """VOLTAR reabre a separacao (onda nova de um pedido); PARCIAL segue so com o conferido."""
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
        ts = wms.agora()
        conn.execute("INSERT INTO wms_decisoes_falta (pedido_id, conferencia_id, decisao, motivo, por, em) "
                     "VALUES (?,?,?,?,?,?)", (p["id"], c["id"] if c else None, decisao, motivo[:500], str(por or "")[:80], ts))
        onda_id = None
        if decisao == "VOLTAR":
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
            _marcar_conferido(conn, p, ts)
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
    """Pedido cancelado (ou reservas liberadas): o que estava separado pra ele vira DEVOLVIDA e o
    pedido sai do fluxo. NAO faz commit (roda na transacao de quem chama)."""
    # Banco que nunca abriu a separacao (o sync conecta so com wms_pedidos): nada a devolver,
    # e criar tabela aqui fecharia a transacao de quem chamou.
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'wms_separacao_itens'").fetchone():
        return 0
    ts = wms.agora()
    itens = conn.execute("SELECT * FROM wms_separacao_itens WHERE pedido_id = ? "
                         "AND etapa IN ('SEPARADA', 'DISTRIBUIDA', 'CONFERIDA')", (int(pedido_id),)).fetchall()
    for it in itens:
        conn.execute("UPDATE wms_separacao_itens SET etapa = 'DEVOLVIDA', atualizado_em = ? WHERE id = ?",
                     (ts, it["id"]))
        if it["caixa_inteira"]:
            cur = conn.execute("UPDATE wms_caixas SET situacao = 'DEVOLVIDA', atualizado_em = ? WHERE serial = ? "
                               "AND situacao IN ('SEPARADA', 'DISTRIBUIDA', 'CONFERIDA')", (ts, it["serial"]))
            if cur.rowcount:
                _evento_caixa(conn, it["serial"], it["etapa"], "DEVOLVIDA", f"sep-canc-{it['id']}", None,
                              "Pedido cancelado: guardar a caixa", it["posicao"])
    conn.execute("UPDATE wms_conferencias SET resultado = 'ABANDONADA', fechada_em = ? "
                 "WHERE pedido_id = ? AND resultado = ''", (ts, int(pedido_id)))
    conn.execute("UPDATE wms_pedidos SET execucao = '', onda_id = NULL WHERE id = ? AND COALESCE(execucao, '') <> ''",
                 (int(pedido_id),))
    return len(itens)


def consumir_do_pedido(conn, *, pedido_id: int, posicao: str, produto_id: int, lote: str, validade: str | None,
                       qtd_un: float, movimento_uuid: str) -> float:
    """
    Baixa da expedicao de uma reserva do pedido: consome primeiro o que foi separado
    PARA ELE naquela chave. Caixa inteira vira EXPEDIDA; parte de caixa desconta a
    quantidade da caixa de origem. Devolve quanto ainda falta consumir pelo caminho
    antigo (wms_caixas.consumir_caixas). NAO faz commit.
    """
    restante = _r(qtd_un)
    validade = str(validade or "")
    itens = conn.execute(
        "SELECT * FROM wms_separacao_itens WHERE pedido_id = ? AND produto_id = ? AND posicao = ? AND lote = ? "
        "AND validade = ? AND etapa IN ('SEPARADA', 'DISTRIBUIDA', 'CONFERIDA') ORDER BY caixa_inteira DESC, id",
        (int(pedido_id), int(produto_id), posicao, lote, validade)).fetchall()
    ts = wms.agora()
    for it in itens:
        if restante <= EPS:
            break
        q = _r(it["qtd_un"])
        if q > restante + EPS:
            continue  # a reserva e menor que este item: deixa pro caminho antigo, sem partir o item
        caixa = conn.execute("SELECT situacao, qtd_un FROM wms_caixas WHERE serial = ?", (it["serial"],)).fetchone()
        if caixa:
            sobra = _r(caixa["qtd_un"] - q)
            if it["caixa_inteira"] or sobra <= EPS:
                # qtd_un da caixa expedida fica como o que saiu (mesma convencao de consumir_caixas)
                conn.execute("UPDATE wms_caixas SET situacao = 'EXPEDIDA', atualizado_em = ? WHERE serial = ?",
                             (ts, it["serial"]))
                para = "EXPEDIDA"
            else:
                para = "ABERTA" if caixa["situacao"] in ("EM_ESTOQUE", "ABERTA") else caixa["situacao"]
                conn.execute("UPDATE wms_caixas SET situacao = ?, qtd_un = ?, atualizado_em = ? WHERE serial = ?",
                             (para, sobra, ts, it["serial"]))
            conn.execute(
                # movimento_uuid fica de fora de proposito: consumir_caixas usa "ja existe evento
                # com este movimento" como marca de ja consumido, e ainda roda pro que sobrar.
                "INSERT OR IGNORE INTO wms_caixa_eventos (uuid, serial, de, para, posicao, observacao, criado_em) "
                "VALUES (?,?,?,?,?,?,?)",
                (f"{movimento_uuid}-sep-{it['id']}", it["serial"], caixa["situacao"], para, posicao,
                 f"Baixa da expedicao (separado, mov {movimento_uuid}): {q:g} UN", ts))
        conn.execute("UPDATE wms_separacao_itens SET etapa = 'EXPEDIDA', atualizado_em = ? WHERE id = ?",
                     (ts, it["id"]))
        restante = _r(restante - q)
    return max(restante, 0.0)
