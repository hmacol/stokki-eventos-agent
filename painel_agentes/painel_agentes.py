# -*- coding: utf-8 -*-
"""
painel_agentes.py

Painel web pra rodar e acompanhar os agentes do agente_stokki_eventos
-- pedido do Hugo, 03/08: "rodar e acompanhar o processamento numa
tela web mais amigável que o cmd".

Mesmo padrão do dashboard_embarcadores/: Flask + HTTP Basic Auth
(config.yaml -> painel_agentes.usuario/.senha), waitress em produção.

COMO USAR (desenvolvimento):
    py -3.11 painel_agentes.py
COMO USAR (produção):
    py -3.11 -m waitress --host=0.0.0.0 --port=8070 painel_agentes:app
"""
import hmac
import logging
import re
import sys
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

(Path(__file__).parent / "dados").mkdir(parents=True, exist_ok=True)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(Path(__file__).parent / "dados" / "painel_agentes.log", encoding="utf-8"),
    ],
)

from urllib.parse import urlparse

import yaml
from flask import (
    Flask, abort, g, redirect, render_template, request, url_for,
    jsonify, send_file, session,
)
from werkzeug.middleware.proxy_fix import ProxyFix

from agentes import AGENTES, buscar_agente, categorias_ordenadas
from executor import (
    iniciar_execucao, iniciar_sequencia, buscar_execucao, buscar_ultima_execucao,
    listar_execucoes_recentes, ha_execucao_rodando, ler_log, limpar_execucoes_travadas,
    encerrar_todas_execucoes,
)
from mapa_rotas import buscar_rotas_para_mapa
from laboratorio_rotas import buscar_dados_laboratorio
from planejamento_rotas import (
    buscar_dados_planejamento, buscar_pool_e_agendados, gerar_romaneio_pdf,
    carregar_documentos_do_rascunho, roteirizar_selecionados,
    alocar_motoristas_rascunhos, desalocar_motoristas_rascunhos, cancelar_pedido, reagendar_pedido,
    salvar_disponibilidade_dia, marcar_disponibilidade_periodo, limpar_disponibilidade_dia,
    ETAPAS_AGENTES_PLANEJAMENTO, montar_etapas_agentes_planejamento,
)
from motoristas import dados_pagina_motoristas, listar_agentes_vuupt_nao_cadastrados, cadastrar_motorista
import rascunhos_rota
import torre_controle
import tratativas

def _carregar_config() -> dict:
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


app = Flask(__name__)
# Permite o painel morar sob um prefixo (ex: app.freshhub.com.br/painel,
# atrás do Caddy com `handle_path` + `header_up X-Forwarded-Prefix`) --
# sem isso, url_for()/redirect() gerariam link pra raiz do domínio, não
# pro prefixo. x_for/x_proto/x_host: 1 hop de proxy confiável (Caddy).
# Sem proxy na frente (uso local direto, LAN), os cabeçalhos X-Forwarded-*
# não existem e isso vira um no-op -- não muda o comportamento atual.
app.wsgi_app = ProxyFix(app.wsgi_app, x_prefix=1, x_proto=1, x_for=1, x_host=1)

# Sessão de login (substitui o Basic Auth do navegador, 17/08 -- pedido do
# Hugo por uma tela de login de verdade + botão de sair). secret_key
# assina o cookie de sessão -- sem ele, a sessão não é criptograficamente
# segura contra forjar/adulterar. 14 dias: painel de uso operacional
# diário, não precisa logar de novo toda hora.
_cfg_inicial = _carregar_config()
_secret_key = _cfg_inicial.get("painel_agentes", {}).get("secret_key", "")
if not _secret_key:
    raise RuntimeError(
        "painel_agentes.secret_key ausente no config.yaml -- gere uma string "
        "aleatória forte (ex: python -c \"import secrets; print(secrets.token_hex(32))\") "
        "antes de subir o painel."
    )
app.secret_key = _secret_key
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=14)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# Roda uma vez, assim que o painel sobe -- destrava qualquer execução
# que ficou presa em RODANDO por causa de um encerramento à força do
# processo anterior (ex: Ctrl+C no meio de uma execução, pedido do
# Hugo 04/08). Nada pode estar genuinamente rodando nesse momento.
limpar_execucoes_travadas()


def _nivel_das_credenciais(usuario: str, senha: str, cfg_painel: dict):
    """Confere usuário/senha contra os três pares possíveis e devolve o
    nível de acesso correspondente ("total", "operador" ou "leitura"), ou
    None se não bateram com nenhum dos três."""
    if not usuario or not senha:
        return None
    usuario_total = cfg_painel.get("usuario")
    senha_total = cfg_painel.get("senha")
    if usuario_total and senha_total and hmac.compare_digest(usuario, usuario_total) \
            and hmac.compare_digest(senha, senha_total):
        return "total"
    usuario_operador = cfg_painel.get("usuario_operador")
    senha_operador = cfg_painel.get("senha_operador")
    if usuario_operador and senha_operador and hmac.compare_digest(usuario, usuario_operador) \
            and hmac.compare_digest(senha, senha_operador):
        return "operador"
    usuario_leitura = cfg_painel.get("usuario_leitura")
    senha_leitura = cfg_painel.get("senha_leitura")
    if usuario_leitura and senha_leitura and hmac.compare_digest(usuario, usuario_leitura) \
            and hmac.compare_digest(senha, senha_leitura):
        return "leitura"
    return None


def requer_auth(f=None, *, niveis=("total",)):
    """Login por sessão (cookie assinado) com três níveis: "total"
    (usuario/senha, acesso irrestrito), "operador" (usuario_operador/
    senha_operador, opera Torre de Controle, Planejamento de Rotas e
    cadastro de Motoristas, mas não roda agentes avulsos nem vê o
    Histórico) e "leitura" (usuario_leitura/senha_leitura, só as telas e
    APIs marcadas com niveis=(..., "leitura"), sem nenhum botão de ação).
    Rota sem `niveis` exige nível total. Pedido do Hugo, 13/08: time
    acompanha Torre e Planejamento sem poder disparar ações; nível
    "operador" adicionado 17/08 pra quem toca a operação do dia a dia sem
    precisar de acesso total. Trocado de Basic Auth pra tela de login de
    verdade + botão de sair, 17/08 -- Basic Auth não tem um jeito confiável
    de "deslogar" (o navegador guarda a senha até fechar/limpar cache)."""
    if f is not None:
        return requer_auth(niveis=niveis)(f)

    def decorator(func):
        @wraps(func)
        def decorado(*args, **kwargs):
            config = _carregar_config()
            cfg_painel = config.get("painel_agentes", {})
            if not cfg_painel.get("usuario") or not cfg_painel.get("senha"):
                return (
                    "Painel de agentes desabilitado: configure painel_agentes.usuario "
                    "e painel_agentes.senha no config.yaml antes de subir.", 500,
                )
            nivel = session.get("nivel_acesso")
            if nivel is None:
                if request.path.startswith(f"{request.script_root}/api/"):
                    return jsonify({"erro": "Sessão expirada -- faça login de novo."}), 401
                return redirect(url_for("login", proximo=request.script_root + request.full_path))
            if nivel not in niveis:
                abort(403, "Seu usuário não tem permissão pra essa ação.")
            g.nivel_acesso = nivel
            return func(*args, **kwargs)
        return decorado
    return decorator


def exige_mesma_origem(f):
    """
    Bloqueia POSTs cuja Origin/Referer não seja deste próprio host --
    proteção contra CSRF (achado da auditoria de 09/08: as rotas que
    disparam/derrubam agentes só tinham Basic Auth, que o navegador
    reanexa automaticamente a qualquer POST same-origin, inclusive um
    form auto-submit hospedado em outro site). Aplicado só nas rotas
    POST de ação -- não muda em nada o uso normal via navegador, que
    sempre manda Origin/Referer em um submit de formulário.
    """
    @wraps(f)
    def decorado(*args, **kwargs):
        origem = request.headers.get("Origin") or request.headers.get("Referer")
        if not origem or urlparse(origem).netloc != request.host:
            abort(403, "Origem da requisição não confere (proteção CSRF).")
        return f(*args, **kwargs)
    return decorado


def requer_token_impressao(f):
    """Autenticação por token fixo pro print-agent local (Fase 7, 17/08)
    -- é máquina-a-máquina (um script rodando via Agendador do Windows,
    sem navegador/usuário), então não faz sentido usar a sessão de login.
    Token comparado com hmac.compare_digest (mesmo cuidado contra timing
    attack já usado em _nivel_das_credenciais)."""
    @wraps(f)
    def decorado(*args, **kwargs):
        config = _carregar_config()
        token_esperado = config.get("painel_agentes", {}).get("token_impressao", "")
        token_recebido = request.headers.get("X-Token-Impressao", "")
        if not token_esperado or not hmac.compare_digest(token_recebido, token_esperado):
            abort(401, "Token de impressão inválido ou ausente.")
        return f(*args, **kwargs)
    return decorado


# roteirizacao/gerar_pdf_romaneios.py grava em roteirizacao/dados/romaneios/<AAAA-MM-DD>/*.pdf
# (PASTA_ROMANEIOS lá) -- essas rotas só SERVEM o que já foi gerado, nunca geram nada.
PASTA_ROMANEIOS_DIA = _RAIZ / "roteirizacao" / "dados" / "romaneios"
_PADRAO_DATA = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PADRAO_NOME_PDF = re.compile(r"^[\w\-.]+\.pdf$")


@app.route("/api/romaneios/pendentes")
@requer_token_impressao
def api_romaneios_pendentes():
    """Lista os romaneios já gerados HOJE -- o print-agent local pergunta
    isso periodicamente e decide, do lado dele, o que ainda não imprimiu
    (o controle do que já foi impresso fica só local, de propósito: essa
    VPS não sabe nem precisa saber o que já saiu fisicamente na impressora)."""
    hoje = date.today().isoformat()
    pasta = PASTA_ROMANEIOS_DIA / hoje
    romaneios = []
    if pasta.is_dir():
        for caminho in sorted(pasta.glob("*.pdf")):
            romaneios.append({
                "nome": caminho.name,
                "url": url_for("api_romaneio_pdf", data=hoje, nome_arquivo=caminho.name),
            })
    return jsonify({"data": hoje, "romaneios": romaneios})


@app.route("/api/romaneios/<data>/<nome_arquivo>")
@requer_token_impressao
def api_romaneio_pdf(data, nome_arquivo):
    """Serve o PDF de um romaneio já gerado. Valida `data`/`nome_arquivo`
    contra um padrão fixo antes de tocar no filesystem -- sem isso, um
    ".." no nome do arquivo vazaria pra fora de PASTA_ROMANEIOS_DIA
    (path traversal)."""
    if not _PADRAO_DATA.match(data) or not _PADRAO_NOME_PDF.match(nome_arquivo):
        abort(400, "Data ou nome de arquivo inválido.")
    caminho = PASTA_ROMANEIOS_DIA / data / nome_arquivo
    if not caminho.is_file():
        abort(404, "Romaneio não encontrado.")
    return send_file(caminho, mimetype="application/pdf", download_name=nome_arquivo)


@app.route("/login", methods=["GET", "POST"])
def login():
    erro = None
    if request.method == "POST":
        config = _carregar_config()
        cfg_painel = config.get("painel_agentes", {})
        usuario = request.form.get("usuario", "")
        senha = request.form.get("senha", "")
        nivel = _nivel_das_credenciais(usuario, senha, cfg_painel)
        if nivel is None:
            erro = "Usuário ou senha incorretos."
        else:
            session.clear()
            session.permanent = True
            session["nivel_acesso"] = nivel
            session["usuario"] = usuario
            proximo = request.form.get("proximo") or url_for("torre")
            # Só aceita redirecionar pra caminho relativo deste próprio
            # painel -- nunca pra outro domínio (open redirect).
            raiz = request.script_root or ""
            if not (proximo == raiz or proximo.startswith(raiz + "/")):
                proximo = url_for("torre")
            return redirect(proximo)
    return render_template("login.html", erro=erro, proximo=request.args.get("proximo", ""))


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@requer_auth
def index():
    agentes_por_categoria = {}
    ha_algo_rodando = False
    for agente in AGENTES:
        ultima = buscar_ultima_execucao(agente["id"])
        rodando = bool(ultima and ultima["status"] == "RODANDO")
        ha_algo_rodando = ha_algo_rodando or rodando
        item = {**agente, "ultima_execucao": ultima, "rodando": rodando}
        agentes_por_categoria.setdefault(agente["categoria"], []).append(item)

    categorias = [(c, agentes_por_categoria[c]) for c in categorias_ordenadas()]
    encerrado_param = request.args.get("encerrado")
    return render_template(
        "index.html", categorias=categorias, ha_algo_rodando=ha_algo_rodando,
        quantidade_encerrada=encerrado_param,
    )


@app.route("/rodar/<agente_id>", methods=["POST"])
@requer_auth
@exige_mesma_origem
def rodar(agente_id):
    agente = buscar_agente(agente_id)
    if not agente:
        return "Agente não encontrado.", 404
    if ha_execucao_rodando(agente_id):
        return "Esse agente já está rodando -- espera terminar antes de rodar de novo.", 409

    modo_teste = request.form.get("modo_teste") == "on"
    execucao_id = iniciar_execucao(agente, modo_teste)
    return redirect(url_for("execucao", execucao_id=execucao_id))


@app.route("/execucao/<int:execucao_id>")
@requer_auth
def execucao(execucao_id):
    exec_info = buscar_execucao(execucao_id)
    if not exec_info:
        return "Execução não encontrada.", 404
    agente = buscar_agente(exec_info["agente_id"])
    return render_template("execucao.html", execucao=exec_info, agente=agente)


@app.route("/execucao/<int:execucao_id>/status")
@requer_auth
def execucao_status(execucao_id):
    exec_info = buscar_execucao(execucao_id)
    if not exec_info:
        return jsonify({"erro": "não encontrada"}), 404
    return jsonify({
        "status": exec_info["status"],
        "codigo_saida": exec_info["codigo_saida"],
        "finalizado_em": exec_info["finalizado_em"],
        "log": ler_log(execucao_id),
    })


@app.route("/encerrar-tudo", methods=["POST"])
@requer_auth
@exige_mesma_origem
def encerrar_tudo():
    quantidade = encerrar_todas_execucoes()
    return redirect(url_for("index", encerrado=quantidade))


@app.route("/historico")
@requer_auth
def historico():
    execucoes = listar_execucoes_recentes(100)
    return render_template("historico.html", execucoes=execucoes)


@app.route("/mapa-rotas")
@requer_auth(niveis=("total", "operador", "leitura"))
def mapa_rotas():
    data_param = request.args.get("data")
    if data_param:
        try:
            data_alvo = datetime.strptime(data_param, "%Y-%m-%d").date()
        except ValueError:
            data_alvo = date.today() + timedelta(days=1)
    else:
        data_alvo = date.today() + timedelta(days=1)

    try:
        dados = buscar_rotas_para_mapa(data_alvo)
        erro = None
    except Exception as e:
        dados = None
        erro = str(e)

    return render_template(
        "mapa_rotas.html", dados=dados, erro=erro,
        data_alvo_input=data_alvo.strftime("%Y-%m-%d"),
    )


def _parse_data_param(padrao_amanha: bool = False) -> date:
    data_param = request.args.get("data")
    padrao = date.today() + timedelta(days=1) if padrao_amanha else date.today()
    if not data_param:
        return padrao
    try:
        return datetime.strptime(data_param, "%Y-%m-%d").date()
    except ValueError:
        return padrao


@app.route("/planejamento")
@requer_auth(niveis=("total", "operador", "leitura"))
def planejamento():
    data_alvo = _parse_data_param()
    try:
        dados = buscar_dados_planejamento(data_alvo)
        erro = None
    except Exception as e:
        logging.getLogger(__name__).exception("Falha ao montar dados de planejamento")
        dados = None
        erro = str(e)

    return render_template(
        "planejamento_rotas.html", dados=dados, erro=erro,
        data_alvo_input=data_alvo.isoformat(), pode_editar=g.nivel_acesso in ("total", "operador"),
    )


# IDs dos agentes acionáveis pela barra de botões do planejamento --
# usada tanto pra validar o agente_id recebido em /rodar (não deixa
# essa tela disparar qualquer agente do sistema, só os 4 dela) quanto
# como ordem de execução do "Executar tudo".
AGENTES_PLANEJAMENTO_IDS = tuple(e["agente_id"] for e in ETAPAS_AGENTES_PLANEJAMENTO)


@app.route("/api/planejamento/agentes/etapas")
@requer_auth(niveis=("total", "operador", "leitura"))
def api_planejamento_agentes_etapas():
    """Estado da barra de agentes do planejamento (leitura barata no
    SQLite) -- mesmo padrão de /api/torre/etapas, só que restrito aos
    4 agentes relevantes pra essa tela."""
    return jsonify({"etapas": montar_etapas_agentes_planejamento()})


@app.route("/api/planejamento/agentes/rodar", methods=["POST"])
@requer_auth(niveis=("total", "operador"))
@exige_mesma_origem
def api_planejamento_agentes_rodar():
    """Dispara um agente da barra do planejamento. Só a Importação
    aceita filtro (pedido/embarcador) -- vira argv extra pro
    pipeline.py (--pedido/--embarcador), sem precisar de uma entrada
    nova em agentes.py pra cada combinação de filtro possível."""
    body = request.get_json(force=True)
    agente_id = body.get("agente_id", "")
    if agente_id not in AGENTES_PLANEJAMENTO_IDS:
        return jsonify({"erro": "Esse agente não faz parte da barra do planejamento."}), 400
    agente = buscar_agente(agente_id)
    if not agente:
        return jsonify({"erro": "Agente não encontrado."}), 404
    if ha_execucao_rodando(agente_id):
        return jsonify({"erro": "Esse agente já está rodando -- espera terminar antes de rodar de novo."}), 409

    args_extra = None
    if agente_id == "somente_importacao":
        pedido = (body.get("pedido") or "").strip()
        embarcador = (body.get("embarcador") or "").strip()
        args_extra = []
        if pedido:
            args_extra += ["--pedido", pedido]
        if embarcador:
            args_extra += ["--embarcador", embarcador]
        args_extra = args_extra or None

    execucao_id = iniciar_execucao(agente, modo_teste=False, args_extra=args_extra)
    return jsonify({"ok": True, "execucao_id": execucao_id})


@app.route("/api/planejamento/agentes/rodar-tudo", methods=["POST"])
@requer_auth(niveis=("total", "operador"))
@exige_mesma_origem
def api_planejamento_agentes_rodar_tudo():
    """Botão "Executar tudo" da barra do planejamento: os 4 agentes em
    sequência (Importação sem filtro → Criar Rotas Diárias Rascunho →
    Incrementar Rotas → Gerar Romaneios), cada um esperando o anterior
    terminar (iniciar_sequencia)."""
    if any(ha_execucao_rodando(agente_id) for agente_id in AGENTES_PLANEJAMENTO_IDS):
        return jsonify({"erro": "Já tem uma etapa rodando -- espera terminar antes de rodar tudo."}), 409
    passos = [{"agente": buscar_agente(agente_id)} for agente_id in AGENTES_PLANEJAMENTO_IDS]
    iniciar_sequencia(passos)
    return jsonify({"ok": True})


@app.route("/laboratorio-rotas")
@requer_auth(niveis=("total", "operador", "leitura"))
def laboratorio_rotas():
    """Laboratório de comparação visual de esquemas de roteirização --
    pedido do Hugo, 14/08. 100% leitura (ver laboratorio_rotas.py)."""
    data_alvo = _parse_data_param()
    particao = request.args.get("particao", "Seco")
    usar_teste = request.args.get("teste") == "1"
    try:
        dados = buscar_dados_laboratorio(data_alvo, particao, usar_teste=usar_teste)
        erro = None
    except Exception as e:
        logging.getLogger(__name__).exception("Falha ao montar dados do laboratório de roteirização")
        dados = None
        erro = str(e)

    return render_template(
        "laboratorio_rotas.html", dados=dados, erro=erro,
        data_alvo_input=data_alvo.isoformat(), particao_input=particao, teste_input=usar_teste,
    )


@app.route("/historico-tratativas")
@requer_auth(niveis=("total", "operador", "leitura"))
def historico_tratativas():
    """Histórico de tratativas pesquisável por NF, PS, embarcador, cliente,
    motorista ou motivo -- pedido do Hugo, 14/08. 100% leitura (ver
    tratativas.py -- log de eventos alimentado pelo fluxo de insucesso e
    pela Torre de Controle)."""
    try:
        pagina = max(1, int(request.args.get("pagina", "1")))
    except ValueError:
        pagina = 1
    filtros = {
        "busca": request.args.get("busca", ""),
        "motorista": request.args.get("motorista", ""),
        "motivo": request.args.get("motivo", ""),
        "origem": request.args.get("origem", ""),
        "evento": request.args.get("evento", ""),
        "data_de": request.args.get("data_de", ""),
        "data_ate": request.args.get("data_ate", ""),
    }
    try:
        resultado = tratativas.buscar(filtros, pagina=pagina)
        erro = None
    except Exception as e:
        logging.getLogger(__name__).exception("Falha ao buscar histórico de tratativas")
        resultado = {"linhas": [], "total": 0, "pagina": 1, "total_paginas": 1}
        erro = str(e)

    return render_template(
        "historico_tratativas.html", resultado=resultado, erro=erro, filtros=filtros,
        motoristas=tratativas.valores_distintos("motorista_nome"),
        motivos=tratativas.valores_distintos("motivo_texto"),
        eventos=tratativas.EVENTOS_POR_PEDIDO,
    )


@app.route("/torre")
@requer_auth(niveis=("total", "operador", "leitura"))
def torre():
    """Torre de Controle (cockpit) -- pedido do Hugo, 12/08. A página
    sobe só com a casca; os dados chegam por /api/torre/* via JS (a
    coleta na VUUPT leva alguns segundos e não deve segurar o load)."""
    data_alvo = _parse_data_param()
    gmaps_key = _carregar_config().get("google_maps", {}).get("api_key", "")
    return render_template("torre_controle.html", data_alvo_input=data_alvo.isoformat(),
                           google_maps_key=gmaps_key, pode_editar=g.nivel_acesso in ("total", "operador"))


@app.route("/api/torre/dados")
@requer_auth(niveis=("total", "operador", "leitura"))
def api_torre_dados():
    data_alvo = _parse_data_param()
    try:
        return jsonify(torre_controle.buscar_dados_torre(data_alvo))
    except Exception as e:
        logging.getLogger(__name__).exception("Falha ao montar dados da torre")
        return jsonify({"erro": str(e)}), 500


@app.route("/api/torre/stokki")
@requer_auth(niveis=("total", "operador", "leitura"))
def api_torre_stokki():
    """Funil outbound da Stokki -- endpoint separado do resto porque tem
    cache próprio (TTL 5 min) e trava de sessão (não consulta ao vivo
    com agente rodando)."""
    return jsonify(torre_controle.buscar_funil_stokki())


@app.route("/api/torre/etapas")
@requer_auth(niveis=("total", "operador", "leitura"))
def api_torre_etapas():
    """Só o estado das etapas do stepper (leitura barata no SQLite) --
    o front consulta com frequência maior pra dar feedback rápido
    depois de um clique em 'rodar'."""
    return jsonify({"etapas": torre_controle.montar_etapas_pipeline()})


@app.route("/api/torre/rodar", methods=["POST"])
@requer_auth(niveis=("total", "operador"))
@exige_mesma_origem
def api_torre_rodar():
    """Versão JSON do /rodar/<agente_id> pros botões da torre -- mesma
    iniciar_execucao, mas sem redirect (o cockpit continua na própria
    tela acompanhando pelo stepper)."""
    body = request.get_json(force=True)
    agente_id = body.get("agente_id", "")
    agente = buscar_agente(agente_id)
    if not agente:
        return jsonify({"erro": "Agente não encontrado."}), 404
    if ha_execucao_rodando(agente_id):
        return jsonify({"erro": "Esse agente já está rodando -- espera terminar antes de rodar de novo."}), 409
    execucao_id = iniciar_execucao(agente, modo_teste=False)
    return jsonify({"ok": True, "execucao_id": execucao_id})


@app.route("/api/torre/tratar", methods=["POST"])
@requer_auth(niveis=("total", "operador"))
@exige_mesma_origem
def api_torre_tratar():
    """Marca uma exceção da fila como tratada (com motivo) -- ela sai
    da fila ativa e vai pro histórico de tratadas (padrão OCC)."""
    body = request.get_json(force=True)
    try:
        torre_controle.marcar_excecao_tratada(
            body["id"], body.get("data_alvo", ""), body.get("tipo", ""),
            body.get("descricao", ""), body.get("motivo", ""),
            motorista_nome=body.get("motorista"), rota_nome=body.get("rota"),
        )
    except KeyError as e:
        return jsonify({"erro": f"campo obrigatório ausente: {e}"}), 400
    return jsonify({"ok": True})


@app.route("/api/torre/destratar", methods=["POST"])
@requer_auth(niveis=("total", "operador"))
@exige_mesma_origem
def api_torre_destratar():
    body = request.get_json(force=True)
    try:
        desfez = torre_controle.desfazer_excecao_tratada(body["id"])
    except KeyError as e:
        return jsonify({"erro": f"campo obrigatório ausente: {e}"}), 400
    return jsonify({"ok": True, "desfeito": desfez})


@app.route("/api/torre/duplicar", methods=["POST"])
@requer_auth(niveis=("total", "operador"))
@exige_mesma_origem
def api_torre_duplicar():
    """Botão 'Duplicar pedido' da fila de ação -- cria a reentrega no
    VUUPT na hora, mesma lógica do fluxo automático por e-mail."""
    body = request.get_json(force=True)
    try:
        service_id = int(body["service_id"])
        codigo = body["codigo"]
    except (KeyError, TypeError, ValueError):
        return jsonify({"erro": "service_id/codigo ausente ou inválido."}), 400
    resultado = torre_controle.duplicar_pedido_manual(
        service_id, codigo, motorista=body.get("motorista"), rota=body.get("rota"))
    if not resultado.get("ok"):
        return jsonify({"erro": resultado.get("erro", "Falha ao duplicar.")}), 400
    return jsonify(resultado)


@app.route("/api/planejamento/pool")
@requer_auth(niveis=("total", "operador", "leitura"))
def api_pool():
    """Busca ao vivo na VUUPT o pool de não alocados + resumo dos
    pedidos agendados (botão 'Atualizar' da tela) -- não mexe nos
    rascunhos/mapa já carregados, pra não perder o estado de edição em
    andamento. O resumo volta já renderizado (mesmo partial
    _resumo_agendados.html do load da página), a tela só troca o
    innerHTML do container."""
    data_alvo = _parse_data_param()
    try:
        resultado = buscar_pool_e_agendados(data_alvo)
    except Exception as e:
        return jsonify({"erro": str(e)}), 500
    return jsonify({
        "pool": resultado["pool"],
        "resumo_html": render_template("_resumo_agendados.html", resumo_agendados=resultado["resumo_agendados"]),
    })


@app.route("/api/planejamento/romaneio/<int:rascunho_id>")
@requer_auth(niveis=("total", "operador", "leitura"))
def api_romaneio(rascunho_id):
    """Gera (sempre fresco, reflete o estado atual do rascunho) e serve
    o PDF de romaneio -- botão 'Imprimir rota', mesmo motor de
    roteirizacao/gerar_pdf_romaneios.py (capa + NFs + boletos +
    canhoteira) aplicado direto sobre o rascunho local."""
    try:
        caminho = gerar_romaneio_pdf(rascunho_id)
    except ValueError as e:
        return str(e), 404
    except Exception as e:
        logging.getLogger(__name__).exception(f"Falha ao gerar romaneio do rascunho {rascunho_id}")
        return f"Falha ao gerar romaneio: {e}", 500
    return send_file(caminho, mimetype="application/pdf", download_name=caminho.name)


@app.route("/api/planejamento/carregar-documentos", methods=["POST"])
@requer_auth(niveis=("total", "operador"))
@exige_mesma_origem
def api_carregar_documentos():
    """Busca NF/boleto na hora (e-mail + Stokki) pros pedidos do
    rascunho, escopado só a ele -- chamado antes de abrir o romaneio
    (botão 'Imprimir rota'), porque o job agendado de documentos só
    roda às 18h. Pode levar até ~1 min (abre navegador pra cada pedido
    na Stokki)."""
    body = request.get_json(force=True)
    try:
        rascunho_id = body["rascunho_id"]
        contadores = carregar_documentos_do_rascunho(rascunho_id)
    except (KeyError, ValueError) as e:
        return jsonify({"erro": str(e)}), 400
    except Exception as e:
        logging.getLogger(__name__).exception(f"Falha ao carregar documentos do rascunho {body.get('rascunho_id')}")
        return jsonify({"erro": str(e)}), 500
    return jsonify({"ok": True, "contadores": contadores})


def _rascunho_ou_404(rascunho_id):
    rascunho = rascunhos_rota.buscar_rascunho(rascunho_id)
    if not rascunho:
        return None
    return rascunho


@app.route("/api/planejamento/mover-parada", methods=["POST"])
@requer_auth(niveis=("total", "operador"))
@exige_mesma_origem
def api_mover_parada():
    body = request.get_json(force=True)
    try:
        rascunhos_rota.mover_parada(
            body["service_id"], body["rascunho_origem_id"],
            body["rascunho_destino_id"], body["nova_ordem"],
        )
    except (KeyError, ValueError) as e:
        return jsonify({"erro": str(e)}), 400
    return jsonify({
        "ok": True,
        "rascunho_origem": _rascunho_ou_404(body["rascunho_origem_id"]),
        "rascunho_destino": _rascunho_ou_404(body["rascunho_destino_id"]),
    })


@app.route("/api/planejamento/reordenar", methods=["POST"])
@requer_auth(niveis=("total", "operador"))
@exige_mesma_origem
def api_reordenar():
    body = request.get_json(force=True)
    try:
        rascunhos_rota.reordenar_paradas(body["rascunho_id"], body["ordem_service_ids"])
    except (KeyError, ValueError) as e:
        return jsonify({"erro": str(e)}), 400
    return jsonify({"ok": True, "rascunho": _rascunho_ou_404(body["rascunho_id"])})


@app.route("/api/planejamento/inverter-rota", methods=["POST"])
@requer_auth(niveis=("total", "operador"))
@exige_mesma_origem
def api_inverter_rota():
    """Botão "Inverter rota" do card -- gira a ordem de execução das
    paradas de trás pra frente (Hugo, 17/08)."""
    body = request.get_json(force=True)
    try:
        rascunhos_rota.inverter_ordem(body["rascunho_id"])
    except (KeyError, ValueError) as e:
        return jsonify({"erro": str(e)}), 400
    return jsonify({"ok": True, "rascunho": _rascunho_ou_404(body["rascunho_id"])})


@app.route("/api/planejamento/mover-paradas", methods=["POST"])
@requer_auth(niveis=("total", "operador"))
@exige_mesma_origem
def api_mover_paradas():
    """Seleção múltipla de paradas (de uma ou mais rotas) pra mover
    tudo de uma vez pro mesmo rascunho destino -- botão "Mover
    selecionados" da tela (Hugo, 17/08). Cada item de `itens` já vem
    com a rota de origem (mesmo formato de mover-parada, só que em
    lote)."""
    body = request.get_json(force=True)
    try:
        itens = body["itens"]
        rascunho_destino_id = body["rascunho_destino_id"]
        if not itens:
            return jsonify({"erro": "Nenhuma parada selecionada."}), 400
        movidas = rascunhos_rota.mover_paradas(itens, rascunho_destino_id)
    except (KeyError, ValueError) as e:
        return jsonify({"erro": str(e)}), 400
    origens_afetadas = sorted({int(item["rascunho_origem_id"]) for item in itens} - {int(rascunho_destino_id)})
    return jsonify({
        "ok": True, "movidas": movidas,
        "rascunho_destino": _rascunho_ou_404(rascunho_destino_id),
        "rascunhos_origem": [_rascunho_ou_404(rid) for rid in origens_afetadas],
    })


@app.route("/api/planejamento/fundir-rotas", methods=["POST"])
@requer_auth(niveis=("total", "operador"))
@exige_mesma_origem
def api_fundir_rotas():
    """Funde um rascunho no outro -- botão "Fundir com" do card (Hugo,
    17/08): todas as paradas da rota ORIGEM passam pra rota DESTINO e
    a ORIGEM vira DESCARTADO."""
    body = request.get_json(force=True)
    try:
        resultado = rascunhos_rota.fundir_rascunhos(body["rascunho_origem_id"], body["rascunho_destino_id"])
    except (KeyError, ValueError) as e:
        return jsonify({"erro": str(e)}), 400
    return jsonify({"ok": True, "rascunho": _rascunho_ou_404(body["rascunho_destino_id"]), **resultado})


@app.route("/api/planejamento/remover-parada", methods=["POST"])
@requer_auth(niveis=("total", "operador"))
@exige_mesma_origem
def api_remover_parada():
    body = request.get_json(force=True)
    try:
        parada_removida = rascunhos_rota.remover_parada(body["rascunho_id"], body["service_id"])
    except (KeyError, ValueError) as e:
        return jsonify({"erro": str(e)}), 400
    return jsonify({
        "ok": True,
        "rascunho": _rascunho_ou_404(body["rascunho_id"]),
        "parada_removida": parada_removida,
    })


@app.route("/api/planejamento/adicionar-parada", methods=["POST"])
@requer_auth(niveis=("total", "operador"))
@exige_mesma_origem
def api_adicionar_parada():
    body = request.get_json(force=True)
    try:
        rascunhos_rota.adicionar_parada(body["rascunho_id"], body["parada"], body.get("ordem"))
    except (KeyError, ValueError) as e:
        return jsonify({"erro": str(e)}), 400
    return jsonify({"ok": True, "rascunho": _rascunho_ou_404(body["rascunho_id"])})


@app.route("/api/planejamento/trocar-motorista", methods=["POST"])
@requer_auth(niveis=("total", "operador"))
@exige_mesma_origem
def api_trocar_motorista():
    body = request.get_json(force=True)
    try:
        rascunhos_rota.trocar_motorista(
            body["rascunho_id"], body.get("agent_id"), body.get("vehicle_id"), body.get("motorista_nome"),
        )
    except (KeyError, ValueError) as e:
        return jsonify({"erro": str(e)}), 400
    return jsonify({"ok": True, "rascunho": _rascunho_ou_404(body["rascunho_id"])})


@app.route("/api/planejamento/renomear-rota", methods=["POST"])
@requer_auth(niveis=("total", "operador"))
@exige_mesma_origem
def api_renomear_rota():
    body = request.get_json(force=True)
    try:
        rascunhos_rota.renomear_rascunho(body["rascunho_id"], body["nome"])
    except (KeyError, ValueError) as e:
        return jsonify({"erro": str(e)}), 400
    return jsonify({"ok": True, "rascunho": _rascunho_ou_404(body["rascunho_id"])})


@app.route("/api/planejamento/alocar-motoristas", methods=["POST"])
@requer_auth(niveis=("total", "operador"))
@exige_mesma_origem
def api_alocar_motoristas():
    """Roda a alocação equitativa de motoristas (mesma do criador de
    rotas) nos rascunhos do lote ativo que ainda estão sem motorista --
    botão "Alocar motoristas" da tela (Hugo, 13/08). Rascunho com
    motorista já escolhido (manual ou sugerido) não é alterado."""
    body = request.get_json(force=True)
    try:
        data_alvo = datetime.strptime(body["data_alvo"], "%Y-%m-%d").date()
        resultado = alocar_motoristas_rascunhos(data_alvo)
    except (KeyError, ValueError) as e:
        return jsonify({"erro": str(e)}), 400
    except Exception as e:
        logging.getLogger(__name__).exception("Falha ao alocar motoristas nos rascunhos")
        return jsonify({"erro": str(e)}), 500
    return jsonify({"ok": True, **resultado})


@app.route("/api/planejamento/desalocar-motoristas", methods=["POST"])
@requer_auth(niveis=("total", "operador"))
@exige_mesma_origem
def api_desalocar_motoristas():
    """Limpa o motorista de todo rascunho (ainda não enviado) do lote
    ativo -- botão "Desalocar motoristas" da tela, oposto do "Alocar
    motoristas" (Hugo, 15/08)."""
    body = request.get_json(force=True)
    try:
        data_alvo = datetime.strptime(body["data_alvo"], "%Y-%m-%d").date()
        resultado = desalocar_motoristas_rascunhos(data_alvo)
    except (KeyError, ValueError) as e:
        return jsonify({"erro": str(e)}), 400
    except Exception as e:
        logging.getLogger(__name__).exception("Falha ao desalocar motoristas dos rascunhos")
        return jsonify({"erro": str(e)}), 500
    return jsonify({"ok": True, **resultado})


@app.route("/api/planejamento/disponibilidade-motoristas", methods=["POST"])
@requer_auth(niveis=("total", "operador"))
@exige_mesma_origem
def api_salvar_disponibilidade_motoristas():
    """Tela "Disponibilidade de motoristas" (Hugo, 16/08): grava o
    snapshot dos checkboxes marcados/desmarcados pro dia -- fonte que
    "Alocar motoristas" (e os jobs agendados) sempre respeitam."""
    body = request.get_json(force=True)
    try:
        data_alvo = datetime.strptime(body["data_alvo"], "%Y-%m-%d").date()
        resultado = salvar_disponibilidade_dia(data_alvo, body.get("ajustes") or {})
    except (KeyError, ValueError) as e:
        return jsonify({"erro": str(e)}), 400
    except Exception as e:
        logging.getLogger(__name__).exception("Falha ao salvar disponibilidade de motoristas")
        return jsonify({"erro": str(e)}), 500
    return jsonify({"ok": True, **resultado})


@app.route("/api/planejamento/disponibilidade-motoristas/periodo", methods=["POST"])
@requer_auth(niveis=("total", "operador"))
@exige_mesma_origem
def api_marcar_disponibilidade_periodo():
    """Mini-formulário "Marcar período" da tela de disponibilidade --
    lança férias/atestado de um motorista em várias datas de uma vez."""
    body = request.get_json(force=True)
    try:
        agent_id = int(body["agent_id"])
        data_inicio = datetime.strptime(body["data_inicio"], "%Y-%m-%d").date()
        data_fim = datetime.strptime(body["data_fim"], "%Y-%m-%d").date()
        disponivel = bool(body.get("disponivel"))
        motivo = (body.get("motivo") or "").strip() or None
        resultado = marcar_disponibilidade_periodo(agent_id, data_inicio, data_fim, disponivel, motivo)
    except (KeyError, ValueError) as e:
        return jsonify({"erro": str(e)}), 400
    except Exception as e:
        logging.getLogger(__name__).exception("Falha ao marcar disponibilidade por período")
        return jsonify({"erro": str(e)}), 500
    return jsonify({"ok": True, **resultado})


@app.route("/api/planejamento/disponibilidade-motoristas/limpar", methods=["POST"])
@requer_auth(niveis=("total", "operador"))
@exige_mesma_origem
def api_limpar_disponibilidade_motoristas():
    """Botão "Redefinir" de uma linha da tela de disponibilidade --
    volta o motorista pro padrão semanal (DIAS_DISPONIVEIS) naquele dia."""
    body = request.get_json(force=True)
    try:
        agent_id = int(body["agent_id"])
        data_alvo = datetime.strptime(body["data"], "%Y-%m-%d").date()
        resultado = limpar_disponibilidade_dia(agent_id, data_alvo)
    except (KeyError, ValueError) as e:
        return jsonify({"erro": str(e)}), 400
    except Exception as e:
        logging.getLogger(__name__).exception("Falha ao limpar ajuste de disponibilidade")
        return jsonify({"erro": str(e)}), 500
    return jsonify({"ok": True, **resultado})


@app.route("/motoristas")
@requer_auth(niveis=("total", "operador", "leitura"))
def motoristas():
    """Tela "Motoristas" (Hugo, 16/08): lista quem está em
    BD_MOTORISTAS.xlsx e, pra quem tem login total, o cadastro de
    motorista novo (regras/cadastro_motoristas.py)."""
    try:
        dados = dados_pagina_motoristas()
        erro = None
    except Exception as e:
        logging.getLogger(__name__).exception("Falha ao montar dados da tela de motoristas")
        dados = None
        erro = str(e)
    return render_template("motoristas.html", dados=dados, erro=erro, pode_editar=g.nivel_acesso in ("total", "operador"))


@app.route("/api/motoristas/vuupt-disponiveis")
@requer_auth(niveis=("total", "operador"))
def api_motoristas_vuupt_disponiveis():
    """Dropdown "Motorista (VUUPT)" do formulário de cadastro -- agentes
    do VUUPT que ainda não têm linha na planilha."""
    try:
        agentes = listar_agentes_vuupt_nao_cadastrados(_carregar_config())
    except Exception as e:
        logging.getLogger(__name__).exception("Falha ao listar agentes do VUUPT sem cadastro")
        return jsonify({"erro": str(e)}), 500
    return jsonify({"agentes": agentes})


@app.route("/api/motoristas", methods=["POST"])
@requer_auth(niveis=("total", "operador"))
@exige_mesma_origem
def api_cadastrar_motorista():
    body = request.get_json(force=True)
    try:
        resultado = cadastrar_motorista(_carregar_config(), body)
    except (KeyError, ValueError) as e:
        return jsonify({"erro": str(e)}), 400
    except PermissionError as e:
        return jsonify({"erro": str(e)}), 409
    except Exception as e:
        logging.getLogger(__name__).exception("Falha ao cadastrar motorista")
        return jsonify({"erro": str(e)}), 500
    return jsonify({"ok": True, **resultado})


@app.route("/api/planejamento/nova-rota", methods=["POST"])
@requer_auth(niveis=("total", "operador"))
@exige_mesma_origem
def api_nova_rota():
    body = request.get_json(force=True)
    try:
        data_alvo = datetime.strptime(body["data_alvo"], "%Y-%m-%d").date()
        referencia = rascunhos_rota.referencia_para_rascunho_manual(data_alvo)
        rascunho_id = rascunhos_rota.criar_rascunho_vazio(
            data_alvo, referencia["lote_id"], body.get("particao") or referencia["particao"],
            body.get("tipo_rota") or referencia["tipo_rota"],
            referencia["start_location_base_id"], referencia["end_location_base_id"],
            referencia["start_at"],
        )
    except (KeyError, ValueError) as e:
        return jsonify({"erro": str(e)}), 400
    return jsonify({"ok": True, "rascunho": _rascunho_ou_404(rascunho_id)})


@app.route("/api/planejamento/criar-rota-com-paradas", methods=["POST"])
@requer_auth(niveis=("total", "operador"))
@exige_mesma_origem
def api_criar_rota_com_paradas():
    """Rascunho novo já com as paradas selecionadas no pool (seleção
    múltipla da tela, Hugo 12/08) -- herda partição/tipo/bases/start_at
    do lote ativo da data (ou dos padrões do pipeline, quando a data
    ainda não tem lote), sem o vaivém de criar vazia e arrastar parada
    por parada."""
    body = request.get_json(force=True)
    try:
        data_alvo = datetime.strptime(body["data_alvo"], "%Y-%m-%d").date()
        paradas = body["paradas"]
        if not paradas:
            return jsonify({"erro": "Nenhum pedido selecionado."}), 400
        referencia = rascunhos_rota.referencia_para_rascunho_manual(data_alvo)
        rascunho_id = rascunhos_rota.criar_rascunho_com_paradas(
            data_alvo, referencia["lote_id"], referencia["particao"], referencia["tipo_rota"],
            referencia["start_location_base_id"], referencia["end_location_base_id"],
            referencia["start_at"], paradas,
        )
    except (KeyError, ValueError) as e:
        return jsonify({"erro": str(e)}), 400
    return jsonify({"ok": True, "rascunho": _rascunho_ou_404(rascunho_id)})


@app.route("/api/planejamento/roteirizar-selecionados", methods=["POST"])
@requer_auth(niveis=("total", "operador"))
@exige_mesma_origem
def api_roteirizar_selecionados():
    """Roda o criador de rotas (mesmo miolo do job diário: partição
    Seco/Frio, seleção de modelo + 2-opt, motorista sugerido) só com os
    pedidos selecionados na tela (Hugo, 12/08) -- os rascunhos gerados
    entram no lote ativo da data. Pode levar alguns segundos
    (geocodificação + comparação dos modelos, ou só 1 se `modelo_forcado`
    vier no corpo -- escolha manual do tipo de roteirização, Hugo, 15/08).

    `max_paradas_por_rota` (Hugo, 15/08): teto de pedidos por rota --
    campo ausente no corpo mantém o padrão do pipeline (18); presente
    com um número usa esse teto; presente com `null`/`0` remove o
    limite."""
    body = request.get_json(force=True)
    try:
        data_alvo = datetime.strptime(body["data_alvo"], "%Y-%m-%d").date()
        service_ids = [int(sid) for sid in body["service_ids"]]
        modelo_forcado = body.get("modelo_forcado") or None
        if not service_ids:
            return jsonify({"erro": "Nenhum pedido selecionado."}), 400
        kwargs_roteirizacao = {}
        if "max_paradas_por_rota" in body:
            valor_limite = body["max_paradas_por_rota"]
            kwargs_roteirizacao["max_paradas_por_rota"] = int(valor_limite) if valor_limite else None
        resultado = roteirizar_selecionados(data_alvo, service_ids, modelo_forcado=modelo_forcado,
                                            **kwargs_roteirizacao)
    except (KeyError, ValueError) as e:
        return jsonify({"erro": str(e)}), 400
    except Exception as e:
        logging.getLogger(__name__).exception("Falha ao roteirizar a seleção")
        return jsonify({"erro": str(e)}), 500
    return jsonify({"ok": True, **resultado})


@app.route("/api/planejamento/otimizar-sequencia", methods=["POST"])
@requer_auth(niveis=("total", "operador"))
@exige_mesma_origem
def api_otimizar_sequencia():
    body = request.get_json(force=True)
    try:
        rascunhos_rota.otimizar_sequencia(body["rascunho_id"])
    except (KeyError, ValueError) as e:
        return jsonify({"erro": str(e)}), 400
    return jsonify({"ok": True, "rascunho": _rascunho_ou_404(body["rascunho_id"])})


@app.route("/api/planejamento/duplicar-rota", methods=["POST"])
@requer_auth(niveis=("total", "operador"))
@exige_mesma_origem
def api_duplicar_rota():
    """Duplica uma rota do card -- funciona tanto em RASCUNHO quanto em
    ENVIADO (Hugo, 13/08): a cópia nasce sempre em RASCUNHO, editável,
    no mesmo lote da origem."""
    body = request.get_json(force=True)
    try:
        novo_id = rascunhos_rota.duplicar_rascunho(body["rascunho_id"])
    except (KeyError, ValueError) as e:
        return jsonify({"erro": str(e)}), 400
    return jsonify({"ok": True, "rascunho": _rascunho_ou_404(novo_id)})


@app.route("/api/planejamento/descartar-rota", methods=["POST"])
@requer_auth(niveis=("total", "operador"))
@exige_mesma_origem
def api_descartar_rota():
    """Descarta um rascunho (botão do card, rascunho_id) ou vários de
    uma vez (botão "Descartar todos os rascunhos", rascunho_ids) --
    mesmo padrão de api_confirmar_envio/api_cancelar_rota."""
    body = request.get_json(force=True)
    rascunho_ids = body.get("rascunho_ids")
    if rascunho_ids is None:
        try:
            rascunho_ids = [body["rascunho_id"]]
        except KeyError as e:
            return jsonify({"erro": str(e)}), 400

    for rascunho_id in rascunho_ids:
        rascunhos_rota.descartar_rascunho(rascunho_id)
    return jsonify({"ok": True})


@app.route("/api/planejamento/confirmar-envio", methods=["POST"])
@requer_auth(niveis=("total", "operador"))
@exige_mesma_origem
def api_confirmar_envio():
    """Materializa os rascunhos aprovados na VUUPT de verdade (Fase 3).
    Processa cada rascunho_id independentemente -- falha em um não
    impede os outros de serem enviados (falha parcial é reportada por
    item, não aborta o lote inteiro)."""
    body = request.get_json(force=True)
    try:
        rascunho_ids = body["rascunho_ids"]
    except KeyError as e:
        return jsonify({"erro": str(e)}), 400

    token = _carregar_config().get("vuupt_api", {}).get("token", "")
    resultados = [rascunhos_rota.enviar_rascunho(rid, token) for rid in rascunho_ids]
    return jsonify({"ok": True, "resultados": resultados})


@app.route("/api/planejamento/cancelar-rota", methods=["POST"])
@requer_auth(niveis=("total", "operador"))
@exige_mesma_origem
def api_cancelar_rota():
    """Cancela na VUUPT a(s) rota(s) já enviada(s) indicada(s) -- botão
    "Cancelar rota"/"Cancelar todas as rotas" da tela. Só funciona pra
    rota que ainda não iniciou deslocamento (checado ao vivo contra a
    API dentro de cancelar_rota_enviada). Mesmo padrão de
    api_confirmar_envio: cada id é processado independentemente, falha
    em um não impede os outros."""
    body = request.get_json(force=True)
    try:
        rascunho_ids = body["rascunho_ids"]
    except KeyError as e:
        return jsonify({"erro": str(e)}), 400

    token = _carregar_config().get("vuupt_api", {}).get("token", "")
    resultados = [rascunhos_rota.cancelar_rota_enviada(rid, token) for rid in rascunho_ids]
    return jsonify({"ok": True, "resultados": resultados})


@app.route("/api/planejamento/cancelar-pedido", methods=["POST"])
@requer_auth(niveis=("total", "operador"))
@exige_mesma_origem
def api_cancelar_pedido():
    """Cancela DE VERDADE um pedido na VUUPT (DELETE /services/{id}) --
    botão "Cancelar pedido" da tela, em qualquer lugar onde ele esteja
    (pool, rascunho ainda não enviado, ou rota já enviada -- ver
    planejamento_rotas.cancelar_pedido)."""
    body = request.get_json(force=True)
    try:
        service_id = int(body["service_id"])
    except (KeyError, ValueError) as e:
        return jsonify({"erro": str(e)}), 400
    rascunho_id = body.get("rascunho_id")
    rascunho_id = int(rascunho_id) if rascunho_id is not None else None

    resultado = cancelar_pedido(service_id, rascunho_id)
    if not resultado["ok"]:
        return jsonify({"erro": resultado["erro"]}), 400
    return jsonify({"ok": True})


@app.route("/api/planejamento/reagendar-pedido", methods=["POST"])
@requer_auth(niveis=("total", "operador"))
@exige_mesma_origem
def api_reagendar_pedido():
    """Agenda/reagenda um pedido na VUUPT (scheduled_start/scheduled_end)
    -- opção "Agendar / reagendar" do menu de contexto (ver
    planejamento_rotas.reagendar_pedido)."""
    body = request.get_json(force=True)
    try:
        service_id = int(body["service_id"])
        data = body["data"]
        hora_inicio = body["hora_inicio"]
        hora_fim = body["hora_fim"]
    except (KeyError, ValueError) as e:
        return jsonify({"erro": str(e)}), 400

    resultado = reagendar_pedido(service_id, data, hora_inicio, hora_fim)
    if not resultado["ok"]:
        return jsonify({"erro": resultado["erro"]}), 400
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8070, debug=False)
