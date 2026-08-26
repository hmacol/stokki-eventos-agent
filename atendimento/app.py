# -*- coding: utf-8 -*-
"""
atendimento/app.py

Central de atendimento (WhatsApp via Evolution API) -- substitui o
Chatwoot (Hugo, 26/08: custo por conversa da Cloud API + verificação de
CNPJ travada na Meta inviabilizaram continuar por ali). Mesmo padrão dos
outros apps do projeto: Flask + waitress, sessão de login por cookie
(não Basic Auth), SQLite direto (atendimento/banco.py) -- ver
nucleo/api_motorista.py (fábrica criar_app) e painel_agentes/painel_agentes.py
(auth por sessão + exige_mesma_origem).

Diferença de auth em relação ao painel_agentes: lá são poucos usuários
fixos em config.yaml; aqui o time de atendentes cresce/muda, então login
é uma tabela de usuários de verdade (senha com hash) -- ver
atendimento/gerenciar_usuarios.py pra criar o primeiro admin.

COMO USAR (desenvolvimento, porta livre -- nunca 8060/8070/8071/8072/8073/8090):
    py -3.11 -m waitress --host=127.0.0.1 --port=8095 --call atendimento.app:criar_app
COMO USAR (produção, VPS -- infra/atendimento-central.service):
    waitress-serve --host=127.0.0.1 --port=8095 --call atendimento.app:criar_app
Atrás do Caddy nativo em atendimento.freshhub.com.br (atendimento/infra/Caddyfile-atendimento).

config.yaml:
    atendimento:
      secret_key: "<aleatório longo>"   # assina a sessão; trocar desloga todo mundo
      porta: 8095
    evolution_api:
      base_url: "http://127.0.0.1:8080"
      api_key: "..."
      instance: "..."
      webhook_secret: "..."
"""
import hmac
import logging
import sys
from datetime import timedelta
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))

import yaml
from flask import Flask, abort, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

from atendimento import banco
import integracao_evolution

logger = logging.getLogger("atendimento.app")


def _carregar_config() -> dict:
    caminho = _RAIZ / "config.yaml"
    if not caminho.exists():
        return {}
    with open(caminho, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def conn():
    if "conn" not in g:
        g.conn = banco.conectar()
    return g.conn


def exige_mesma_origem(f):
    """Bloqueia POSTs cuja Origin/Referer não seja deste próprio host --
    proteção contra CSRF (mesmo padrão de painel_agentes.py, adotado
    depois de uma auditoria que achou rotas de ação só com Basic Auth,
    que o navegador reanexa automaticamente a POSTs same-origin)."""
    @wraps(f)
    def decorado(*args, **kwargs):
        origem = request.headers.get("Origin") or request.headers.get("Referer")
        if not origem or urlparse(origem).netloc != request.host:
            abort(403, "Origem da requisição não confere (proteção CSRF).")
        return f(*args, **kwargs)
    return decorado


def requer_auth(f=None, *, niveis=("admin", "atendente")):
    """Login por sessão (cookie assinado) contra a tabela `usuarios` --
    diferente de painel_agentes.requer_auth, que checa contra pares fixos
    do config.yaml. Papel "admin" enxerga tudo; "atendente" não vê
    /usuarios nem /admin/whatsapp."""
    if f is not None:
        return requer_auth(niveis=niveis)(f)

    def decorator(func):
        @wraps(func)
        def decorado(*args, **kwargs):
            usuario_id = session.get("usuario_id")
            if usuario_id is None:
                if request.path.startswith(f"{request.script_root}/api/"):
                    return jsonify({"erro": "Sessão expirada -- faça login de novo."}), 401
                return redirect(url_for("login", proximo=request.script_root + request.full_path))
            papel = session.get("papel")
            if papel not in niveis:
                abort(403, "Seu usuário não tem permissão pra essa ação.")
            g.usuario_id = usuario_id
            g.papel = papel
            return func(*args, **kwargs)
        return decorado
    return decorator


def criar_app(config: dict | None = None) -> Flask:
    config = config if config is not None else _carregar_config()
    cfg_atendimento = config.get("atendimento", {}) or {}
    cfg_evolution = config.get("evolution_api", {}) or {}

    app = Flask(__name__)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_prefix=1, x_proto=1, x_for=1, x_host=1)
    app.config["CONFIG_EVOLUTION"] = cfg_evolution

    secret = cfg_atendimento.get("secret_key")
    if not secret:
        raise RuntimeError(
            "atendimento.secret_key ausente no config.yaml -- gere uma string aleatória "
            "forte (ex: python -c \"import secrets; print(secrets.token_hex(32))\") "
            "antes de subir o app."
        )
    app.secret_key = secret
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=14)
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SECURE"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    @app.teardown_appcontext
    def _fechar(_exc):
        c = g.pop("conn", None)
        if c is not None:
            c.close()

    # ── auth ─────────────────────────────────────────────────────────
    @app.route("/login", methods=["GET", "POST"])
    def login():
        erro = None
        if request.method == "POST":
            login_form = request.form.get("usuario", "").strip()
            senha = request.form.get("senha", "")
            usuario = conn().execute(
                "SELECT * FROM usuarios WHERE login = ? AND ativo = 1", (login_form,),
            ).fetchone()
            if not usuario or not check_password_hash(usuario["senha_hash"], senha):
                erro = "Usuário ou senha incorretos."
            else:
                session.clear()
                session.permanent = True
                session["usuario_id"] = usuario["id"]
                session["nome"] = usuario["nome"]
                session["papel"] = usuario["papel"]
                conn().execute(
                    "UPDATE usuarios SET ultimo_login_em = datetime('now','localtime') WHERE id = ?",
                    (usuario["id"],),
                )
                conn().commit()
                raiz = request.script_root or ""
                proximo = request.form.get("proximo") or url_for("inbox")
                if not (proximo == raiz or proximo.startswith(raiz + "/")):
                    proximo = url_for("inbox")
                return redirect(proximo)
        return render_template("login.html", erro=erro, proximo=request.args.get("proximo", ""))

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # ── páginas ──────────────────────────────────────────────────────
    @app.route("/")
    @requer_auth
    def raiz():
        return redirect(url_for("inbox"))

    @app.route("/inbox")
    @requer_auth
    def inbox():
        return render_template("inbox.html", times=banco.TIMES)

    @app.route("/usuarios")
    @requer_auth(niveis=("admin",))
    def usuarios_pagina():
        return render_template("usuarios.html")

    @app.route("/admin/whatsapp")
    @requer_auth(niveis=("admin",))
    def admin_whatsapp():
        return render_template("admin_whatsapp.html")

    # ── API: conversas ───────────────────────────────────────────────
    def _linha_conversa(row: dict) -> dict:
        return {
            "id": row["id"], "protocolo": row["protocolo"], "time": row["time"],
            "status": row["status"], "atendente_id": row["atendente_id"],
            "atendente_nome": row["atendente_nome"],
            "contato_nome": row["contato_nome"], "contato_telefone": row["contato_telefone"],
            "ultima_mensagem_em": row["ultima_mensagem_em"],
            "ultima_mensagem_preview": row["ultima_mensagem_preview"],
            "aberta_em": row["aberta_em"],
        }

    _SELECT_CONVERSAS = """
        SELECT c.*, ct.nome AS contato_nome, ct.telefone_e164 AS contato_telefone,
               u.nome AS atendente_nome
        FROM conversas c
        JOIN contatos ct ON ct.id = c.contato_id
        LEFT JOIN usuarios u ON u.id = c.atendente_id
    """

    @app.get("/api/conversas")
    @requer_auth
    def api_conversas():
        filtro = request.args.get("filtro", "fila")
        clausulas = ["c.status = 'ABERTA'"]
        parametros = []
        if filtro == "minhas":
            clausulas.append("c.atendente_id = ?")
            parametros.append(g.usuario_id)
        elif filtro == "fila":
            clausulas.append("c.atendente_id IS NULL")
        elif filtro.startswith("time:"):
            clausulas.append("c.time = ?")
            parametros.append(filtro.split(":", 1)[1])
        elif filtro == "resolvidas":
            clausulas = ["c.status = 'RESOLVIDA'"]
        # filtro == "todas": sem cláusula extra além do status ABERTA
        sql = f"{_SELECT_CONVERSAS} WHERE {' AND '.join(clausulas)} ORDER BY c.ultima_mensagem_em DESC"
        linhas = conn().execute(sql, parametros).fetchall()
        return jsonify({"conversas": [_linha_conversa(r) for r in linhas]})

    @app.get("/api/conversas/<int:conversa_id>/mensagens")
    @requer_auth
    def api_mensagens(conversa_id):
        conversa = conn().execute(f"{_SELECT_CONVERSAS} WHERE c.id = ?", (conversa_id,)).fetchone()
        if not conversa:
            return jsonify({"erro": "Conversa não encontrada."}), 404
        mensagens = conn().execute(
            "SELECT m.*, u.nome AS atendente_nome FROM mensagens m "
            "LEFT JOIN usuarios u ON u.id = m.atendente_id "
            "WHERE m.conversa_id = ? ORDER BY m.id ASC",
            (conversa_id,),
        ).fetchall()
        return jsonify({
            "conversa": _linha_conversa(conversa),
            "mensagens": [dict(m) for m in mensagens],
        })

    @app.post("/api/conversas/<int:conversa_id>/assumir")
    @requer_auth
    @exige_mesma_origem
    def api_assumir(conversa_id):
        ganhou = banco.assumir_conversa(conn(), conversa_id, g.usuario_id)
        if not ganhou:
            return jsonify({"erro": "Essa conversa já foi assumida por outro atendente."}), 409
        return jsonify({"ok": True})

    @app.post("/api/conversas/<int:conversa_id>/responder")
    @requer_auth
    @exige_mesma_origem
    def api_responder(conversa_id):
        conversa = conn().execute(
            "SELECT c.*, ct.telefone_e164 FROM conversas c JOIN contatos ct ON ct.id = c.contato_id "
            "WHERE c.id = ?", (conversa_id,),
        ).fetchone()
        if not conversa:
            return jsonify({"erro": "Conversa não encontrada."}), 404
        if conversa["atendente_id"] not in (None, g.usuario_id) and g.papel != "admin":
            return jsonify({"erro": "Essa conversa está com outro atendente."}), 403
        texto = (request.get_json(silent=True) or {}).get("texto", "").strip()
        if not texto:
            return jsonify({"erro": "Mensagem vazia."}), 400

        sucesso, evolution_id = integracao_evolution.enviar_texto(
            app.config["CONFIG_EVOLUTION"], conversa["telefone_e164"], texto,
        )
        if not sucesso:
            return jsonify({"erro": "Falha ao enviar pelo WhatsApp -- tente de novo em instantes."}), 502
        banco.registrar_mensagem(conn(), conversa_id, "OUT", texto, g.usuario_id, evolution_id)
        return jsonify({"ok": True})

    @app.post("/api/conversas/<int:conversa_id>/time")
    @requer_auth
    @exige_mesma_origem
    def api_time(conversa_id):
        time = (request.get_json(silent=True) or {}).get("time")
        if time not in banco.TIMES + (None,):
            return jsonify({"erro": "Time inválido."}), 400
        conn().execute("UPDATE conversas SET time = ? WHERE id = ?", (time, conversa_id))
        conn().commit()
        return jsonify({"ok": True})

    @app.post("/api/conversas/<int:conversa_id>/encerrar")
    @requer_auth
    @exige_mesma_origem
    def api_encerrar(conversa_id):
        conn().execute(
            "UPDATE conversas SET status = 'RESOLVIDA', encerrada_em = datetime('now','localtime') WHERE id = ?",
            (conversa_id,),
        )
        conn().commit()
        return jsonify({"ok": True})

    # ── API: usuários (admin) ────────────────────────────────────────
    @app.get("/api/usuarios")
    @requer_auth(niveis=("admin",))
    def api_usuarios_listar():
        linhas = conn().execute(
            "SELECT id, login, nome, papel, ativo, ultimo_login_em FROM usuarios ORDER BY nome",
        ).fetchall()
        return jsonify({"usuarios": [dict(r) for r in linhas]})

    @app.post("/api/usuarios")
    @requer_auth(niveis=("admin",))
    @exige_mesma_origem
    def api_usuarios_criar():
        corpo = request.get_json(silent=True) or {}
        login_novo = (corpo.get("login") or "").strip()
        nome = (corpo.get("nome") or "").strip()
        senha = corpo.get("senha") or ""
        papel = corpo.get("papel") if corpo.get("papel") in ("admin", "atendente") else "atendente"
        if not login_novo or not nome or len(senha) < 8:
            return jsonify({"erro": "Login, nome e senha (mín. 8 caracteres) são obrigatórios."}), 400
        try:
            conn().execute(
                "INSERT INTO usuarios (login, nome, senha_hash, papel) VALUES (?, ?, ?, ?)",
                (login_novo, nome, generate_password_hash(senha), papel),
            )
            conn().commit()
        except Exception:
            return jsonify({"erro": f"Já existe um usuário com o login '{login_novo}'."}), 409
        return jsonify({"ok": True}), 201

    @app.post("/api/usuarios/<int:usuario_id>/desativar")
    @requer_auth(niveis=("admin",))
    @exige_mesma_origem
    def api_usuarios_desativar(usuario_id):
        conn().execute("UPDATE usuarios SET ativo = 0 WHERE id = ?", (usuario_id,))
        conn().commit()
        return jsonify({"ok": True})

    @app.post("/api/usuarios/<int:usuario_id>/reativar")
    @requer_auth(niveis=("admin",))
    @exige_mesma_origem
    def api_usuarios_reativar(usuario_id):
        conn().execute("UPDATE usuarios SET ativo = 1 WHERE id = ?", (usuario_id,))
        conn().commit()
        return jsonify({"ok": True})

    # ── API: administração do WhatsApp (admin) ──────────────────────
    @app.get("/api/whatsapp/status")
    @requer_auth(niveis=("admin",))
    def api_whatsapp_status():
        if not integracao_evolution.configurado(app.config["CONFIG_EVOLUTION"]):
            return jsonify({"erro": "evolution_api não configurado no config.yaml."}), 400
        try:
            return jsonify(integracao_evolution.status_instancia(app.config["CONFIG_EVOLUTION"]))
        except Exception as exc:
            return jsonify({"erro": f"Falha ao consultar a Evolution API: {exc}"}), 502

    @app.post("/api/whatsapp/parear")
    @requer_auth(niveis=("admin",))
    @exige_mesma_origem
    def api_whatsapp_parear():
        if not integracao_evolution.configurado(app.config["CONFIG_EVOLUTION"]):
            return jsonify({"erro": "evolution_api não configurado no config.yaml."}), 400
        numero = (request.get_json(silent=True) or {}).get("numero")
        try:
            return jsonify(integracao_evolution.gerar_pareamento(app.config["CONFIG_EVOLUTION"], numero))
        except Exception as exc:
            return jsonify({"erro": f"Falha ao gerar pareamento: {exc}"}), 502

    # ── webhook (Evolution API -> aqui, sem sessão) ─────────────────
    @app.post("/webhook/evolution/<segredo>")
    def webhook_evolution(segredo):
        esperado = app.config["CONFIG_EVOLUTION"].get("webhook_secret", "")
        if not esperado or not hmac.compare_digest(segredo, esperado):
            abort(404)  # 404, não 401 -- não confirma pra fora que o path existe

        payload = request.get_json(silent=True) or {}
        if payload.get("event") != "messages.upsert":
            return jsonify({"ok": True})  # outros eventos (connection.update etc.) -- nada a fazer ainda

        dado = payload.get("data") or {}
        chave = dado.get("key") or {}
        remote_jid = chave.get("remoteJid", "")
        telefone = remote_jid.split("@")[0] if "@" in remote_jid else remote_jid
        if not telefone:
            return jsonify({"ok": True})
        telefone_e164 = f"+{telefone}" if not telefone.startswith("+") else telefone

        texto = (
            (dado.get("message") or {}).get("conversation")
            or ((dado.get("message") or {}).get("extendedTextMessage") or {}).get("text")
            or ""
        )
        nome_push = dado.get("pushName")
        de_mim_mesmo = bool(chave.get("fromMe"))

        contato = banco.buscar_ou_criar_contato(conn(), telefone_e164, nome_push)
        conversa = banco.conversa_aberta_do_contato(conn(), contato["id"])
        if conversa is None:
            conversa = banco.abrir_conversa(conn(), contato["id"])

        # fromMe:true = mensagem mandada direto do celular vinculado (não
        # pela UI) -- ainda assim tem que aparecer na thread, senão o
        # histórico fica incompleto (achado do design, ver plano).
        direcao = "OUT" if de_mim_mesmo else "IN"
        banco.registrar_mensagem(
            conn(), conversa["id"], direcao, texto,
            atendente_id=None, evolution_message_id=chave.get("id"),
        )
        return jsonify({"ok": True})

    return app


app = None
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--porta", type=int, default=8095)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
    app = criar_app()
    app.run(host="127.0.0.1", port=args.porta, debug=False)
else:
    app = None
