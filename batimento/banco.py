# -*- coding: utf-8 -*-
"""
batimento/banco.py

Tabelas do batimento em dados/dados.db (DOC_EXECUCAO_CLAUDE_BATIMENTO_PEDIDOS.md):

  batimento_pedidos   Uma linha por pedido-base lancado na Stokki desde a
                      data de corte. NUNCA apagada. Guarda a caixa da
                      equacao (DESTINO / EM_ANDAMENTO / DIVERGENCIA), o
                      destino ou motivo, as evidencias e desde quando.
                      Pedido que chegou a DESTINO fica congelado: a leitura
                      da Vuupt cobre so os ultimos N dias, e reclassificar
                      depois disso faria entrega antiga virar divergencia.
  batimento_rodadas   Cada rodada do job: totais da equacao e se fechou.
"""
import json
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "dados" / "dados.db"
FMT = "%Y-%m-%d %H:%M:%S"
DESTINO = "DESTINO"
DIVERGENCIA = "DIVERGENCIA"


def conectar(db_path: Path | None = None) -> sqlite3.Connection:
    caminho = db_path or DB_PATH
    caminho.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(caminho, timeout=30)
    conn.row_factory = sqlite3.Row
    garantir_esquema(conn)
    return conn


def garantir_esquema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS batimento_pedidos (
            codigo          TEXT PRIMARY KEY,   -- PS-12345 (pedido-base)
            id_stokki       INTEGER,
            embarcador      TEXT,
            transportadora  TEXT,
            status_stokki   TEXT,               -- texto da coluna "state"
            status_nucleo   TEXT,
            caixa           TEXT NOT NULL,      -- DESTINO | EM_ANDAMENTO | DIVERGENCIA | SEM_CLASSIFICAR
            rotulo          TEXT NOT NULL,      -- destino final, motivo da divergencia ou estado do vigia
            evidencias_json TEXT,
            desde           TEXT NOT NULL,      -- quando entrou na caixa/rotulo atual
            fechado_em      TEXT,               -- quando chegou a DESTINO
            primeira_vez_em TEXT NOT NULL,
            visto_em        TEXT NOT NULL,      -- ultima rodada que listou o pedido
            tratado_em      TEXT,
            tratado_por     TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_batimento_pedidos_caixa ON batimento_pedidos(caixa, rotulo);
        CREATE TABLE IF NOT EXISTS batimento_rodadas (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            rodada_em       TEXT NOT NULL,
            id_minimo       INTEGER,
            lancados        INTEGER NOT NULL,
            destino         INTEGER NOT NULL,
            em_andamento    INTEGER NOT NULL,
            divergencia     INTEGER NOT NULL,
            sem_classificar INTEGER NOT NULL,
            fecha           INTEGER NOT NULL,
            resumo_json     TEXT
        );
    """)
    colunas = {r[1] for r in conn.execute("PRAGMA table_info(batimento_pedidos)")}
    _adicionar_coluna(conn, colunas, "tratado_obs")
    # div_rotulo/div_desde (06/10): motivo de divergencia e desde quando, que
    # NAO mudam quando o pedido oscila pra EM_ANDAMENTO e volta ao mesmo motivo.
    _adicionar_coluna(conn, colunas, "div_rotulo")
    if _adicionar_coluna(conn, colunas, "div_desde"):
        conn.execute("UPDATE batimento_pedidos SET div_rotulo = rotulo, div_desde = desde "
                     "WHERE caixa = 'DIVERGENCIA' AND div_desde IS NULL")
        conn.commit()


def _adicionar_coluna(conn: sqlite3.Connection, colunas: set, nome: str) -> bool:
    """ALTER idempotente. True se esta chamada criou a coluna. Duas threads
    do painel podem chegar aqui juntas no 1o acesso pos-deploy."""
    if nome in colunas:
        return False
    try:
        conn.execute(f"ALTER TABLE batimento_pedidos ADD COLUMN {nome} TEXT")
        conn.commit()
        return True
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            raise
        return False


def gravar_rodada(conn: sqlite3.Connection, pedidos: list[dict], resumo: dict, id_minimo: int | None,
                  agora: datetime | None = None) -> dict:
    """`pedidos`: saida de medir.medir() (um dict por pedido, com codigo,
    caixa, rotulo, evidencias...). Devolve contagem do que mudou."""
    agora_txt = (agora or datetime.now()).strftime(FMT)
    mudou = {"novos": 0, "mudaram": 0, "congelados": 0, "iguais": 0}
    for p in pedidos:
        codigo = p["codigo"]
        atual = conn.execute("SELECT caixa, rotulo, div_rotulo FROM batimento_pedidos WHERE codigo = ?",
                             (codigo,)).fetchone()
        if atual and atual["caixa"] == DESTINO:
            conn.execute("UPDATE batimento_pedidos SET visto_em = ? WHERE codigo = ?", (agora_txt, codigo))
            mudou["congelados"] += 1
            continue
        evid = json.dumps(str(p.get("evidencias") or "").split(" | "), ensure_ascii=False)
        fechado = agora_txt if p["caixa"] == DESTINO else None
        divergente = p["caixa"] == DIVERGENCIA
        if atual is None:
            conn.execute("""
                INSERT INTO batimento_pedidos
                    (codigo, id_stokki, embarcador, transportadora, status_stokki, status_nucleo,
                     caixa, rotulo, evidencias_json, desde, fechado_em, primeira_vez_em, visto_em,
                     div_rotulo, div_desde)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (codigo, p.get("id_stokki"), p.get("embarcador"), p.get("transportadora"),
                  p.get("status_stokki_bruto"), p.get("nucleo_status"), p["caixa"], p["rotulo"], evid,
                  agora_txt, fechado, agora_txt, agora_txt,
                  p["rotulo"] if divergente else None, agora_txt if divergente else None))
            mudou["novos"] += 1
            continue
        trocou = (atual["caixa"], atual["rotulo"]) != (p["caixa"], p["rotulo"])
        # Motivo de divergencia NOVO reinicia div_desde e apaga a tratativa
        # (ela valia pro motivo anterior). Oscilar pra EM_ANDAMENTO e voltar
        # ao mesmo motivo nao mexe em nenhum dos dois (Hugo, 06/10).
        div_novo = divergente and atual["div_rotulo"] != p["rotulo"]
        extra, valores = "", []
        if trocou:
            extra += ", desde = ?"
            valores.append(agora_txt)
        if div_novo:
            extra += ", div_rotulo = ?, div_desde = ?, tratado_em = NULL, tratado_por = NULL, tratado_obs = NULL"
            valores += [p["rotulo"], agora_txt]
        conn.execute(f"""
            UPDATE batimento_pedidos SET
                id_stokki = ?, embarcador = ?, transportadora = ?, status_stokki = ?, status_nucleo = ?,
                caixa = ?, rotulo = ?, evidencias_json = ?, fechado_em = ?, visto_em = ?{extra}
            WHERE codigo = ?
        """, (p.get("id_stokki"), p.get("embarcador"), p.get("transportadora"), p.get("status_stokki_bruto"),
              p.get("nucleo_status"), p["caixa"], p["rotulo"], evid, fechado, agora_txt, *valores, codigo))
        mudou["mudaram" if trocou else "iguais"] += 1

    # A equacao vale sobre o que ESTA rodada listou, com os congelados
    # contando como DESTINO: soma das caixas tem que dar os lancados.
    totais = totais_da_rodada(conn, agora_txt)
    fecha = bool(resumo["equacao_fecha"]) and sum(totais.values()) == resumo["lancados"]
    conn.execute("""
        INSERT INTO batimento_rodadas
            (rodada_em, id_minimo, lancados, destino, em_andamento, divergencia, sem_classificar, fecha, resumo_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (agora_txt, id_minimo, resumo["lancados"], totais.get("DESTINO", 0), totais.get("EM_ANDAMENTO", 0),
          totais.get("DIVERGENCIA", 0), totais.get("SEM_CLASSIFICAR", 0), 1 if fecha else 0,
          json.dumps({**resumo, "mudancas": mudou}, ensure_ascii=False, default=str)))
    conn.commit()
    return {**mudou, "totais": totais, "fecha": fecha}


def marcar_tratado(conn: sqlite3.Connection, codigo: str, por: str, obs: str,
                   agora: datetime | None = None) -> None:
    """Tratativa manual de uma divergencia (aba Fechamento). Vale enquanto
    o pedido nao trocar de motivo: gravar_rodada limpa na troca."""
    obs = (obs or "").strip()
    if not obs:
        raise ValueError("Escreva o que foi feito (observação obrigatória).")
    row = conn.execute("SELECT caixa FROM batimento_pedidos WHERE codigo = ?", (codigo,)).fetchone()
    if row is None:
        raise ValueError(f"{codigo} não está no batimento.")
    if row[0] != "DIVERGENCIA":
        raise ValueError(f"{codigo} não está em divergência.")
    conn.execute("UPDATE batimento_pedidos SET tratado_em = ?, tratado_por = ?, tratado_obs = ? WHERE codigo = ?",
                 ((agora or datetime.now()).strftime(FMT), por, obs[:300], codigo))
    conn.commit()


def desmarcar_tratado(conn: sqlite3.Connection, codigo: str) -> bool:
    """Desfaz a tratativa ("Desfazer" na Torre). True se havia o que limpar."""
    cur = conn.execute("UPDATE batimento_pedidos SET tratado_em = NULL, tratado_por = NULL, tratado_obs = NULL "
                       "WHERE codigo = ? AND tratado_em IS NOT NULL", (codigo,))
    conn.commit()
    return cur.rowcount > 0


def totais_da_rodada(conn: sqlite3.Connection, visto_em: str) -> dict:
    return {r["caixa"]: r["n"] for r in conn.execute(
        "SELECT caixa, COUNT(*) AS n FROM batimento_pedidos WHERE visto_em = ? GROUP BY caixa", (visto_em,))}
