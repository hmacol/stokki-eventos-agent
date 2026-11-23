# -*- coding: utf-8 -*-
"""
painel_agentes/grupos_equipe.py

Grupos internos da equipe dentro do /atendimento (Hugo, 19/10/2026): a
conversa que hoje roda nos grupos de WhatsApp (pedido, cliente, conversa
encaminhada) passa a ficar ao lado dos chamados.

Regras combinadas com o Hugo:
- qualquer usuário do Atendimento cria grupo; todo grupo é aberto (todos
  veem, leem e escrevem), sem convite nem lista de membros;
- só texto nesta versão; ninguém edita nem apaga mensagem;
- não lidas por pessoa; um chamado pode ser enviado pra um grupo.

O login do painel pode ser compartilhado por nível (o atendente informa o
nome na tela), então "minha mensagem" e o "lido" valem por login E nome.

Tabelas em dados/dados.db (mesmo banco dos chamados). Rotas registradas
por `registrar(app, ...)`, chamado de atendimento_chamados.py.
"""
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from flask import abort, g, jsonify, request, session

logger = logging.getLogger("painel.grupos_equipe")

DB_PATH = Path(__file__).parent.parent / "dados" / "dados.db"
NOME_MIN, NOME_MAX = 2, 60
TEXTO_MAX = 4000


class ErroGrupo(Exception):
    """Mensagem pro usuário (vira 400 na API)."""


def conectar(caminho=None) -> sqlite3.Connection:
    conn = sqlite3.connect(caminho or DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS equipe_grupos (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            nome          TEXT NOT NULL,
            criado_por    TEXT,
            criado_em     TEXT NOT NULL,
            atualizado_em TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS equipe_grupos_mensagens (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            grupo_id    INTEGER NOT NULL,
            usuario     TEXT NOT NULL,
            autor       TEXT NOT NULL,
            texto       TEXT NOT NULL,
            chamado_id  INTEGER,
            criado_em   TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_equipe_grupos_msg ON equipe_grupos_mensagens (grupo_id, id);
        CREATE TABLE IF NOT EXISTS equipe_grupos_lidas (
            grupo_id  INTEGER NOT NULL,
            usuario   TEXT NOT NULL,
            ultimo_id INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (grupo_id, usuario)
        );
    """)
    return conn


def _agora() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _leitor(usuario: str, autor: str) -> str:
    return f"{usuario}|{autor}"


def _quando(valor: str | None) -> str:
    """'14:32' se hoje, '18/10 14:32' se outro dia."""
    if not valor:
        return ""
    try:
        d = datetime.strptime(valor, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return valor
    return d.strftime("%H:%M") if d.date() == datetime.now().date() else d.strftime("%d/%m %H:%M")


def criar_grupo(conn, nome: str, usuario: str, autor: str) -> dict:
    nome = " ".join((nome or "").split())
    if not NOME_MIN <= len(nome) <= NOME_MAX:
        raise ErroGrupo(f"O nome do grupo precisa ter de {NOME_MIN} a {NOME_MAX} letras.")
    if conn.execute("SELECT 1 FROM equipe_grupos WHERE lower(nome) = lower(?)", (nome,)).fetchone():
        raise ErroGrupo(f"Já existe um grupo chamado {nome}.")
    agora = _agora()
    cur = conn.execute("INSERT INTO equipe_grupos (nome, criado_por, criado_em, atualizado_em) VALUES (?, ?, ?, ?)",
                       (nome, autor, agora, agora))
    conn.commit()
    logger.info("grupo %s '%s' criado por %s (%s)", cur.lastrowid, nome, autor, usuario)
    return buscar_grupo(conn, cur.lastrowid)


def buscar_grupo(conn, grupo_id: int) -> dict | None:
    r = conn.execute("SELECT id, nome, criado_por, criado_em FROM equipe_grupos WHERE id = ?", (grupo_id,)).fetchone()
    return {**dict(r), "criado_quando": _quando(r["criado_em"])} if r else None


def listar_grupos(conn, usuario: str, autor: str) -> list[dict]:
    """Todos os grupos, o de atividade mais recente primeiro, com as não
    lidas de quem pergunta e a última mensagem."""
    rows = conn.execute("""
        SELECT gr.id, gr.nome, gr.criado_por, gr.atualizado_em,
               (SELECT COUNT(*) FROM equipe_grupos_mensagens m
                 WHERE m.grupo_id = gr.id AND m.id > COALESCE(l.ultimo_id, 0)
                   AND NOT (m.usuario = :usuario AND m.autor = :autor)) AS nao_lidas,
               u.texto AS ultima_texto, u.autor AS ultima_autor, u.criado_em AS ultima_em
        FROM equipe_grupos gr
        LEFT JOIN equipe_grupos_lidas l ON l.grupo_id = gr.id AND l.usuario = :leitor
        LEFT JOIN equipe_grupos_mensagens u ON u.id = (SELECT MAX(id) FROM equipe_grupos_mensagens WHERE grupo_id = gr.id)
        ORDER BY gr.atualizado_em DESC, COALESCE(u.id, 0) DESC, gr.id DESC
    """, {"usuario": usuario, "autor": autor, "leitor": _leitor(usuario, autor)}).fetchall()
    return [{**dict(r), "quando": _quando(r["ultima_em"] or r["atualizado_em"])} for r in rows]


def mensagens(conn, grupo_id: int, desde: int = 0, usuario: str = "", autor: str = "", limite: int = 300) -> list[dict]:
    """Mensagens depois de `desde` (as últimas `limite`, em ordem)."""
    rows = conn.execute("""
        SELECT * FROM (SELECT id, usuario, autor, texto, chamado_id, criado_em FROM equipe_grupos_mensagens
                       WHERE grupo_id = ? AND id > ? ORDER BY id DESC LIMIT ?) ORDER BY id
    """, (grupo_id, desde, limite)).fetchall()
    return [{"id": r["id"], "autor": r["autor"], "texto": r["texto"], "chamado_id": r["chamado_id"],
             "quando": _quando(r["criado_em"]), "eu": r["usuario"] == usuario and r["autor"] == autor} for r in rows]


def marcar_lido(conn, grupo_id: int, usuario: str, autor: str, ate_id: int) -> None:
    conn.execute("""
        INSERT INTO equipe_grupos_lidas (grupo_id, usuario, ultimo_id) VALUES (?, ?, ?)
        ON CONFLICT (grupo_id, usuario) DO UPDATE SET ultimo_id = MAX(ultimo_id, excluded.ultimo_id)
    """, (grupo_id, _leitor(usuario, autor), ate_id))
    conn.commit()


def postar(conn, grupo_id: int, usuario: str, autor: str, texto: str, chamado_id: int | None = None) -> dict:
    texto = (texto or "").strip()
    if not texto:
        raise ErroGrupo("Escreva a mensagem.")
    if len(texto) > TEXTO_MAX:
        raise ErroGrupo(f"Mensagem muito longa (máximo {TEXTO_MAX} letras).")
    if not buscar_grupo(conn, grupo_id):
        raise ErroGrupo("Grupo não encontrado.")
    agora = _agora()
    cur = conn.execute("INSERT INTO equipe_grupos_mensagens (grupo_id, usuario, autor, texto, chamado_id, criado_em) "
                       "VALUES (?, ?, ?, ?, ?, ?)", (grupo_id, usuario, autor, texto, chamado_id, agora))
    conn.execute("UPDATE equipe_grupos SET atualizado_em = ? WHERE id = ?", (agora, grupo_id))
    conn.commit()
    marcar_lido(conn, grupo_id, usuario, autor, cur.lastrowid)
    return mensagens(conn, grupo_id, cur.lastrowid - 1, usuario, autor)[0]


def texto_chamado(chamado: dict, comentario: str = "") -> str:
    """Resumo do chamado que vai pro grupo no 'Enviar para grupo'."""
    quem = ("motorista " if chamado.get("de_motorista") else "") + (chamado.get("solicitante") or "")
    linhas = [f"Chamado #{chamado['id']} · {quem}".rstrip(" ·")]
    if chamado.get("assunto"):
        linhas.append(f"Assunto: {chamado['assunto']}")
    if chamado.get("pedido_ref"):
        linhas.append(f"{'Parada' if chamado.get('de_motorista') else 'Pedido'}: {chamado['pedido_ref']}")
    if chamado.get("status_rotulo"):
        linhas.append(f"Situação: {chamado['status_rotulo']}")
    comentario = (comentario or "").strip()
    return (comentario + "\n\n" if comentario else "") + "\n".join(linhas)


def registrar(app, *, requer_auth, exige_mesma_origem, niveis, buscar_chamado):
    """Rotas /api/atendimento/grupos*. buscar_chamado(id) -> dict | None
    (o chamado já formatado, como o /atendimento usa)."""

    def _usuario() -> str:
        return session.get("usuario") or g.nivel_acesso

    def _nome() -> str:
        return session.get("atendente_nome") or _usuario()

    def _erro(e):
        return jsonify({"erro": str(e)}), 400

    @app.route("/api/atendimento/grupos")
    @requer_auth(niveis=niveis)
    def api_grupos():
        conn = conectar()
        try:
            grupos = listar_grupos(conn, _usuario(), _nome())
            return jsonify({"grupos": grupos, "nao_lidas": sum(x["nao_lidas"] for x in grupos)})
        finally:
            conn.close()

    @app.route("/api/atendimento/grupos", methods=["POST"])
    @requer_auth(niveis=niveis)
    @exige_mesma_origem
    def api_grupos_criar():
        corpo = request.get_json(silent=True) or {}
        conn = conectar()
        try:
            return jsonify({"grupo": criar_grupo(conn, corpo.get("nome"), _usuario(), _nome())})
        except ErroGrupo as e:
            return _erro(e)
        finally:
            conn.close()

    @app.route("/api/atendimento/grupos/<int:grupo_id>")
    @requer_auth(niveis=niveis)
    def api_grupo(grupo_id):
        desde = request.args.get("desde", 0, type=int)
        conn = conectar()
        try:
            grupo = buscar_grupo(conn, grupo_id)
            if not grupo:
                abort(404)
            msgs = mensagens(conn, grupo_id, desde, _usuario(), _nome())
            if msgs:
                marcar_lido(conn, grupo_id, _usuario(), _nome(), msgs[-1]["id"])
            return jsonify({"grupo": grupo, "mensagens": msgs})
        finally:
            conn.close()

    @app.route("/api/atendimento/grupos/<int:grupo_id>/mensagens", methods=["POST"])
    @requer_auth(niveis=niveis)
    @exige_mesma_origem
    def api_grupo_mensagem(grupo_id):
        corpo = request.get_json(silent=True) or {}
        conn = conectar()
        try:
            return jsonify({"mensagem": postar(conn, grupo_id, _usuario(), _nome(), corpo.get("texto"))})
        except ErroGrupo as e:
            return _erro(e)
        finally:
            conn.close()

    @app.route("/api/atendimento/chamados/<int:chamado_id>/enviar-grupo", methods=["POST"])
    @requer_auth(niveis=niveis)
    @exige_mesma_origem
    def api_chamado_enviar_grupo(chamado_id):
        corpo = request.get_json(silent=True) or {}
        chamado = buscar_chamado(chamado_id)
        if not chamado:
            abort(404)
        conn = conectar()
        try:
            m = postar(conn, int(corpo.get("grupo_id") or 0), _usuario(), _nome(),
                       texto_chamado(chamado, corpo.get("comentario") or ""), chamado_id=chamado_id)
            logger.info("chamado %s enviado ao grupo %s por %s", chamado_id, corpo.get("grupo_id"), _nome())
            return jsonify({"mensagem": m})
        except (ErroGrupo, ValueError) as e:
            return _erro(e)
        finally:
            conn.close()
