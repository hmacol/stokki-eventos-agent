# -*- coding: utf-8 -*-
"""
nucleo/api_motorista.py

API JSON do app de motoristas (Fase B, seção 3.3 do
DOC_EXECUCAO_CLAUDE_APP_MOTORISTAS.md). Casca HTTP fina sobre
nucleo/operacao.py + nucleo/auth_motorista.py + nucleo/financeiro.py.

Autenticação: `Authorization: Bearer <token de acesso>` (POST /api/login
com CPF + PIN). Toda resposta é JSON; erro = {"erro": "..."} com o status
HTTP certo (400 regra, 401 auth, 403, 404, 409 conflito, 429 bloqueio).

COMO USAR (desenvolvimento, porta livre -- nunca a 8070/8071 do painel):
    py -3.11 nucleo/api_motorista.py --porta 8073
COMO USAR (produção, VPS -- infra/motorista-api.service):
    waitress-serve --host=127.0.0.1 --port=8073 --call nucleo.api_motorista:criar_app
Atrás do Caddy em app.freshhub.com.br/motorista (infra/Caddyfile-motorista),
mesmo padrão handle_path + X-Forwarded-Prefix + ProxyFix do painel.

config.yaml:
    api_motorista:
      secret_key: "<aleatório longo>"   # assina os tokens; trocar desloga todo mundo
      porta: 8073
      pasta_comprovantes: dados/comprovantes   # opcional
Fotos: gravadas em disco (pasta acima) e, best-effort, no bucket GCS já
usado pelos documentos (documentos_pedido/storage_gcs.py, mesmo caminho
pedidos/{codigo}/{tipo}/...).
"""
import hashlib
import logging
import re
import secrets
import sys
import threading
import time
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))

from flask import Flask, g, jsonify, render_template, request
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

from nucleo import (atualizacao_motorista as atualizacao, auth_motorista as auth, banco,
                    cadastro_motorista as cadastro, financeiro, operacao, ponte_vuupt, trajeto_rota, validacao_fotos)
from nucleo.marketplace_remoto import ClienteMarketplace
from nucleo.auth_motorista import AutenticacaoInvalida
from nucleo.operacao import OperacaoInvalida
from regras import tarifa_motorista

logger = logging.getLogger("nucleo.api_motorista")

EXTENSOES_FOTO = {"jpg", "jpeg", "png", "webp", "heic"}
TAMANHO_MAX_FOTO = 15 * 1024 * 1024

# Aviso de APK novo no app (/api/app-versao). None = sem aviso; liga-se trocando por commit.
APK_VERSAO_ATUAL: str | None = None
APK_LINK = "https://app.freshhub.com.br/motorista/app.apk"


def _carregar_config() -> dict:
    import yaml
    caminho = _RAIZ / "config.yaml"
    if not caminho.exists():
        return {}
    with open(caminho, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _sem_token(caminho: str) -> str:
    """Token do link de atualização não vai pro journal."""
    return re.sub(r"(/(?:atualizar|api/atualizacao)/)[^/?]+", r"\g<1>***", caminho)


def _data(valor: str | None, padrao: date) -> date:
    if not valor:
        return padrao
    try:
        return date.fromisoformat(valor)
    except ValueError:
        raise OperacaoInvalida(f"Data inválida: {valor!r} (use YYYY-MM-DD).")


def criar_app(config: dict | None = None) -> Flask:
    config = config if config is not None else _carregar_config()
    cfg = config.get("api_motorista", {}) or {}

    app = Flask(__name__)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_prefix=1, x_proto=1, x_for=1, x_host=1)
    app.config["MAX_CONTENT_LENGTH"] = TAMANHO_MAX_FOTO
    app.config["JSON_AS_ASCII"] = False
    app.config["CONFIG_PROJETO"] = config

    secret = cfg.get("secret_key")
    if not secret:
        # Sem segredo configurado os tokens morrem a cada restart -- serve
        # pra desenvolvimento, nunca pra produção (aviso alto no log).
        secret = secrets.token_hex(32)
        logger.warning("api_motorista.secret_key ausente no config.yaml -- usando segredo temporário (tokens não sobrevivem a restart).")
    app.config["SECRET_TOKENS"] = secret
    app.config["PASTA_COMPROVANTES"] = Path(cfg.get("pasta_comprovantes") or (_RAIZ / "dados" / "comprovantes"))
    app.config["GCS_ATIVO"] = bool(cfg.get("gcs_ativo", True))
    # Validação automática de nitidez/NF das fotos (Hugo, 12/09):
    # DESLIGADA por padrão -- liga em api_motorista.validacao_fotos.ativo.
    app.config["VALIDACAO_FOTOS"] = validacao_fotos.config_validacao(config)
    # Ponte núcleo -> Vuupt (12/11): rota da Vuupt operável pelo nosso app,
    # DESLIGADA por padrão -- liga em api_motorista.ponte_vuupt (ver o módulo).
    ponte_vuupt.definir_config(config)
    _ponte = ponte_vuupt.config_ponte(config)
    if _ponte["ativo"]:
        logger.info("Ponte Vuupt LIGADA (todos=%s, agentes=%s).", _ponte["todos"], sorted(_ponte["agentes"]))
    if app.config["VALIDACAO_FOTOS"]["ativo"]:
        logger.info("Validação automática de fotos LIGADA (modelo %s, nitidez mínima %s).",
                    app.config["VALIDACAO_FOTOS"]["modelo"], app.config["VALIDACAO_FOTOS"]["nitidez_minima"])

    # ── conexão por request ──────────────────────────────────────────────────
    def conn():
        if "conn" not in g:
            g.conn = banco.conectar()
        return g.conn

    @app.teardown_appcontext
    def _fechar(_exc):
        c = g.pop("conn", None)
        if c is not None:
            c.close()

    # ── log de requisições (11/09: pra enxergar o que o celular manda) ───────
    # waitress não loga requisição; sem isso o piloto fica cego (o teste do
    # Hugo mandou eventos que nunca chegaram e não havia como saber se
    # bateram na API). Uma linha por request, exceto /api/saude.
    logger.setLevel(logging.INFO)

    @app.before_request
    def _marcar_inicio():
        g.inicio = time.monotonic()

    @app.after_request
    def _logar(resp):
        if request.path != "/api/saude":
            ms = int((time.monotonic() - getattr(g, "inicio", time.monotonic())) * 1000)
            agente = (getattr(g, "motorista", None) or {}).get("agent_id")
            # chave_envio do cadastro pelo link não vai pro journal
            caminho = _sem_token(re.sub(r"(chave_envio=)[^&]*", r"\g<1>***", request.full_path.rstrip("?")))
            logger.info("%s %s -> %s %dms agent=%s ip=%s len=%s",
                        request.method, caminho, resp.status_code, ms, agente,
                        request.remote_addr, request.content_length)
        return resp

    # ── erros ────────────────────────────────────────────────────────────────
    @app.errorhandler(OperacaoInvalida)
    def _erro_operacao(e):
        logger.info("Regra recusou %s %s: %s", request.method, _sem_token(request.path), e.mensagem)
        return jsonify({"erro": e.mensagem}), e.codigo

    @app.errorhandler(AutenticacaoInvalida)
    def _erro_auth(e):
        logger.info("Auth recusou %s %s: %s", request.method, _sem_token(request.path), e.mensagem)
        return jsonify({"erro": e.mensagem}), e.codigo

    @app.errorhandler(cadastro.CadastroInvalido)
    def _erro_cadastro(e):
        logger.info("Cadastro recusou %s %s: %s", request.method, _sem_token(request.path), e.mensagem)
        return jsonify({"erro": e.mensagem}), e.codigo

    @app.errorhandler(atualizacao.LinkInvalido)
    def _erro_link(e):
        logger.info("Link de atualizacao recusou %s %s: %s", request.method, _sem_token(request.path), e.mensagem)
        return jsonify({"erro": e.mensagem}), e.codigo

    @app.errorhandler(404)
    def _404(_e):
        return jsonify({"erro": "Rota não encontrada."}), 404

    @app.errorhandler(413)
    def _413(_e):
        return jsonify({"erro": "Arquivo grande demais (máx. 15 MB)."}), 413

    @app.errorhandler(Exception)
    def _erro_geral(e):
        logger.exception("Erro não tratado na API do motorista")
        return jsonify({"erro": "Erro interno."}), 500

    # ── auth ─────────────────────────────────────────────────────────────────
    def requer_motorista(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            cabecalho = request.headers.get("Authorization", "")
            if not cabecalho.startswith("Bearer "):
                raise AutenticacaoInvalida("Faça login.")
            g.motorista = auth.motorista_do_token_acesso(app.config["SECRET_TOKENS"], cabecalho[7:].strip(), conn())
            # PIN provisório (revisão 08/10): até trocar, o motorista só vê o
            # próprio cadastro e troca o PIN -- nada de rota, extrato ou chat.
            if g.motorista.get("trocar_pin") and request.endpoint not in ("eu", "trocar_pin"):
                raise AutenticacaoInvalida("Troque o PIN provisório antes de continuar.", 403)
            return f(*args, **kwargs)
        return wrapper

    def corpo() -> dict:
        dados = request.get_json(silent=True)
        return dados if isinstance(dados, dict) else {}

    def agent_id() -> int | None:
        return g.motorista.get("agent_id")

    # ── endpoints ────────────────────────────────────────────────────────────
    @app.get("/api/saude")
    def saude():
        conn().execute("SELECT 1")
        return jsonify({"ok": True, "hora": banco.agora()})

    @app.post("/api/login")
    def login():
        d = corpo()
        m = auth.autenticar(conn(), d.get("cpf", ""), d.get("pin", ""))
        return jsonify({**auth.emitir_tokens(app.config["SECRET_TOKENS"], m), "motorista": auth.publico(m)})

    @app.post("/api/refresh")
    def refresh():
        d = corpo()
        tokens = auth.renovar(app.config["SECRET_TOKENS"], d.get("refresh", ""), conn())
        return jsonify(tokens)

    @app.get("/api/eu")
    @requer_motorista
    def eu():
        return jsonify(auth.publico(g.motorista))

    @app.post("/api/trocar-pin")
    @requer_motorista
    def trocar_pin():
        # Obrigatória quando o cadastro marcou trocar_pin (PIN inicial igual
        # pra todos, 08/10); o motorista também pode trocar quando quiser.
        d = corpo()
        m = auth.trocar_pin(conn(), g.motorista["cpf"], str(d.get("pin_atual") or ""), str(d.get("pin_novo") or ""))
        return jsonify({**auth.emitir_tokens(app.config["SECRET_TOKENS"], m), "motorista": auth.publico(m)})

    # ── Cadastro pelo app (Hugo, 08/10): meus dados + auto-cadastro ──────────
    def _url_painel() -> str:
        cfg = app.config["CONFIG_PROJETO"].get("portal_cliente", {}).get("chamados", {})
        return (cfg.get("url_painel") or "https://app.freshhub.com.br/painel").rstrip("/")

    def _avisar_whatsapp(tipo: str, texto: str, assinatura: str | None = None) -> None:
        """Grupo ALERTAS FRESH (whatsapp_notificacoes); nunca derruba a rota."""
        try:
            import notificar_whatsapp
            notificar_whatsapp.despachar(app.config["CONFIG_PROJETO"], "cadastro_motorista", tipo, texto, assinatura)
        except Exception as e:
            logger.warning("Aviso de cadastro no WhatsApp falhou: %s", e)

    @app.put("/api/eu")
    @requer_motorista
    def editar_eu():
        """Telefone, e-mail, chave PIX e placa valem na hora (decisão do
        Hugo); telefone/e-mail/placa vão também pra tabela bd_motoristas (a placa só se o CPF tem uma linha)."""
        m, mudancas = cadastro.atualizar_meus_dados(conn(), g.motorista["cpf"], corpo())
        if mudancas:
            na_planilha = {x["campo"]: (x["para"] or "") for x in mudancas if x["campo"] in ("telefone", "email", "placa")}
            if na_planilha:
                try:
                    from regras.cadastro_motoristas import atualizar_contato_planilha
                    atualizar_contato_planilha(app.config["CONFIG_PROJETO"], m["cpf"], **na_planilha)
                except Exception as e:
                    logger.warning("Planilha de motoristas não atualizada pra %s: %s", m["cpf"], e)
            import notificar_whatsapp
            _avisar_whatsapp("alteracao_cadastro", notificar_whatsapp.texto_alteracao_cadastro(m["nome"], mudancas))
        return jsonify({"motorista": auth.publico(m), "mudancas": mudancas})

    # Rota pública do app: freio simples em memória contra enxurrada (5 pedidos por
    # IP a cada hora, 40 por dia no total). O IP real vem do ProxyFix (x_for).
    # O cadastro pelo link tem o próprio freio, contado no banco.
    _pedidos_cadastro: dict[str, list[float]] = {}
    LIMITE_CADASTRO_IP, LIMITE_CADASTRO_DIA = 5, 40

    def _freio_cadastro(ip: str) -> None:
        agora = time.time()
        for chave in list(_pedidos_cadastro):
            _pedidos_cadastro[chave] = [t for t in _pedidos_cadastro[chave] if agora - t < 86400]
            if not _pedidos_cadastro[chave]:
                del _pedidos_cadastro[chave]
        por_ip = [t for t in _pedidos_cadastro.get(ip, []) if agora - t < 3600]
        total_dia = sum(len(v) for v in _pedidos_cadastro.values())
        if len(por_ip) >= LIMITE_CADASTRO_IP or total_dia >= LIMITE_CADASTRO_DIA:
            raise cadastro.CadastroInvalido("Muitos pedidos de cadastro. Tente de novo mais tarde.", 429)

    @app.post("/api/cadastro")
    def cadastro_novo():
        """Sem login: motorista novo pede cadastro. Fica PENDENTE até o
        Hugo aprovar no painel; os documentos sobem na rota abaixo. Só o
        cadastro CRIADO conta no freio (erro de digitação não gasta cota)."""
        ip = request.remote_addr or "?"
        _freio_cadastro(ip)
        r = cadastro.criar_cadastro(conn(), corpo())
        _pedidos_cadastro.setdefault(ip, []).append(time.time())
        return jsonify(r), 201

    # Freio por IP das rotas públicas que usam a chave_envio (documentos,
    # situação, concluir): 60 por IP por hora em cada grupo, em memória.
    _pedidos_por_grupo: dict[tuple[str, str], list[float]] = {}
    LIMITE_ROTA_IP_HORA = 60

    def _freio(grupo: str) -> None:
        agora, ip = time.time(), request.remote_addr or "?"
        for chave in list(_pedidos_por_grupo):
            _pedidos_por_grupo[chave] = [t for t in _pedidos_por_grupo[chave] if agora - t < 3600]
            if not _pedidos_por_grupo[chave]:
                del _pedidos_por_grupo[chave]
        lista = _pedidos_por_grupo.setdefault((grupo, ip), [])
        if len(lista) >= LIMITE_ROTA_IP_HORA:
            raise cadastro.CadastroInvalido("Muitos pedidos. Tente de novo mais tarde.", 429)
        lista.append(agora)

    @app.post("/api/cadastro/<int:cadastro_id>/documentos")
    def cadastro_documento(cadastro_id):
        _freio("documentos")
        arq = request.files.get("arquivo")
        if arq is None or not arq.filename:
            raise cadastro.CadastroInvalido("Mande a foto no campo 'arquivo'.")
        _exigir_link_vivo_do_rascunho(cadastro_id)
        c = cadastro.guardar_documento(conn(), cadastro_id, request.form.get("chave_envio"), request.form.get("tipo"),
                                       arq.filename, arq.read())
        # Cadastro do link: quem avisa o grupo é o processar, depois do Enviar.
        # Atualização de quem já é motorista nunca avisa o grupo de cadastro novo.
        if (c["completo"] and not c.get("avisado_em")
                and c.get("origem") not in (cadastro.ORIGEM_LINK, atualizacao.ORIGEM_ATUALIZACAO)):
            import notificar_whatsapp
            _avisar_whatsapp("cadastro_novo", notificar_whatsapp.texto_cadastro_motorista(c, f"{_url_painel()}/motoristas"),
                             assinatura=f"cadastro:{cadastro_id}")
            cadastro.marcar_avisado(conn(), cadastro_id)
            c = cadastro.buscar_cadastro(conn(), cadastro_id)
        return jsonify(c)

    # ── Cadastro pelo link (WhatsApp -> navegador, sem app) ──────────────────
    # Atrás de cadastro_motorista_link.ativo; /api/cadastro e o envio de
    # documentos acima seguem valendo com a chave desligada.
    MSG_LINK_OFF = "Cadastro indisponível no momento."

    def _link_ativo() -> bool:
        return bool((app.config["CONFIG_PROJETO"].get("cadastro_motorista_link") or {}).get("ativo"))

    def _exigir_link() -> None:
        if not _link_ativo():
            raise cadastro.CadastroInvalido(MSG_LINK_OFF, 503)

    @app.get("/cadastro")
    def cadastro_pagina():
        if not _link_ativo():
            return ("<!doctype html><html lang=pt-BR><meta charset=utf-8>"
                    "<meta name=viewport content='width=device-width,initial-scale=1'><title>Fresh Log</title>"
                    "<body style='font-family:sans-serif;padding:32px 16px;text-align:center'>"
                    f"<h1 style='font-size:20px'>{MSG_LINK_OFF}</h1></body></html>"), 503
        from regras.tipo_veiculo import TIPOS_VEICULO
        return render_template("cadastro_motorista.html", zonas=cadastro.ZONAS, dias=cadastro.DIAS, tipos=TIPOS_VEICULO)

    @app.get("/como-funciona")
    def como_funciona():
        """Página informativa pública (operação, pagamento, app, obrigatoriedades)
        pra quem vai se cadastrar. Não depende do link de cadastro estar ligado."""
        from regras.tarifa_motorista import TARIFAS_PADRAO
        return render_template("como_funciona_motorista.html", tarifas=list(TARIFAS_PADRAO.values()))

    @app.post("/api/cadastro-link")
    def cadastro_link_novo():
        """Freio contado no banco (5/IP/hora, 40/24 h; restart não zera), à
        parte do /api/cadastro do app."""
        _exigir_link()
        r = cadastro.criar_cadastro_link(conn(), corpo(), ip=request.remote_addr or "?")
        return jsonify(r), 201

    @app.get("/api/cadastro/<int:cadastro_id>/situacao")
    def cadastro_link_situacao(cadastro_id):
        _exigir_link()
        _freio("situacao")
        return jsonify(cadastro.situacao_link(conn(), cadastro_id, request.args.get("chave_envio")))

    @app.post("/api/cadastro/<int:cadastro_id>/concluir")
    def cadastro_link_concluir(cadastro_id):
        _exigir_link()
        _freio("concluir")
        chave = corpo().get("chave_envio")
        _, gravou = cadastro.registrar_envio(conn(), cadastro_id, chave)
        primeiro_nome = cadastro.situacao_link(conn(), cadastro_id, chave)["primeiro_nome"]
        # Só a chamada que registrou o envio processa; repetir só responde (quem retenta é o timer).
        if gravou:
            # Vuupt + intervalo do WhatsApp levam 20-40 s: o motorista não espera.
            # A thread guardada em app.config é o gancho dos testes (join).
            t = threading.Thread(target=_processar_link_em_fundo, args=(cadastro_id,), daemon=True)
            app.config["THREAD_CADASTRO_LINK"] = t
            t.start()
        return jsonify({"ok": True, "primeiro_nome": primeiro_nome})

    def _processar_link_em_fundo(cadastro_id: int) -> None:
        from nucleo import cadastro_motorista_link
        if not cadastro_motorista_link.link_ativo(app.config["CONFIG_PROJETO"]):
            logger.info("cadastro_motorista_link.ativo desligado: cadastro #%s fica pro timer.", cadastro_id)
            return
        c = banco.conectar()
        try:
            cadastro_motorista_link.processar(c, app.config["CONFIG_PROJETO"], cadastro_id)
        except Exception:
            # O cadastro já está salvo e enviado; o timer de retentativa termina o serviço.
            logger.exception("Processar do cadastro #%s falhou (o timer retenta)", cadastro_id)
        finally:
            c.close()

    # ── Atualização cadastral de quem já é motorista (link pessoal) ──────────
    # Atrás de atualizacao_motoristas.ativo, independente do cadastro novo.
    def _atualizacao_ativa() -> bool:
        # mesmo critério do painel (_pagina_atualizacao_ligada): "false" entre aspas não liga
        from regras.preferencias_motoristas import _parse_bool
        return _parse_bool((app.config["CONFIG_PROJETO"].get("atualizacao_motoristas") or {}).get("ativo"))

    def _exigir_atualizacao() -> None:
        if not _atualizacao_ativa():
            raise cadastro.CadastroInvalido(MSG_LINK_OFF, 503)

    def _pagina_simples(texto: str, codigo: int):
        return ("<!doctype html><html lang=pt-BR><meta charset=utf-8>"
                "<meta name=viewport content='width=device-width,initial-scale=1'><title>Fresh Log</title>"
                "<body style='font-family:sans-serif;padding:32px 16px;text-align:center'>"
                f"<h1 style='font-size:20px'>{texto}</h1></body></html>"), codigo

    def _exigir_link_vivo_do_rascunho(cadastro_id: int) -> None:
        """Upload de documento de rascunho de atualização: o link dono do rascunho
        tem de estar ligado, não cancelado, não vencido e não concluído. Outras
        origens passam direto (a chave_envio e o status ficam com o guardar_documento)."""
        c = conn()
        row = c.execute("SELECT origem FROM motoristas_cadastros WHERE id = ?", (cadastro_id,)).fetchone()
        if not row or row["origem"] != atualizacao.ORIGEM_ATUALIZACAO:
            return
        _exigir_atualizacao()
        atualizacao.exigir_link_vivo_do_rascunho(c, cadastro_id)

    @app.get("/atualizar/<token>")
    def atualizacao_pagina(token):
        if not _atualizacao_ativa():
            return _pagina_simples(MSG_LINK_OFF, 503)
        try:
            atualizacao._conferir_aberto(atualizacao._link(conn(), token), atualizacao._ts(None))
        except atualizacao.LinkInvalido:
            return _pagina_simples(atualizacao.MSG_LINK_INVALIDO, 404)   # sem dizer o motivo
        from regras.tipo_veiculo import TIPOS_VEICULO
        return render_template("cadastro_motorista.html", modo="atualizar", token=token, tipos=TIPOS_VEICULO,
                               zonas=[], dias=[])

    @app.post("/api/atualizacao/<token>/abrir")
    def atualizacao_abrir(token):
        _exigir_atualizacao()
        _freio("atualizacao")
        return jsonify(atualizacao.abrir(conn(), token, corpo().get("cpf")))

    @app.post("/api/atualizacao/<token>/dados")
    def atualizacao_dados(token):
        _exigir_atualizacao()
        _freio("atualizacao")
        d = corpo()
        return jsonify(atualizacao.gravar(conn(), token, d.get("chave_envio"), d))

    @app.post("/api/atualizacao/<token>/concluir")
    def atualizacao_concluir(token):
        _exigir_atualizacao()
        _freio("atualizacao")
        return jsonify(atualizacao.concluir(conn(), app.config["CONFIG_PROJETO"], token, corpo().get("chave_envio")))

    @app.post("/api/push-token")
    @requer_motorista
    def push_token():
        token = (corpo().get("token") or "").strip()[:300] or None
        conn().execute("UPDATE motoristas SET push_token = ?, atualizado_em = ? WHERE cpf = ?",
                       (token, banco.agora(), g.motorista["cpf"]))
        conn().commit()
        return jsonify({"ok": True})

    @app.get("/api/checklist")
    @requer_motorista
    def checklist():
        return jsonify({**operacao.carregar_checklist(conn()),
                        "validacao_fotos": validacao_fotos.publico(app.config["VALIDACAO_FOTOS"])})

    @app.get("/api/rotas")
    @requer_motorista
    def rotas():
        hoje = date.today()
        de = _data(request.args.get("de"), hoje)
        ate = _data(request.args.get("ate"), hoje + timedelta(days=1))
        return jsonify({"rotas": operacao.listar_rotas_motorista(conn(), agent_id(), de, ate)})

    @app.get("/api/rotas/<int:rota_id>")
    @requer_motorista
    def rota(rota_id):
        r = operacao.rota_do_motorista(conn(), rota_id, agent_id())
        return jsonify(operacao.montar_rota(conn(), r))

    @app.get("/api/rotas/<int:rota_id>/trajeto")
    @requer_motorista
    def rota_trajeto(rota_id):
        r = operacao.rota_do_motorista(conn(), rota_id, agent_id())
        if not operacao.rota_liberada(r):
            # rota de dia futuro abre as 00h: nem pontos nem chamada ao Google
            return jsonify({"pontos": [], "fonte": trajeto_rota.FONTE_LINHA_RETA, "assinatura": "",
                            "base": [trajeto_rota.COORD_CD[0], trajeto_rota.COORD_CD[1]]})
        chave =(app.config["CONFIG_PROJETO"].get("google_maps") or {}).get("api_key")
        return jsonify(trajeto_rota.trajeto(conn(), rota_id, chave))

    @app.get("/api/app-versao")
    def app_versao():
        return jsonify({"versao_atual": APK_VERSAO_ATUAL, "link": APK_LINK})

    @app.post("/api/rotas/<int:rota_id>/aceitar")
    @requer_motorista
    def aceitar(rota_id):
        d = corpo()
        return jsonify(operacao.aceitar_rota(conn(), rota_id, agent_id(), d.get("ocorrido_em"),
                                             d.get("latitude"), d.get("longitude"), d.get("uuid")))

    @app.post("/api/rotas/<int:rota_id>/recusar")
    @requer_motorista
    def recusar(rota_id):
        d = corpo()
        return jsonify(operacao.recusar_rota(conn(), rota_id, agent_id(), d.get("motivo"), d.get("ocorrido_em"), d.get("uuid")))

    @app.post("/api/rotas/<int:rota_id>/iniciar")
    @requer_motorista
    def iniciar(rota_id):
        d = corpo()
        return jsonify(operacao.iniciar_rota(conn(), rota_id, agent_id(), d.get("ocorrido_em"),
                                             d.get("latitude"), d.get("longitude"), d.get("uuid")))

    @app.post("/api/rotas/<int:rota_id>/finalizar")
    @requer_motorista
    def finalizar(rota_id):
        d = corpo()
        return jsonify(operacao.finalizar_rota(conn(), rota_id, agent_id(), d.get("km_informado"), d.get("pedagio"),
                                               d.get("observacoes"), d.get("ocorrido_em"), d.get("uuid")))

    @app.post("/api/paradas/<int:parada_id>/eventos")
    @requer_motorista
    def evento_parada(parada_id):
        resultado = operacao.registrar_evento_parada(conn(), parada_id, agent_id(), corpo())
        return jsonify(resultado), (200 if resultado["ja_registrado"] else 201)

    def _extensao_foto(arquivo) -> str:
        ext = (arquivo.filename.rsplit(".", 1)[-1] if "." in arquivo.filename else "jpg").lower()
        if ext not in EXTENSOES_FOTO:
            raise OperacaoInvalida(f"Extensão não aceita: .{ext}")
        return ext

    def _guardar_foto(conteudo: bytes, codigo_pasta: str, tipo: str, uuid: str, ext: str) -> tuple[str, str | None]:
        """Grava em disco (pasta_comprovantes/<codigo>/) e, best-effort, no
        GCS. Devolve (caminho relativo em disco, caminho GCS ou None)."""
        sha = hashlib.sha256(conteudo).hexdigest()
        pasta = app.config["PASTA_COMPROVANTES"] / secure_filename(codigo_pasta)
        pasta.mkdir(parents=True, exist_ok=True)
        nome = f"{tipo.lower()}_{secure_filename(uuid or sha[:16])}.{ext}"
        caminho = pasta / nome
        caminho.write_bytes(conteudo)

        caminho_gcs = None
        if app.config["GCS_ATIVO"] and app.config["CONFIG_PROJETO"].get("gcs"):
            try:
                from documentos_pedido.storage_gcs import enviar_documento
                caminho_gcs = enviar_documento(app.config["CONFIG_PROJETO"], caminho, codigo_pasta, tipo.capitalize())
            except Exception as e:
                logger.warning(f"Foto {nome} salva em disco, mas não subiu pro GCS: {e}")
        relativo = str(caminho.relative_to(_RAIZ)) if _RAIZ in caminho.parents else str(caminho)
        return relativo, caminho_gcs

    def _validar_conteudo(tipo: str, conteudo: bytes, sha: str, *, parada=None, rota_id=None,
                          nf=None, valor=None, no_ato=True) -> dict | None:
        """Roda (ou reaproveita) a validação da foto. Devolve o dict do
        resultado ou None quando não há nada a registrar.

        Reaproveita por sha256: a foto conferida no ato (POST /api/fotos/validar,
        com o motorista ainda no cliente) chega depois pela fila offline e
        NÃO é cobrada de novo no modelo."""
        cfg = app.config["VALIDACAO_FOTOS"]
        nfs = validacao_fotos.nfs_do_pedido(conn(), parada["codigo"]) if parada is not None else []
        codigos = validacao_fotos.codigos_base(parada["codigo"]) if parada is not None else []
        ja = validacao_fotos.buscar_validacao(conn(), sha)
        if ja:
            salvo = {"resultado": ja["resultado"], "motivo": ja["motivo"], "nitidez": ja["nitidez"],
                     "modelo": ja["modelo"], **ja.get("dados", {})}
            # A mesma foto pode servir a outra parada (canhoteira com
            # várias entregas): refaz a decisão com a leitura guardada.
            novo = validacao_fotos.redecidir_canhoto(cfg, salvo, nfs_esperadas=nfs, nf_alvo=nf, codigos_pedido=codigos) \
                if tipo == validacao_fotos.TIPO_CANHOTO else None
            return {**(novo or salvo), "no_ato": bool(ja["no_ato"])}
        if not cfg["ativo"]:
            return None
        r = validacao_fotos.validar_foto(cfg, tipo, conteudo, nfs_esperadas=nfs, nf_alvo=nf, valor_informado=valor,
                                         codigos_pedido=codigos)
        validacao_fotos.registrar_validacao(
            conn(), sha, tipo, r, agent_id(),
            f"parada:{parada['id']}" if parada is not None else (f"rota:{rota_id}" if rota_id else None),
            no_ato=no_ato)
        conn().commit()
        return {**r, "no_ato": no_ato}

    @app.post("/api/fotos/validar")
    @requer_motorista
    def validar_foto_endpoint():
        """Confere a foto ANTES do envio definitivo, com o motorista ainda
        no cliente (Hugo, 12/09). Multipart: arquivo, tipo (CANHOTO |
        PEDAGIO), parada_id (canhoto) ou rota_id (pedágio), nf (a NF que
        ESTA foto deve mostrar -- um canhoto por NF), valor (pedágio),
        tentativa (1, 2, ...).

        Nunca grava a foto: só devolve o veredito e guarda o resultado
        por sha256 pra a fila não pagar o modelo de novo. Com a validação
        desligada devolve NAO_VERIFICADO e pode_seguir=true -- o app segue
        exatamente como hoje."""
        cfg = app.config["VALIDACAO_FOTOS"]
        arquivo = request.files.get("arquivo")
        if arquivo is None or not arquivo.filename:
            raise OperacaoInvalida("Envie o arquivo no campo 'arquivo' (multipart).")
        tipo = (request.form.get("tipo") or validacao_fotos.TIPO_CANHOTO).upper()
        _extensao_foto(arquivo)
        try:
            tentativa = int(request.form.get("tentativa") or 1)
        except ValueError:
            tentativa = 1

        parada = None
        rota_id = None
        if request.form.get("parada_id"):
            parada = conn().execute("SELECT id, codigo, rota_id FROM nucleo_paradas WHERE id = ?",
                                    (request.form["parada_id"],)).fetchone()
            if not parada:
                raise OperacaoInvalida("Parada não encontrada.", 404)
            operacao.rota_do_motorista(conn(), parada["rota_id"], agent_id())
        elif request.form.get("rota_id"):
            rota_id = operacao.rota_do_motorista(conn(), int(request.form["rota_id"]), agent_id())["id"]

        conteudo = arquivo.read()
        sha = hashlib.sha256(conteudo).hexdigest()
        valor = request.form.get("valor")
        try:
            valor_f = float(str(valor).replace(",", ".")) if valor else None
        except ValueError:
            valor_f = None
        r = _validar_conteudo(tipo, conteudo, sha, parada=parada, rota_id=rota_id,
                              nf=request.form.get("nf"), valor=valor_f, no_ato=True)
        if r is None:
            r = {"resultado": validacao_fotos.NAO_VERIFICADO, "motivo": "Validação automática desligada."}
        elif r.get("guardar_como") == validacao_fotos.TIPO_FACHADA and parada is not None:
            # Fachada/portão: não vale como canhoto, mas fica guardada
            # (Hugo, 20/11). O app recusa a foto e pede o canhoto.
            # Falha ao guardar NÃO pode virar erro HTTP: o app trata erro
            # como "sem sinal" e aceitaria a fachada como canhoto.
            uuid = f"fachada-{sha[:16]}"
            try:
                caminho, caminho_gcs = _guardar_foto(conteudo, parada["codigo"] or f"parada-{parada['id']}",
                                                     validacao_fotos.TIPO_FACHADA, uuid, _extensao_foto(arquivo))
                operacao.registrar_comprovante(conn(), parada["id"], agent_id(), validacao_fotos.TIPO_FACHADA, uuid,
                                               caminho, sha, len(conteudo), banco.agora(), caminho_gcs, validacao=r)
            except Exception as e:  # noqa: BLE001
                conn().rollback()
                logger.warning(f"Fachada da parada {parada['id']} não guardada ({type(e).__name__}: {e}); veredito segue.")
        return jsonify({**validacao_fotos.aplicar_tentativas(cfg, r, tentativa), "sha256": sha})

    @app.post("/api/paradas/<int:parada_id>/comprovantes")
    @requer_motorista
    def comprovante(parada_id):
        arquivo = request.files.get("arquivo")
        if arquivo is None or not arquivo.filename:
            raise OperacaoInvalida("Envie o arquivo no campo 'arquivo' (multipart).")
        tipo = (request.form.get("tipo") or "CANHOTO").upper()
        # `tipo` vira parte do nome do arquivo: valida antes de gravar em disco
        if tipo not in operacao.TIPOS_COMPROVANTE:
            raise OperacaoInvalida(f"Tipo de comprovante inválido: {tipo!r}.")
        uuid = (request.form.get("uuid") or "").strip()
        capturado_em = request.form.get("capturado_em") or banco.agora()
        ext = _extensao_foto(arquivo)

        # Valida posse/idempotência ANTES de gravar em disco
        ja = conn().execute("SELECT id FROM nucleo_comprovantes WHERE uuid = ?", (uuid,)).fetchone() if uuid else None
        if ja:
            return jsonify({"id": ja["id"], "ja_registrado": True})
        p = conn().execute("SELECT id, codigo, rota_id FROM nucleo_paradas WHERE id = ?", (parada_id,)).fetchone()
        if not p:
            raise OperacaoInvalida("Parada não encontrada.", 404)
        operacao.rota_do_motorista(conn(), p["rota_id"], agent_id())

        conteudo = arquivo.read()
        sha = hashlib.sha256(conteudo).hexdigest()
        codigo = p["codigo"] or f"parada-{parada_id}"
        # Já validada no ato? reaproveita. Senão (motorista sem sinal na
        # hora), valida agora marcando no_ato=false -- o painel vê que a
        # conferência não aconteceu com o motorista no cliente.
        validacao = _validar_conteudo(tipo, conteudo, sha, parada=p, nf=request.form.get("nf"), no_ato=False) \
            if tipo == validacao_fotos.TIPO_CANHOTO else None
        if validacao and validacao.get("guardar_como") == validacao_fotos.TIPO_FACHADA:
            # Chegou pela fila (sem sinal na hora) e não é canhoto: guarda
            # como FACHADA -- a parada continua sem canhoto (Hugo, 20/11).
            tipo = validacao_fotos.TIPO_FACHADA
        # Texto na posição de leitura (Hugo, 20/11); o sha256 segue o do
        # original (idempotência e cache da validação).
        gravar = validacao_fotos.corrigir_orientacao(conteudo, validacao)
        if gravar is not conteudo:
            ext = "jpg"
        caminho, caminho_gcs = _guardar_foto(gravar, codigo, tipo, uuid or sha[:16], ext)
        resultado = operacao.registrar_comprovante(
            conn(), parada_id, agent_id(), tipo, uuid or sha, caminho, sha, len(gravar), capturado_em, caminho_gcs,
            validacao=validacao,
        )
        return jsonify({**resultado, "gcs": caminho_gcs is not None,
                        "validacao": (validacao or {}).get("resultado")}), 201

    @app.post("/api/rotas/<int:rota_id>/pedagios")
    @requer_motorista
    def pedagio(rota_id):
        """Pedágio da rota (Hugo, 11/09): valor + foto do recibo, um por
        recibo. Fica PENDENTE até o painel aprovar. Multipart: arquivo
        (foto, obrigatória), valor, uuid, capturado_em; despesas adicionais
        (14/09): tipo, descricao (Outros), parada_id (Estacionamento/Descarga)."""
        arquivo = request.files.get("arquivo")
        if arquivo is None or not arquivo.filename:
            raise OperacaoInvalida("Envie a foto do recibo no campo 'arquivo' (multipart).")
        uuid = (request.form.get("uuid") or "").strip()
        valor = request.form.get("valor")
        capturado_em = request.form.get("capturado_em") or banco.agora()
        ext = _extensao_foto(arquivo)

        ja = conn().execute("SELECT id FROM nucleo_pedagios WHERE uuid = ?", (uuid,)).fetchone() if uuid else None
        if ja:
            return jsonify({"id": ja["id"], "ja_registrado": True})
        rota = operacao.rota_do_motorista(conn(), rota_id, agent_id())

        conteudo = arquivo.read()
        sha = hashlib.sha256(conteudo).hexdigest()
        try:
            valor_f = float(str(valor).replace(",", ".")) if valor else None
        except ValueError:
            valor_f = None
        # Despesa adicional (Hugo, 14/09): mesmo endpoint, `tipo` +
        # `descricao`. A conferência automática só sabe ler recibo de pedágio.
        tipo = (request.form.get("tipo") or banco.DESPESA_PEDAGIO).strip().upper()
        descricao = request.form.get("descricao")
        if tipo not in banco.TIPOS_DESPESA:
            raise OperacaoInvalida("Tipo de despesa inválido.")
        if tipo == banco.DESPESA_OUTROS and not (descricao or "").strip():
            raise OperacaoInvalida("Descreva a despesa (tipo Outros).")
        parada_id = (request.form.get("parada_id") or "").strip() or None
        if tipo in banco.DESPESAS_COM_PEDIDO and not parada_id:
            raise OperacaoInvalida(f"Informe o pedido de referência ({banco.TIPOS_DESPESA[tipo]}).")
        validacao = _validar_conteudo(validacao_fotos.TIPO_PEDAGIO, conteudo, sha, rota_id=rota["id"],
                                      valor=valor_f, no_ato=False) if tipo == banco.DESPESA_PEDAGIO else None
        caminho, caminho_gcs = _guardar_foto(conteudo, f"rota-{rota['id']}", tipo, uuid or sha[:16], ext)
        resultado = operacao.registrar_pedagio(
            conn(), rota_id, agent_id(), uuid or sha, valor, caminho, sha, len(conteudo), capturado_em, caminho_gcs,
            validacao=validacao, tipo=tipo, descricao=descricao, parada_id=parada_id,
        )
        return jsonify({**resultado, "gcs": caminho_gcs is not None, "pedagios": operacao.listar_pedagios(conn(), rota_id)}), 201

    @app.post("/api/rotas/<int:rota_id>/pedagios/<int:pedagio_id>/cancelar")
    @requer_motorista
    def cancelar_pedagio(rota_id, pedagio_id):
        """Motorista desiste de um pedágio que mandou por engano. Só enquanto
        PENDENTE (409 se o painel já revisou). Devolve a lista atualizada."""
        return jsonify({"pedagios": operacao.cancelar_pedagio(conn(), rota_id, pedagio_id, agent_id())})

    @app.post("/api/gps")
    @requer_motorista
    def gps():
        pontos = corpo().get("pontos") or []
        if not isinstance(pontos, list):
            raise OperacaoInvalida("Envie {'pontos': [...]}.")
        return jsonify({"novos": operacao.registrar_gps(conn(), agent_id(), pontos[:2000])})

    @app.get("/abrir/ofertas")
    def abrir_ofertas():
        """Link do aviso de rota disponivel (WhatsApp so torna clicavel https):
        abre o app na aba Ofertas. Pagina fixa, sem dado nenhum."""
        destino = "freshlogmotorista://ofertas"
        return ("<!doctype html><html lang=pt-BR><meta charset=utf-8>"
                "<meta name=viewport content='width=device-width,initial-scale=1'><title>Fresh Log</title>"
                f"<meta http-equiv=refresh content='0;url={destino}'>"
                "<body style='font-family:sans-serif;padding:32px 16px;text-align:center'>"
                "<h1 style='font-size:20px'>Rota disponível</h1>"
                f"<p><a href='{destino}' style='display:inline-block;padding:14px 22px;background:#1b7f4c;color:#fff;"
                "border-radius:10px;text-decoration:none;font-weight:700'>Abrir o app Fresh Log</a></p>"
                "<p style='color:#666'>Se não abrir, entre no app e toque em Ofertas.</p></body></html>")

    @app.get("/abrir/ofertas/<token>")
    def abrir_oferta(token):
        """Pagina publica da oferta de rota sem aceite (Hugo, 19/11): resumo,
        coleta, valor, pagamento, botao pro app e pro cadastro. Token assinado
        (ofertas_rota_enviada.token_oferta); invalido = 404 sem dizer por que."""
        from nucleo import ofertas_rota_enviada
        oferta = ofertas_rota_enviada.oferta_por_token(conn(), app.config["SECRET_TOKENS"], token)
        if oferta is None:
            return render_template("oferta_rota.html", oferta=None), 404
        return render_template("oferta_rota.html", oferta=ofertas_rota_enviada.dados_pagina(conn(), oferta),
                               abrir_app="freshlogmotorista://ofertas")

    @app.get("/api/ofertas")
    @requer_motorista
    def ofertas():
        return jsonify(operacao.listar_ofertas(conn(), agent_id()))

    @app.post("/api/ofertas/<int:rascunho_id>/escolher")
    @requer_motorista
    def escolher(rascunho_id):
        # A disputa é na página pública do marketplace (Hugo, 14/09).
        return jsonify(operacao.escolher_oferta(conn(), agent_id(), rascunho_id,
                                                marketplace=ClienteMarketplace.do_config(app.config["CONFIG_PROJETO"])))

    @app.post("/api/ofertas/<int:rascunho_id>/cancelar")
    @requer_motorista
    def cancelar_escolha(rascunho_id):
        return jsonify(operacao.cancelar_escolha_oferta(conn(), agent_id(), rascunho_id,
                                                        marketplace=ClienteMarketplace.do_config(app.config["CONFIG_PROJETO"])))

    @app.get("/api/disponibilidade")
    @requer_motorista
    def disponibilidade():
        hoje = date.today()
        de = _data(request.args.get("de"), hoje)
        ate = _data(request.args.get("ate"), hoje + timedelta(days=30))
        return jsonify({"ajustes": operacao.listar_disponibilidade(conn(), agent_id(), de, ate)})

    @app.put("/api/disponibilidade")
    @requer_motorista
    def definir_disponibilidade():
        d = corpo()
        de = _data(d.get("de") or d.get("data"), date.today())
        ate = _data(d.get("ate"), de)
        n = operacao.definir_disponibilidade(conn(), agent_id(), de, ate, d.get("disponivel"), d.get("motivo"))
        return jsonify({"dias": n, "ajustes": operacao.listar_disponibilidade(conn(), agent_id(), de, ate)})

    @app.get("/api/financeiro")
    @requer_motorista
    def extrato():
        # Motorista que dirige carro de outro não vê o extrato (Hugo, 08/10);
        # pedágio e despesas continuam nas rotas de /pedagios.
        if not g.motorista.get("ve_financeiro", 1):
            raise OperacaoInvalida("O extrato desta rota vai pro dono do veículo.", 403)
        hoje = date.today()
        de = _data(request.args.get("de"), hoje.replace(day=1))
        ate = _data(request.args.get("ate"), hoje)
        tipo = g.motorista.get("tipo_veiculo")
        if agent_id() is None:
            return jsonify({"linhas": [], "por_dia": [], "total": 0.0, "aviso": "Motorista sem agent_id."})
        ex = financeiro.extrato_motorista(agent_id(), de, ate, tipo, conn=conn())
        ex["tarifa"] = (tarifa_motorista.calcular_valor_rota(tipo, 0, tarifa_motorista.carregar_tarifas(conn())) or None)
        if ex["tarifa"]:
            ex["tarifa"] = ex["tarifa"].como_dict()
        return jsonify(ex)

    # ── atendimento (aba Ajuda do app) ───────────────────────────────────────
    # Chat do motorista com o assistente e com a logística. Cai na mesma fila
    # da tela /atendimento do painel (aba "Motoristas"). Módulo separado:
    # nucleo/chamados_motorista.py. Best-effort no registro -- se o
    # atendimento não estiver disponível (dependência faltando), o resto da
    # API do motorista continua no ar.
    try:
        from nucleo import chamados_motorista
        chamados_motorista.registrar(app, requer_motorista=requer_motorista,
                                     carregar_config=lambda: app.config["CONFIG_PROJETO"])
    except Exception as e:
        logger.exception("atendimento do motorista não registrado: %s", e)

    return app


# `waitress-serve --call nucleo.api_motorista:criar_app` chama a factory;
# `app` abaixo serve pro modo desenvolvimento e pra quem preferir
# `waitress-serve nucleo.api_motorista:app`.
app = None
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--porta", type=int, default=8073)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
    app = criar_app()
    app.run(host="127.0.0.1", port=args.porta, debug=False)
else:
    app = None
