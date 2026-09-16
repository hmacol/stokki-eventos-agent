"""
nucleo/consulta.py

Consulta de rotas e pedidos JÁ CRIADOS, lendo só o núcleo próprio
(dados/dados.db) -- nunca a VUUPT ao vivo (Hugo, 12/09).

Por que existir: a Torre e o Planejamento são sempre do DIA (data-alvo).
Não havia como responder "o que aconteceu com o pedido PS-12345 na semana
passada?" sem abrir a VUUPT na mão. O histórico já acumula sozinho via
nucleo/sincronizar_vuupt.py (timer de 30 min na VPS); aqui só se LÊ.

Esta é a camada única por trás da tela /consulta do painel e, na Fase 3,
do histórico do portal do cliente -- as telas são casca, toda regra de
busca e de recorte mora aqui.

RECORTE POR EMBARCADOR: `sender_id`, quando vem preenchido, limita tudo
ao remetente daquele id -- rota só aparece se tiver parada dele, e as
paradas dos OUTROS embarcadores da mesma rota são removidas. É o que o
portal do cliente vai usar, e lá o sender_id sai da SESSÃO, nunca do
request (mesmo padrão de portal_cliente/app.py:409).

Os campos internos (motorista, km, pedágio) continuam saindo daqui: quem
esconde do cliente é a Fase 3, na borda -- esta camada não decide
visibilidade.
"""
import json
import logging
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from nucleo import banco
from nucleo.normalizacao import normalizar_codigo

logger = logging.getLogger(__name__)

_RAIZ = Path(__file__).parent.parent

LIMITE_PADRAO = 200

# Dígito puro é ambíguo (id de rota OU número do pedido sem o prefixo).
# Abaixo disso, só id de rota: "2" casando PS-xxxx2 pelo sufixo trazia
# rota demais e escondia a rota 2, que é o que a pessoa pediu.
_MIN_DIGITOS_CODIGO = 4

# Como o termo digitado foi entendido -- vai pra tela, pra ficar óbvio
# quando a busca "não achou" porque interpretou outra coisa.
TERMO_ROTA = "ROTA"
TERMO_PEDIDO = "PEDIDO"
TERMO_PLACA = "PLACA"
TERMO_TEXTO = "TEXTO"
TERMO_VAZIO = "VAZIO"

# AAA0000 (antiga) e AAA0A00 (Mercosul), com ou sem hífen/espaço.
_RE_PLACA = re.compile(r"^[A-Z]{3}[- ]?\d[A-Z0-9]\d{2}$")
# PS-12345, #PS-12345, "ps 12345" e a reentrega PS-12345-R1. O '#' é a
# forma que a VUUPT usa desde 20/08 e vai colado no que o usuário copia da
# tela dela; sem aceitá-lo aqui, a busca caía em "texto" e não achava nada.
_RE_CODIGO = re.compile(r"^#?\s*[A-Z]{1,4}[-\s]?\d{2,}(?:-[A-Z0-9]+)*$")


def _ler_json(bruto):
    if not bruto:
        return {}
    try:
        return json.loads(bruto)
    except (TypeError, ValueError):
        return {}


def _iso(d) -> str | None:
    if d in (None, ""):
        return None
    return d.isoformat() if isinstance(d, date) else str(d)


def _periodo(de, ate, dias_padrao: int = 60) -> tuple[str, str]:
    """Janela da busca. Sem nada informado, olha os últimos `dias_padrao`
    dias -- é a profundidade que o backfill do sincronizar_vuupt cobre."""
    fim = _iso(ate) or date.today().isoformat()
    inicio = _iso(de) or (date.fromisoformat(fim) - timedelta(days=dias_padrao)).isoformat()
    if inicio > fim:
        inicio, fim = fim, inicio
    return inicio, fim


# ── Interpretação do termo ────────────────────────────────────────────────────

def classificar_termo(termo: str | None) -> tuple[str, str]:
    """Devolve (tipo, termo_normalizado). Só olha o formato -- não vai ao
    banco. Dígito puro é ambíguo (id de rota OU número de pedido), então
    quem busca tenta os dois e junta."""
    limpo = (termo or "").strip()
    if not limpo:
        return TERMO_VAZIO, ""
    alto = limpo.upper()
    if _RE_PLACA.match(alto):
        return TERMO_PLACA, alto.replace(" ", "").replace("-", "")
    if alto.isdigit():
        return TERMO_ROTA, alto
    if _RE_CODIGO.match(alto):
        # Chave do núcleo: sem '#', com hífen entre prefixo e número.
        codigo = normalizar_codigo(alto.replace(" ", ""))
        return TERMO_PEDIDO, re.sub(r"^([A-Z]{1,4})(\d)", r"\1-\2", codigo or "")
    return TERMO_TEXTO, limpo


def resolver_placa(placa: str) -> list[int]:
    """Placa -> agent_id(s), via a planilha de motoristas (coluna PLACA de
    BD_MOTORISTAS.xlsx). O núcleo NÃO guarda placa -- nucleo_rotas tem só
    agent_id/vehicle_id -- e este é o único jeito de ligar as duas coisas.

    Import preguiçoso e tolerante a falha de propósito: o catálogo puxa
    pandas e lê um Excel, e uma busca por placa que não resolve deve
    devolver "não achei", nunca derrubar a tela de consulta."""
    alvo = (placa or "").upper().replace(" ", "").replace("-", "")
    if not alvo:
        return []
    try:
        import yaml
        from regras.preferencias_motoristas import CatalogoMotoristas
        config = yaml.safe_load((_RAIZ / "config.yaml").read_text(encoding="utf-8")) or {}
        # Mesma seção que sincronizar_vuupt.py:377 e planejamento_rotas.py
        # usam -- errei as chaves na primeira versão e a busca por placa
        # nunca achava nada (a planilha vinha como None, sem erro nenhum).
        cfg = config.get("motoristas", {}) or {}
        catalogo = CatalogoMotoristas.carregar(cfg.get("planilha", ""), cfg.get("json_fallback", ""))
    except Exception:
        logger.exception("Busca por placa: não foi possível carregar o catálogo de motoristas")
        return []
    ids = []
    for m in catalogo.motoristas:
        dele = (getattr(m, "placa", None) or "").upper().replace(" ", "").replace("-", "")
        if dele and dele == alvo and m.agent_id:
            ids.append(int(m.agent_id))
    return ids


# ── Montagem ──────────────────────────────────────────────────────────────────

def _linha(row: sqlite3.Row, com_json: bool = False) -> dict:
    d = dict(row)
    bruto = d.pop("dados_json", None)
    if com_json:
        d["dados"] = _ler_json(bruto)
    return d


def _paradas_da_rota(conn, rota_id: int, sender_id: int | None) -> list[dict]:
    sql = "SELECT * FROM nucleo_paradas WHERE rota_id = ?"
    params: list = [rota_id]
    if sender_id is not None:
        sql += " AND sender_id = ?"
        params.append(int(sender_id))
    sql += " ORDER BY ordem"
    return [_linha(r) for r in conn.execute(sql, params)]


def _resumo_rota(conn, row: sqlite3.Row, sender_id: int | None) -> dict:
    """Cartão da listagem: a rota + quantas paradas ela tem. Com recorte
    de embarcador, `paradas_do_cliente` conta só as dele (os totais da
    rota continuam sendo os da rota inteira -- são dados da operação)."""
    rota = _linha(row)
    if sender_id is not None:
        rota["paradas_do_cliente"] = conn.execute(
            "SELECT COUNT(*) FROM nucleo_paradas WHERE rota_id = ? AND sender_id = ?",
            (row["id"], int(sender_id)),
        ).fetchone()[0]
    return rota


def _sql_recorte_sender(sender_id: int | None) -> tuple[str, list]:
    """Mantém só rota que TEM alguma parada do embarcador. Para consulta
    que não junta paradas (rota por id, por placa, por motorista).

    Alias `px` de propósito: as consultas de fora usam `p` pra parada
    juntada, e um `p` aqui dentro sombrearia o de fora -- foi exatamente
    isso que deixou a busca por código vazar rota de outro embarcador
    (o EXISTS respondia "essa rota tem parada sua?" em vez de "a parada
    que casou é sua?"). Pego no teste, 12/09."""
    if sender_id is None:
        return "", []
    return (" AND EXISTS (SELECT 1 FROM nucleo_paradas px WHERE px.rota_id = r.id AND px.sender_id = ?)",
            [int(sender_id)])


def _sql_recorte_na_parada(sender_id: int | None, alias: str = "p") -> tuple[str, list]:
    """Recorte aplicado na PARADA que casou com a busca -- é o certo
    sempre que a consulta junta nucleo_paradas: o cliente só acha pelo
    que é dele, não pela rota onde ele por acaso também tem parada."""
    if sender_id is None:
        return "", []
    return f" AND {alias}.sender_id = ?", [int(sender_id)]


# ── Listagem ──────────────────────────────────────────────────────────────────

def listar_rotas(de=None, ate=None, status: str | None = None, provedor: str | None = None,
                 agent_id: int | None = None, sender_id: int | None = None,
                 limite: int = LIMITE_PADRAO, conn: sqlite3.Connection | None = None) -> dict:
    """Rotas de um período, mais recentes primeiro (a Torre lista o dia;
    aqui é o histórico)."""
    inicio, fim = _periodo(de, ate)
    fechar = conn is None
    conn = conn or banco.conectar()
    try:
        sql = "SELECT r.* FROM nucleo_rotas r WHERE r.data_rota BETWEEN ? AND ?"
        params: list = [inicio, fim]
        if status:
            sql += " AND r.status = ?"
            params.append(str(status).upper())
        if provedor:
            sql += " AND r.provedor = ?"
            params.append(str(provedor).upper())
        if agent_id:
            sql += " AND r.agent_id = ?"
            params.append(int(agent_id))
        clausula, extra = _sql_recorte_sender(sender_id)
        sql += clausula
        params += extra
        sql += " ORDER BY r.data_rota DESC, r.start_at DESC, r.id DESC LIMIT ?"
        params.append(int(limite) + 1)
        rows = conn.execute(sql, params).fetchall()
        truncado = len(rows) > limite
        return {"periodo": {"de": inicio, "ate": fim},
                "rotas": [_resumo_rota(conn, r, sender_id) for r in rows[:limite]],
                "truncado": truncado}
    finally:
        if fechar:
            conn.close()


# A data que vale pro pedido é a da ROTA em que ele entrou; sem rota,
# cai no agendamento e, por último, em criado_em.
#
# NUNCA filtrar pedido só por criado_em: essa coluna é quando a LINHA
# nasceu no núcleo, não quando o pedido existiu. O backfill de 60 dias
# (12/09) trouxe 1.053 pedidos antigos e todos nasceram com criado_em de
# hoje -- por criado_em, um pedido de 20/07 apareceria como sendo de
# setembro.
_DATA_PEDIDO = """COALESCE(
    (SELECT MIN(r.data_rota) FROM nucleo_paradas p JOIN nucleo_rotas r ON r.id = p.rota_id
      WHERE p.codigo = pe.codigo),
    date(pe.agendamento_inicio), date(pe.criado_em))"""


def listar_pedidos(de=None, ate=None, status: str | None = None, sender_id: int | None = None,
                   limite: int = LIMITE_PADRAO, conn: sqlite3.Connection | None = None) -> dict:
    """Pedidos de um período, pela data de referência (ver `_DATA_PEDIDO`).
    Cada pedido sai com `data_referencia` e `data_rota` -- a tela mostra
    qual data foi usada, senão o filtro parece mentir."""
    inicio, fim = _periodo(de, ate)
    fechar = conn is None
    conn = conn or banco.conectar()
    try:
        sql = f"""
            WITH base AS (
                SELECT pe.*, {_DATA_PEDIDO} AS data_referencia,
                       (SELECT MIN(r.data_rota) FROM nucleo_paradas p JOIN nucleo_rotas r ON r.id = p.rota_id
                         WHERE p.codigo = pe.codigo) AS data_rota
                FROM nucleo_pedidos pe
            )
            SELECT * FROM base WHERE data_referencia BETWEEN ? AND ?
        """
        params: list = [inicio, fim]
        if status:
            sql += " AND status = ?"
            params.append(str(status).upper())
        if sender_id is not None:
            sql += " AND sender_id = ?"
            params.append(int(sender_id))
        sql += " ORDER BY data_referencia DESC, codigo DESC LIMIT ?"
        params.append(int(limite) + 1)
        rows = conn.execute(sql, params).fetchall()
        return {"periodo": {"de": inicio, "ate": fim},
                "pedidos": [_linha(r) for r in rows[:limite]],
                "truncado": len(rows) > limite}
    finally:
        if fechar:
            conn.close()


# ── Busca ─────────────────────────────────────────────────────────────────────

def buscar(termo: str | None, de=None, ate=None, sender_id: int | None = None,
           limite: int = LIMITE_PADRAO, conn: sqlite3.Connection | None = None) -> dict:
    """Caixa de busca única da tela: código do pedido, nº da rota, placa,
    nome de motorista, destinatário ou embarcador.

    Não obriga escolher "tipo" antes de digitar: classifica o formato e,
    quando é ambíguo (dígito puro = id de rota OU número de pedido),
    tenta os dois caminhos e junta o que achou."""
    tipo, limpo = classificar_termo(termo)
    if tipo == TERMO_VAZIO:
        return {"termo": "", "interpretado": TERMO_VAZIO, "rotas": [], "pedidos": [], "truncado": False}

    inicio, fim = _periodo(de, ate)
    fechar = conn is None
    conn = conn or banco.conectar()
    try:
        rotas: dict[int, dict] = {}
        pedidos: dict[str, dict] = {}
        clausula_sender, p_sender = _sql_recorte_sender(sender_id)
        na_parada, p_na_parada = _sql_recorte_na_parada(sender_id)

        def add_rotas(sql: str, params: list):
            for r in conn.execute(sql, params):
                if r["id"] not in rotas:
                    rotas[r["id"]] = _resumo_rota(conn, r, sender_id)

        def add_pedidos(sql: str, params: list):
            for r in conn.execute(sql, params):
                if r["codigo"] not in pedidos:
                    pedidos[r["codigo"]] = _linha(r)

        if tipo == TERMO_ROTA:
            # id da rota (sem filtro de período: quem digita o número quer aquela rota)
            add_rotas(f"SELECT r.* FROM nucleo_rotas r WHERE r.id = ?{clausula_sender}",
                      [int(limpo)] + p_sender)
            # ...e o número pode ser o pedido sem o prefixo, desde que
            # tenha dígito suficiente pra ser um código: casar "2" pelo
            # sufixo trazia toda rota com PS-xxxx2 dentro (achado no teste).
            if len(limpo) >= _MIN_DIGITOS_CODIGO:
                add_pedidos("SELECT * FROM nucleo_pedidos WHERE codigo LIKE ?"
                            + (" AND sender_id = ?" if sender_id is not None else "") + " LIMIT ?",
                            [f"%{limpo}"] + ([int(sender_id)] if sender_id is not None else []) + [limite])
                add_rotas(f"""SELECT DISTINCT r.* FROM nucleo_rotas r
                              JOIN nucleo_paradas p ON p.rota_id = r.id
                              WHERE p.codigo LIKE ?{na_parada} LIMIT ?""",
                          [f"%{limpo}"] + p_na_parada + [limite])

        elif tipo == TERMO_PEDIDO:
            # Aceita as duas grafias enquanto houver linha antiga gravada com
            # '#' (a migração de 15/09 normaliza o que já existe).
            grafias = [limpo, "#" + limpo]
            add_pedidos("SELECT * FROM nucleo_pedidos WHERE codigo IN (?, ?)"
                        + (" AND sender_id = ?" if sender_id is not None else ""),
                        grafias + ([int(sender_id)] if sender_id is not None else []))
            add_rotas(f"""SELECT DISTINCT r.* FROM nucleo_rotas r
                          JOIN nucleo_paradas p ON p.rota_id = r.id
                          WHERE p.codigo IN (?, ?){na_parada}""",
                      grafias + p_na_parada)

        elif tipo == TERMO_PLACA:
            ids = resolver_placa(limpo)
            for agent_id in ids:
                add_rotas(f"""SELECT r.* FROM nucleo_rotas r
                              WHERE r.agent_id = ? AND r.data_rota BETWEEN ? AND ?{clausula_sender}
                              ORDER BY r.data_rota DESC LIMIT ?""",
                          [agent_id, inicio, fim] + p_sender + [limite])

        else:  # TERMO_TEXTO
            curinga = f"%{limpo}%"
            add_rotas(f"""SELECT r.* FROM nucleo_rotas r
                          WHERE r.data_rota BETWEEN ? AND ?
                            AND (r.motorista_nome LIKE ? OR r.nome LIKE ?){clausula_sender}
                          ORDER BY r.data_rota DESC LIMIT ?""",
                      [inicio, fim, curinga, curinga] + p_sender + [limite])
            add_rotas(f"""SELECT DISTINCT r.* FROM nucleo_rotas r
                          JOIN nucleo_paradas p ON p.rota_id = r.id
                          WHERE r.data_rota BETWEEN ? AND ?
                            AND (p.destinatario_nome LIKE ? OR p.remetente_nome LIKE ?
                                 OR p.endereco LIKE ?){na_parada}
                          ORDER BY r.data_rota DESC LIMIT ?""",
                      [inicio, fim, curinga, curinga, curinga] + p_na_parada + [limite])
            add_pedidos(f"""WITH base AS (
                                SELECT pe.*, {_DATA_PEDIDO} AS data_referencia FROM nucleo_pedidos pe
                            )
                            SELECT * FROM base
                            WHERE data_referencia BETWEEN ? AND ?
                              AND (destinatario_nome LIKE ? OR remetente_nome LIKE ?
                                   OR endereco LIKE ? OR destinatario_codigo LIKE ?)"""
                        + (" AND sender_id = ?" if sender_id is not None else "")
                        + " ORDER BY data_referencia DESC LIMIT ?",
                        [inicio, fim, curinga, curinga, curinga, curinga]
                        + ([int(sender_id)] if sender_id is not None else []) + [limite])

        ordenadas = sorted(rotas.values(), key=lambda r: (r.get("data_rota") or "", r.get("id") or 0), reverse=True)
        return {"termo": limpo, "interpretado": tipo, "periodo": {"de": inicio, "ate": fim},
                "rotas": ordenadas[:limite], "pedidos": list(pedidos.values())[:limite],
                "truncado": len(ordenadas) > limite or len(pedidos) > limite}
    finally:
        if fechar:
            conn.close()


# ── Detalhe ───────────────────────────────────────────────────────────────────

def _eventos(conn, rota_id: int | None = None, parada_id: int | None = None) -> list[dict]:
    """Timeline do que o app/sync registrou. `ocorrido_em` é o relógio de
    quem gerou; `recebido_em`, quando vimos -- os dois vão pra tela porque
    o completed_at da VUUPT chega em lote (ver sincronizar_vuupt.py:10-15)."""
    if parada_id is not None:
        sql = "SELECT * FROM nucleo_eventos WHERE parada_id = ? ORDER BY ocorrido_em, id"
        params: list = [parada_id]
    else:
        sql = "SELECT * FROM nucleo_eventos WHERE rota_id = ? ORDER BY ocorrido_em, id"
        params = [rota_id]
    return [_linha(r, com_json=True) for r in conn.execute(sql, params)]


def _comprovantes(conn, parada_ids: list[int]) -> dict[int, list[dict]]:
    if not parada_ids:
        return {}
    marcas = ",".join("?" * len(parada_ids))
    saida: dict[int, list[dict]] = {}
    for r in conn.execute(
        f"SELECT * FROM nucleo_comprovantes WHERE parada_id IN ({marcas}) ORDER BY id", parada_ids
    ):
        d = _linha(r, com_json=True)
        saida.setdefault(d["parada_id"], []).append(d)
    return saida


def detalhar_rota(rota_id: int, sender_id: int | None = None,
                  conn: sqlite3.Connection | None = None) -> dict | None:
    """Rota inteira: paradas na ordem, com canhoto/foto e a timeline de
    cada uma, mais os pedágios da rota. Com `sender_id`, só as paradas
    daquele embarcador (e devolve None se ele não tem nenhuma)."""
    fechar = conn is None
    conn = conn or banco.conectar()
    try:
        row = conn.execute("SELECT * FROM nucleo_rotas WHERE id = ?", (rota_id,)).fetchone()
        if not row:
            return None
        paradas = _paradas_da_rota(conn, rota_id, sender_id)
        if sender_id is not None and not paradas:
            return None
        rota = _linha(row, com_json=True)
        fotos = _comprovantes(conn, [p["id"] for p in paradas])
        for p in paradas:
            p["comprovantes"] = fotos.get(p["id"], [])
            p["eventos"] = _eventos(conn, parada_id=p["id"])
        rota["paradas"] = paradas
        rota["eventos"] = _eventos(conn, rota_id=rota_id)
        rota["pedagios"] = [_linha(r, com_json=True) for r in conn.execute(
            "SELECT * FROM nucleo_pedagios WHERE rota_id = ? ORDER BY id", (rota_id,))]
        return rota
    finally:
        if fechar:
            conn.close()


def detalhar_pedido(codigo: str, sender_id: int | None = None,
                    conn: sqlite3.Connection | None = None) -> dict | None:
    """Um pedido e por onde ele passou. Devolve as paradas (plural de
    propósito: pedido reentregue/duplicado aparece em mais de uma rota),
    cada uma com a rota, a timeline e as fotos."""
    codigo = normalizar_codigo(codigo)
    if not codigo:
        return None
    grafias = [codigo, "#" + codigo]   # linha antiga gravada com '#'
    fechar = conn is None
    conn = conn or banco.conectar()
    try:
        pedido_row = conn.execute("SELECT * FROM nucleo_pedidos WHERE codigo IN (?, ?) ORDER BY atualizado_em DESC",
                                  grafias).fetchone()
        pedido = _linha(pedido_row, com_json=True) if pedido_row else None
        if pedido and sender_id is not None and pedido.get("sender_id") != int(sender_id):
            return None

        sql = """SELECT p.*, r.data_rota, r.nome AS rota_nome, r.status AS rota_status,
                        r.motorista_nome, r.provedor, r.agent_id
                 FROM nucleo_paradas p LEFT JOIN nucleo_rotas r ON r.id = p.rota_id
                 WHERE p.codigo IN (?, ?)"""
        params: list = list(grafias)
        if sender_id is not None:
            sql += " AND p.sender_id = ?"
            params.append(int(sender_id))
        sql += " ORDER BY r.data_rota DESC, p.id DESC"
        paradas = [_linha(r) for r in conn.execute(sql, params)]
        if not pedido and not paradas:
            return None
        fotos = _comprovantes(conn, [p["id"] for p in paradas])
        for p in paradas:
            p["comprovantes"] = fotos.get(p["id"], [])
            p["eventos"] = _eventos(conn, parada_id=p["id"])
        return {"codigo": codigo, "pedido": pedido, "paradas": paradas}
    finally:
        if fechar:
            conn.close()


# ── Apoio à tela ──────────────────────────────────────────────────────────────

def cobertura(conn: sqlite3.Connection | None = None) -> dict:
    """Desde quando há histórico -- a tela mostra isso pra não parecer que
    "não achou" quando na verdade o período nunca foi sincronizado."""
    fechar = conn is None
    conn = conn or banco.conectar()
    try:
        r = conn.execute("SELECT COUNT(*) n, MIN(data_rota) de, MAX(data_rota) ate FROM nucleo_rotas").fetchone()
        p = conn.execute(f"""SELECT COUNT(*) n, MIN({_DATA_PEDIDO}) de, MAX({_DATA_PEDIDO}) ate
                             FROM nucleo_pedidos pe""").fetchone()
        return {"rotas": dict(r), "pedidos": dict(p)}
    finally:
        if fechar:
            conn.close()


def motoristas_com_rota(de=None, ate=None, conn: sqlite3.Connection | None = None) -> list[dict]:
    """Dropdown de motorista da tela, só com quem tem rota no período."""
    inicio, fim = _periodo(de, ate)
    fechar = conn is None
    conn = conn or banco.conectar()
    try:
        return [dict(r) for r in conn.execute("""
            SELECT agent_id, motorista_nome, COUNT(*) rotas FROM nucleo_rotas
            WHERE agent_id IS NOT NULL AND data_rota BETWEEN ? AND ?
            GROUP BY agent_id, motorista_nome ORDER BY motorista_nome
        """, (inicio, fim))]
    finally:
        if fechar:
            conn.close()
