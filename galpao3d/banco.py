# -*- coding: utf-8 -*-
"""
galpao3d/banco.py

Tabelas do Galpao 3D em dados/dados.db (o coletor grava, as telas leem):

  galpao_pedidos       Ultimo estado conhecido de cada pedido de saida da Stokki
                       (situacao vem da listagem; show_pendente = falta ler a show;
                       falhas_show = falhas seguidas da show, 3 = desiste).
  galpao_eventos       Linha do tempo (historico do pedido) sem duplicar.
  galpao_conferencias  Dados da conferencia (estacao, usuario, tempo).
  galpao_coletas       Log de cada rodada do coletor (fonte stokki|vuupt).
  galpao_contagens     Saida de contar_pedidos por rodada (json).
  galpao_rua           Resumo da Vuupt (carros na rua, entregas) por rodada (json).
"""
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "dados" / "dados.db"
FMT = "%Y-%m-%d %H:%M:%S"


def conectar(db_path: Path | None = None) -> sqlite3.Connection:
    caminho = db_path or DB_PATH
    caminho.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(caminho, timeout=30)
    conn.row_factory = sqlite3.Row
    garantir_esquema(conn)
    return conn


def garantir_esquema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS galpao_pedidos (
            id_stokki       INTEGER PRIMARY KEY,
            codigo          TEXT,
            embarcador      TEXT,
            situacao        TEXT,
            unidades        INTEGER,
            itens           INTEGER,
            operador_tarefa TEXT,
            codigo_separacao TEXT,
            atualizado_em   TEXT,
            falhas_show     INTEGER DEFAULT 0,
            show_pendente   INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS galpao_eventos (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            id_stokki  INTEGER NOT NULL,
            situacao   TEXT NOT NULL,
            usuario    TEXT,
            quando     TEXT NOT NULL,
            UNIQUE(id_stokki, situacao, quando)
        );
        CREATE TABLE IF NOT EXISTS galpao_conferencias (
            id_stokki INTEGER PRIMARY KEY,
            estacao   TEXT,
            usuario   TEXT,
            inicio    TEXT,
            fim       TEXT,
            segundos  INTEGER,
            problema  INTEGER
        );
        CREATE TABLE IF NOT EXISTS galpao_coletas (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            fonte     TEXT NOT NULL,      -- stokki | vuupt
            quando    TEXT NOT NULL,
            resultado TEXT NOT NULL,      -- ok | parcial | ocupada | erro
            obs       TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_galpao_coletas_fonte ON galpao_coletas(fonte, resultado, quando);
        CREATE TABLE IF NOT EXISTS galpao_contagens (quando TEXT PRIMARY KEY, json TEXT);
        CREATE TABLE IF NOT EXISTS galpao_rua (quando TEXT PRIMARY KEY, json TEXT);
    """)
    # migracao aditiva: bancos criados antes dessas colunas
    existentes = {row[1] for row in conn.execute("PRAGMA table_info(galpao_pedidos)")}
    for nome in ("falhas_show", "show_pendente"):
        if nome not in existentes:
            conn.execute(f"ALTER TABLE galpao_pedidos ADD COLUMN {nome} INTEGER DEFAULT 0")
    conn.commit()


def _txt(dt) -> str | None:
    return dt.strftime(FMT) if isinstance(dt, datetime) else (dt or None)


# situacao "Fora dos abertos" = pedido sumiu dos abertos e a linha do tempo nao diz para onde foi.
def gravar_historico(conn: sqlite3.Connection, id_stokki: int, codigo: str, embarcador: str,
                     situacao: str, hist: dict) -> int:
    """Grava o pedido, a linha do tempo e a conferencia lidos da pagina show.
    Devolve quantos eventos eram novos (regravar o mesmo historico = 0)."""
    conn.execute("""
        INSERT INTO galpao_pedidos (id_stokki, codigo, embarcador, situacao, unidades, itens,
                                    operador_tarefa, codigo_separacao, atualizado_em)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id_stokki) DO UPDATE SET
            codigo = excluded.codigo, embarcador = excluded.embarcador, situacao = excluded.situacao,
            unidades = excluded.unidades, itens = excluded.itens,
            operador_tarefa = excluded.operador_tarefa, codigo_separacao = excluded.codigo_separacao,
            atualizado_em = excluded.atualizado_em
    """, (id_stokki, codigo, embarcador, situacao, hist.get("unidades"), hist.get("itens"),
          hist.get("operador_tarefa"), hist.get("codigo_separacao"), datetime.now().strftime(FMT)))
    novos = 0
    for ev in hist.get("linha_do_tempo") or []:
        cur = conn.execute(
            "INSERT OR IGNORE INTO galpao_eventos (id_stokki, situacao, usuario, quando) VALUES (?, ?, ?, ?)",
            (id_stokki, ev["situacao"], ev.get("usuario"), _txt(ev["quando"])))
        novos += cur.rowcount
    conf = hist.get("conferencia")
    if conf:
        conn.execute("""
            INSERT OR REPLACE INTO galpao_conferencias (id_stokki, estacao, usuario, inicio, fim, segundos, problema)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (id_stokki, conf.get("estacao"), conf.get("usuario"), _txt(conf.get("inicio")),
              _txt(conf.get("fim")), conf.get("segundos"), 1 if conf.get("problema") else 0))
    conn.commit()
    return novos


def ultima_coleta(conn: sqlite3.Connection, fonte: str, resultado: str | None = "ok") -> datetime | None:
    sql = "SELECT MAX(quando) FROM galpao_coletas WHERE fonte = ?"
    args = [fonte]
    if resultado:
        sql += " AND resultado = ?"
        args.append(resultado)
    valor = conn.execute(sql, args).fetchone()[0]
    return datetime.strptime(valor, FMT) if valor else None
