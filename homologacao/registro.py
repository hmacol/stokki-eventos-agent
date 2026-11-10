"""Registro dos envios bloqueados na homologacao (tabela homolog_envios)."""
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
_AQUI = Path(__file__).resolve().parent
CAMINHO_BANCO = Path(os.environ.get("HOMOLOG_REGISTRO_BANCO") or (_RAIZ / "dados" / "dados.db"))
TIMEOUT = 5
LIMITE_CONTEUDO = 65536

_DDL = ("CREATE TABLE IF NOT EXISTS homolog_envios("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, quando TEXT NOT NULL, canal TEXT NOT NULL, "
        "metodo TEXT, destino TEXT, resumo TEXT, conteudo TEXT, origem TEXT, pid INTEGER)")


def _origem() -> str:
    # primeiro frame fora de homologacao/ (modulos de teste do pacote contam como fora)
    f = sys._getframe(1)
    while f is not None:
        arq = Path(f.f_code.co_filename).resolve()
        if arq.parent != _AQUI or arq.name.startswith("test_"):
            return "%s:%d" % (arq.name, f.f_lineno)
        f = f.f_back
    return "?"


def registrar_envio(canal, metodo, destino, resumo, conteudo, origem=None):
    """Nunca levanta. Devolve o id gravado, ou None se caiu no .jsonl."""
    try:
        if isinstance(conteudo, (bytes, bytearray)):
            conteudo = bytes(conteudo).decode("utf-8", errors="replace")
        if conteudo is not None:
            conteudo = str(conteudo)[:LIMITE_CONTEUDO]
        if origem is None:
            origem = _origem()
        quando = datetime.now().isoformat(timespec="seconds")
        linha = (quando, canal, metodo, destino, resumo, conteudo, origem, os.getpid())
        banco = Path(CAMINHO_BANCO)
        try:
            con = sqlite3.connect(str(banco), timeout=TIMEOUT)
            try:
                con.execute(_DDL)
                cur = con.execute(
                    "INSERT INTO homolog_envios(quando, canal, metodo, destino, resumo, "
                    "conteudo, origem, pid) VALUES (?,?,?,?,?,?,?,?)", linha)
                con.commit()
                return cur.lastrowid
            finally:
                con.close()
        except sqlite3.Error:
            chaves = ("quando", "canal", "metodo", "destino", "resumo", "conteudo", "origem", "pid")
            with open(banco.parent / "homolog_envios.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(dict(zip(chaves, linha)), ensure_ascii=False) + "\n")
            return None
    except Exception:
        return None


def _conectar():
    con = sqlite3.connect(str(CAMINHO_BANCO), timeout=TIMEOUT)
    con.row_factory = sqlite3.Row
    con.execute(_DDL)
    return con


def listar_envios(canal=None, limite=200):
    con = _conectar()
    try:
        if canal:
            cur = con.execute("SELECT * FROM homolog_envios WHERE canal=? ORDER BY id DESC LIMIT ?",
                              (canal, limite))
        else:
            cur = con.execute("SELECT * FROM homolog_envios ORDER BY id DESC LIMIT ?", (limite,))
        return [dict(r) for r in cur.fetchall()]
    finally:
        con.close()


def buscar_envio(envio_id):
    con = _conectar()
    try:
        r = con.execute("SELECT * FROM homolog_envios WHERE id=?", (envio_id,)).fetchone()
        return dict(r) if r else None
    finally:
        con.close()
