# -*- coding: utf-8 -*-
"""
vigia/banco.py

Tabelas do vigia em dados/dados.db:

  vigia_stokki_abertos  Retrato dos pedidos ABERTOS na Stokki que o
                        pipeline viu na última listagem completa (o vigia
                        não loga na Stokki -- sessão única, ver
                        stokki/sessao_uso.py). Traz a última ação do
                        pipeline pro pedido (erro, aguardando_redespacho,
                        pulado_cancelado_vuupt...) = o MOTIVO de ele não
                        ter virado serviço.
  vigia_pedidos         Estado atual de cada pedido aberto, desde quando,
                        e quando o prazo vence.
  vigia_historico       Cada estado que um pedido já teve (desde/até) --
                        base pra medir quanto tempo pedido fica parado.
"""
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "dados" / "dados.db"
FMT = "%Y-%m-%d %H:%M:%S"
AUSENCIAS_PRA_FECHAR = 2


def conectar(db_path: Path | None = None) -> sqlite3.Connection:
    caminho = db_path or DB_PATH
    caminho.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(caminho, timeout=30)
    conn.row_factory = sqlite3.Row
    garantir_esquema(conn)
    return conn


def garantir_esquema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS vigia_stokki_abertos (
            codigo          TEXT PRIMARY KEY,   -- PS-12345
            id_stokki       INTEGER,
            embarcador      TEXT,
            status_stokki   TEXT,
            fonte           TEXT,               -- prioritario | aguardando_transportador | estacao_impressao
            ultima_acao     TEXT,               -- acao do pipeline (criado, erro, aguardando_redespacho...)
            ultima_obs      TEXT,
            primeira_vez_em TEXT NOT NULL,
            ultima_vez_em   TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS vigia_listagens (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            concluida_em TEXT NOT NULL,
            completa    INTEGER NOT NULL,       -- 0 = alguma fonte falhou (não fecha ninguém)
            qtd         INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS vigia_pedidos (
            codigo      TEXT PRIMARY KEY,
            estado      TEXT NOT NULL,
            motivo      TEXT,
            desde       TEXT NOT NULL,
            vence_em    TEXT,                   -- NULL = estado sem prazo (ex.: agendado pra frente)
            vencido     INTEGER NOT NULL DEFAULT 0,
            service_id  INTEGER,
            detalhe     TEXT,                   -- rota/rascunho/embarcador, texto curto pra tela
            visto_em    TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_vigia_pedidos_estado ON vigia_pedidos(estado, vencido);
        CREATE TABLE IF NOT EXISTS vigia_historico (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            codigo  TEXT NOT NULL,
            estado  TEXT NOT NULL,
            motivo  TEXT,
            desde   TEXT NOT NULL,
            ate     TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_vigia_historico_codigo ON vigia_historico(codigo);
    """)
    colunas = {r[1] for r in conn.execute("PRAGMA table_info(vigia_stokki_abertos)")}
    if "ausencias" not in colunas:
        conn.execute("ALTER TABLE vigia_stokki_abertos ADD COLUMN ausencias INTEGER NOT NULL DEFAULT 0")
        conn.commit()


def normalizar(codigo) -> str:
    return str(codigo or "").strip().lstrip("#").strip().upper()


def registrar_listagem_stokki(itens: list[dict], completa: bool, agora: datetime | None = None,
                              conn: sqlite3.Connection | None = None) -> None:
    """Chamado pelo pipeline no fim de uma rodada SEM filtro. `itens`:
    [{"codigo", "id_stokki", "embarcador", "status_stokki", "fonte",
    "acao", "observacao"}]. Quem não apareceu em DUAS listagens completas
    seguidas sai (foi expedido, cancelado...). Uma ausência só não basta:
    uma fonte pode voltar vazia sem erro (a Estação de Impressão devolve []
    quando a captura falha), e apagar zeraria o "desde" do pedido.
    Listagem incompleta só atualiza quem apareceu."""
    agora_txt = (agora or datetime.now()).strftime(FMT)
    fechar = conn is None
    conn = conn or conectar()
    try:
        vistos = set()
        for it in itens:
            codigo = normalizar(it.get("codigo"))
            if not codigo:
                continue
            vistos.add(codigo)
            conn.execute("""
                INSERT INTO vigia_stokki_abertos
                    (codigo, id_stokki, embarcador, status_stokki, fonte, ultima_acao, ultima_obs,
                     primeira_vez_em, ultima_vez_em, ausencias)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(codigo) DO UPDATE SET
                    id_stokki = excluded.id_stokki, embarcador = excluded.embarcador,
                    status_stokki = excluded.status_stokki, fonte = excluded.fonte,
                    ultima_acao = excluded.ultima_acao, ultima_obs = excluded.ultima_obs,
                    ultima_vez_em = excluded.ultima_vez_em, ausencias = 0
            """, (codigo, it.get("id_stokki"), it.get("embarcador"), it.get("status_stokki"),
                  it.get("fonte"), it.get("acao"), (it.get("observacao") or "")[:300],
                  agora_txt, agora_txt))
        if completa:
            antigos = {r["codigo"] for r in conn.execute("SELECT codigo FROM vigia_stokki_abertos")}
            for codigo in antigos - vistos:
                conn.execute("UPDATE vigia_stokki_abertos SET ausencias = ausencias + 1 WHERE codigo = ?",
                             (codigo,))
            conn.execute(f"DELETE FROM vigia_stokki_abertos WHERE ausencias >= {AUSENCIAS_PRA_FECHAR}")
        conn.execute("INSERT INTO vigia_listagens (concluida_em, completa, qtd) VALUES (?, ?, ?)",
                     (agora_txt, 1 if completa else 0, len(vistos)))
        conn.commit()
    finally:
        if fechar:
            conn.close()


def ultima_listagem_completa(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT MAX(concluida_em) AS quando FROM vigia_listagens WHERE completa = 1").fetchone()
    return row["quando"] if row else None
