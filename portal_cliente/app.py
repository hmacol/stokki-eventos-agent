# -*- coding: utf-8 -*-
"""
portal_cliente/app.py

Portal do cliente (embarcador): acompanhamento do dia a dia dos pedidos
-- pedido do Hugo, 08/09. Publicado em app.freshhub.com.br/cliente pelo
Caddy (`handle_path /cliente/*`, ver infra/Caddyfile-cliente), app Flask
separado do painel interno (mesmo padrão de resposta_insucesso/app.py e
nucleo/api_motorista.py), reaproveitando o MESMO venv/config.yaml/
dados.db do repositório.

Acesso (decisão do Hugo, 08/09): CNPJ + PIN de 6 dígitos, com primeiro
acesso e "esqueci o PIN" por link no e-mail cadastrado na tabela
`interno` -- ver auth_cliente.py. Dados: dados_cliente.py (VUUPT
filtrada por sender_id + tabelas locais).

COMO RODAR (dev):
    py -3 portal_cliente/app.py            # http://127.0.0.1:8074
    (PORTAL_CLIENTE_DEV=1 desliga o cookie Secure pra testar em http)
COMO RODAR (produção -- ver infra/portal-cliente.service):
    waitress-serve --host=127.0.0.1 --port=8074 app:app
"""
import html as html_mod
import io
import json
import logging
import os
import sys
import threading
import time
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

_RAIZ = Path(__file__).parent.parent
_AQUI = Path(__file__).parent
# ORDEM IMPORTA (mesmo achado de resposta_insucesso/app.py): a raiz antes
# de insucesso_entrega/, que tem uma cópia velha de expedir_pedidos.py.
sys.path.insert(0, str(_RAIZ))
sys.path.insert(1, str(_RAIZ / "insucesso_entrega"))

from homologacao import ambiente as _ambiente
_ambiente.exigir_coerencia(_RAIZ)

import yaml
from flask import (Flask, Response, abort, g, jsonify, redirect, render_template, request, send_file,
                   send_from_directory, session, url_for)
from jinja2 import ChoiceLoader, FileSystemLoader
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

import auth_cliente as auth
import dados_cliente as dados
import envio_pedidos as envios
import anexos_pedido
import lotes_cliente
import cotacao
import entrada_equipe
import endereco_entrega
import transportadora_envio
import alterar_entrega
import cancelamento
import entradas as entradas_mod
from email_utils import enviar_email, envelope_html
import preferencias_notificacao as preferencias
import cadastro_embarcador as cad_emb
import regioes_atendimento as regioes_info
import aula as aula_info
from fingerprint_aguardando_resposta import buscar_pendentes_por_grupo
import aplicar_resposta_insucesso as logica_insucesso
from painel_agentes import acesso_telas

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("portal_cliente")


def _carregar_config() -> dict:
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


_CONFIG = _carregar_config()
_CFG = _CONFIG.get("portal_cliente", {}) or {}
_SECRET = _CFG.get("secret_key")
if not _SECRET:
    raise RuntimeError("portal_cliente.secret_key ausente no config.yaml -- gere uma string aleatória forte.")
_URL_BASE = (_CFG.get("url_base") or "https://app.freshhub.com.br/cliente").rstrip("/")
_DEV = os.environ.get("PORTAL_CLIENTE_DEV") == "1"

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_prefix=1, x_proto=1, x_for=1, x_host=1)
# Reaproveita o partial de paleta do painel (_paleta_cores.html) -- única
# fonte dos tokens de cor claro/escuro, pra não divergir do resto.
app.jinja_loader = ChoiceLoader([
    FileSystemLoader(str(_AQUI / "templates")),
    FileSystemLoader(str(_RAIZ / "painel_agentes" / "templates")),
])
app.secret_key = _SECRET
app.config.update(
    SESSION_COOKIE_NAME="portal_cliente",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=not _DEV,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=14),
    # 64 KB bastava pros formulários; a máscara de envio recebe XMLs/ZIPs
    # de NF-e (08/09) -- um lote de 100 XMLs zipados fica na casa de 1-2 MB.
    MAX_CONTENT_LENGTH=30 * 1024 * 1024,
)

# Homologacao (FRESH_AMBIENTE=homolog): faixa, erros amigaveis e /homologacao/saude.
if _ambiente.homolog_ativo():
    from homologacao import flask_homolog
    flask_homolog.instalar(app, servico="portal")

ACOES_VALIDAS = ("manter", "reagendar", "cancelar")
# "Primeiro acesso / esqueci o PIN": teto de e-mails por CNPJ e de pedidos por
# IP, por hora. O de IP é folgado porque um escritório inteiro sai pelo mesmo
# IP e um grupo (ex.: Marchef) pode pedir link de vários CNPJs em sequência.
_MAX_LINKS_POR_CNPJ_HORA = 3
_MAX_PEDIDOS_LINK_POR_IP_HORA = 20
# Consulta de CEP da prévia do envio (revisão de segurança 30/10, item 2):
# cada CEP fora do cache vira até 2 chamadas HTTP síncronas e o ViaCEP
# bloqueia IP que abusa. Teto por cliente, em memória (o waitress é um
# processo só; reiniciar zera).
_MAX_CEP_POR_MINUTO = 30
_CONSULTAS_CEP: dict[str, list[float]] = {}
_TRAVA_CEP = threading.Lock()
# Níveis do painel interno que podem entrar no portal em nome de um
# cliente (decisão do Hugo, 08/09, item 2: "os dois" sobem XML). Leitura
# só olha.
_NIVEIS_EQUIPE_ENVIA = ("total", "operador")


# ── Sessão ─────────────────────────────────────────────────────────────────────

def _cliente_da_equipe(cnpj: str) -> dict | None:
    """A equipe enxerga o mesmo que o cliente enxergaria -- inclusive as
    empresas do grupo quando o CNPJ escolhido é o login de um grupo."""
    conn = auth.conectar()
    try:
        cliente = auth.montar_cliente(conn, cnpj, "equipe")
    finally:
        conn.close()
    return {**cliente, "equipe": True} if cliente else None


@app.before_request
def _carregar_cliente():
    g.cliente = None
    g.equipe = session.get("equipe")
    if g.equipe and g.equipe.get("nominal") and not _nominal_segue_valendo(g.equipe):
        session.clear()
        g.equipe = None
    if g.equipe:
        # Equipe Fresh Log logada (usuário do painel) escolhendo um cliente.
        cnpj = session.get("cnpj_equipe")
        g.cliente = _cliente_da_equipe(cnpj) if cnpj else None
        if cnpj and not g.cliente:
            session.pop("cnpj_equipe", None)
        return
    cnpj, versao = session.get("cnpj"), session.get("v")
    if cnpj and versao:
        conn = auth.conectar()
        try:
            g.cliente = auth.sessao_valida(conn, cnpj, versao)
            completar = bool(g.cliente) and cad_emb.precisa_completar(conn, g.cliente["cnpj"])
        finally:
            conn.close()
        if not g.cliente:
            session.clear()
        elif g.cliente.get("trocar_pin") and request.endpoint not in _LIVRES_ANTES_DA_TROCA:
            # Conta criada com o PIN padrao (02/10): nada abre antes da troca.
            if request.path.startswith("/api/"):
                return jsonify({"erro": "Antes de continuar, é preciso trocar o PIN."}), 403
            return redirect(url_for("trocar_pin"))
        elif completar and request.endpoint not in _LIVRES_ANTES_DO_CADASTRO:
            # Pre-cadastro automatico (10/10): tipo de carga e contatos antes de tudo.
            if request.path.startswith("/api/"):
                return jsonify({"erro": "Antes de continuar, complete o cadastro da empresa."}), 403
            return redirect(url_for("completar_cadastro"))


_LIVRES_ANTES_DA_TROCA = {"trocar_pin", "sair", "static", "saude", "regioes", "regioes_logo", "aula", "aula_arquivo",
                          "ciencia_pagina", "ciencia_ciente", "ciencia_reagendar"}  # link publico da ciencia do dedicado
_LIVRES_ANTES_DO_CADASTRO = _LIVRES_ANTES_DA_TROCA | {"completar_cadastro"}


def requer_cliente(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not g.cliente:
            if request.path.startswith("/api/"):
                return jsonify({"erro": "Sessão expirada. Entre de novo."}), 401
            if g.get("equipe"):
                return redirect(url_for("equipe_cliente"))
            return redirect(url_for("login", proximo=request.script_root + request.full_path.rstrip("?")))
        return f(*args, **kwargs)
    return wrapper


def _quem_envia() -> str:
    """Carimbo de quem fez a ação (portal_envios.enviado_por)."""
    if g.get("equipe"):
        return f"equipe:{g.equipe.get('usuario', '?')}"
    return "cliente"


def _exige_pode_enviar():
    """Cliente sempre pode; equipe só nos níveis total/operador."""
    if g.get("equipe") and g.equipe.get("nivel") not in _NIVEIS_EQUIPE_ENVIA:
        abort(Response(jsonify({"erro": "Seu usuário só tem acesso de leitura."}).get_data(), status=403,
                       mimetype="application/json"))


def exige_mesma_origem(f):
    """Anti-CSRF por Origin/Referer nos POSTs (mesmo padrão do painel)."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        origem = request.headers.get("Origin") or request.headers.get("Referer")
        if origem:
            host = urlparse(origem).netloc
            if host and host != request.host:
                abort(403)
        return f(*args, **kwargs)
    return wrapper


def _proximo_seguro(valor: str | None) -> str:
    """Só caminho relativo dentro do próprio app (anti open-redirect), SEMPRE
    com o prefixo do Caddy (/cliente) na frente -- redirect("/") sem o
    prefixo manda o navegador pra raiz do site (achado no 1º teste em
    produção, 08/09)."""
    raiz = request.script_root or ""
    if valor and valor.startswith("/") and not valor.startswith("//") and "\\" not in valor:
        if raiz and not (valor == raiz or valor.startswith(raiz + "/")):
            valor = raiz + valor
        return valor
    return url_for("inicio")


@app.context_processor
def _globais():
    return {"cliente": g.get("cliente"), "equipe": g.get("equipe"), "ano": date.today().year}


# ── Acesso da equipe Fresh Log (em nome de um cliente) ─────────────────────────

def _nivel_equipe(usuario: str, senha: str) -> str | None:
    """Mesmos pares usuário/senha do painel interno (painel_agentes.* no
    config.yaml) -- reaproveita as credenciais que a equipe já tem."""
    import hmac
    cfg = _carregar_config().get("painel_agentes", {}) or {}
    if not usuario or not senha:
        return None
    pares = (("usuario", "senha", "total"), ("usuario_operador", "senha_operador", "operador"),
             ("usuario_leitura", "senha_leitura", "leitura"))
    for ch_u, ch_s, nivel in pares:
        u, s = cfg.get(ch_u), cfg.get(ch_s)
        if u and s and hmac.compare_digest(usuario, str(u)) and hmac.compare_digest(senha, str(s)):
            return nivel
    return None


def _conta_nominal_portal(usuario: str, senha: str) -> dict | None:
    """Login nominal da /acessos com "Portal do cliente" marcado (Hugo,
    29/10): entra com poder de operador, seja qual for o nível no painel.
    Senha provisória não entra (quem sabe é quem redefiniu)."""
    c = acesso_telas.conferir_senha(usuario, senha)
    return c if c and c["portal_equipe"] and not c["trocar_senha"] else None


def _nominal_segue_valendo(equipe: dict) -> bool:
    """Reconfere a cada request, como o painel: desmarcar, desativar,
    redefinir a senha ou "Derrubar todas as sessões" (/acessos) derruba a
    sessão do portal no próximo clique."""
    c = acesso_telas.conta(equipe.get("usuario", ""))
    return bool(c and c["ativo"] and c["portal_equipe"] and not c["trocar_senha"]
                and c["versao_senha"] == equipe.get("v")
                and acesso_telas.rodada_sessoes()["rodada"] == equipe.get("r"))


# Teto por login vindo de qualquer IP (mesmo do painel): o contador por IP
# zera quando alguém entra, então sozinho não segura chute de quem tem login.
_MAX_ERROS_POR_LOGIN = 30


def _ip_cliente() -> str:
    return request.remote_addr or "?"   # IP real: ProxyFix(x_for=1) atrás do Caddy


@app.route("/equipe", methods=["GET", "POST"])
def equipe_login():
    """Mesma trava do login do cliente (5 erros -> 15 min), só que por IP:
    aqui não há conta no banco pra pendurar o contador."""
    erro = None
    status = 200
    if request.method == "POST":
        usuario = (request.form.get("usuario") or "").strip()
        ip = _ip_cliente()
        conn = auth.conectar()
        try:
            if auth.contar_tentativas(conn, "equipe_ip", ip, auth.BLOQUEIO_MINUTOS) >= auth.MAX_TENTATIVAS \
                    or auth.contar_tentativas(conn, "equipe_login", usuario.lower(),
                                              auth.BLOQUEIO_MINUTOS) >= _MAX_ERROS_POR_LOGIN:
                logger.warning(f"equipe login bloqueado ip={ip} usuario={usuario}")
                erro, status = "Muitas tentativas. Tente de novo em alguns minutos.", 429
            else:
                senha = request.form.get("senha") or ""
                nivel = _nivel_equipe(usuario, senha)
                equipe = {"usuario": usuario, "nivel": nivel} if nivel else None
                if equipe is None:
                    c = _conta_nominal_portal(usuario, senha)
                    if c:
                        nivel = "operador"
                        equipe = {"usuario": c["usuario"], "nivel": nivel, "nominal": True, "v": c["versao_senha"],
                                  "r": acesso_telas.rodada_sessoes()["rodada"]}
                if equipe is None:
                    auth.registrar_tentativa(conn, "equipe_ip", ip)
                    auth.registrar_tentativa(conn, "equipe_login", usuario.lower())
                    erro = "Usuário ou senha incorretos."
                else:
                    # Só zera o do login que entrou: entrar com a própria
                    # senha não limpa os erros contra outro login (revisão 29/10).
                    auth.limpar_tentativas(conn, "equipe_ip", ip)
                    auth.limpar_tentativas(conn, "equipe_login", usuario.lower())
                    session.clear()
                    session["equipe"] = equipe
                    session.permanent = True
                    logger.info(f"equipe login usuario={usuario} nivel={nivel}")
                    return redirect(url_for("equipe_cliente", proximo=request.args.get("proximo") or None))
        finally:
            conn.close()
    return render_template("equipe.html", erro=erro, modo="login", proximo=request.args.get("proximo") or ""), status


def _equipe_do_passe(dados: dict) -> dict | None:
    """Sessão de equipe pra quem chegou do painel com o passe assinado
    (entrada_equipe.py). Mesmas regras do login /equipe, sem a senha: login
    nominal só com "Portal do cliente" marcado na /acessos; par fixo do
    config só total/operador/leitura."""
    usuario, nivel = dados["usuario"], dados["nivel"]
    c = acesso_telas.conta(usuario)
    if c:
        if not (c["ativo"] and c["portal_equipe"] and not c["trocar_senha"]):
            return None
        return {"usuario": c["usuario"], "nivel": "operador", "nominal": True, "v": c["versao_senha"],
                "r": acesso_telas.rodada_sessoes()["rodada"]}
    import hmac
    chave = {"total": "usuario", "operador": "usuario_operador", "leitura": "usuario_leitura"}.get(nivel)
    esperado = (_carregar_config().get("painel_agentes", {}) or {}).get(chave) if chave else None
    if esperado and hmac.compare_digest(usuario, str(esperado)):
        return {"usuario": usuario, "nivel": nivel}
    return None


# Passes do painel ja usados (numero unico -> hora): cada um so abre uma
# sessao. Em memoria, como o teto de CEP: o waitress e um processo so, e um
# restart no meio so reabre a janela dos 60 s de validade do passe.
_PASSES_USADOS: dict[str, float] = {}
_TRAVA_PASSES = threading.Lock()


def _passe_ja_usado(unico: str) -> bool:
    """Marca o passe como usado; True se ja tinha sido."""
    agora = time.time()
    with _TRAVA_PASSES:
        for k in [k for k, t in _PASSES_USADOS.items() if agora - t > entrada_equipe.VALIDADE_SEGUNDOS + 5]:
            del _PASSES_USADOS[k]
        if unico in _PASSES_USADOS:
            return True
        _PASSES_USADOS[unico] = agora
        return False


@app.route("/equipe/entrar")
def equipe_entrar():
    """Chegada do painel interno (botão Cotação da Torre, 16/11): abre a
    sessão de equipe pelo passe e manda escolher o cliente, sempre -- pra
    ninguém cotar em nome do cliente da vez anterior sem perceber."""
    proximo = request.args.get("proximo") or ""
    dados = entrada_equipe.ler(_SECRET, request.args.get("t") or "")
    if dados and _passe_ja_usado(dados["unico"]):
        logger.warning(f"equipe entrada pelo painel: passe repetido usuario={dados['usuario']} ip={_ip_cliente()}")
        dados = None
    equipe = _equipe_do_passe(dados) if dados else None
    if equipe is None:
        erro = ("Seu usuário não tem o Portal do cliente liberado. Peça ao Hugo pra marcar em Acessos."
                if dados else "A entrada pelo painel venceu. Volte no painel e clique de novo, ou entre com usuário e senha.")
        logger.warning(f"equipe entrada pelo painel recusada usuario={(dados or {}).get('usuario', '?')} ip={_ip_cliente()}")
        return render_template("equipe.html", erro=erro, modo="login", proximo=proximo), 403
    anterior = session.get("equipe") or {}
    cnpj_anterior = session.get("cnpj_equipe") if anterior.get("usuario") == equipe["usuario"] else None
    session.clear()
    session["equipe"] = equipe
    session.permanent = True
    if cnpj_anterior:
        session["cnpj_equipe"] = cnpj_anterior   # só pré-seleciona na lista
    logger.info(f"equipe entrada pelo painel usuario={equipe['usuario']} nivel={equipe['nivel']}")
    return redirect(url_for("equipe_cliente", proximo=proximo or None))


@app.route("/equipe/cliente", methods=["GET", "POST"])
def equipe_cliente():
    if not g.get("equipe"):
        return redirect(url_for("equipe_login"))
    conn = auth.conectar()
    try:
        embarcadores = auth.embarcadores_para_equipe(conn)
    finally:
        conn.close()
    if request.method == "POST":
        cnpj = auth.normalizar_cnpj(request.form.get("cnpj"))
        if _cliente_da_equipe(cnpj):
            session["cnpj_equipe"] = cnpj
            proximo = request.form.get("proximo") or ""
            return redirect(_proximo_seguro(proximo) if proximo else url_for("inicio"))
    return render_template("equipe.html", modo="cliente", embarcadores=embarcadores,
                           cnpj_atual=session.get("cnpj_equipe", ""), proximo=request.values.get("proximo") or "")


# ── Login / acesso ─────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    if g.cliente:
        return redirect(url_for("inicio"))
    erro = None
    cnpj_digitado = ""
    if request.method == "POST":
        cnpj_digitado = (request.form.get("cnpj") or "").strip()
        pin = (request.form.get("pin") or "").strip()
        conn = auth.conectar()
        try:
            conta = auth.autenticar(conn, cnpj_digitado, pin)
            session.clear()
            session["cnpj"] = conta["cnpj"]
            session["v"] = auth.versao_conta(conta)
            session.permanent = True
            logger.info(f"login ok cnpj={conta['cnpj']} sender_id={conta.get('sender_id')}")
            return redirect(_proximo_seguro(request.form.get("proximo")))
        except auth.AutenticacaoInvalida as e:
            erro = e.mensagem
        finally:
            conn.close()
    return render_template("login.html", erro=erro, cnpj=cnpj_digitado,
                           proximo=request.values.get("proximo", ""))


@app.route("/sair")
def sair():
    session.clear()
    return redirect(url_for("login"))


@app.route("/primeiro-acesso", methods=["GET", "POST"])
def primeiro_acesso():
    """Primeiro acesso E 'esqueci o PIN': manda o link de definição de PIN
    pro(s) e-mail(s) da tabela interno.

    A resposta é a MESMA pra CNPJ cadastrado, não cadastrado, sem e-mail ou
    acima do limite (revisão de segurança de 15/09: as 3 mensagens antigas
    deixavam enumerar os clientes da Fresh Log, e o e-mail saía sem limite).
    O motivo real fica só no log."""
    erro = None
    cnpj_digitado = ""
    if request.method == "POST":
        cnpj_digitado = (request.form.get("cnpj") or "").strip()
        cnpj = auth.normalizar_cnpj(cnpj_digitado)
        if len(cnpj) != 14:
            erro = "Confira o CNPJ: são 14 dígitos."
        else:
            ip = _ip_cliente()
            conn = auth.conectar()
            try:
                emb = auth.buscar_embarcador(conn, cnpj)
                if auth.contar_tentativas(conn, "link_pin_ip", ip, 60) >= _MAX_PEDIDOS_LINK_POR_IP_HORA:
                    logger.warning(f"primeiro acesso: limite por IP ip={ip} cnpj={cnpj}")
                elif not emb or not emb["emails"]:
                    auth.registrar_tentativa(conn, "link_pin_ip", ip)
                    logger.info(f"primeiro acesso: cnpj={cnpj} {'sem e-mail' if emb else 'fora do cadastro'} ip={ip}")
                elif auth.contar_tentativas(conn, "link_pin_cnpj", cnpj, 60) >= _MAX_LINKS_POR_CNPJ_HORA:
                    auth.registrar_tentativa(conn, "link_pin_ip", ip)
                    logger.warning(f"primeiro acesso: limite de links cnpj={cnpj} ip={ip}")
                else:
                    auth.registrar_tentativa(conn, "link_pin_ip", ip)
                    auth.registrar_tentativa(conn, "link_pin_cnpj", cnpj)
                    token = auth.gerar_token_definir_pin(_SECRET, conn, emb["cnpj"])
                    if not _enviar_link_pin(emb, f"{_URL_BASE}/definir-pin/{token}"):
                        logger.error(f"primeiro acesso: FALHA ao enviar o link cnpj={cnpj}")
            finally:
                conn.close()
            return render_template("mensagem.html", titulo="Confira seu e-mail",
                                   texto="Se esse CNPJ estiver no nosso cadastro, enviamos um link pra definir o PIN "
                                         "no e-mail que a Fresh Log tem da sua empresa. Ele vale por 24 horas. "
                                         "Não chegou em alguns minutos? Confira o CNPJ e a caixa de spam, "
                                         "ou fale com a Fresh Log.",
                                   voltar=url_for("login"))
    return render_template("primeiro_acesso.html", erro=erro, cnpj=cnpj_digitado)


def _enviar_link_pin(emb: dict, link: str) -> bool:
    corpo = envelope_html(
        f"<p>Olá, <strong>{emb['nome']}</strong>.</p>"
        f"<p>Use o botão abaixo pra definir o PIN de acesso ao portal de acompanhamento de entregas "
        f"da Fresh Log (CNPJ {auth.formatar_cnpj(emb['cnpj'])}).</p>"
        f"<p style='margin:24px 0'><a href='{link}' style='background:#0EA575;color:#fff;padding:12px 22px;"
        f"border-radius:7px;text-decoration:none;font-weight:700'>Definir meu PIN</a></p>"
        f"<p style='color:#6B7280;font-size:13px'>O link vale por 24 horas e só pode ser usado uma vez. "
        f"Se não foi você que pediu, ignore este e-mail.</p>"
        f"<p style='color:#6B7280;font-size:12px'>Se o botão não abrir, copie este endereço: {link}</p>",
        rodape="Fresh Log · Portal de acompanhamento de entregas",
    )
    if _DEV:
        logger.info(f"[dev] link de PIN para {emb['emails']}: {link}")
    return enviar_email(emb["emails"], "Fresh Log · Defina seu PIN de acesso", corpo, _CONFIG.get("email", {}))


@app.route("/definir-pin/<token>", methods=["GET", "POST"])
def definir_pin(token):
    conn = auth.conectar()
    try:
        v = auth.validar_token_definir_pin(_SECRET, conn, token)
        if v["estado"] != "ok":
            textos = {
                "expirado": "Esse link já venceu (vale 24 horas). Peça um novo em “Primeiro acesso / esqueci o PIN”.",
                "usado": "Esse link já foi usado. Se precisar trocar o PIN de novo, peça um novo link.",
                "invalido": "Link inválido.",
            }
            return render_template("mensagem.html", titulo="Link não vale mais", texto=textos[v["estado"]],
                                   voltar=url_for("primeiro_acesso"), voltar_rotulo="Pedir novo link"), 410
        erro = None
        if request.method == "POST":
            pin, conf = (request.form.get("pin") or "").strip(), (request.form.get("pin2") or "").strip()
            try:
                auth.validar_pin_formato(pin)
                if pin != conf:
                    raise ValueError("Os dois PINs não conferem.")
                conta = auth.definir_pin(conn, v["cnpj"], pin)
                session.clear()
                session["cnpj"] = conta["cnpj"]
                session["v"] = auth.versao_conta(conta)
                session.permanent = True
                logger.info(f"PIN definido cnpj={conta['cnpj']}")
                return redirect(url_for("inicio"))
            except ValueError as e:
                erro = str(e)
        return render_template("definir_pin.html", embarcador=v["embarcador"],
                               cnpj_formatado=auth.formatar_cnpj(v["cnpj"]), erro=erro)
    finally:
        conn.close()


@app.route("/trocar-pin", methods=["GET", "POST"])
def trocar_pin():
    """Troca obrigatória de quem entrou com o PIN padrão (02/10). Só o
    cliente logado; a equipe e quem já trocou vão direto pro início."""
    if g.get("equipe"):
        return redirect(url_for("inicio"))
    if not g.cliente:
        return redirect(url_for("login"))
    if not g.cliente.get("trocar_pin"):
        return redirect(url_for("inicio"))
    erro = None
    if request.method == "POST":
        pin, conf = (request.form.get("pin") or "").strip(), (request.form.get("pin2") or "").strip()
        conn = auth.conectar()
        try:
            auth.validar_pin_formato(pin)
            if pin != conf:
                raise ValueError("Os dois PINs não conferem.")
            conta = auth.trocar_pin_obrigatorio(conn, g.cliente["cnpj"], pin)
            session.clear()
            session["cnpj"] = conta["cnpj"]
            session["v"] = auth.versao_conta(conta)
            session.permanent = True
            logger.info(f"PIN padrao trocado cnpj={conta['cnpj']}")
            return redirect(url_for("inicio"))
        except ValueError as e:
            erro = str(e)
        finally:
            conn.close()
    return render_template("trocar_pin.html", erro=erro)


def _avisar_cadastro_preenchido(nome: str, tipo: str) -> None:
    try:
        import notificar_whatsapp as nw
        nw.despachar(_CONFIG, "embarcadores_stokki", "cadastro",
                     f"{nome} completou o cadastro: {tipo}. Aguardando aprovação no /inicio do painel.")
    except Exception as e:
        logger.warning(f"Aviso de cadastro preenchido nao enviado: {e}")


@app.route("/completar-cadastro", methods=["GET", "POST"])
def completar_cadastro():
    """Pre-cadastro automatico (10/10): tipo de carga, e-mails e WhatsApp antes
    de abrir o portal. O tipo só vale no roteirizador depois da aprovação."""
    if g.get("equipe"):
        return redirect(url_for("inicio"))
    if not g.cliente:
        return redirect(url_for("login"))
    cnpj = g.cliente["cnpj"]
    conn = auth.conectar()
    try:
        if not cad_emb.precisa_completar(conn, cnpj):
            return redirect(url_for("inicio"))
        prefs = preferencias.ler(conn, cnpj)
        valores = {"habilidade": "", "emails": "\n".join(prefs["emails"] or [(cad_emb.buscar(conn, cnpj) or {}).get("email_stokki") or ""]),
                   "whatsapp": prefs["whatsapp"]}
        erro = None
        if request.method == "POST":
            valores = {chave: (request.form.get(chave) or "") for chave in ("habilidade", "emails", "whatsapp")}
            emails = valores["emails"].replace(";", "\n").replace(",", "\n").splitlines()
            try:
                tipo = cad_emb.normalizar_habilidade(valores["habilidade"])
                if not any(e.strip() for e in emails):
                    raise ValueError("Informe pelo menos um e-mail para os avisos.")
                preferencias.salvar(conn, cnpj, emails, {}, "cliente", whatsapp=valores["whatsapp"])
                cad_emb.salvar_preenchimento(conn, cnpj, tipo)
            except ValueError as e:
                erro = str(e)
            else:
                logger.info(f"cadastro preenchido cnpj={cnpj} tipo={tipo}")
                _avisar_cadastro_preenchido(g.cliente["nome"], tipo)
                return redirect(url_for("inicio"))
        return render_template("completar_cadastro.html", valores=valores, erro=erro,
                               habilidades=list(cad_emb.DESCRICOES_HABILIDADE.items()))
    finally:
        conn.close()


# ── Telas ──────────────────────────────────────────────────────────────────────

def _data_da_query() -> date:
    bruto = request.args.get("data", "")
    try:
        return date.fromisoformat(bruto) if bruto else date.today()
    except ValueError:
        return date.today()


def _dias_da_query() -> int | None:
    """?dias=N -> lista corrida dos últimos N dias (21/09). Sem o parâmetro
    (ou com valor fora das opções) a resposta continua sendo a do dia só --
    é o que o e-mail e quem tiver link antigo esperam."""
    bruto = request.args.get("dias", "")
    return int(bruto) if bruto.isdigit() and int(bruto) in dados.DIAS_JANELA_OPCOES else None


def _empresas_com_envio() -> list[dict]:
    """Empresas do login que são cliente na Stokki (interno.stkkc_id) -- são
    as que aparecem no seletor da vista "Enviar pedidos" do login de grupo.
    Nenhuma com stkkc_id: devolve a do login, e a tela avisa como sempre."""
    conn = envios.conectar()
    try:
        com_envio = []
        for e in g.cliente["empresas"]:
            try:
                if envios.config_stokki_cliente(conn, e["cnpj"], _CONFIG)["client_id"]:
                    com_envio.append({"cnpj": e["cnpj"], "nome": e["nome"]})
            except envios.ErroEnvio:
                continue
    finally:
        conn.close()
    return com_envio or [{"cnpj": g.cliente["cnpj"], "nome": g.cliente["nome"]}]


@app.route("/")
@requer_cliente
def inicio():
    aba = request.args.get("aba") or "acompanhamento"
    conn = envios.conectar()
    try:   # embarcador piloto do WMS: a tela de envio explica a indicação de lote (18/11)
        lotes_ativo = any(lotes_cliente.embarcador_wms(conn, e["cnpj"], _CONFIG) is not None for e in g.cliente["empresas"])
    finally:
        conn.close()
    conn = entradas_mod.conectar()
    try:
        entradas_ativo = entradas_mod.entradas_ativas_para(conn, [e["cnpj"] for e in g.cliente["empresas"]])
    finally:
        conn.close()
    abas = ("acompanhamento", "envios") + (("entradas",) if entradas_ativo else ())
    return render_template("acompanhamento.html", data_inicial=_data_da_query().isoformat(),
                           empresas_envio=_empresas_com_envio(), lotes_ativo=lotes_ativo,
                           hoje=date.today().isoformat(), aba_inicial=aba if aba in abas else "acompanhamento",
                           entradas_ativo=entradas_ativo,
                           pode_enviar=not (g.get("equipe") and g.equipe.get("nivel") not in _NIVEIS_EQUIPE_ENVIA))


@app.route("/saude")
def saude():
    return jsonify({"ok": True, "agora": datetime.now().isoformat(timespec="seconds")})


# ── Página pública: regiões e dias de atendimento (Hugo, 07/10) ────────────────

@app.route("/regioes")
def regioes():
    """Informativo aos embarcadores, sem login: regiões, dias de visita,
    galpões e calendário do mês, lidos da configuração de dia fixo e do
    calendário de feriados (regioes_atendimento.py). `?mes=AAAA-MM` navega
    dentro da janela permitida; fora dela cai no mês corrente."""
    hoje = date.today()
    ano, mes = regioes_info.validar_mes(request.args.get("mes"), hoje)
    d = regioes_info.montar(ano, mes, hoje)
    # O mesmo template vira o informativo mensal (gerar_informativo_mensal.py),
    # por isso os links entram como variáveis, não como url_for no HTML.
    resp = Response(render_template("regioes.html", d=d, logo_src=url_for("regioes_logo"),
                                    url_mes_anterior=url_for("regioes", mes=d["mes_anterior"]),
                                    url_mes_seguinte=url_for("regioes", mes=d["mes_seguinte"]),
                                    url_portal=url_for("login")))
    resp.headers["Cache-Control"] = "public, max-age=900"
    return resp


@app.route("/regioes/logo.png")
def regioes_logo():
    return send_file(_RAIZ / "assets" / "logo_freshlog.png", mimetype="image/png", max_age=86400)


# ── Página pública: aula em vídeo (Hugo, 06/11) ────────────────────────────────

def _aula_videos() -> list[dict]:
    """O vídeo da cotação (Hugo, 16/11) só aparece quando a calculadora for
    liberada pra todos os clientes (portal_cliente.cotacao.visivel_cliente)."""
    liberadas = {"cotacao"} if cotacao.regras_de(_CONFIG).get("visivel_cliente") else set()
    return aula_info.carregar_manifesto(liberadas=liberadas)


@app.route("/aula")
def aula():
    """Videos curtos de treinamento do portal, sem login (o link vai por
    WhatsApp ate pra quem ainda nao entrou). So lista o que esta no
    manifesto gerado pelo aula_portal/gravar.py."""
    return render_template("aula.html", videos=_aula_videos(),
                           logo_src=url_for("regioes_logo"))


@app.route("/aula/arquivo/<nome>")
def aula_arquivo(nome):
    # conditional=True responde Range com 206: o Safari do iPhone so toca mp4 assim.
    if not aula_info.arquivo_permitido(nome, _aula_videos()):
        abort(404)
    resp = send_from_directory(aula_info.PASTA_AULA, nome, conditional=True, max_age=3600)
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


# ── API ────────────────────────────────────────────────────────────────────────

@app.route("/api/dia")
@requer_cliente
def api_dia():
    data_alvo = _data_da_query()
    dias = _dias_da_query()
    forcar = request.args.get("atualizar") == "1"
    try:
        dia = (dados.montar_janela(g.cliente["sender_ids"], dias, _CONFIG, forcar=forcar) if dias
               else dados.montar_dia(g.cliente["sender_ids"], data_alvo, _CONFIG, forcar=forcar))
        # `empresas` fora do cache: o front usa pra coluna/filtro "Empresa" do grupo.
        return jsonify({**dia, "empresas": [{"sender_id": e["sender_id"], "nome": e["nome"]}
                                            for e in g.cliente["empresas"]],
                        "alteracao_entrega": any(alterar_entrega.ativa_para(_CONFIG, e["cnpj"])
                                                 for e in g.cliente["empresas"])})
    except Exception as e:
        logger.exception(f"api_dia falhou sender_ids={g.cliente['sender_ids']} data={data_alvo}")
        return jsonify({"erro": f"Não foi possível carregar os pedidos agora ({type(e).__name__}). Tente de novo."}), 502


@app.route("/api/canhoto/<int:service_id>")
@requer_cliente
def api_canhoto(service_id):
    token = _CONFIG.get("vuupt_api", {}).get("token", "")
    servico = dados.buscar_servico(token, service_id)
    if not servico or servico.get("sender_id") not in g.cliente["sender_ids"]:
        abort(404)
    checklist_id = dados.checklist_id_do_servico(servico)
    if not checklist_id:
        return render_template("mensagem.html", titulo="Comprovante ainda não disponível",
                               texto="O motorista ainda não enviou a foto do canhoto deste pedido, "
                                     "ou ela ainda está sendo processada. Tente de novo mais tarde.",
                               voltar=url_for("inicio")), 404
    pdf = dados.comprovante_proprio(servico, _CONFIG) or \
        dados.baixar_canhoto_pdf(token, checklist_id, servico.get("code") or str(service_id))
    if not pdf:
        return render_template("mensagem.html", titulo="Comprovante indisponível",
                               texto="Não conseguimos gerar o comprovante agora. Tente de novo em alguns minutos.",
                               voltar=url_for("inicio")), 502
    nome = f"comprovante_{(servico.get('code') or str(service_id)).lstrip('#')}.pdf"
    return send_file(pdf, mimetype="application/pdf", as_attachment=False, download_name=nome, max_age=0)


# ── Alteração de entrega (endereço, data, horário de recebimento) ──────────────

_FORA_DA_AREA = f"Esse endereço fica fora da área atendida. {alterar_entrega.CHAT}"


def _mapa_horarios() -> dict:
    """Horário padrão de recebimento da planilha BD_CLIENTES (como o envio de pedidos)."""
    return alterar_entrega.mapa_horarios(_CONFIG)


def _servico_alteravel(service_id: int) -> tuple[dict, dict]:
    """(serviço da Vuupt, empresa do login dona dele); 404 se a chave está
    desligada pra essa empresa ou o serviço não é do login. Id negativo = pedido
    ainda sem serviço na Vuupt (pool direto da Stokki): vem do núcleo, sem Vuupt."""
    token = _CONFIG.get("vuupt_api", {}).get("token", "")
    servico = dados.buscar_servico_ou_nucleo(token, service_id, incluir="checklistAnswers,customer",
                                             config=_CONFIG)
    if not servico or servico.get("sender_id") not in g.cliente["sender_ids"]:
        abort(404)
    empresa = next(e for e in g.cliente["empresas"] if e["sender_id"] == servico.get("sender_id"))
    if not alterar_entrega.ativa_para(_CONFIG, empresa["cnpj"]):
        abort(404)
    return servico, empresa


def _avaliar_alteracao(conn, servico: dict) -> alterar_entrega.Avaliacao:
    return alterar_entrega.avaliar_no_nucleo(conn, servico, _CONFIG)


def _destinatario(servico: dict) -> tuple[str, str]:
    cli = dados._desembrulhar(servico, "customer")
    return alterar_entrega._digitos(cli.get("code")), cli.get("name") or ""


@app.route("/api/pedidos/<int(signed=True):service_id>/alteracao")
@requer_cliente
def api_alteracao(service_id):
    servico, _empresa = _servico_alteravel(service_id)
    try:
        conn = envios.conectar()
        try:
            av = _avaliar_alteracao(conn, servico)
        finally:
            conn.close()
        from regras.complexidade_entrega import carregar_ajustes_manuais, horario_efetivo
        doc, nome = _destinatario(servico)
        ini, fim = horario_efetivo(doc, _mapa_horarios(), carregar_ajustes_manuais())
        data = alterar_entrega._data_agendada(servico)
    except Exception:
        logger.exception(f"alteracao (consulta) sid={service_id} -> erro")
        return jsonify({"erro": f"Não foi possível consultar o pedido agora. {alterar_entrega.CHAT}"}), 502
    return jsonify({"pode": av.pode, "motivo": av.motivo, "em_rota": av.em_rota, "destinatario": nome,
                    "atual": {"endereco": servico.get("address") or "", "data": data.isoformat() if data else None,
                              "horario": {"inicio": ini, "fim": fim}}})


@app.route("/api/pedidos/<int(signed=True):service_id>/alteracao", methods=["POST"])
@requer_cliente
@exige_mesma_origem
def api_alteracao_aplicar(service_id):
    _exige_pode_enviar()
    servico, empresa = _servico_alteravel(service_id)
    corpo = request.get_json(silent=True)
    if not isinstance(corpo, dict):
        corpo = {}
    por = _quem_envia()
    conn = envios.conectar()
    conn_registro = None
    chamou_aplicar = False
    try:
        av = _avaliar_alteracao(conn, servico)   # sempre de novo: nunca confia no GET
        if not av.pode:
            return jsonify({"erro": av.motivo}), 409
        try:
            validado = alterar_entrega.validar(corpo, date.today())
        except alterar_entrega.ErroAlteracao as e:
            return jsonify({"erro": str(e)}), 400
        if validado["endereco"]:
            try:
                fora = envios._identificar_area(
                    [{"id": 0, "address": alterar_entrega.endereco_vuupt(validado["endereco"])}],
                    (_CONFIG.get("google_maps") or {}).get("api_key") or "")
            except Exception as e:  # noqa: BLE001 -- mesma regra do envio: sem checagem nao segura
                logger.warning(f"alteracao: checagem de area falhou ({e}); deixando passar")
                fora = []
            if any(tipo for _s, tipo in fora):
                return jsonify({"erro": _FORA_DA_AREA}), 400
        import registro_dia_fixo
        conn_registro = registro_dia_fixo.conectar()
        plano = alterar_entrega.planejar(servico, validado, av, conn_registro, date.today())
        if plano.avisos and not corpo.get("confirmar"):
            return jsonify({"ok": False, "precisa_confirmar": True, "avisos": plano.avisos})
        doc, nome = _destinatario(servico)
        contexto = {"cnpj_embarcador": empresa["cnpj"], "sender_id": servico.get("sender_id"), "por": por,
                    "destinatario_doc": doc, "destinatario_nome": nome, "config": _CONFIG,
                    "mapa_horarios": _mapa_horarios(), "empresa_nome": empresa["nome"]}
        chamou_aplicar = True
        resultado = alterar_entrega.aplicar(conn, servico, validado, plano, contexto,
                                            alterar_entrega.dependencias_reais(_CONFIG, por))
    except Exception:
        logger.exception(f"alteracao cnpj={empresa['cnpj']} sid={service_id} por={por} -> erro")
        return jsonify({"erro": f"Não foi possível alterar agora. {alterar_entrega.CHAT}"}), 502
    finally:
        conn.close()
        if conn_registro is not None:
            conn_registro.close()
        if chamou_aplicar:  # a Vuupt pode ter mudado mesmo se o aplicar estourou
            dados.invalidar_cache(servico["sender_id"])
    logger.info(f"alteracao cnpj={empresa['cnpj']} sid={service_id} por={por} -> {'ok' if resultado['ok'] else 'erro'}")
    if not resultado["ok"]:
        return jsonify({"erro": resultado["mensagem"]}), 502
    return jsonify({"ok": True, "mensagem": resultado["mensagem"]})


@app.route("/api/responder", methods=["POST"])
@requer_cliente
@exige_mesma_origem
def api_responder():
    """Decisão do embarcador sobre insucessos aguardando resposta -- mesmo
    caminho da página /insucesso (aplicar_decisao aplica por grupo
    sender_id + failed_reason_id)."""
    corpo = request.get_json(silent=True) or {}
    try:
        failed_reason_id = int(corpo.get("failed_reason_id"))
    except (TypeError, ValueError):
        return jsonify({"erro": "Motivo inválido."}), 400
    acao = corpo.get("acao")
    if acao not in ACOES_VALIDAS:
        return jsonify({"erro": "Ação inválida."}), 400

    # Login de grupo: o card diz de qual empresa é o insucesso. Sem o campo
    # (tela antiga em cache, login avulso) vale a empresa do login.
    try:
        sender_id = int(corpo.get("sender_id") or g.cliente["sender_id"])
    except (TypeError, ValueError):
        return jsonify({"erro": "Empresa inválida."}), 400
    if sender_id not in g.cliente["sender_ids"]:
        return jsonify({"erro": "Essa empresa não faz parte do seu acesso."}), 403
    if not buscar_pendentes_por_grupo(sender_id, failed_reason_id):
        return jsonify({"erro": "Esses pedidos já foram respondidos.", "resolvido": True}), 409

    nova_data, hora = None, None
    if acao == "reagendar":
        try:
            nova_data = date.fromisoformat((corpo.get("data") or "").strip())
            if nova_data < date.today():
                raise ValueError
        except ValueError:
            return jsonify({"erro": "Informe uma data válida, a partir de hoje."}), 400
        hora = (corpo.get("hora") or "").strip()
        try:
            datetime.strptime(hora, "%H:%M")
        except ValueError:
            return jsonify({"erro": "Informe um horário válido (HH:MM)."}), 400

    try:
        resultados = logica_insucesso.aplicar_decisao(sender_id, failed_reason_id, acao, nova_data, _CONFIG,
                                                      hora_pedida=hora)
    except Exception as e:
        logger.exception("aplicar_decisao falhou")
        return jsonify({"erro": f"Não foi possível aplicar a resposta ({type(e).__name__})."}), 502
    logger.info(f"resposta insucesso cnpj={g.cliente['cnpj']} sender_id={sender_id} motivo={failed_reason_id} acao={acao} -> {resultados}")
    dados.invalidar_cache(sender_id)
    return jsonify({"ok": True, "resultados": [{**r, "data": r["data"].isoformat() if r.get("data") else None}
                                               for r in resultados]})


# ── Preferências de notificação ────────────────────────────────────────────────
# Pedido do Hugo, 17/09: botão Notificações -- o próprio cliente liga/desliga
# cada tipo de e-mail e informa o e-mail em que quer recebê-los. Regra e
# validação em preferencias_notificacao.py (as rotinas de envio leem de lá).

def _resposta_notificacoes(prefs: dict):
    return jsonify({
        "tipos": [{"tipo": tipo, "grupo": info["grupo"], "rotulo": info["rotulo"], "descricao": info["descricao"],
                   "ligado": prefs["tipos"][tipo]} for tipo, info in preferencias.TIPOS.items()],
        "emails": prefs["emails"],
        "emails_cadastro": prefs["emails_cadastro"],
        "whatsapp": prefs["whatsapp"],
        "max_emails": preferencias.MAX_EMAILS,
        "somente_leitura": bool(g.get("equipe") and g.equipe.get("nivel") not in _NIVEIS_EQUIPE_ENVIA),
    })


@app.route("/api/notificacoes")
@requer_cliente
def api_notificacoes():
    conn = auth.conectar()
    try:
        return _resposta_notificacoes(preferencias.ler(conn, g.cliente["cnpj"]))
    finally:
        conn.close()


@app.route("/api/notificacoes", methods=["POST"])
@requer_cliente
@exige_mesma_origem
def api_notificacoes_salvar():
    """Sempre no CNPJ da sessão -- nada do corpo escolhe o embarcador."""
    _exige_pode_enviar()
    corpo = request.get_json(silent=True) or {}
    emails, tipos = corpo.get("emails", ""), corpo.get("tipos", {})
    whatsapp = corpo.get("whatsapp")  # ausente = mantém o que está
    if isinstance(emails, str):
        emails = emails.replace(";", "\n").replace(",", "\n").splitlines()
    if not isinstance(emails, list) or not isinstance(tipos, dict) or not isinstance(whatsapp, (str, type(None))):
        return jsonify({"erro": "Dados inválidos."}), 400
    conn = auth.conectar()
    try:
        prefs = preferencias.salvar(conn, g.cliente["cnpj"], emails, tipos, _quem_envia(), whatsapp=whatsapp)
    except ValueError as e:
        return jsonify({"erro": str(e)}), 400
    finally:
        conn.close()
    logger.info(f"notificacoes cnpj={g.cliente['cnpj']} por={_quem_envia()} tipos={prefs['tipos']} "
                f"emails={len(prefs['emails'])} whatsapp={'sim' if prefs['whatsapp'] else 'nao'}")
    return _resposta_notificacoes(prefs)


# ── Máscara de envio de pedidos (XML → fila → Stokki) ──────────────────────────
# Pedido do Hugo, 08/09: ver envio_pedidos.py (dados/validação) e
# enviar_stokki.py (worker que cria na Stokki respeitando a trava de sessão).

def _empresa_envio() -> dict:
    """Empresa em que o envio acontece. Login avulso: a própria. Login de
    grupo: a informada na chamada (cabeçalho X-Portal-Empresa ou `?empresa=`);
    sem ela, a dona do pedido da rota ou a primeira que é cliente na Stokki
    (rotas de lote usam _grupo_sem_seletor) -- cada empresa é um cliente
    diferente na Stokki, então fila, destinatários e emitente são por empresa."""
    pedido = auth.normalizar_cnpj(request.headers.get("X-Portal-Empresa") or request.args.get("empresa"))
    if not pedido:
        # Grupo sem seletor (12/11): rota de um pedido já enviado vale pra empresa dona dele
        envio_id = (request.view_args or {}).get("envio_id")
        if len(g.cliente["empresas"]) < 2:
            return g.cliente["empresas"][0]
        if envio_id is not None:
            return _empresa_do_envio(envio_id)
        if "empresa_padrao_envio" not in g:   # grupo em que a do login não é cliente na Stokki
            primeira = _empresas_com_envio()[0]["cnpj"]
            g.empresa_padrao_envio = next(e for e in g.cliente["empresas"] if e["cnpj"] == primeira)
        return g.empresa_padrao_envio
    for e in g.cliente["empresas"]:
        if e["cnpj"] == pedido:
            return e
    abort(Response(jsonify({"erro": "Essa empresa não faz parte do seu acesso."}).get_data(), status=403,
                   mimetype="application/json"))


def _empresa_do_envio(envio_id: int) -> dict:
    """Empresa do login dona do envio; envio de fora (ou inexistente) cai na
    do login, e a busca do envio por CNPJ dá 404 como sempre."""
    if g.get("empresa_do_envio", (None,))[0] != envio_id:
        conn = envios.conectar()
        try:
            r = conn.execute("SELECT cnpj_embarcador FROM portal_envios WHERE id = ?", (envio_id,)).fetchone()
        finally:
            conn.close()
        dono = r["cnpj_embarcador"] if r else None
        g.empresa_do_envio = (envio_id, next((e for e in g.cliente["empresas"] if e["cnpj"] == dono),
                                             g.cliente["empresas"][0]))
    return g.empresa_do_envio[1]


def _grupo_sem_seletor() -> dict[str, dict] | None:
    """Login de grupo sem empresa informada na chamada (12/11, pedido do Hugo:
    a tela não tem mais seletor; a planilha diz a empresa de cada pedido e o
    XML vai pelo emitente): {cnpj: empresa} das que são cliente na Stokki.
    None = uma empresa só (login avulso, ou X-Portal-Empresa/?empresa= mandado)."""
    if request.headers.get("X-Portal-Empresa") or request.args.get("empresa") or len(g.cliente["empresas"]) < 2:
        return None
    if "grupo_envio" not in g:
        com_envio = {e["cnpj"] for e in _empresas_com_envio()}
        g.grupo_envio = {e["cnpj"]: e for e in g.cliente["empresas"] if e["cnpj"] in com_envio}
    return g.grupo_envio if len(g.grupo_envio) > 1 else None


def _outras_empresas_envio() -> dict[str, str]:
    atual = _empresa_envio()["cnpj"]
    return {e["cnpj"]: e["nome"] for e in g.cliente["empresas"] if e["cnpj"] != atual}


def _json_erro_envio(e: Exception, status: int = 400):
    return jsonify({"erro": str(e)}), status


def _avisar_xd_ausente(nome: str, erro: str) -> None:
    """Fluxo Cross sem produto XD no catálogo (Hugo, 25/10): o cliente vê na
    prévia e o atendimento recebe e-mail pra cadastrar/conferir o XD."""
    try:
        from html import escape as html_escape
        from bloqueio_area import forcar_destino
        email_cfg = _CONFIG.get("email", {}) or {}
        destino = forcar_destino(_CONFIG) or email_cfg.get("email_atendimento") or email_cfg.get("email_responsavel")
        if not destino:
            return
        corpo = envelope_html(
            f"<p><b>{html_escape(nome)}</b> tentou enviar pedidos no fluxo Cross, mas o produto XD dele não foi "
            f"encontrado no catálogo da Stokki ({html_escape(erro or '')}).</p>"
            f"<p>Os pedidos Cross ficaram recusados na tela do cliente. Cadastre ou reative o produto XD do "
            f"embarcador na Stokki (código começando com XD); depois da próxima sincronização de produtos "
            f"o envio volta a funcionar.</p>")
        enviar_email([destino], f"[Portal] Produto XD não encontrado · {nome}", corpo, email_cfg)
    except Exception:
        logger.exception("aviso de XD ausente falhou")


@app.route("/api/envios")
@requer_cliente
def api_envios_listar():
    conn = envios.conectar()
    try:
        grupo = _grupo_sem_seletor()
        if grupo:   # todas as empresas juntas, com o nome de cada uma na linha
            lista = [{**l, "empresa": e["nome"]} for e in grupo.values() for l in envios.listar_envios(conn, e["cnpj"])]
            lista.sort(key=lambda l: (l.get("criado_em") or "", l.get("id") or 0), reverse=True)
        else:
            lista = envios.listar_envios(conn, _empresa_envio()['cnpj'])
        anexos_pedido.anotar(conn, lista)
        cfg = envios.config_stokki_cliente(conn, _empresa_envio()['cnpj'], _CONFIG)
    except envios.ErroEnvio as e:
        return _json_erro_envio(e)
    finally:
        conn.close()
    try:
        from stokki.sessao_uso import em_uso
        fila = em_uso()
    except Exception:
        fila = None
    return jsonify({"envios": lista, "resumo": envios.resumo_envios(lista), "dias": envios.DIAS_LISTAGEM,
                    "regra_xml": cfg["regra_xml"], "regra_xml_rotulo": envios.REGRAS_XML.get(cfg["regra_xml"], ""),
                    "envio_ativo": cfg["envio_ativo"], "client_id": cfg["client_id"], "fila": fila,
                    "endereco_entrega_ativo": endereco_entrega.entrega_ativa(_CONFIG),
                    "atualizado_em": datetime.now().strftime("%H:%M")})


@app.route("/api/envios/analisar", methods=["POST"])
@requer_cliente
@exige_mesma_origem
def api_envios_analisar():
    """Recebe .xml/.zip, lê cada NF-e, valida (item 9) e devolve a prévia
    -- nada entra na fila ainda. Os XMLs válidos ficam num temporário
    identificado por token até o cliente confirmar."""
    _exige_pode_enviar()
    arquivos = request.files.getlist("arquivos")
    if not arquivos:
        return jsonify({"erro": "Selecione pelo menos um arquivo: XML da NF-e (ou ZIP com XMLs) ou planilha .xlsx no modelo Fresh Log."}), 400
    itens, rejeitados, vistos = [], [], set()
    anexos = []  # PDFs do lote: documentos dos pedidos (anexos_pedido.py, 11/11)
    n_planilhas = 0
    # Colunas da planilha indicadas na tela (16/11): {arquivo: {"linha", "colunas"}}.
    # `indicar=1`: o cliente pediu pra indicar mesmo que os nomes sejam conhecidos.
    try:
        colunas_indicadas = json.loads(request.form.get("colunas") or "{}")
    except ValueError:
        colunas_indicadas = {}
    if not isinstance(colunas_indicadas, dict):
        colunas_indicadas = {}
    pedir_indicacao = request.form.get("indicar") == "1"
    indicar = []  # planilhas esperando o cliente dizer as colunas
    entrega_ativa = endereco_entrega.entrega_ativa(_CONFIG)
    conn = envios.conectar()
    try:
        # Cada item da prévia tem a empresa dele (grupo sem seletor: a da coluna
        # da planilha ou o emitente do XML); `ctx` guarda o que é por empresa.
        grupo = _grupo_sem_seletor()
        padrao = _empresa_envio()['cnpj']
        ctxs: dict[str, dict] = {}

        def ctx(cnpj: str) -> dict:
            if cnpj not in ctxs:
                fl, xd_ = envios.fluxos_para_envio(conn, cnpj)
                ctxs[cnpj] = {"cfg": envios.config_stokki_cliente(conn, cnpj, _CONFIG), "fluxos": fl, "xd": xd_,
                              "nome": ((grupo or {}).get(cnpj) or _empresa_envio())["nome"],
                              # embarcador piloto do WMS: pode indicar lote (lotes_cliente.py, 18/11)
                              "emb_lotes": lotes_cliente.embarcador_wms(conn, cnpj, _CONFIG)}
            return ctxs[cnpj]

        cfg = ctx(padrao)["cfg"]
        lotes_planilhas = []   # planilhas de lotes do cliente lidas e gravadas neste envio
        for f in arquivos:
            try:
                partes = envios.expandir_upload(f.filename or "", f.read())
            except envios.ErroEnvio as e:
                rejeitados.append({"arquivo": f.filename or "arquivo", "erro": str(e)})
                continue
            for nome, conteudo in partes:
                if anexos_pedido.e_pdf(nome, conteudo):
                    if len(anexos) >= anexos_pedido.MAX_PDFS_POR_ENVIO:
                        rejeitados.append({"arquivo": nome, "erro": f"{nome}: no máximo {anexos_pedido.MAX_PDFS_POR_ENVIO} PDFs por envio."})
                        continue
                    try:
                        anexos.append(anexos_pedido.guardar_previa(nome, conteudo, g.cliente["cnpj"]))
                    except envios.ErroEnvio as e:
                        rejeitados.append({"arquivo": nome, "erro": str(e)})
                    continue
                if envios.e_planilha(nome, conteudo) and ctx(padrao)["emb_lotes"] is not None:
                    # Planilha de lotes do cliente (18/11): grava na hora; casa com o pedido pela NF
                    try:
                        lido = lotes_cliente.ler_planilha_de_lotes(nome, conteudo)
                        if lido is not None:
                            lotes_planilhas.append(lotes_cliente.importar_planilha(
                                conn, ctx(padrao)["emb_lotes"], lido, nome, _quem_envia()))
                            logger.info(f"analisar planilha de lotes cnpj={padrao} arquivo={nome!r} "
                                        f"indicacoes={lotes_planilhas[-1]['gravadas']}")
                            continue
                    except lotes_cliente.ErroLote as e:
                        rejeitados.append({"arquivo": nome, "erro": f"Planilha de lotes: {e}"})
                        continue
                if envios.e_planilha(nome, conteudo):
                    # Planilha no modelo Fresh Log (09/09): vários pedidos por
                    # arquivo; cada pedido vira um item da prévia com token
                    # próprio (JSON temporário) apontando pra planilha original.
                    n_planilhas += 1
                    indicado = colunas_indicadas.get(nome)
                    mapa_usado: dict = {}
                    avisos_plan: list = []   # coluna de item alterada: lê, avisa e não trava (17/11)
                    try:
                        pedidos, rej = envios.ler_planilha(
                            conteudo, nome, padrao, {c: e["nome"] for c, e in grupo.items()} if grupo else None,
                            guardado=envios.carregar_mapa_planilha(conn, g.cliente["cnpj"]),
                            indicado=indicado if isinstance(indicado, dict) else None,
                            pedir_indicacao=pedir_indicacao, mapa_usado=mapa_usado, avisos=avisos_plan)
                    except envios.ColunasPorIndicar as e:
                        indicar.append(e.dados)
                        continue
                    except envios.ErroEnvio as e:
                        rejeitados.append({"arquivo": nome, "erro": str(e)})
                        continue
                    if mapa_usado:   # lida com as colunas indicadas: valem pras próximas planilhas deste login
                        envios.guardar_mapa_planilha(conn, g.cliente["cnpj"], mapa_usado, _quem_envia())
                    rejeitados.extend(rej)
                    if avisos_plan:
                        logger.info(f"analisar planilha alterada cnpj={g.cliente['cnpj']} arquivo={nome!r}: {' '.join(avisos_plan)!r}")
                    token_plan = envios.guardar_temporario(conteudo, Path(nome).suffix.lstrip(".") or "xlsx")
                    for ped in pedidos:
                        rotulo = f"Pedido {ped['referencia']}"
                        if ped["chave_nfe"] in vistos:
                            rejeitados.append({"arquivo": nome, "rotulo": rotulo, "erro": "Repetido dentro do mesmo envio."})
                            continue
                        vistos.add(ped["chave_nfe"])
                        emp = ped["emitente_cnpj"]
                        v = envios.validar_item(conn, ped, emp, _outras_empresas_envio())
                        erros_sku, avisos_sku = envios.validar_skus(conn, ped, ctx(emp)["cfg"])
                        if ped.get("transportadora_texto"):   # coluna "Transportadora" da planilha
                            try:
                                t_plan = transportadora_envio.achar_por_texto(conn, ped["transportadora_texto"])
                                ped["transportadora_id"], ped["transportadora_nome"] = t_plan["id"], t_plan["nome"]
                            except transportadora_envio.ErroTransportadora as err_t:
                                erros_sku = [*erros_sku, f"Coluna Transportadora: {err_t}"]
                        if not v["ok"] or erros_sku:
                            rejeitados.append({"arquivo": nome, "rotulo": rotulo, "erro": " ".join(v["erros"] + erros_sku)})
                            continue
                        token = envios.guardar_temporario_pedido(ped, token_plan)
                        item = {k: v_ for k, v_ in ped.items() if k not in ("emitente_cnpj",)}
                        item.update({"token": token, "avisos": avisos_plan + v["avisos"] + avisos_sku, "rotulo": rotulo,
                                     "empresa_cnpj": emp, "empresa": ctx(emp)["nome"] if grupo else "",
                                     "destinatario_doc_formatado": envios.formatar_documento(ped["destinatario_doc"])})
                        itens.append(item)
                    continue
                try:
                    nfe = envios.ler_nfe(conteudo, nome)
                except envios.ErroEnvio as e:
                    rejeitados.append({"arquivo": nome, "erro": str(e)})
                    continue
                nfe["origem"] = envios.ORIGEM_XML
                if nfe["chave_nfe"] in vistos:
                    rejeitados.append({"arquivo": nome, "numero_nf": nfe["numero_nf"], "rotulo": f"NF {nfe['numero_nf']}",
                                       "erro": "Repetida dentro do mesmo envio."})
                    continue
                vistos.add(nfe["chave_nfe"])
                emp = nfe["emitente_cnpj"] if grupo and nfe["emitente_cnpj"] in grupo else padrao
                v = envios.validar_item(conn, nfe, emp, _outras_empresas_envio())
                if not v["ok"]:
                    rejeitados.append({"arquivo": nome, "numero_nf": nfe["numero_nf"], "rotulo": f"NF {nfe['numero_nf']}",
                                       "erro": " ".join(v["erros"])})
                    continue
                token = envios.guardar_temporario(conteudo)
                item = {k: v_ for k, v_ in nfe.items() if k not in ("emitente_cnpj",)}
                item.update({"token": token, "avisos": v["avisos"], "rotulo": f"NF {nfe['numero_nf']}",
                             "empresa_cnpj": emp, "empresa": ctx(emp)["nome"] if grupo else "",
                             "destinatario_doc_formatado": envios.formatar_documento(nfe["destinatario_doc"]),
                             "lotes_ativo": ctx(emp)["emb_lotes"] is not None and bool(nfe.get("produtos"))})
                itens.append(item)
        if entrega_ativa:
            _casos_de_entrega(conn, itens)
        usadas = list(dict.fromkeys(it["empresa_cnpj"] for it in itens)) or [padrao]
        destinatarios: dict[str, dict] = {}
        for cnpj in usadas:
            do_cnpj = [it for it in itens if it["empresa_cnpj"] == cnpj]
            for doc, d in envios.info_destinatarios(conn, cnpj, do_cnpj, _CONFIG).items():
                ant = destinatarios.get(doc)
                # mesmo destinatário em duas empresas do grupo: só é "conhecido"
                # (não pergunta o horário) se as duas já souberem
                destinatarios[doc] = d if ant is None else {
                    **(ant if ant["conhecido"] else d), "conhecido": ant["conhecido"] and d["conhecido"],
                    "requer_agendamento": ant["requer_agendamento"] or d["requer_agendamento"]}
        # XD ausente: o cliente vê na prévia; o atendimento recebe 1 e-mail por
        # embarcador por dia, e só quando o lote tem pedido que iria em Cross.
        avisar_xd = [c for c in usadas if ctx(c)["xd"]["erro"] and any(it["empresa_cnpj"] == c for it in itens)
                     and envios.primeiro_aviso_xd_do_dia(conn, c)]
        for it in itens:
            it["fluxos"] = ctx(it["empresa_cnpj"])["fluxos"]
        # Lotes que a Fresh Log tem em estoque, de sugestão no "Indicar lote" (só pra piloto do WMS)
        for cnpj in usadas:
            emb_lotes = ctx(cnpj)["emb_lotes"]
            com_lote = [it for it in itens if it["empresa_cnpj"] == cnpj and it.get("lotes_ativo")]
            if emb_lotes is None or not com_lote:
                continue
            estoque = lotes_cliente.lotes_em_estoque(conn, emb_lotes, {p["sku"] for it in com_lote for p in it["produtos"]})
            for it in com_lote:
                it["lotes_estoque"] = {p["sku"].upper(): estoque[p["sku"].upper()] for p in it["produtos"]
                                       if p["sku"].upper() in estoque}
        # Transportadoras do cadastro, pro embarcador corrigir a da nota (só XML)
        transportadoras = transportadora_envio.opcoes(conn) if any(it.get("origem") == envios.ORIGEM_XML for it in itens) else []
    except envios.ErroEnvio as e:
        return _json_erro_envio(e)
    finally:
        conn.close()
    for c in avisar_xd:
        _avisar_xd_ausente(ctxs[c]["nome"] or c, ctxs[c]["xd"]["erro"])
    # só Cross e o XD falta: nada daquela empresa pode ir
    sem_fluxo = {c for c in usadas if ctxs[c]["xd"]["erro"] and not ctxs[c]["fluxos"]}
    rejeitados.extend({"arquivo": it.get("nome_arquivo") or it.get("rotulo"), "numero_nf": it.get("numero_nf"),
                       "rotulo": it.get("rotulo"), "erro": envios.MSG_XD_AUSENTE}
                      for it in itens if it["empresa_cnpj"] in sem_fluxo)
    itens = [it for it in itens if it["empresa_cnpj"] not in sem_fluxo]
    fluxos_disp = [f for f in envios.FLUXOS if any(f in ctxs[c]["fluxos"] for c in usadas)]
    xd = next((ctxs[c]["xd"] for c in usadas if ctxs[c]["xd"]["erro"]), ctxs[usadas[0]]["xd"])
    _sugerir_pedidos(anexos, [{**it, "chave": it["token"]} for it in itens])
    logger.info(f"analisar cnpj={','.join(usadas)} por={_quem_envia()} validos={len(itens)} rejeitados={len(rejeitados)} "
                f"planilhas={n_planilhas} anexos={len(anexos)}")
    for r in rejeitados[:20]:   # o motivo fica no log: em 17/11 só deu pra saber baixando a planilha
        logger.info(f"analisar rejeitado arquivo={r.get('arquivo')!r} {str(r.get('rotulo') or '')[:80]!r}: {str(r.get('erro'))[:300]!r}")
    return jsonify({"itens": itens, "rejeitados": rejeitados, "destinatarios": destinatarios, "anexos": anexos,
                    "indicar": indicar, "lotes_planilhas": lotes_planilhas,
                    "regra_xml": cfg["regra_xml"], "regra_xml_rotulo": envios.REGRAS_XML.get(cfg["regra_xml"], ""),
                    "envio_ativo": cfg["envio_ativo"], "hoje": date.today().isoformat(), "entrega_ativa": entrega_ativa,
                    "fluxos_disponiveis": fluxos_disp, "fluxo_rotulos": envios.FLUXO_ROTULOS, "xd": xd,
                    "transportadoras": transportadoras})


def _casos_de_entrega(conn, itens: list[dict]) -> None:
    """Caso do endereço de entrega de cada XML da prévia (27/10, item["entrega"]):
    memória pela empresa do item (login de grupo: a emitente do XML). Mesmo
    destinatário/endereço da mesma empresa no lote calcula uma vez só."""
    empresas = {e["cnpj"]: e for e in g.cliente["empresas"]}
    casos: dict[tuple, dict] = {}
    for it in itens:
        if it.get("origem") != envios.ORIGEM_XML:
            continue
        emp = empresas.get(it.get("empresa_cnpj")) or _empresa_envio()
        chave = tuple(it.get(k) or "" for k in ("empresa_cnpj", "destinatario_doc", "destinatario_cep", "destinatario_endereco",
                                                "endereco_fiscal", "transportadora_nome", "transportadora_cnpj"))
        if chave not in casos:
            casos[chave] = endereco_entrega.classificar_entrega(conn, it, emp["cnpj"], emp.get("sender_id"))
        it["entrega"] = casos[chave]


def _cep_acima_do_limite(chave: str) -> bool:
    """Janela de 60 s por cliente; só a consulta liberada conta."""
    agora = time.monotonic()
    with _TRAVA_CEP:
        recentes = [t for t in _CONSULTAS_CEP.get(chave, ()) if agora - t < 60]
        acima = len(recentes) >= _MAX_CEP_POR_MINUTO
        if not acima:
            recentes.append(agora)
        _CONSULTAS_CEP[chave] = recentes
    return acima


@app.route("/api/envios/cep/<cep>")
@requer_cliente
def api_envios_cep(cep):
    """CEP do formulário de endereço de entrega da prévia (27/10) e da janela
    'Pedir alteração' do acompanhamento. Quem grava consulta de novo: isto só
    preenche a tela. Sem nenhuma das duas chaves pro login, a rota não existe
    (revisão de segurança 30/10 -- não abrir proxy de CEP sem uso)."""
    alteracao = any(alterar_entrega.ativa_para(_CONFIG, e["cnpj"]) for e in g.cliente["empresas"])
    if not (endereco_entrega.entrega_ativa(_CONFIG) or alteracao):
        abort(404)
    _exige_pode_enviar()
    if _cep_acima_do_limite(g.cliente["cnpj"]):
        logger.warning(f"envios cep: limite por minuto cnpj={g.cliente['cnpj']}")
        return jsonify({"erro": "Muitas consultas de CEP. Tente de novo em um minuto."}), 429
    achado = cotacao.consultar_cep(cotacao.normalizar_cep(cep))
    if not achado:
        return jsonify({"erro": "CEP não encontrado"}), 404
    return jsonify({k: achado.get(k) or "" for k in ("cep", "logradouro", "bairro", "cidade", "uf")})


@app.route("/api/envios/modelo-planilha")
@requer_cliente
def api_envios_modelo_planilha():
    """Modelo .xlsx da Fresh Log pra importação por planilha (09/09)."""
    grupo = _grupo_sem_seletor()
    conteudo = envios.gerar_modelo_planilha(_empresa_envio()["nome"] or "",
                                            {c: e["nome"] for c, e in grupo.items()} if grupo else None)
    return send_file(io.BytesIO(conteudo), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True, download_name="modelo_pedidos_freshlog.xlsx", max_age=0)

def _sugerir_pedidos(anexos: list[dict], pedidos: list[dict]) -> None:
    """Poe em cada anexo o pedido sugerido (`pedido` = chave, ou None) e o
    motivo. E so sugestao: o usuario confere antes de enviar."""
    for a in anexos:
        a["pedido"], a["motivo"] = anexos_pedido.sugerir_pedido(a, pedidos)


@app.route("/api/envios/anexos/ler", methods=["POST"])
@requer_cliente
@exige_mesma_origem
def api_envios_anexos_ler():
    """PDFs (ou o XML da NF-e): le cada um, guarda no temporario e sugere o pedido entre os
    que a tela mandou em `pedidos` (JSON [{chave, numero_nf, destinatario_doc,
    referencia}]) -- os da previa ou um pedido ja enviado. Nada e anexado
    aqui; quem anexa e o confirmar (previa) ou POST /api/envios/<id>/anexos."""
    _exige_pode_enviar()
    arquivos = request.files.getlist("arquivos")
    if not arquivos:
        return jsonify({"erro": "Selecione pelo menos um arquivo."}), 400
    if len(arquivos) > anexos_pedido.MAX_PDFS_POR_ENVIO:
        return jsonify({"erro": f"Envie no máximo {anexos_pedido.MAX_PDFS_POR_ENVIO} arquivos por vez."}), 400
    try:
        pedidos = [p for p in json.loads(request.form.get("pedidos") or "[]") if isinstance(p, dict) and p.get("chave")]
    except ValueError:
        pedidos = []
    anexos, rejeitados = [], []
    for f in arquivos:
        try:
            anexos.append(anexos_pedido.guardar_previa(f.filename or "documento.pdf", f.read(), g.cliente["cnpj"]))
        except envios.ErroEnvio as e:
            rejeitados.append({"arquivo": f.filename or "arquivo", "erro": str(e)})
    _sugerir_pedidos(anexos, pedidos[:500])
    return jsonify({"anexos": anexos, "rejeitados": rejeitados})


def _envio_pra_anexo(conn, envio_id: int) -> dict:
    envio = envios.buscar_envio(conn, envio_id, _empresa_envio()['cnpj'])
    if not envio:
        abort(404)
    return envio


@app.route("/api/envios/<int:envio_id>/anexos", methods=["POST"])
@requer_cliente
@exige_mesma_origem
def api_envios_anexar(envio_id):
    """Anexa a um pedido ja enviado os documentos lidos em /anexos/ler.
    Corpo: {anexos: [token], conferido: true}."""
    _exige_pode_enviar()
    corpo = request.get_json(silent=True) or {}
    tokens = [t for t in (corpo.get("anexos") or []) if isinstance(t, str)]
    if not tokens:
        return jsonify({"erro": "Nenhum documento pra anexar."}), 400
    if corpo.get("conferido") is not True:
        return jsonify({"erro": "Confirme que os documentos são deste pedido."}), 400
    conn = envios.conectar()
    try:
        envio = _envio_pra_anexo(conn, envio_id)
        if not anexos_pedido.pode_anexar(conn, envio):
            return jsonify({"erro": "Este pedido já saiu pra entrega (ou foi encerrado) e não aceita mais documentos."}), 409
        gravados = anexos_pedido.anexar(conn, envio, [{"token": t, "casado_por": "manual"} for t in tokens], _quem_envia(),
                                        dono=g.cliente["cnpj"])
        conn.commit()
    except envios.ErroEnvio as e:
        conn.rollback()
        return _json_erro_envio(e)
    finally:
        conn.close()
    logger.info(f"anexar envio={envio_id} por={_quem_envia()} n={len(gravados)}")
    return jsonify({"ok": True, "anexados": gravados})


@app.route("/api/envios/<int:envio_id>/anexos/<int:anexo_id>")
@requer_cliente
def api_envios_anexo_baixar(envio_id, anexo_id):
    conn = envios.conectar()
    try:
        _envio_pra_anexo(conn, envio_id)
        anexo = anexos_pedido.buscar(conn, envio_id, anexo_id)
    finally:
        conn.close()
    if not anexo or not anexos_pedido.caminho(anexo).is_file():
        abort(404)
    # XML sempre como download: aberto na pagina, um XML enviado pelo cliente poderia rodar script.
    xml = anexo["tipo"] == anexos_pedido.TIPO_XML
    resposta = send_file(anexos_pedido.caminho(anexo), mimetype="application/xml" if xml else "application/pdf",
                         as_attachment=xml, download_name=anexo["nome_original"], max_age=0)
    resposta.headers["X-Content-Type-Options"] = "nosniff"
    return resposta


@app.route("/api/envios/<int:envio_id>/anexos/<int:anexo_id>/remover", methods=["POST"])
@requer_cliente
@exige_mesma_origem
def api_envios_anexo_remover(envio_id, anexo_id):
    _exige_pode_enviar()
    conn = envios.conectar()
    try:
        envio = _envio_pra_anexo(conn, envio_id)
        anexo = anexos_pedido.buscar(conn, envio_id, anexo_id)
        if not anexo:
            abort(404)
        if not anexos_pedido.pode_anexar(conn, envio):
            return jsonify({"erro": "Este pedido já saiu pra entrega (ou foi encerrado): o documento não pode mais ser removido."}), 409
        anexos_pedido.remover(conn, anexo)
        conn.commit()
    finally:
        conn.close()
    logger.info(f"remover anexo={anexo_id} envio={envio_id} por={_quem_envia()}")
    return jsonify({"ok": True})


@app.route("/api/envios/confirmar", methods=["POST"])
@requer_cliente
@exige_mesma_origem
def api_envios_confirmar():
    _exige_pode_enviar()
    corpo = request.get_json(silent=True) or {}
    itens = corpo.get("itens") or []
    if not isinstance(itens, list) or not itens:
        return jsonify({"erro": "Nenhum pedido pra confirmar."}), 400
    if len(itens) > 200:
        return jsonify({"erro": "Envie no máximo 200 notas por vez."}), 400
    # Documentos dos pedidos (11/11): item["anexos"] = [{token, casado_por}].
    # So entram com a confirmacao do usuario de que cada um esta no pedido certo.
    anexos_por_token = {it.get("token"): [a for a in (it.get("anexos") or []) if isinstance(a, dict)]
                        for it in itens if isinstance(it, dict) and isinstance(it.get("anexos"), list) and it["anexos"]}
    if anexos_por_token and corpo.get("anexos_conferidos") is not True:
        return jsonify({"erro": "Confirme que cada documento está no pedido correto antes de enviar."}), 400
    # Lote marcado na prévia (18/11): item["lotes"] = {SKU: lote}. O formato do lote é conferido
    # ANTES de gravar o envio; o produto, depois, contra o XML gravado (lotes_cliente.py).
    lotes_por_token = {}
    for it in itens:
        if not (isinstance(it, dict) and isinstance(it.get("lotes"), dict) and it["lotes"]):
            continue
        if len(it["lotes"]) > lotes_cliente.MAX_LOTES_POR_NOTA:
            return jsonify({"erro": "Lotes demais para uma nota."}), 400
        try:
            lotes_por_token[it.get("token")] = {str(k)[:60]: lotes_cliente.normalizar_lote(v) for k, v in it["lotes"].items()}
        except lotes_cliente.ErroLote as e:
            return jsonify({"erro": str(e)}), 400
    anexos_falhas = []
    lotes_gravados, lotes_avisos = 0, []
    conn = envios.conectar()
    try:
        anexos_pedido.validar_tokens([a.get("token", "") for lista in anexos_por_token.values() for a in lista],
                                     g.cliente["cnpj"])
        # Data fora do dia de visita da região (Hugo, 03/10): pergunta antes de
        # gravar; o embarcador troca a data ou confirma assim mesmo
        # (aceita_fora_dia_fixo no item) e o pedido vira envio dedicado.
        fora = envios.avisos_fora_dia_fixo(itens, _CONFIG)
        if fora:
            return jsonify({"erro": "Há pedidos com data fora do dia de visita da região.", "fora_dia_fixo": fora}), 409
        grupo = _grupo_sem_seletor()
        if grupo:   # a empresa de cada pedido sai do próprio temporário (emitente)
            por_empresa = {c: {"regra_xml": envios.config_stokki_cliente(conn, c, _CONFIG)["regra_xml"],
                               "fluxos": envios.fluxos_para_envio(conn, c)[0], "sender_id": e.get("sender_id")}
                           for c, e in grupo.items()}
            criados = envios.confirmar_envios(conn, g.cliente["cnpj"], itens, _quem_envia(), _CONFIG, "nenhuma",
                                              por_empresa=por_empresa)
        else:
            cfg = envios.config_stokki_cliente(conn, _empresa_envio()['cnpj'], _CONFIG)
            fluxos_disp, _xd = envios.fluxos_para_envio(conn, _empresa_envio()['cnpj'])
            criados = envios.confirmar_envios(conn, _empresa_envio()['cnpj'], itens, _quem_envia(), _CONFIG, cfg["regra_xml"],
                                              fluxos_disponiveis=fluxos_disp, sender_id=_empresa_envio().get("sender_id"))
        # Lotes marcados na prévia: os pedidos já estão gravados, falha vira aviso
        for c in criados:
            marcados = lotes_por_token.get(c.get("token"))
            if not marcados or c.get("origem") != envios.ORIGEM_XML:
                continue
            try:
                envio = envios.buscar_envio(conn, c["id"], c.get("cnpj_embarcador") or _empresa_envio()['cnpj'])
                emb_lotes = lotes_cliente.embarcador_wms(conn, envio["cnpj_embarcador"], _CONFIG)
                if emb_lotes is None:
                    continue
                produtos = envios.ler_nfe(envios.caminho_xml(envio).read_bytes())["produtos"]
                r_lotes = lotes_cliente.gravar_lotes_da_nota(conn, emb_lotes, envio["numero_nf"], marcados, produtos, _quem_envia())
                lotes_gravados += r_lotes["gravados"]
                lotes_avisos += r_lotes["avisos"]
            except lotes_cliente.ErroLote as e:
                lotes_avisos.append(str(e))
            except Exception:
                conn.rollback()
                logger.exception(f"lotes do envio {c['id']} falharam")
                lotes_avisos.append(f"NF {c.get('numero_nf')}: o lote indicado não foi gravado. Fale com a Fresh Log.")
        # Os pedidos ja estao gravados: documento que falhar nao derruba o envio,
        # vira aviso (o cliente anexa de novo pela lista).
        for c in criados:
            lista = anexos_por_token.get(c.pop("token", None))
            if not lista:
                continue
            try:
                anexos_pedido.anexar(conn, envios.buscar_envio(conn, c["id"], c.get("cnpj_embarcador") or _empresa_envio()['cnpj']),
                                     lista, _quem_envia(), dono=g.cliente["cnpj"])
                conn.commit()
            except Exception as e:
                conn.rollback()
                logger.exception(f"anexos do envio {c['id']} falharam")
                anexos_falhas.append(str(e) if isinstance(e, envios.ErroEnvio)
                                     else f"{c.get('numero_nf') or c.get('referencia')}: documentos não anexados.")
        # Área não atendida (Hugo, 23/09): 1 chamado no chat + e-mails; a
        # linha fica AGUARDANDO_LIBERACAO até a equipe liberar no painel.
        bloqueados = [c for c in criados if c.get("status") == envios.STATUS_AGUARDANDO_LIBERACAO]
        if bloqueados:
            import bloqueio_area
            # CNPJ do LOGIN (g.cliente), não da empresa do pedido: o widget do
            # chat busca o chamado pelo CNPJ logado (login de grupo -- revisão 23/09)
            for cnpj in dict.fromkeys(c.get("cnpj_embarcador") for c in bloqueados):
                emp = (grupo or {}).get(cnpj) or _empresa_envio()
                bloqueio_area.abrir_bloqueio(conn, {"cnpj": g.cliente["cnpj"], "sender_id": emp.get("sender_id"), "nome": emp.get("nome")},
                                             [c for c in criados if c.get("cnpj_embarcador") == cnpj], _CONFIG)
    except envios.ErroEnvio as e:
        return _json_erro_envio(e)
    except Exception as e:
        logger.exception("confirmar_envios falhou")
        return jsonify({"erro": f"Não foi possível registrar os pedidos ({type(e).__name__})."}), 500
    finally:
        conn.close()
    logger.info(f"confirmar cnpj={','.join(dict.fromkeys(c.get('cnpj_embarcador') or _empresa_envio()['cnpj'] for c in criados))} "
                f"por={_quem_envia()} n={len(criados)}")
    n = len(criados)
    if bloqueados:
        b = len(bloqueados)
        mensagem = (f"{n} pedido{'s' if n > 1 else ''} recebido{'s' if n > 1 else ''}. "
                    f"{b} {'ficaram' if b > 1 else 'ficou'} aguardando liberação por região não atendida -- veja a lista abaixo.")
    else:
        mensagem = (f"{n} pedido{'s' if n > 1 else ''} recebido{'s' if n > 1 else ''} com sucesso. "
                    f"A criação na Stokki pode levar alguns instantes -- acompanhe o status na lista abaixo.")
    if anexos_falhas:
        mensagem += " Atenção: " + " ".join(anexos_falhas) + " Anexe de novo pela lista abaixo (Documentos)."
    if lotes_gravados:
        mensagem += f" Lote indicado em {lotes_gravados} produto{'s' if lotes_gravados > 1 else ''}."
    if lotes_avisos:
        mensagem += " Lotes: " + " ".join(lotes_avisos)
    return jsonify({"ok": True, "criados": criados, "bloqueados": bloqueados, "mensagem": mensagem, "anexos_falhas": anexos_falhas,
                    "lotes_gravados": lotes_gravados, "lotes_avisos": lotes_avisos})


@app.route("/api/envios/reenviar-erros", methods=["POST"])
@requer_cliente
@exige_mesma_origem
def api_envios_reenviar_erros():
    """Reenvia de uma vez todos os pedidos com erro da empresa (Hugo, 02/10)."""
    _exige_pode_enviar()
    conn = envios.conectar()
    try:
        r = {"na_fila": 0, "liberacao": 0}
        for cnpj in (_grupo_sem_seletor() or {_empresa_envio()['cnpj']: None}):
            for k, v in envios.reenviar_erros(conn, cnpj, _quem_envia()).items():
                r[k] += v
    except envios.ErroEnvio as e:
        return _json_erro_envio(e)
    finally:
        conn.close()
    logger.info(f"reenviar-erros cnpj={_empresa_envio()['cnpj']} por={_quem_envia()} -> {r}")
    n = r["na_fila"] + r["liberacao"]
    if not n:
        mensagem = "Nenhum pedido com erro pra reenviar."
    else:
        mensagem = f"{n} pedido{'s' if n > 1 else ''} reenviado{'s' if n > 1 else ''}"
        mensagem += f": {r['na_fila']} de volta à fila" if r["liberacao"] else " -- de volta à fila, serão enviados à Stokki em instantes"
        if r["liberacao"]:
            mensagem += f" e {r['liberacao']} aguardando liberação da Fresh Log (região não atendida)"
        mensagem += "."
    return jsonify({"ok": True, **r, "mensagem": mensagem})


@app.route("/api/envios/<int:envio_id>/acao", methods=["POST"])
@requer_cliente
@exige_mesma_origem
def api_envios_acao(envio_id):
    """Cancelar / tirar da rota / reagendar / reenviar / retomar (item 12). O que
    não dá pra aplicar sozinho vira solicitação pra operação + e-mail."""
    _exige_pode_enviar()
    corpo = request.get_json(silent=True) or {}
    tipo = corpo.get("tipo")
    # cada troca consulta CEP, geocodifica e escreve no chamado: mesmo limite por minuto do CEP
    if tipo == "alterar_endereco" and _cep_acima_do_limite(g.cliente["cnpj"]):
        return jsonify({"erro": "Muitas tentativas. Tente de novo em um minuto."}), 429
    conn = envios.conectar()
    try:
        envio = envios.buscar_envio(conn, envio_id, _empresa_envio()['cnpj'])
        if not envio:
            return jsonify({"erro": "Pedido não encontrado."}), 404
        if tipo == "alterar_endereco":
            # pedido barrado pela area (13/11): troca o endereco e reavalia a regiao
            resultado = envios.alterar_endereco_bloqueado(conn, envio, corpo.get("endereco"), _CONFIG, _quem_envia(), corpo)
        else:
            resultado = envios.aplicar_acao(conn, envio, tipo, corpo, _quem_envia())
    except envios.ForaDiaFixo as e:
        # regiao nova com outro dia de visita (16/11): a tela pede nova data ou a confirmacao
        return jsonify({"erro": str(e), "fora_dia_fixo": e.aviso}), 409
    except envios.ErroEnvio as e:
        return _json_erro_envio(e)
    finally:
        conn.close()
    logger.info(f"acao cnpj={_empresa_envio()['cnpj']} envio={envio_id} tipo={tipo} por={_quem_envia()} -> {resultado}")
    if tipo == "alterar_endereco" and envio.get("bloqueio_chamado_id"):
        import bloqueio_area
        bloqueio_area.avisar_endereco_alterado(envio, resultado, _quem_envia())
    if resultado.get("precisa_operacao"):
        _avisar_operacao_solicitacao(envio, tipo, corpo)
    # cancelamento de envio bloqueado por área (23/09): registra no chamado do chat
    if tipo == "cancelar" and resultado.get("aplicado") and envio.get("bloqueio_chamado_id"):
        import bloqueio_area
        bloqueio_area.avisar_cancelamento(envio, _quem_envia())
    return jsonify({"ok": True, **resultado})


def _avisar_operacao_solicitacao(envio: dict, tipo: str, corpo: dict) -> None:
    email_cfg = _CONFIG.get("email", {}) or {}
    destino = email_cfg.get("email_atendimento") or email_cfg.get("email_responsavel")
    if not destino:
        return
    rotulo = envios.ROTULOS_SOLICITACAO.get(tipo, tipo)
    # tudo que o cliente digitou ou veio do XML dele entra escapado no HTML
    esc = lambda v: html_mod.escape(str(v if v is not None else ""))  # noqa: E731
    detalhe = ""
    if tipo == "reagendar":
        detalhe = f"<p>Nova data: <b>{esc('/'.join(reversed(str(corpo.get('data') or '').split('-'))))} "
        detalhe += f"{esc(corpo.get('hora_inicio') or '')}–{esc(corpo.get('hora_fim') or '')}</b></p>"
    if corpo.get("motivo"):
        detalhe += f"<p>Motivo informado: {esc(str(corpo['motivo'])[:300])}</p>"
    html = envelope_html(
        f"<p><b>{esc(_empresa_envio()['nome'])}</b> (CNPJ {esc(_empresa_envio()['cnpj_formatado'])}) pediu pelo portal: <b>{esc(rotulo)}</b>.</p>"
        f"<p>Pedido {esc(envio.get('codigo_pedido') or '(código ainda não identificado)')} · NF {esc(envio.get('numero_nf'))} · "
        f"{esc(envio.get('destinatario_nome'))} · situação no portal: {esc(envios.ROTULOS_STATUS.get(envio['status'], envio['status']))}.</p>"
        f"{detalhe}<p>Ao concluir, marque a solicitação como atendida: "
        f"<code>portal_cliente/gerenciar_clientes.py solicitacoes</code>.</p>",
        rodape="Fresh Log · Portal do cliente · solicitação", cor_acento="#F5A623")
    enviar_email([destino], f"[Portal] {rotulo} · NF {envio.get('numero_nf')} · {_empresa_envio()['nome']}", html, email_cfg)


@app.route("/api/envios/<int:envio_id>/xml")
@app.route("/api/envios/<int:envio_id>/arquivo")
@requer_cliente
def api_envios_xml(envio_id):
    """Arquivo original do envio: o XML da NF-e ou a planilha de onde o
    pedido foi lido (09/09)."""
    conn = envios.conectar()
    try:
        envio = envios.buscar_envio(conn, envio_id, _empresa_envio()['cnpj'])
    finally:
        conn.close()
    if not envio:
        abort(404)
    caminho = envios.caminho_xml(envio)
    if not caminho.is_file():
        abort(404)
    if (envio.get("origem") or envios.ORIGEM_XML) == envios.ORIGEM_PLANILHA:
        tipos = {".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xls": "application/vnd.ms-excel"}
        return send_file(caminho, mimetype=tipos.get(caminho.suffix.lower(), "application/octet-stream"), as_attachment=True,
                         download_name=caminho.name.split("_", 3)[-1] if caminho.name.count("_") >= 3 else caminho.name, max_age=0)
    return send_file(caminho, mimetype="application/xml", as_attachment=True, download_name=f"{envio['chave_nfe']}.xml", max_age=0)


# ── Pedidos de Entrada (25/09) ─────────────────────────────────────────────────

def _exige_entradas_ativas(conn) -> None:
    if not entradas_mod.config_entradas_cliente(conn, _empresa_envio()["cnpj"])["entradas_ativo"]:
        abort(Response(jsonify({"erro": "A aba Pedidos de Entrada não está liberada pra esta empresa."}).get_data(),
                       status=403, mimetype="application/json"))


@app.route("/api/entradas")
@requer_cliente
def api_entradas_listar():
    conn = entradas_mod.conectar()
    try:
        _exige_entradas_ativas(conn)
        lista = entradas_mod.listar_entradas(conn, _empresa_envio()["cnpj"])
    finally:
        conn.close()
    return jsonify({"entradas": lista, "resumo": entradas_mod.resumo_entradas(lista), "dias": entradas_mod.DIAS_LISTAGEM,
                    "hoje": date.today().isoformat(), "atualizado_em": datetime.now().strftime("%H:%M")})


@app.route("/api/entradas/analisar", methods=["POST"])
@requer_cliente
@exige_mesma_origem
def api_entradas_analisar():
    """Recebe .xml/.zip/.xlsx, lê cada NF-e de remessa ou remessa da
    planilha, valida e devolve a prévia. Nada entra na fila ainda."""
    _exige_pode_enviar()
    arquivos = request.files.getlist("arquivos")
    if not arquivos:
        return jsonify({"erro": "Selecione pelo menos um arquivo: XML da NF-e de remessa (ou ZIP) ou planilha .xlsx no modelo Fresh Log de entrada."}), 400
    itens, rejeitados, vistos = [], [], set()
    conn = entradas_mod.conectar()
    try:
        _exige_entradas_ativas(conn)
        cnpj = _empresa_envio()["cnpj"]
        # Abre tudo primeiro e confere o teto ANTES de guardar qualquer temporário (revisão 23/11, B4):
        # sem isso um ZIP cheio de XMLs enche _temporarios (limpeza só a cada ciclo do worker).
        expandidos, total_arquivos, total_bytes = [], 0, 0
        for f in arquivos:
            try:
                partes = envios.expandir_upload(f.filename or "", f.read())
            except envios.ErroEnvio as e:
                rejeitados.append({"arquivo": f.filename or "arquivo", "erro": str(e)})
                continue
            total_arquivos += len(partes)
            total_bytes += sum(len(c) for _, c in partes)
            if total_arquivos > entradas_mod.MAX_ARQUIVOS_UPLOAD or total_bytes > entradas_mod.MAX_BYTES_UPLOAD:
                return jsonify({"erro": f"Muitos arquivos de uma vez (máximo {entradas_mod.MAX_ARQUIVOS_UPLOAD} arquivos "
                                        f"e {entradas_mod.MAX_BYTES_UPLOAD // (1024 * 1024)} MB). Envie em partes."}), 400
            expandidos.append(partes)
        for partes in expandidos:
            for nome, conteudo in partes:
                if envios.e_planilha(nome, conteudo):
                    try:
                        remessas, rej = entradas_mod.ler_planilha_entrada(conteudo, nome, cnpj)
                    except envios.ErroEnvio as e:
                        rejeitados.append({"arquivo": nome, "erro": str(e)})
                        continue
                    rejeitados.extend(rej)
                    token_plan = envios.guardar_temporario(conteudo, Path(nome).suffix.lstrip(".") or "xlsx")
                    lidos = [(entradas_mod.rotulo_entrada(r), r, lambda r=r: envios.guardar_temporario_pedido(r, token_plan)) for r in remessas]
                else:
                    try:
                        nfe = entradas_mod.ler_nfe_entrada(conteudo, nome)
                    except envios.ErroEnvio as e:
                        rejeitados.append({"arquivo": nome, "erro": str(e)})
                        continue
                    lidos = [(entradas_mod.rotulo_entrada(nfe), nfe, lambda c=conteudo: envios.guardar_temporario(c))]
                for rotulo, item, guardar in lidos:
                    if item["chave_nfe"] in vistos:
                        rejeitados.append({"arquivo": nome, "rotulo": rotulo, "erro": "Repetida dentro do mesmo envio."})
                        continue
                    vistos.add(item["chave_nfe"])
                    v = entradas_mod.validar_entrada(conn, item, cnpj, _CONFIG, _outras_empresas_envio())
                    if not v["ok"]:
                        rejeitados.append({"arquivo": nome, "rotulo": rotulo, "erro": " ".join(v["erros"]),
                                           "conferencia": v["conferencia"]})
                        continue
                    previa = {k: x for k, x in item.items() if k not in ("emitente_cnpj",)}
                    previa.update({"token": guardar(), "avisos": v["avisos"], "rotulo": rotulo, "conferencia": v["conferencia"]})
                    itens.append(previa)
    except envios.ErroEnvio as e:
        return _json_erro_envio(e)
    finally:
        conn.close()
    logger.info(f"entradas.analisar cnpj={_empresa_envio()['cnpj']} por={_quem_envia()} validos={len(itens)} rejeitados={len(rejeitados)}")
    return jsonify({"itens": itens, "rejeitados": rejeitados, "hoje": date.today().isoformat()})


@app.route("/api/entradas/modelo-planilha")
@requer_cliente
def api_entradas_modelo_planilha():
    conn = entradas_mod.conectar()
    try:
        _exige_entradas_ativas(conn)
    finally:
        conn.close()
    conteudo = entradas_mod.gerar_modelo_planilha_entrada(_empresa_envio()["nome"] or "")
    return send_file(io.BytesIO(conteudo), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True, download_name="modelo_entradas_freshlog.xlsx", max_age=0)


@app.route("/api/entradas/confirmar", methods=["POST"])
@requer_cliente
@exige_mesma_origem
def api_entradas_confirmar():
    _exige_pode_enviar()
    corpo = request.get_json(silent=True) or {}
    itens = corpo.get("itens") or []
    if not isinstance(itens, list) or not itens:
        return jsonify({"erro": "Nenhuma remessa pra confirmar."}), 400
    if len(itens) > 200:
        return jsonify({"erro": "Anuncie no máximo 200 notas por vez."}), 400
    conn = entradas_mod.conectar()
    try:
        _exige_entradas_ativas(conn)
        criados = entradas_mod.confirmar_entradas(conn, _empresa_envio()["cnpj"], itens, _quem_envia(), _CONFIG)
    except envios.ErroEnvio as e:
        return _json_erro_envio(e)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("confirmar_entradas falhou")
        return jsonify({"erro": f"Não foi possível registrar as entradas ({type(e).__name__})."}), 500
    finally:
        conn.close()
    n = len(criados)
    logger.info(f"entradas.confirmar cnpj={_empresa_envio()['cnpj']} por={_quem_envia()} n={n}")
    return jsonify({"ok": True, "criados": criados,
                    "mensagem": f"{n} entrada{'s' if n > 1 else ''} anunciada{'s' if n > 1 else ''}. "
                                f"O recebimento é criado na Stokki em instantes -- acompanhe o status na lista."})


@app.route("/api/entradas/<int:entrada_id>/cancelar", methods=["POST"])
@requer_cliente
@exige_mesma_origem
def api_entradas_cancelar(entrada_id):
    _exige_pode_enviar()
    conn = entradas_mod.conectar()
    try:
        _exige_entradas_ativas(conn)
        entrada = entradas_mod.buscar_entrada(conn, entrada_id, _empresa_envio()["cnpj"])
        if not entrada:
            return jsonify({"erro": "Entrada não encontrada."}), 404
        emp = _empresa_envio()
        corpo = request.get_json(silent=True) or {}
        motivo = (corpo.get("motivo") or "")[:300]
        # CNPJ do LOGIN (g.cliente), como bloqueio_area: o chat busca o chamado pelo CNPJ logado
        r = entradas_mod.cancelar_entrada(conn, entrada, _quem_envia(),
                                          {"cnpj": g.cliente["cnpj"], "sender_id": emp.get("sender_id"), "nome": emp.get("nome")}, _CONFIG,
                                          motivo=motivo)
    except envios.ErroEnvio as e:
        return _json_erro_envio(e)
    finally:
        conn.close()
    logger.info(f"entradas.cancelar cnpj={_empresa_envio()['cnpj']} entrada={entrada_id} por={_quem_envia()} -> {r}")
    return jsonify({"ok": True, **r})


@app.route("/api/entradas/<int:entrada_id>/arquivo")
@requer_cliente
def api_entradas_arquivo(entrada_id):
    conn = entradas_mod.conectar()
    try:
        _exige_entradas_ativas(conn)
        entrada = entradas_mod.buscar_entrada(conn, entrada_id, _empresa_envio()["cnpj"])
    finally:
        conn.close()
    if not entrada:
        abort(404)
    caminho = entradas_mod.caminho_arquivo(entrada)
    if not caminho.is_file():
        abort(404)
    if entrada["origem"] == envios.ORIGEM_PLANILHA:
        tipos = {".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xls": "application/vnd.ms-excel"}
        return send_file(caminho, mimetype=tipos.get(caminho.suffix.lower(), "application/octet-stream"), as_attachment=True,
                         download_name=caminho.name.split("_", 3)[-1] if caminho.name.count("_") >= 3 else caminho.name, max_age=0)
    return send_file(caminho, mimetype="application/xml", as_attachment=True, download_name=f"{entrada['chave_nfe']}.xml", max_age=0)


@app.route("/api/destinatarios", methods=["POST"])
@requer_cliente
@exige_mesma_origem
def api_destinatarios_gravar():
    """Horário de recebimento (e flag de agendamento) de um destinatário --
    gravado uma vez por CNPJ/CPF e repassado à roteirização."""
    _exige_pode_enviar()
    corpo = request.get_json(silent=True) or {}
    conn = envios.conectar()
    try:
        envios.gravar_destinatario(conn, _empresa_envio()['cnpj'], corpo.get("documento", ""), corpo.get("nome", ""),
                                   corpo.get("horario_inicio", ""), corpo.get("horario_fim", ""),
                                   bool(corpo.get("requer_agendamento")), _CONFIG)
    except envios.ErroEnvio as e:
        return _json_erro_envio(e)
    finally:
        conn.close()
    return jsonify({"ok": True})


# ── Exportação ─────────────────────────────────────────────────────────────────

_COLUNAS_XLSX = [
    ("codigo", "Pedido"), ("nf", "NF"), ("destinatario", "Destinatário"), ("endereco_completo", "Endereço"),
    ("volumes", "Volumes"), ("situacao_rotulo", "Situação"), ("tentativas_texto", "Tentativas"),
    ("motorista", "Motorista"), ("rota", "Rota"),
    ("ordem", "Parada"), ("detalhe", "Previsão / conclusão"), ("concluido_em", "Concluído às"),
    ("motivo", "Motivo do insucesso"), ("janela_atendimento", "Janela de atendimento"),
    ("telefone_destinatario", "Telefone do destinatário"), ("criado_em", "Data do pedido"), ("observacoes", "Observações"),
]


@app.route("/exportar.xlsx")
@requer_cliente
def exportar_xlsx():
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    data_alvo = _data_da_query()
    dias = _dias_da_query()
    d = (dados.montar_janela(g.cliente["sender_ids"], dias, _CONFIG) if dias
         else dados.montar_dia(g.cliente["sender_ids"], data_alvo, _CONFIG))
    pedidos = d["pedidos"] + d.get("agendados_futuros", [])
    empresa = request.args.get("empresa", "")   # filtro "Empresa" da tela (login de grupo)
    if empresa.isdigit() and int(empresa) in g.cliente["sender_ids"]:
        pedidos = [p for p in pedidos if p.get("sender_id") == int(empresa)]
    colunas = ([("data", "Data")] if dias else []) + _COLUNAS_XLSX
    if len(g.cliente["empresas"]) > 1:   # login de grupo: de qual empresa é cada pedido
        nomes = {e["sender_id"]: e["nome"] for e in g.cliente["empresas"]}
        pedidos = [{**p, "empresa": nomes.get(p.get("sender_id"), "")} for p in pedidos]
        colunas = [("empresa", "Empresa")] + colunas
    wb = Workbook()
    ws = wb.active
    ws.title = f"{dias} dias" if dias else data_alvo.strftime("%d-%m-%Y")
    ws.append([rotulo for _, rotulo in colunas])
    for c in ws[1]:
        c.font = Font(bold=True)
    for p in pedidos:
        ws.append([p.get(chave, "") if p.get(chave) is not None else "" for chave, _ in colunas])
    for i, (chave, rotulo) in enumerate(colunas, start=1):
        largura = max([len(rotulo)] + [len(str(p.get(chave) or "")) for p in pedidos] or [10])
        ws.column_dimensions[get_column_letter(i)].width = min(max(largura + 2, 10), 60)
    ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    nome = (f"pedidos_{g.cliente['cnpj']}_{d['periodo']['de']}_a_{d['periodo']['ate']}.xlsx" if dias
            else f"pedidos_{g.cliente['cnpj']}_{data_alvo.isoformat()}.xlsx")
    return send_file(buf, as_attachment=True, download_name=nome,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ── Atendimento (chat + chamados), 09/09 -- rotas em chamados_web.py ──────────
import chamados_web
chamados_web.registrar(app, requer_cliente=requer_cliente, exige_mesma_origem=exige_mesma_origem, config=_CONFIG)

# ── Calculadora de frete dedicado, 11/09 -- rotas em cotacao_web.py ──────────
import cotacao_web
cotacao_web.registrar(app, requer_cliente=requer_cliente, exige_mesma_origem=exige_mesma_origem, config=_CONFIG,
                      secret=_SECRET, url_base=_URL_BASE)

# ── Link público da consulta de transportadora (embarcador responde), 23/10 ──
import consulta_transportadora_web
consulta_transportadora_web.registrar(app, config=_CONFIG, secret=_SECRET)

# ── Link público do canhoto (e-mail de resumo diário), 17/09 -- link_canhoto.py ──
import link_canhoto
link_canhoto.registrar(app, config=_CONFIG, secret=_SECRET)

# ── Link público da ciência do dedicado fora do dia fixo, 17/11 -- link_ciencia.py ──
import link_ciencia
link_ciencia.registrar(app, config=_CONFIG, secret=_SECRET)


@app.errorhandler(404)
def _404(_e):
    if request.path.startswith("/api/"):
        return jsonify({"erro": "Não encontrado."}), 404
    return render_template("mensagem.html", titulo="Página não encontrada", texto="", voltar=url_for("inicio")), 404


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(_CFG.get("porta") or 8074), debug=False)
