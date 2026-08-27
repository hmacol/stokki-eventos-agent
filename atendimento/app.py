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
import base64
import hmac
import logging
import mimetypes
import random
import re
import sys
import threading
from datetime import timedelta
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))

import yaml
from flask import Flask, abort, g, jsonify, redirect, render_template, request, send_file, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

from atendimento import alertas, banco
import integracao_evolution

logger = logging.getLogger("atendimento.app")

_PASTA_MIDIA = _RAIZ / "dados" / "atendimento_midia"

# (chave do payload da Evolution, categoria estável pro front-end, rótulo em
# português pro texto de mensagens.corpo)
_TIPOS_MIDIA = (
    ("imageMessage", "imagem", "Imagem"), ("videoMessage", "video", "Vídeo"),
    ("audioMessage", "audio", "Áudio"), ("documentMessage", "documento", "Documento"),
    ("stickerMessage", "figurinha", "Figurinha"),
)


def _analisar_midia(mensagem: dict) -> tuple[str, str, str] | None:
    """(chave_evolution, categoria, rótulo) do primeiro tipo de mídia
    reconhecido em `mensagem`, ou None se não for mídia."""
    for chave, categoria, rotulo in _TIPOS_MIDIA:
        if mensagem.get(chave):
            return chave, categoria, rotulo
    return None


def _descrever_conteudo_mensagem(mensagem: dict) -> str | None:
    """Texto pra guardar em mensagens.corpo a partir do payload bruto do
    webhook -- pra mídia, é o rótulo (📎 Tipo: legenda) usado como legenda
    abaixo do arquivo de verdade (ver _baixar_e_salvar_midia) ou, se o
    download falhar, como único indício de que algo chegou. Retorna None
    quando o evento não tem conteúdo reconhecível (ex.: reação, recibo,
    distribuição de chave) -- quem chama decide não gravar nada nesse caso."""
    texto = mensagem.get("conversation") or (mensagem.get("extendedTextMessage") or {}).get("text")
    if texto:
        return texto
    achado = _analisar_midia(mensagem)
    if achado:
        chave, _categoria, rotulo = achado
        m = mensagem[chave]
        partes = [f"📎 {rotulo}"]
        if m.get("fileName"):
            partes.append(m["fileName"])
        descricao = " — ".join(partes)
        if m.get("caption"):
            descricao += f": {m['caption']}"
        return descricao
    return None


def _baixar_e_salvar_midia(cfg_evolution: dict, dado: dict, achado_midia: tuple,
                            evolution_message_id: str | None) -> dict | None:
    """Baixa a mídia de uma mensagem recebida (ver integracao_evolution.baixar_midia,
    que exige o `dado` INTEIRO do webhook, não só a key) e salva em
    dados/atendimento_midia/. Nunca levanta -- None em qualquer falha (rede,
    mídia expirada, base64 inválido), e quem chama cai de volta pro
    comportamento de só gravar o rótulo de texto."""
    _chave, categoria, _rotulo = achado_midia
    resultado = integracao_evolution.baixar_midia(cfg_evolution, dado)
    if not resultado:
        return None
    try:
        conteudo = base64.b64decode(resultado["base64"])
    except Exception:
        logger.warning("Base64 de mídia inválido recebido da Evolution API.")
        return None

    mime = resultado.get("mimetype") or "application/octet-stream"
    extensao = mimetypes.guess_extension(mime.split(";")[0].strip()) or ""
    id_seguro = re.sub(r"[^A-Za-z0-9_-]", "_", evolution_message_id or "") or uuid4().hex
    nome_arquivo = f"{id_seguro}{extensao}"

    _PASTA_MIDIA.mkdir(parents=True, exist_ok=True)
    caminho = _PASTA_MIDIA / nome_arquivo
    caminho.write_bytes(conteudo)

    return {
        "categoria": categoria, "mime": mime,
        "caminho": str(caminho.relative_to(_RAIZ)),
        "nome_original": resultado.get("fileName"),
    }


_TENTATIVAS_MENU_MAX = 2  # depois disso o bot desiste e cai pra fila geral

_MENU_TRIAGEM_TEXTO = (
    "Olá! 👋 Pra te ajudar mais rápido, escolha uma opção:\n\n"
    "1️⃣ Status do pedido/entrega\n"
    "2️⃣ Enviar canhoto/comprovante\n"
    "3️⃣ Cotação de frete\n"
    "4️⃣ Outro assunto / falar com atendente\n\n"
    "Responda só com o número (ex: 1)."
)

_MENU_TRIAGEM_REPETIR = (
    "Não entendi 🤔 Responda só com o número de uma das opções:\n\n"
    "1️⃣ Status do pedido/entrega\n"
    "2️⃣ Enviar canhoto/comprovante\n"
    "3️⃣ Cotação de frete\n"
    "4️⃣ Outro assunto / falar com atendente"
)

_MENU_TRIAGEM_DESISTENCIA = "Sem problemas, já te encaminho pra um atendente conversar com você."

# Resposta instantânea (<1s) é um dos sinais que o WhatsApp usa pra
# identificar automação -- suspeito de contribuir pro erro 463 (ver
# integracao_evolution.enviar_texto). Faixa aleatória, não fixa, pra não
# ficar óbvio que é sempre o mesmo tempo.
_BOT_DELAY_MS_MIN = 1500
_BOT_DELAY_MS_MAX = 3500

# Mapeamento motivo->time fácil de ajustar (uma linha) se o Hugo quiser outro
# time pra alguma opção depois -- ver plano.
_OPCOES_TRIAGEM = {
    "1": {"motivo": "status_pedido", "time": "DESTINATARIOS",
          "resposta": "Certo! Já te encaminho pro time responsável pelo status do seu pedido. Só um instante 🙏"},
    "2": {"motivo": "canhoto", "time": "DESTINATARIOS",
          "resposta": "Perfeito! Encaminhando pro time que cuida de canhotos/comprovantes. Já te retornamos."},
    "3": {"motivo": "cotacao", "time": "COMERCIAL_FINANCEIRO",
          "resposta": "Show! Vou te direcionar pro time comercial pra cotação de frete."},
    "4": {"motivo": "outro", "time": None,
          "resposta": "Ok, já te encaminho pra um atendente."},
}


def _extrair_opcao_menu(texto: str | None) -> str | None:
    """Primeiro dígito 1-4 encontrado na mensagem (tolera "1)", "opção 2"
    etc.) -- triagem por menu, não por IA/NLP, ver plano."""
    if not texto:
        return None
    m = re.search(r"[1-4]", texto)
    return m.group(0) if m else None


def _bot_enviar(cfg_evolution: dict, conexao, conversa_id: int, telefone_e164: str, texto: str) -> None:
    """Manda uma mensagem do bot de triagem e grava igual a uma resposta de
    atendente -- mesmo caminho de falha/retry (status PENDENTE cai na fila
    de reenviar_pendentes.py, que não distingue quem mandou). Delay + "digitando"
    aleatório (ver _BOT_DELAY_MS_*) pra não responder instantaneamente."""
    delay_ms = random.randint(_BOT_DELAY_MS_MIN, _BOT_DELAY_MS_MAX)
    sucesso, evolution_id = integracao_evolution.enviar_texto(cfg_evolution, telefone_e164, texto, delay_ms=delay_ms)
    bot_id = banco.usuario_bot_id(conexao)
    banco.registrar_mensagem(
        conexao, conversa_id, "OUT", texto, atendente_id=bot_id,
        evolution_message_id=evolution_id, status="ENVIADA" if sucesso else "PENDENTE",
    )


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
    app.config["CONFIG_COMPLETA"] = config  # e-mail de alerta (atendimento/alertas.py) precisa da seção email
    app.config["SUSPENSAO_463_HORAS"] = float(
        cfg_atendimento.get("suspensao_463_horas", banco.SUSPENSAO_463_HORAS_PADRAO)
    )

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

    @app.route("/respostas-rapidas")
    @requer_auth(niveis=("admin",))
    def respostas_rapidas_pagina():
        return render_template("respostas_rapidas.html")

    @app.route("/admin/whatsapp")
    @requer_auth(niveis=("admin",))
    def admin_whatsapp():
        return render_template("admin_whatsapp.html")

    @app.get("/api/metricas")
    @requer_auth
    def api_metricas():
        return jsonify(banco.calcular_metricas(conn()))

    # ── API: conversas ───────────────────────────────────────────────
    def _linha_conversa(row: dict) -> dict:
        return {
            "id": row["id"], "protocolo": row["protocolo"], "time": row["time"],
            "motivo_contato": row["motivo_contato"],
            "status": row["status"], "atendente_id": row["atendente_id"],
            "atendente_nome": row["atendente_nome"],
            "contato_id": row["contato_id"],
            "contato_nome": row["contato_nome"], "contato_telefone": row["contato_telefone"],
            "ultima_mensagem_em": row["ultima_mensagem_em"],
            "ultima_mensagem_preview": row["ultima_mensagem_preview"],
            "aberta_em": row["aberta_em"], "encerrada_em": row["encerrada_em"],
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
        busca = (request.args.get("busca") or "").strip()
        if busca:
            # Busca vale pra qualquer conversa (aberta ou resolvida) --
            # revisar o que já foi falado é justamente o caso de uso do
            # histórico de conversas encerradas.
            termo = f"%{busca}%"
            sql = f"{_SELECT_CONVERSAS} WHERE ct.nome LIKE ? OR ct.telefone_e164 LIKE ? OR c.protocolo LIKE ? " \
                  "ORDER BY c.ultima_mensagem_em DESC LIMIT 50"
            linhas = conn().execute(sql, (termo, termo, termo)).fetchall()
            return jsonify({"conversas": [_linha_conversa(r) for r in linhas]})

        filtro = request.args.get("filtro", "fila")
        clausulas = ["c.status = 'ABERTA'"]
        parametros = []
        ordem = "c.ultima_mensagem_em DESC"
        if filtro == "minhas":
            clausulas.append("c.atendente_id = ?")
            parametros.append(g.usuario_id)
        elif filtro == "fila":
            clausulas.append("c.atendente_id IS NULL")
            # Fila de espera de verdade: quem está esperando há mais tempo
            # entra primeiro, não a mensagem mais recente (isso já é
            # "minhas"/"todas", que são sobre atividade, não espera).
            ordem = "c.aberta_em ASC"
        elif filtro.startswith("time:"):
            clausulas.append("c.time = ?")
            parametros.append(filtro.split(":", 1)[1])
        elif filtro == "resolvidas":
            clausulas = ["c.status = 'RESOLVIDA'"]
            ordem = "c.encerrada_em DESC"
        # filtro == "todas": sem cláusula extra além do status ABERTA
        sql = f"{_SELECT_CONVERSAS} WHERE {' AND '.join(clausulas)} ORDER BY {ordem}"
        linhas = conn().execute(sql, parametros).fetchall()
        return jsonify({"conversas": [_linha_conversa(r) for r in linhas]})

    @app.get("/api/mensagens/novas")
    @requer_auth
    def api_mensagens_novas():
        """Base da notificação de mensagem nova (som/Notification API no
        navegador, ver base.html): mensagens recebidas em conversas que
        são minhas ou ainda não assumidas por ninguém, com id maior que
        o último visto pelo cliente."""
        desde = request.args.get("desde", type=int) or 0
        linhas = conn().execute(
            "SELECT m.id, m.conversa_id, m.corpo, c.protocolo, "
            "       ct.nome AS contato_nome, ct.telefone_e164 AS contato_telefone "
            "FROM mensagens m "
            "JOIN conversas c ON c.id = m.conversa_id "
            "JOIN contatos ct ON ct.id = c.contato_id "
            "WHERE m.id > ? AND m.direcao = 'IN' AND c.status = 'ABERTA' "
            "  AND (c.atendente_id IS NULL OR c.atendente_id = ?) "
            "ORDER BY m.id ASC LIMIT 50",
            (desde, g.usuario_id),
        ).fetchall()
        max_id = conn().execute("SELECT COALESCE(MAX(id), 0) AS m FROM mensagens").fetchone()["m"]
        return jsonify({"mensagens": [dict(r) for r in linhas], "max_id": max_id})

    @app.post("/api/contatos/<int:contato_id>/nome")
    @requer_auth
    @exige_mesma_origem
    def api_contato_renomear(contato_id):
        nome = (request.get_json(silent=True) or {}).get("nome", "").strip()
        if not nome:
            return jsonify({"erro": "Nome não pode ser vazio."}), 400
        conn().execute("UPDATE contatos SET nome = ? WHERE id = ?", (nome, contato_id))
        conn().commit()
        return jsonify({"ok": True})

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
        notas = conn().execute(
            "SELECT n.*, u.nome AS atendente_nome FROM notas_internas n "
            "LEFT JOIN usuarios u ON u.id = n.atendente_id "
            "WHERE n.conversa_id = ? ORDER BY n.id ASC",
            (conversa_id,),
        ).fetchall()
        return jsonify({
            "conversa": _linha_conversa(conversa),
            "mensagens": [dict(m) for m in mensagens],
            "notas": [dict(n) for n in notas],
        })

    @app.post("/api/conversas/<int:conversa_id>/notas")
    @requer_auth
    @exige_mesma_origem
    def api_notas_criar(conversa_id):
        corpo = (request.get_json(silent=True) or {}).get("corpo", "").strip()
        if not corpo:
            return jsonify({"erro": "Nota vazia."}), 400
        conn().execute(
            "INSERT INTO notas_internas (conversa_id, atendente_id, corpo) VALUES (?, ?, ?)",
            (conversa_id, g.usuario_id, corpo),
        )
        conn().commit()
        return jsonify({"ok": True}), 201

    @app.post("/api/conversas/<int:conversa_id>/reabrir")
    @requer_auth
    @exige_mesma_origem
    def api_reabrir(conversa_id):
        conn().execute(
            "UPDATE conversas SET status = 'ABERTA', encerrada_em = NULL WHERE id = ?",
            (conversa_id,),
        )
        conn().commit()
        return jsonify({"ok": True})

    @app.get("/api/respostas-rapidas")
    @requer_auth
    def api_respostas_rapidas_listar():
        linhas = conn().execute("SELECT * FROM respostas_rapidas ORDER BY titulo").fetchall()
        return jsonify({"respostas": [dict(r) for r in linhas]})

    @app.post("/api/respostas-rapidas")
    @requer_auth(niveis=("admin",))
    @exige_mesma_origem
    def api_respostas_rapidas_criar():
        corpo_json = request.get_json(silent=True) or {}
        titulo = (corpo_json.get("titulo") or "").strip()
        corpo = (corpo_json.get("corpo") or "").strip()
        if not titulo or not corpo:
            return jsonify({"erro": "Título e texto são obrigatórios."}), 400
        conn().execute("INSERT INTO respostas_rapidas (titulo, corpo) VALUES (?, ?)", (titulo, corpo))
        conn().commit()
        return jsonify({"ok": True}), 201

    @app.post("/api/respostas-rapidas/<int:resposta_id>/excluir")
    @requer_auth(niveis=("admin",))
    @exige_mesma_origem
    def api_respostas_rapidas_excluir(resposta_id):
        conn().execute("DELETE FROM respostas_rapidas WHERE id = ?", (resposta_id,))
        conn().commit()
        return jsonify({"ok": True})

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
            # Não perde a mensagem: grava como PENDENTE (aparece na thread com
            # selo de "reenviando") e deixa reenviar_pendentes.py (timer a cada
            # 1min) assumir o retry -- em vez do 502 anterior, que só devolvia
            # erro pro atendente sem guardar nada.
            banco.registrar_mensagem(conn(), conversa_id, "OUT", texto, g.usuario_id, status="PENDENTE")
            return jsonify({"ok": True, "pendente": True}), 202
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
            "SELECT id, login, nome, papel, ativo, ultimo_login_em FROM usuarios "
            "WHERE papel != 'bot' ORDER BY nome",
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
        # Sempre devolve o que o monitor já sabe (estado_evolution + fila de
        # reenvio, sem chamada de rede) mesmo quando a checagem ao vivo abaixo
        # falha -- é justamente quando a Evolution API está fora do ar que
        # essa informação importa mais pro admin ver.
        resposta = {
            "estado_monitorado": banco.estado_evolution_atual(conn()),
            "fila_reenvio": banco.contagem_fila_reenvio(conn()),
            "suspensao": banco.suspensao_envios(conn()),
        }
        if not integracao_evolution.configurado(app.config["CONFIG_EVOLUTION"]):
            resposta["erro_live"] = "evolution_api não configurado no config.yaml."
            return jsonify(resposta)
        try:
            resposta.update(integracao_evolution.status_instancia(app.config["CONFIG_EVOLUTION"]))
        except Exception as exc:
            resposta["erro_live"] = f"Falha ao consultar a Evolution API: {exc}"
        return jsonify(resposta)

    @app.post("/api/whatsapp/pausar")
    @requer_auth(niveis=("admin",))
    @exige_mesma_origem
    def api_whatsapp_pausar():
        """Pausa manual dos envios automáticos (bot + fila de reenvio) --
        sentinela PAUSA_MANUAL_ATE, só sai pelo /retomar."""
        banco.suspender_envios_automaticos(
            conn(), banco.PAUSA_MANUAL_ATE, f"pausa manual por {session.get('nome')}",
        )
        return jsonify({"ok": True, "suspensao": banco.suspensao_envios(conn())})

    @app.post("/api/whatsapp/retomar")
    @requer_auth(niveis=("admin",))
    @exige_mesma_origem
    def api_whatsapp_retomar():
        """Libera bot e fila de reenvio -- inclusive por cima de uma
        suspensão automática por 463 ainda vigente (decisão explícita do admin)."""
        banco.retomar_envios_automaticos(conn())
        return jsonify({"ok": True})

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
        evento = payload.get("event")

        if evento == "messages.update":
            # A Evolution API confirma o envio na hora (POST 2xx com um id),
            # mas a entrega de verdade só vem depois por aqui -- achado real
            # (26-27/08): erro 463 (trava "reach-out" do WhatsApp, ver
            # banco.SUSPENSAO_463_HORAS_PADRAO), a mensagem "sai" mas o
            # WhatsApp reporta status=ERROR segundos depois. É falha
            # definitiva: NÃO reenviar (0/14 reenvios funcionaram e cada um
            # renova a trava) -- marca FALHOU, arma o disjuntor e avisa.
            dado_update = payload.get("data") or {}
            if dado_update.get("status") == "ERROR":
                falha = banco.marcar_entrega_falhou_definitivo(conn(), dado_update.get("keyId"))
                if falha:
                    banco.suspender_envios_automaticos(
                        conn(), banco.prazo_em_horas(app.config["SUSPENSAO_463_HORAS"]),
                        f"falha de entrega (463) pra {falha['telefone_e164']}",
                    )
                    suspensao = banco.suspensao_envios(conn()) or {}
                    logger.warning(
                        f"Entrega recusada pelo WhatsApp (keyId={dado_update.get('keyId')}, "
                        f"{falha['telefone_e164']}) -- marcada FALHOU; envios automáticos suspensos "
                        f"até {suspensao.get('ate')}.",
                    )
                    # E-mail em thread pra não segurar a resposta do webhook
                    # (SMTP pode levar até 30s; a Evolution reenviaria o evento).
                    threading.Thread(
                        target=alertas.avisar_mensagem_nao_entregue,
                        args=(app.config["CONFIG_COMPLETA"], falha["protocolo"], falha["telefone_e164"],
                              falha["corpo"], "o WhatsApp recusou a entrega (erro 463, trava de abordagem)"),
                        kwargs={"suspenso_ate": suspensao.get("ate")}, daemon=True,
                    ).start()
            return jsonify({"ok": True})

        if evento != "messages.upsert":
            return jsonify({"ok": True})  # outros eventos (connection.update etc.) -- nada a fazer ainda

        dado = payload.get("data") or {}
        chave = dado.get("key") or {}
        remote_jid = chave.get("remoteJid", "")
        # Grupo (@g.us) não é um contato individual -- achado real (27/08):
        # o bot disparou o menu dentro de um grupo (JID tratado como se fosse
        # telefone) e uma menção com dígito foi lida como "opção 1" do menu.
        # Enviar pro "número" de um grupo também sempre falha (400 Bad
        # Request) -- não é um alvo válido pro campo que a Evolution espera.
        eh_grupo = remote_jid.endswith("@g.us")
        telefone = remote_jid.split("@")[0] if "@" in remote_jid else remote_jid
        if not telefone:
            return jsonify({"ok": True})
        telefone_e164 = f"+{telefone}" if not telefone.startswith("+") else telefone

        texto = _descrever_conteudo_mensagem(dado.get("message") or {})
        if texto is None:
            # Evento sem conteúdo reconhecível (reação, recibo, distribuição
            # de chave etc.) -- nada pra mostrar na Inbox, não vale abrir/
            # tocar uma conversa por causa disso.
            return jsonify({"ok": True})
        de_mim_mesmo = bool(chave.get("fromMe"))
        # pushName é o nome de quem MANDOU essa mensagem -- numa mensagem
        # fromMe:true (mandada direto do celular vinculado, fora da UI),
        # quem mandou somos NÓS, então pushName vem com o nosso próprio
        # nome de perfil ("Freshlog"), não o do cliente. Achado real
        # (27/08): isso gravava "Freshlog" como nome de vários contatos,
        # permanentemente (só atualiza se o nome ainda estiver vazio) --
        # só usar pushName pra nomear o contato em mensagem genuinamente
        # recebida do cliente.
        nome_push = dado.get("pushName") if not de_mim_mesmo else None

        contato = banco.buscar_ou_criar_contato(conn(), telefone_e164, nome_push)
        conversa = banco.conversa_aberta_do_contato(conn(), contato["id"])
        conversa_eh_nova = conversa is None
        if conversa_eh_nova:
            conversa = banco.abrir_conversa(conn(), contato["id"])

        # fromMe:true = mensagem mandada direto do celular vinculado (não
        # pela UI) -- ainda assim tem que aparecer na thread, senão o
        # histórico fica incompleto (achado do design, ver plano).
        direcao = "OUT" if de_mim_mesmo else "IN"
        achado_midia = _analisar_midia(dado.get("message") or {})
        midia_info = None
        if achado_midia:
            # Tem que ser agora, com o `dado` completo em mãos -- ver
            # docstring de integracao_evolution.baixar_midia. Falha aqui
            # nunca derruba o webhook, só cai de volta pro rótulo de texto.
            midia_info = _baixar_e_salvar_midia(
                app.config["CONFIG_EVOLUTION"], dado, achado_midia, chave.get("id"),
            )
        banco.registrar_mensagem(
            conn(), conversa["id"], direcao, texto,
            atendente_id=None, evolution_message_id=chave.get("id"), midia=midia_info,
        )

        # Bot de triagem: só entra na primeira mensagem de conversa nova e no
        # follow-up que responde o menu -- depois disso (time classificado)
        # nunca mais interfere nessa conversa. Nunca em grupo (eh_grupo) e
        # nunca com o disjuntor armado (suspensao_envios): sem poder mandar o
        # menu, a conversa só cai na fila humana sem classificação.
        if direcao == "IN" and not eh_grupo and not banco.suspensao_envios(conn()):
            cfg_evolution = app.config["CONFIG_EVOLUTION"]
            if conversa_eh_nova:
                banco.marcar_bot_aguardando_menu(conn(), conversa["id"], True)
                _bot_enviar(cfg_evolution, conn(), conversa["id"], telefone_e164, _MENU_TRIAGEM_TEXTO)
            elif conversa["time"] is None and conversa["bot_aguardando_menu"]:
                opcao = _extrair_opcao_menu(texto)
                if opcao:
                    dados_opcao = _OPCOES_TRIAGEM[opcao]
                    banco.classificar_conversa_pelo_bot(
                        conn(), conversa["id"], dados_opcao["time"], dados_opcao["motivo"],
                    )
                    _bot_enviar(cfg_evolution, conn(), conversa["id"], telefone_e164, dados_opcao["resposta"])
                else:
                    tentativas = banco.incrementar_tentativas_bot(conn(), conversa["id"])
                    if tentativas > _TENTATIVAS_MENU_MAX:
                        banco.desistir_bot(conn(), conversa["id"])
                        _bot_enviar(cfg_evolution, conn(), conversa["id"], telefone_e164, _MENU_TRIAGEM_DESISTENCIA)
                    else:
                        _bot_enviar(cfg_evolution, conn(), conversa["id"], telefone_e164, _MENU_TRIAGEM_REPETIR)

        return jsonify({"ok": True})

    @app.get("/midia/<int:mensagem_id>")
    @requer_auth
    def midia(mensagem_id):
        linha = conn().execute(
            "SELECT midia_caminho, midia_mime, midia_nome_original FROM mensagens WHERE id = ?",
            (mensagem_id,),
        ).fetchone()
        if not linha or not linha["midia_caminho"]:
            abort(404)
        caminho = (_RAIZ / linha["midia_caminho"]).resolve()
        # Defesa em profundidade: midia_caminho é sempre construído por
        # _baixar_e_salvar_midia (nunca vem do usuário), mas confirmar mesmo
        # assim que continua dentro da pasta esperada antes de servir.
        if not caminho.is_relative_to(_PASTA_MIDIA.resolve()) or not caminho.is_file():
            abort(404)
        return send_file(caminho, mimetype=linha["midia_mime"] or None,
                          download_name=linha["midia_nome_original"] or caminho.name)

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
