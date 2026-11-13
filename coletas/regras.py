# -*- coding: utf-8 -*-
"""
coletas/regras.py

Regras da coleta de pedido de retirada na Expedicao (Hugo, 12/11): o que esta
a coletar, a folha (COL-n) e a confirmacao. So banco: quem fala com a Stokki e
o robo (expedir_pedidos.py --coleta N), que confere tem_foto antes de expedir.

Sem foto, nome do motorista e placa nao existe confirmacao. Folha: no maximo
30 pedidos, uma transportadora; no "Cliente Retira", um embarcador so.
"""
import sqlite3
from datetime import datetime

from coletas import banco

MAX_PEDIDOS = 30


class ErroColeta(Exception):
    pass


def _agora(agora=None) -> str:
    return (agora or datetime.now()).strftime(banco.FMT)


def _cliente_retira(transportadora: str) -> bool:
    return "CLIENTE RETIRA" in (transportadora or "").upper()


def _tabelas(conn) -> set:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('galpao_pedidos', 'portal_envios')")}


def _extras(conn, ids: list[int]) -> dict:
    """{id_stokki: {unidades, nf, destinatario}} do espelho do galpao e do
    ultimo envio do portal, quando as tabelas existem."""
    extras = {i: {"unidades": None, "nf": None, "destinatario": None} for i in ids}
    if not ids:
        return extras
    tabelas = _tabelas(conn)
    marcas = ",".join("?" * len(ids))
    if "galpao_pedidos" in tabelas:
        for r in conn.execute(f"SELECT id_stokki, unidades FROM galpao_pedidos WHERE id_stokki IN ({marcas})", ids):
            extras[r["id_stokki"]]["unidades"] = r["unidades"]
    if "portal_envios" in tabelas:
        codigos = [f"PS-{i}" for i in ids]
        for r in conn.execute(
                f"SELECT codigo_pedido, numero_nf, destinatario_nome, volumes FROM portal_envios "
                f"WHERE codigo_pedido IN ({marcas}) AND status NOT IN ('ERRO', 'DUPLICADO') ORDER BY id", codigos):
            e = extras[int(r["codigo_pedido"][3:])]
            e["nf"], e["destinatario"] = r["numero_nf"], r["destinatario_nome"]
            if e["unidades"] is None:
                e["unidades"] = r["volumes"]
    return extras


def a_coletar(conn) -> dict:
    """Pedidos na listagem da Stokki, fora de folha viva. grupos = retirada,
    por transportadora (Cliente Retira dividido por embarcador); sem_cadastro
    = resumo das transportadoras que nao sao de retirada: nao entram em folha."""
    linhas = [dict(r) for r in conn.execute(
        "SELECT f.* FROM expedicao_coletas_fila f WHERE f.saiu_em IS NULL AND NOT EXISTS ("
        " SELECT 1 FROM expedicao_coletas_pedidos p WHERE p.id_stokki = f.id_stokki"
        " AND p.estado IN ('NA_FOLHA', 'EXPEDINDO', 'FALHOU')) ORDER BY f.id_stokki")]
    retiradas = [l for l in linhas if l["retirada"]]
    extras = _extras(conn, [l["id_stokki"] for l in retiradas])
    grupos = {}
    for l in retiradas:
        transportadora = l["transportadora"] or ""
        embarcador = l["embarcador"] if _cliente_retira(transportadora) else None
        g = grupos.setdefault((transportadora, embarcador), {"transportadora": transportadora,
                                                              "embarcador": embarcador, "pedidos": []})
        g["pedidos"].append({"id_stokki": l["id_stokki"], "codigo": l["codigo"], "embarcador": l["embarcador"],
                             "carga_inicial": l["carga_inicial"], "desde": l["visto_primeiro_em"],
                             **extras[l["id_stokki"]]})
    return {"grupos": sorted(grupos.values(), key=lambda g: (-len(g["pedidos"]), g["transportadora"],
                                                             g["embarcador"] or "")),
            "sem_cadastro": sem_cadastro_de_retirada(conn)}


def sem_cadastro_de_retirada(conn) -> list[dict]:
    """Transportadoras fora do cadastro de retirada, com a contagem. A maioria
    e entrega normal (Fresh Log): a tela mostra so o resumo."""
    return [dict(r) for r in conn.execute(
        "SELECT COALESCE(NULLIF(transportadora, ''), '(não informada)') AS transportadora, COUNT(*) AS n "
        "FROM expedicao_coletas_fila WHERE saiu_em IS NULL AND retirada = 0 GROUP BY 1 ORDER BY n DESC")]


def gerar_folha(conn, ids_stokki, usuario: str, agora=None) -> int:
    ids = list(dict.fromkeys(int(i) for i in (ids_stokki or [])))
    if not ids:
        raise ErroColeta("Marque pelo menos um pedido.")
    if len(ids) > MAX_PEDIDOS:
        raise ErroColeta(f"A folha leva no máximo {MAX_PEDIDOS} pedidos. Gere mais de uma.")
    marcas = ",".join("?" * len(ids))
    fila = {r["id_stokki"]: r for r in conn.execute(
        f"SELECT * FROM expedicao_coletas_fila WHERE id_stokki IN ({marcas})", ids)}
    for i in ids:
        f = fila.get(i)
        if f is None or f["saiu_em"]:
            raise ErroColeta(f"PS-{i} não está mais aguardando transportador na Stokki. Atualize a tela.")
        if not f["retirada"]:
            raise ErroColeta(f"PS-{i}: a transportadora ({f['transportadora'] or 'não informada'}) "
                             "não está cadastrada como retirada.")
    transportadoras = {fila[i]["transportadora"] for i in ids}
    if len(transportadoras) > 1:
        raise ErroColeta("A folha é de uma transportadora só.")
    transportadora = transportadoras.pop()
    embarcador = None
    if _cliente_retira(transportadora):
        embarcadores = {fila[i]["embarcador"] for i in ids}
        if len(embarcadores) > 1:
            raise ErroColeta("Cliente Retira: a folha é de um embarcador só.")
        embarcador = embarcadores.pop()
    extras = _extras(conn, ids)
    try:
        cur = conn.execute("INSERT INTO expedicao_coletas (transportadora, embarcador, status, criada_em, criada_por) "
                           "VALUES (?, ?, 'ABERTA', ?, ?)", (transportadora, embarcador, _agora(agora), usuario))
        for i in ids:
            e = extras[i]
            conn.execute("INSERT INTO expedicao_coletas_pedidos (coleta_id, id_stokki, codigo, embarcador, unidades,"
                         " nf, destinatario, estado) VALUES (?, ?, ?, ?, ?, ?, ?, 'NA_FOLHA')",
                         (cur.lastrowid, i, fila[i]["codigo"], fila[i]["embarcador"], e["unidades"], e["nf"],
                          e["destinatario"]))
    except sqlite3.IntegrityError:
        conn.rollback()
        raise ErroColeta("Algum desses pedidos já está em outra folha de coleta. Atualize a tela.")
    conn.commit()
    return cur.lastrowid


def _coleta(conn, coleta_id):
    r = conn.execute("SELECT * FROM expedicao_coletas WHERE id = ?", (coleta_id,)).fetchone()
    if r is None:
        raise ErroColeta("Coleta não encontrada.")
    return r


def coleta(conn, coleta_id) -> dict:
    c = dict(_coleta(conn, coleta_id))
    c["numero"] = banco.numero(c["id"])
    c["pedidos"] = [dict(r) for r in conn.execute(
        "SELECT * FROM expedicao_coletas_pedidos WHERE coleta_id = ? ORDER BY id_stokki", (coleta_id,))]
    return c


def cancelar_folha(conn, coleta_id, usuario: str) -> None:
    if _coleta(conn, coleta_id)["status"] != "ABERTA":
        raise ErroColeta("Só dá pra cancelar folha que ainda não foi confirmada.")
    conn.execute("UPDATE expedicao_coletas SET status = 'CANCELADA', confirmada_por = ? WHERE id = ?",
                 (usuario, coleta_id))
    conn.execute("UPDATE expedicao_coletas_pedidos SET estado = 'NAO_SAIU' WHERE coleta_id = ?", (coleta_id,))
    conn.commit()


def confirmar(conn, coleta_id, ids_que_sairam, motorista_nome: str, placa: str, foto_arquivo: str,
              usuario: str, agora=None) -> list[str]:
    """Confirma a coleta e devolve os codigos a expedir. Nada marcado = cancela
    a folha e devolve []. Foto, nome e placa sao obrigatorios (Hugo, 12/11)."""
    c = _coleta(conn, coleta_id)
    if c["status"] != "ABERTA":
        raise ErroColeta("Coleta já confirmada." if c["status"] in ("CONFIRMADA", "CONCLUIDA")
                         else "Esta folha foi cancelada.")
    da_folha = {r["id_stokki"]: r["codigo"] for r in conn.execute(
        "SELECT id_stokki, codigo FROM expedicao_coletas_pedidos WHERE coleta_id = ?", (coleta_id,))}
    sairam = [i for i in dict.fromkeys(int(i) for i in (ids_que_sairam or [])) if i in da_folha]
    if not sairam:
        cancelar_folha(conn, coleta_id, usuario)
        return []
    motorista_nome, placa = (motorista_nome or "").strip(), (placa or "").strip().upper()
    if not motorista_nome:
        raise ErroColeta("Informe o nome do motorista.")
    if not placa:
        raise ErroColeta("Informe a placa.")
    if not foto_arquivo:
        raise ErroColeta("A foto da folha preenchida é obrigatória.")
    conn.execute("UPDATE expedicao_coletas SET status = 'CONFIRMADA', confirmada_em = ?, confirmada_por = ?,"
                 " motorista_nome = ?, placa = ?, foto_arquivo = ? WHERE id = ?",
                 (_agora(agora), usuario, motorista_nome[:120], placa[:12], foto_arquivo, coleta_id))
    for i in da_folha:
        conn.execute("UPDATE expedicao_coletas_pedidos SET estado = ? WHERE coleta_id = ? AND id_stokki = ?",
                     ("EXPEDINDO" if i in sairam else "NAO_SAIU", coleta_id, i))
    conn.commit()
    return [da_folha[i] for i in sairam]


def registrar_execucao(conn, coleta_id, execucao_id, token) -> None:
    conn.execute("UPDATE expedicao_coletas SET execucao_id = ?, token = ? WHERE id = ?",
                 (execucao_id, token, coleta_id))
    conn.commit()


def _concluir_se_acabou(conn, coleta_id) -> None:
    pendente = conn.execute("SELECT 1 FROM expedicao_coletas_pedidos WHERE coleta_id = ? "
                            "AND estado IN ('EXPEDINDO', 'FALHOU') LIMIT 1", (coleta_id,)).fetchone()
    if not pendente:
        conn.execute("UPDATE expedicao_coletas SET status = 'CONCLUIDA' WHERE id = ? AND status = 'CONFIRMADA'",
                     (coleta_id,))


def aplicar_resultados(conn, coleta_id, resultados: dict, simulada: bool = False, agora=None) -> None:
    """resultados = {PS-N: {ok, mensagem}} do robo. Pedido EXPEDINDO sem
    resultado vira FALHOU (o robo terminou sem falar dele)."""
    quando = _agora(agora)
    for r in conn.execute("SELECT id_stokki, codigo FROM expedicao_coletas_pedidos WHERE coleta_id = ? "
                          "AND estado = 'EXPEDINDO'", (coleta_id,)).fetchall():
        res = (resultados or {}).get(r["codigo"]) or {"ok": False, "mensagem": "a expedição terminou sem resultado"}
        conn.execute("UPDATE expedicao_coletas_pedidos SET estado = ?, mensagem = ? WHERE coleta_id = ? AND id_stokki = ?",
                     ("EXPEDIDO" if res.get("ok") else "FALHOU", (res.get("mensagem") or "")[:300], coleta_id,
                      r["id_stokki"]))
        if res.get("ok"):
            conn.execute("UPDATE expedicao_coletas_fila SET saiu_em = ? WHERE id_stokki = ? AND saiu_em IS NULL",
                         (quando, r["id_stokki"]))
    if simulada:
        conn.execute("UPDATE expedicao_coletas SET simulada = 1 WHERE id = ?", (coleta_id,))
    _concluir_se_acabou(conn, coleta_id)
    conn.commit()


def tentar_de_novo(conn, coleta_id) -> list[str]:
    if _coleta(conn, coleta_id)["status"] != "CONFIRMADA":
        raise ErroColeta("Esta coleta não tem pedido para tentar de novo.")
    codigos = [r["codigo"] for r in conn.execute(
        "SELECT codigo FROM expedicao_coletas_pedidos WHERE coleta_id = ? AND estado = 'FALHOU'", (coleta_id,))]
    if not codigos:
        raise ErroColeta("Esta coleta não tem pedido para tentar de novo.")
    conn.execute("UPDATE expedicao_coletas_pedidos SET estado = 'EXPEDINDO', mensagem = NULL "
                 "WHERE coleta_id = ? AND estado = 'FALHOU'", (coleta_id,))
    conn.commit()
    return codigos


def retirar_pedido(conn, coleta_id, id_stokki, usuario: str) -> None:
    """Pedido que falhou e nao tem conserto (ex.: cancelado na Stokki) sai da
    coleta, pra ela poder concluir."""
    cur = conn.execute("UPDATE expedicao_coletas_pedidos SET estado = 'RETIRADO', "
                       "mensagem = COALESCE(mensagem, '') || ' (retirado por ' || ? || ')' "
                       "WHERE coleta_id = ? AND id_stokki = ? AND estado = 'FALHOU'", (usuario, coleta_id, int(id_stokki)))
    if not cur.rowcount:
        raise ErroColeta("Só dá pra retirar pedido que falhou.")
    _concluir_se_acabou(conn, coleta_id)
    conn.commit()


def _resumos(conn, where: str, params=(), limite: int = 200) -> list[dict]:
    return [dict(r, numero=banco.numero(r["id"])) for r in conn.execute(
        "SELECT c.*, (SELECT COUNT(*) FROM expedicao_coletas_pedidos p WHERE p.coleta_id = c.id) AS n_pedidos,"
        " (SELECT COUNT(*) FROM expedicao_coletas_pedidos p WHERE p.coleta_id = c.id AND p.estado = 'EXPEDIDO') AS n_expedidos,"
        " (SELECT COUNT(*) FROM expedicao_coletas_pedidos p WHERE p.coleta_id = c.id AND p.estado = 'FALHOU') AS n_falhas"
        f" FROM expedicao_coletas c WHERE {where} ORDER BY c.id DESC LIMIT ?", (*params, limite))]


def abertas(conn) -> list[dict]:
    """Folhas que ainda pedem acao: abertas, e confirmadas com pedido pendente."""
    return _resumos(conn, "c.status IN ('ABERTA', 'CONFIRMADA')")


def assinatura(conn) -> str:
    """Muda quando qualquer folha ou pedido de folha muda de estado: a tela do
    computador compara e recarrega quando o celular da doca confirma."""
    r = conn.execute("SELECT COUNT(*), COALESCE(SUM(id), 0), "
                     "COALESCE(GROUP_CONCAT(status || COALESCE(execucao_id, ''), ''), '') FROM expedicao_coletas").fetchone()
    p = conn.execute("SELECT COALESCE(GROUP_CONCAT(estado, ''), '') FROM expedicao_coletas_pedidos").fetchone()
    import hashlib
    return hashlib.sha1(f"{r[0]}|{r[1]}|{r[2]}|{p[0]}".encode()).hexdigest()[:16]


def historico(conn, limite: int = 50) -> list[dict]:
    return _resumos(conn, "c.status = 'CONCLUIDA'", limite=limite)


def coleta_do_pedido(conn, codigo: str) -> dict | None:
    r = conn.execute(
        "SELECT c.id, c.motorista_nome, c.placa, c.confirmada_em, c.confirmada_por, c.transportadora "
        "FROM expedicao_coletas_pedidos p JOIN expedicao_coletas c ON c.id = p.coleta_id "
        "WHERE p.codigo = ? AND p.estado = 'EXPEDIDO' ORDER BY c.id DESC LIMIT 1", (codigo,)).fetchone()
    return dict(r, numero=banco.numero(r["id"])) if r else None


def coletas_dos_pedidos(conn, codigos) -> dict:
    """{codigo: coleta} dos pedidos que sairam por coleta (a ultima de cada)."""
    codigos = list(dict.fromkeys(c for c in (codigos or []) if c))
    if not codigos:
        return {}
    marcas = ",".join("?" * len(codigos))
    return {r["codigo"]: dict(r, numero=banco.numero(r["id"])) for r in conn.execute(
        "SELECT p.codigo, c.id, c.motorista_nome, c.placa, c.confirmada_em, c.foto_arquivo "
        "FROM expedicao_coletas_pedidos p JOIN expedicao_coletas c ON c.id = p.coleta_id "
        f"WHERE p.estado = 'EXPEDIDO' AND p.codigo IN ({marcas}) ORDER BY c.id", codigos)}


def retiradas_novas(conn, codigos) -> list[str]:
    """Dos codigos (PS-N), os que sao retirada vista DEPOIS da 1a leitura da
    aba Coletas e ainda estao na Stokki: so saem por coleta com foto."""
    codigos = list(dict.fromkeys(c for c in (codigos or []) if c))
    if not codigos:
        return []
    marcas = ",".join("?" * len(codigos))
    return [r["codigo"] for r in conn.execute(
        f"SELECT codigo FROM expedicao_coletas_fila WHERE codigo IN ({marcas}) AND retirada = 1 "
        "AND carga_inicial = 0 AND saiu_em IS NULL ORDER BY id_stokki", codigos)]


def tem_foto(conn, coleta_id, codigos) -> bool:
    """O robo so expede retirada nova se a coleta foi confirmada com foto e
    todos os codigos pedidos estao nela, esperando a expedicao."""
    c = conn.execute("SELECT status, foto_arquivo FROM expedicao_coletas WHERE id = ?", (coleta_id,)).fetchone()
    if c is None or c["status"] != "CONFIRMADA" or not c["foto_arquivo"]:
        return False
    esperando = {r["codigo"] for r in conn.execute(
        "SELECT codigo FROM expedicao_coletas_pedidos WHERE coleta_id = ? AND estado = 'EXPEDINDO'", (coleta_id,))}
    codigos = set(codigos or [])
    return bool(codigos) and codigos <= esperando
