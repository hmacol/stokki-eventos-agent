# -*- coding: utf-8 -*-
"""
regras/clientes_boleto.py

Quais clientes (destinatários) pagam com boleto, por embarcador -- pedido
do Hugo, 06/10/2026. A chave é o par (sender_id, documento do
destinatário só com dígitos): o mesmo cliente pode pagar com boleto a um
embarcador e à vista a outro.

O sistema SUGERE (PENDENTE) quando o par teve, nos últimos 90 dias, ao
menos 1 DANFE com duplicata (documentos_processados.cobranca = 1) ou 1
boleto recebido. O time decide na tela /clientes-boleto do painel:
TEM_BOLETO ou NAO_TEM_BOLETO. Nunca sugere NAO_TEM_BOLETO; par decidido
só tem a evidência atualizada.

Quem usa: aviso_boleto_faltando.py e o resumo das 20h (só TEM_BOLETO),
e a régua do boleto esperado do romaneio/vigia (regras_documentos.
boleto_esperado). Spec: docs/superpowers/specs/2026-10-06-clientes-boleto-aviso-design.md
"""
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "dados" / "dados.db"

PENDENTE, TEM_BOLETO, NAO_TEM_BOLETO = "PENDENTE", "TEM_BOLETO", "NAO_TEM_BOLETO"
ROTULOS = {PENDENTE: "Aguardando decisão", TEM_BOLETO: "Tem boleto", NAO_TEM_BOLETO: "Não tem boleto"}

_RE_BASE = re.compile(r"PS-\d+", re.IGNORECASE)


def so_digitos(s) -> str:
    return re.sub(r"\D", "", str(s or ""))


def codigo_base(codigo: str) -> str:
    m = _RE_BASE.search(str(codigo or ""))
    return m.group(0).upper() if m else str(codigo or "").lstrip("#").strip()


def formatar_documento(doc: str) -> str:
    d = so_digitos(doc)
    if len(d) == 14:
        return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"
    if len(d) == 11:
        return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}"
    return d


def _conectar() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS clientes_boleto ("
        " sender_id INTEGER NOT NULL,"
        " documento TEXT NOT NULL,"
        " nome TEXT,"
        " embarcador TEXT,"
        " situacao TEXT NOT NULL,"
        " nfs_com_duplicata INTEGER NOT NULL DEFAULT 0,"
        " nfs_sem_duplicata INTEGER NOT NULL DEFAULT 0,"
        " boletos_recebidos INTEGER NOT NULL DEFAULT 0,"
        " ultimo_pedido TEXT,"
        " sugerido_em TEXT,"
        " decidido_em TEXT,"
        " decidido_por TEXT,"
        " PRIMARY KEY (sender_id, documento))"
    )
    conn.commit()
    return conn


def _tabela_existe(conn: sqlite3.Connection, nome: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (nome,)).fetchone() is not None


def _evidencias(conn: sqlite3.Connection, dias: int) -> dict[tuple[int, str], dict]:
    """{(sender_id, documento): {nome, embarcador, com, sem, boletos, ultimo}} a partir dos
    documentos ENVIADOS dos últimos `dias` dias, ligados ao par pelo nucleo_pedidos."""
    desde = (datetime.now() - timedelta(days=dias)).strftime("%Y-%m-%d %H:%M:%S")
    pedidos = {}
    for r in conn.execute("SELECT codigo, sender_id, destinatario_codigo, destinatario_nome, remetente_nome "
                          "FROM nucleo_pedidos WHERE sender_id IS NOT NULL"):
        doc = so_digitos(r["destinatario_codigo"])
        if doc:
            pedidos[codigo_base(r["codigo"])] = (r["sender_id"], doc, r["destinatario_nome"] or "",
                                                 r["remetente_nome"] or "")
    ev: dict[tuple[int, str], dict] = {}
    for r in conn.execute("SELECT tipo, codigo_pedido, cobranca, processado_em FROM documentos_processados "
                          "WHERE status = 'ENVIADO' AND codigo_pedido IS NOT NULL "
                          "AND tipo IN ('Nota Fiscal', 'Boleto') AND processado_em >= ? ORDER BY processado_em",
                          (desde,)):
        par = pedidos.get(codigo_base(r["codigo_pedido"]))
        if not par:
            continue
        sid, doc, nome, emb = par
        e = ev.setdefault((sid, doc), {"nome": nome, "embarcador": emb.split(" - ")[-1].strip(),
                                       "com": 0, "sem": 0, "boletos": 0, "ultimo": None})
        if r["tipo"] == "Boleto":
            e["boletos"] += 1
        elif r["cobranca"] == 1:
            e["com"] += 1
        else:
            e["sem"] += 1
        e["ultimo"] = codigo_base(r["codigo_pedido"])
    return ev


def sugerir(dias: int = 90, modo_teste: bool = False) -> dict:
    conn = _conectar()
    try:
        if not (_tabela_existe(conn, "nucleo_pedidos") and _tabela_existe(conn, "documentos_processados")):
            return {"novos": [], "atualizados": 0}
        ev = _evidencias(conn, dias)
        existentes = {(r["sender_id"], r["documento"]) for r in conn.execute(
            "SELECT sender_id, documento FROM clientes_boleto")}
        agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        novos, atualizados = [], 0
        for (sid, doc), e in sorted(ev.items()):
            if (sid, doc) in existentes:
                atualizados += 1
                if not modo_teste:
                    conn.execute("UPDATE clientes_boleto SET nfs_com_duplicata=?, nfs_sem_duplicata=?, "
                                 "boletos_recebidos=?, ultimo_pedido=? WHERE sender_id=? AND documento=?",
                                 (e["com"], e["sem"], e["boletos"], e["ultimo"], sid, doc))
            elif e["com"] or e["boletos"]:
                novos.append((sid, doc, e["nome"]))
                if not modo_teste:
                    conn.execute("INSERT INTO clientes_boleto (sender_id, documento, nome, embarcador, situacao, "
                                 "nfs_com_duplicata, nfs_sem_duplicata, boletos_recebidos, ultimo_pedido, sugerido_em) "
                                 "VALUES (?,?,?,?,?,?,?,?,?,?)",
                                 (sid, doc, e["nome"], e["embarcador"], PENDENTE, e["com"], e["sem"],
                                  e["boletos"], e["ultimo"], agora))
        if not modo_teste:
            conn.commit()
        return {"novos": novos, "atualizados": atualizados}
    finally:
        conn.close()


def listar(situacao: str | None = None) -> list[dict]:
    conn = _conectar()
    try:
        sql, params = "SELECT * FROM clientes_boleto", ()
        if situacao:
            sql, params = sql + " WHERE situacao = ?", (situacao,)
        sql += (" ORDER BY CASE situacao WHEN 'PENDENTE' THEN 0 ELSE 1 END, "
                "COALESCE(decidido_em, sugerido_em) DESC, embarcador, nome")
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [{**dict(r), "documento_formatado": formatar_documento(r["documento"]),
             "rotulo": ROTULOS.get(r["situacao"], r["situacao"])} for r in rows]


def decidir(sender_id: int, documento: str, tem_boleto: bool, por: str) -> dict:
    doc = so_digitos(documento)
    situacao = TEM_BOLETO if tem_boleto else NAO_TEM_BOLETO
    conn = _conectar()
    try:
        cur = conn.execute("UPDATE clientes_boleto SET situacao=?, decidido_em=datetime('now','localtime'), "
                           "decidido_por=? WHERE sender_id=? AND documento=?",
                           (situacao, por or "", int(sender_id), doc))
        conn.commit()
    finally:
        conn.close()
    if not cur.rowcount:
        raise ValueError(f"Cliente {formatar_documento(doc)} do remetente {sender_id} não está na lista.")
    return {"situacao": situacao}


def situacao_por_pedido(codigos) -> dict[str, str]:
    bases = sorted({codigo_base(c) for c in codigos if c})
    if not bases or not DB_PATH.exists():
        return {}
    conn = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True, timeout=30)
    try:
        if not (_tabela_existe(conn, "clientes_boleto") and _tabela_existe(conn, "nucleo_pedidos")):
            return {}
        situacoes = {(sid, doc): s for sid, doc, s in conn.execute(
            "SELECT sender_id, documento, situacao FROM clientes_boleto")}
        saida = {}
        for i in range(0, len(bases), 900):
            lote = bases[i:i + 900]
            marc = ",".join("?" * len(lote))
            for codigo, sid, dest in conn.execute(
                    f"SELECT codigo, sender_id, destinatario_codigo FROM nucleo_pedidos "
                    f"WHERE UPPER(REPLACE(codigo, '#', '')) IN ({marc})", lote):
                s = situacoes.get((sid, so_digitos(dest)))
                if s:
                    saida[codigo_base(codigo)] = s
        return saida
    finally:
        conn.close()
