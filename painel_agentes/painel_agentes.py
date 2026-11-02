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
import json
import logging
import re
import sys
import threading
import uuid
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
    Flask, Response, abort, g, redirect, render_template, request, url_for,
    jsonify, send_file, session,
)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.routing import BuildError

from agentes import AGENTES, buscar_agente, categorias_ordenadas, montar_args_parametros
from executor import (
    iniciar_execucao, iniciar_execucao_com_fila, iniciar_sequencia, buscar_execucao, buscar_ultima_execucao,
    listar_execucoes_recentes, ha_execucao_rodando, ha_execucao_pendente, ler_log, limpar_execucoes_travadas,
    encerrar_todas_execucoes, progresso_execucao,
)
from mapa_rotas import buscar_rotas_para_mapa
from laboratorio_rotas import buscar_dados_laboratorio
from planejamento_rotas import (
    buscar_dados_planejamento, buscar_pool_e_agendados, sincronizar_pool_agora, gerar_romaneio_pdf,
    carregar_documentos_do_rascunho, roteirizar_selecionados, incrementar_rascunhos_com_selecionados,
    alocar_motoristas_rascunhos, desalocar_motoristas_rascunhos, cancelar_pedido, reagendar_pedido,
    reagendar_pedidos, checar_reagendamento_dia_fixo, editar_endereco_pedido, editar_endereco_pedidos,
    editar_nivel_horario_pedido, editar_transportadora_pedidos, listar_transportadoras_terceiros,
    marcar_dedicados, remover_dedicados,
    salvar_disponibilidade_dia, marcar_disponibilidade_periodo, limpar_disponibilidade_dia,
    publicar_oferta_rascunho, publicar_ofertas_em_lote, despublicar_oferta_rascunho, despublicar_ofertas_em_lote,
    ETAPAS_AGENTES_PLANEJAMENTO, montar_etapas_agentes_planejamento,
)
from avisar_motoristas_rotas import notificar_oferta_motoristas, push_ofertas_vps
from motoristas import dados_pagina_motoristas, listar_agentes_vuupt_nao_cadastrados, cadastrar_motorista, editar_motorista, MotoristaNaoEncontrado
from expedicao import listar_rotas_do_dia, gerar_romaneio_rota, excluir_pedido_da_rota, MOTIVOS_EXCLUSAO
# Página pública do portal com regiões, dias de visita e calendário (Hugo, 14/10:
# botão na Torre pra consulta interna). Mesma constante que a página usa.
from portal_cliente.regioes_atendimento import URL_PUBLICA as URL_PAGINA_REGIOES
# Calculadora de frete dedicado do portal (Hugo, 14/10: botão na Torre). Oculta
# dos clientes; abre com o login do cliente teste (portal_cliente.cotacao.cnpjs_liberados).
URL_PAGINA_COTACAO = "https://app.freshhub.com.br/cliente/cotacao"
import rascunhos_rota
import torre_controle
from nucleo import justificativas_sem_canhoto
import tratativas
import pedidos_parados_triagem
import consulta_expedicao
import contadores_menu
import pagina_inicial
import wms
import wms_pedidos
import wms_etiqueta_produto
import wms_faltas_recebimento
import acesso_telas
import avisos_pendencias
from galpao3d import banco as galpao3d_banco, estado as galpao3d_estado
import retornos

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


@app.context_processor
def _telas_liberadas_no_template():
    """Lista de telas do acesso personalizado (None = segue o nivel), pro
    menu lateral e os links entre telas."""
    return {"telas_liberadas": g.get("telas_liberadas")}

# Roda uma vez, assim que o painel sobe -- destrava qualquer execução
# que ficou presa em RODANDO por causa de um encerramento à força do
# processo anterior (ex: Ctrl+C no meio de uma execução, pedido do
# Hugo 04/08). Nada pode estar genuinamente rodando nesse momento.
limpar_execucoes_travadas()


def _nivel_das_credenciais(usuario: str, senha: str, cfg_painel: dict):
    """Confere usuário/senha contra os cinco pares possíveis e devolve o
    nível de acesso correspondente ("total", "operador", "leitura",
    "expedicao" ou "galpao"), ou None se não bateram com nenhum deles."""
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
    usuario_expedicao = cfg_painel.get("usuario_expedicao")
    senha_expedicao = cfg_painel.get("senha_expedicao")
    if usuario_expedicao and senha_expedicao and hmac.compare_digest(usuario, usuario_expedicao) \
            and hmac.compare_digest(senha, senha_expedicao):
        return "expedicao"
    usuario_galpao = cfg_painel.get("usuario_galpao")
    senha_galpao = cfg_painel.get("senha_galpao")
    if usuario_galpao and senha_galpao and hmac.compare_digest(usuario, usuario_galpao) \
            and hmac.compare_digest(senha, senha_galpao):
        return "galpao"
    # Nível "atendimento" (09/09, Hugo): só a tela /atendimento (chat e
    # chamados do portal do cliente). Total e operador também a acessam.
    # Ampliado 28/09 (Hugo): também Torre, Pedidos Parados, Tratativas,
    # Consulta e Expedição -- vê tudo e trata ocorrência, mas não roda
    # agente, não encerra rota nem exclui pedido de rota.
    usuario_atendimento = cfg_painel.get("usuario_atendimento")
    senha_atendimento = cfg_painel.get("senha_atendimento")
    if usuario_atendimento and senha_atendimento and hmac.compare_digest(usuario, usuario_atendimento) \
            and hmac.compare_digest(senha, senha_atendimento):
        return "atendimento"
    # Logins nominais (02/10, Hugo): pra pessoa ter login proprio em vez de
    # dividir o par do nivel -- a acao fica registrada no nome dela. Desde
    # 23/10 moram no banco (acesso_telas.painel_usuarios), editados na /acessos.
    conta = acesso_telas.conferir_senha(usuario, senha)
    if conta and conta["nivel"] in acesso_telas.NIVEIS:
        return conta["nivel"]
    return None


def _logins_fixos(cfg_painel: dict) -> set:
    chaves = ("usuario", "usuario_operador", "usuario_leitura", "usuario_expedicao", "usuario_galpao",
              "usuario_atendimento")
    return {cfg_painel.get(c) for c in chaves if cfg_painel.get(c)}


# Limite de tentativas no /login (23/10: 5 senhas erradas travavam 15 min)
# SAIU em 29/10 a pedido do Hugo: senha errada nao trava mais o login do painel.


# Deslogar por inatividade (24/10, Hugo; 27/10 subiu de 30 min pra 6 h): 6 h
# sem a pessoa mexer na tela derruba a sessao. Conta como uso: abrir/navegar pra uma pagina e o aviso
# que o _sessao_inativa_script.html manda ao clicar, digitar, tocar ou rolar.
# As consultas automaticas das telas (Torre, contadores do menu, status da
# execucao a cada 2 s) NAO contam, senao ninguem cairia nunca. Galpao e
# expedicao (logins fixos de aparelho compartilhado) ficam de fora.
_INATIVIDADE_MAX = timedelta(hours=6)
_NIVEIS_SEM_EXPIRAR = ("galpao", "expedicao")


def _uso_de_verdade() -> bool:
    if request.endpoint == "api_sessao_atividade":
        return True
    # Sec-Fetch-Mode=navigate so vem quando a pessoa abre a pagina; fetch()
    # manda "cors"/"same-origin". Navegador antigo sem o cabecalho: GET de
    # pagina conta como uso.
    return request.method == "GET" and not request.path.startswith(f"{request.script_root}/api/") \
        and request.headers.get("Sec-Fetch-Mode", "navigate") == "navigate"


def _sessao_na_versao(conta: dict) -> bool:
    """Senha redefinida ou trocada sobe a versao_senha da conta: sessao
    aberta com a senha anterior deixa de valer (sem isso, uma sessao velha
    escolheria a senha nova em /trocar-senha). Sessao de antes da coluna
    existir conta como versao 0."""
    return session.get("versao_senha", 0) == conta["versao_senha"]


def _pagina_inicial(nivel: str, telas) -> str:
    # Página inicial (/inicio, 29/09) pra quem usa o painel inteiro.
    # Expedição e galpão têm uma tela só e nem acesso ao /inicio --
    # cair nele levaria direto a um 403 pós-login.
    paginas = [t for t in telas or [] if t in acesso_telas.PAGINAS]
    if paginas:
        return url_for(paginas[0])
    return {"expedicao": url_for("expedicao"), "galpao": url_for("wms")}.get(nivel) or url_for("inicio")


def requer_auth(f=None, *, niveis=("total",)):
    """Login por sessão (cookie assinado) com quatro níveis: "total"
    (usuario/senha, acesso irrestrito), "operador" (usuario_operador/
    senha_operador, opera Torre de Controle, Planejamento de Rotas e
    cadastro de Motoristas, mas não roda agentes avulsos nem vê o
    Histórico), "leitura" (usuario_leitura/senha_leitura, só as telas e
    APIs marcadas com niveis=(..., "leitura"), sem nenhum botão de ação) e
    "expedicao" (usuario_expedicao/senha_expedicao, só a tela /expedicao e
    o PDF de romaneio -- nada mais do painel, nem em modo leitura) e
    "galpao" (usuario_galpao/senha_galpao, só a tela /wms de endereçamento
    do galpão e suas APIs, 04/09 -- login fixo do aparelho compartilhado;
    quem opera se identifica por nome + PIN dentro da tela).
    Rota sem `niveis` exige nível total. Pedido do Hugo, 13/08: time
    acompanha Torre e Planejamento sem poder disparar ações; nível
    "operador" adicionado 17/08 pra quem toca a operação do dia a dia sem
    precisar de acesso total; nível "expedicao" adicionado 18/08 pra
    restringir o time de expedição só à tela de impressão de documentos.
    Trocado de Basic Auth pra tela de login de verdade + botão de sair,
    17/08 -- Basic Auth não tem um jeito confiável de "deslogar" (o
    navegador guarda a senha até fechar/limpar cache)."""
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
                return redirect(url_for("login"))
            # "Derrubar todas as sessoes" (24/10, /acessos): sessao de rodada
            # anterior cai, de qualquer login.
            if session.get("rodada_sessoes", 0) != acesso_telas.rodada_sessoes()["rodada"]:
                session.clear()
                if request.path.startswith(f"{request.script_root}/api/"):
                    return jsonify({"erro": "Sessão encerrada -- faça login de novo."}), 401
                return redirect(url_for("login"))
            # Login nominal (23/10): nivel e nome vem do banco a cada request,
            # entao mudanca feita na /acessos vale sem novo login. Senha
            # provisoria trava tudo ate a pessoa trocar.
            conta = acesso_telas.conta(session.get("usuario"))
            if conta:
                if not conta["ativo"] or not _sessao_na_versao(conta):
                    session.clear()
                    if request.path.startswith(f"{request.script_root}/api/"):
                        return jsonify({"erro": "Seu acesso mudou -- faça login de novo."}), 401
                    return redirect(url_for("login"))
                if conta["trocar_senha"]:
                    if request.path.startswith(f"{request.script_root}/api/"):
                        return jsonify({"erro": "Troque a senha provisória antes de continuar."}), 403
                    return redirect(url_for("trocar_senha"))
                if conta["nivel"] != nivel:
                    session["nivel_acesso"] = nivel = conta["nivel"]
            g.nome_usuario = (conta or {}).get("nome") or ""
            # Sessao parada (24/10, Hugo): 30 min sem uso de verdade -> login.
            if nivel not in _NIVEIS_SEM_EXPIRAR:
                agora = datetime.now().timestamp()
                ultimo = session.get("ultimo_uso")
                if ultimo and agora - ultimo > _INATIVIDADE_MAX.total_seconds():
                    session.clear()
                    if request.path.startswith(f"{request.script_root}/api/"):
                        return jsonify({"erro": "Sessão encerrada por 30 min sem uso -- faça login de novo."}), 401
                    return redirect(url_for("login"))
                if ultimo is None or _uso_de_verdade():
                    session["ultimo_uso"] = agora
            # Acesso personalizado (21/10, acesso_telas.py): vale a lista de
            # telas, com poder de total dentro delas, no lugar do nivel.
            telas = conta["telas"] if conta else None
            if telas is not None:
                if not acesso_telas.caminho_liberado(request.path, telas):
                    abort(403, "Essa tela não está liberada pro seu usuário.")
                nivel = "total"
            elif nivel not in niveis:
                abort(403, "Seu usuário não tem permissão pra essa ação.")
            g.nivel_acesso = nivel
            g.telas_liberadas = telas
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


def _resolver_data_alvo_planejamento(body: dict) -> date | None:
    """Descobre a data-alvo do plano que uma chamada de mutação do
    /api/planejamento/* afeta, pra bloquear ação sobre plano de dia já
    passado (pedido do Hugo, 23/08: uma data anterior a hoje é só
    consulta -- sem edição/cancelamento nem Pool). Tenta, nessa ordem:
    'data_alvo' direto no corpo (rotas que operam a data inteira, ex.
    alocar-motoristas/roteirizar-selecionados/nova-rota); senão, o
    primeiro rascunho_id referenciado (direto, em 'rascunho_ids', em
    'rascunho_origem_id'/'rascunho_destino_id', ou dentro de 'itens')
    -- todo rascunho tocado numa mesma chamada pertence ao mesmo
    lote/dia, então o primeiro que existir já basta. None quando o
    corpo não referencia nem data nem rascunho (ex. cancelar-pedido/
    reagendar-pedido/editar-endereco só com service_id, sem
    rascunho_id -- é sempre um pedido do Pool, que já não existe pra
    data passada) ou quando nenhum id referenciado resolve pra um
    rascunho existente -- nesses casos não há o que bloquear aqui, e o
    handler segue tratando erro de dado inexistente do jeito de sempre."""
    valor = body.get("data_alvo")
    if valor:
        try:
            return datetime.strptime(valor, "%Y-%m-%d").date()
        except ValueError:
            return None

    candidatos_id = [body.get("rascunho_id"), body.get("rascunho_origem_id"), body.get("rascunho_destino_id")]
    candidatos_id.extend(body.get("rascunho_ids") or [])
    for item in (body.get("itens") or []):
        if isinstance(item, dict):
            candidatos_id.append(item.get("rascunho_id") or item.get("rascunho_origem_id"))

    for rascunho_id in candidatos_id:
        if rascunho_id is None:
            continue
        try:
            rascunho = rascunhos_rota.buscar_rascunho(int(rascunho_id))
        except (TypeError, ValueError):
            continue
        if rascunho:
            return date.fromisoformat(rascunho["data_alvo"])
    return None


def bloqueia_planejamento_passado(f):
    """Recusa (403) qualquer mutação do planejamento (edição, exclusão,
    envio, cancelamento, oferta) que caia sobre o plano de um dia
    anterior a hoje -- dia passado é só consulta (pedido do Hugo,
    23/08). Aplicado só nas rotas de mutação que fazem sentido escopar
    por dia (não em disparo de agente nem disponibilidade de
    motoristas, que não são sobre editar o plano de um dia específico
    já fechado)."""
    @wraps(f)
    def decorado(*args, **kwargs):
        body = request.get_json(force=True, silent=True) or {}
        data_alvo = _resolver_data_alvo_planejamento(body)
        if data_alvo and data_alvo < date.today():
            return jsonify({
                "erro": f"Plano de {data_alvo.strftime('%d/%m/%Y')} já passou -- essa tela só permite consulta pra dia anterior a hoje.",
            }), 403
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
        conta = acesso_telas.conta(usuario) if nivel else None
        telas = conta["telas"] if conta else None
        paginas = [t for t in telas or [] if t in acesso_telas.PAGINAS]
        if nivel is None:
            erro = "Usuário ou senha incorretos."
        elif telas is not None and not paginas:
            erro = "Seu usuário não tem nenhuma tela liberada. Fale com o Hugo."
        else:
            session.clear()
            session.permanent = True
            session["nivel_acesso"] = nivel
            session["usuario"] = usuario
            session["ultimo_uso"] = datetime.now().timestamp()
            session["rodada_sessoes"] = acesso_telas.rodada_sessoes()["rodada"]
            if conta:
                session["versao_senha"] = conta["versao_senha"]
            if conta and conta["trocar_senha"]:
                return redirect(url_for("trocar_senha"))
            # Todo acesso cai no Inicio (24/10, Hugo), nao na tela que a pessoa
            # tentava abrir. Galpao, expedicao e personalizado sem o Inicio
            # vao pra tela deles (_pagina_inicial).
            return redirect(_pagina_inicial(nivel, telas))
    return render_template("login.html", erro=erro)


@app.route("/trocar-senha", methods=["GET", "POST"])
def trocar_senha():
    """Troca obrigatoria da senha provisoria do login nominal (23/10): a
    /acessos cadastra ou redefine com senha provisoria e requer_auth manda
    pra ca ate a pessoa escolher a dela."""
    conta = acesso_telas.conta(session.get("usuario")) if session.get("nivel_acesso") else None
    if not conta or not conta["ativo"] or not _sessao_na_versao(conta) \
            or session.get("rodada_sessoes", 0) != acesso_telas.rodada_sessoes()["rodada"]:
        session.clear()
        return redirect(url_for("login"))
    if not conta["trocar_senha"]:
        return redirect(_pagina_inicial(conta["nivel"], conta["telas"]))
    erro = None
    if request.method == "POST":
        origem = request.headers.get("Origin") or request.headers.get("Referer")
        if not origem or urlparse(origem).netloc != request.host:
            abort(403, "Origem da requisição não confere (proteção CSRF).")
        nova = request.form.get("nova", "")
        if nova != request.form.get("repetir", ""):
            erro = "As duas senhas não conferem."
        else:
            conn = acesso_telas.conectar()
            try:
                versao = acesso_telas.trocar_senha(conn, conta["usuario"], nova, session.get("versao_senha", 0))
                if versao is None:
                    session.clear()
                    return redirect(url_for("login"))
                session["versao_senha"] = versao
                return redirect(_pagina_inicial(conta["nivel"], conta["telas"]))
            except ValueError as e:
                erro = str(e)
            finally:
                conn.close()
    return render_template("login.html", erro=erro, trocar=True, usuario=conta["usuario"])


def _segundos_restantes():
    """Quanto falta pra sessao cair por inatividade; None pra quem nao expira."""
    if g.nivel_acesso in _NIVEIS_SEM_EXPIRAR or session.get("nivel_acesso") in _NIVEIS_SEM_EXPIRAR:
        return None
    restante = _INATIVIDADE_MAX.total_seconds() - (datetime.now().timestamp() - session.get("ultimo_uso", 0))
    return max(0, int(restante))


@app.route("/api/sessao/atividade", methods=["POST"])
@requer_auth(niveis=acesso_telas.NIVEIS)
@exige_mesma_origem
def api_sessao_atividade():
    """A pessoa mexeu na tela (_sessao_inativa_script.html): requer_auth ja renovou o ultimo_uso."""
    return jsonify({"restante": _segundos_restantes()})


@app.route("/api/sessao/estado")
@requer_auth(niveis=acesso_telas.NIVEIS)
def api_sessao_estado():
    """Consulta sem renovar: 401 se a sessao ja caiu, senao quanto falta."""
    return jsonify({"restante": _segundos_restantes()})


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
        # valor inicial dos campos de data (agentes com 'parametros' tipo date)
        hoje_iso=date.today().isoformat(),
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
    # Campos extras do cartão (ex: data dos romaneios) -> argv do script.
    try:
        args_extra = montar_args_parametros(agente, request.form) or None
    except ValueError as e:
        return str(e), 400
    execucao_id = iniciar_execucao(agente, modo_teste, args_extra=args_extra)
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
    progresso = progresso_execucao(execucao_id) if exec_info["status"] == "RODANDO" else None
    return jsonify({
        "status": exec_info["status"],
        "codigo_saida": exec_info["codigo_saida"],
        "finalizado_em": exec_info["finalizado_em"],
        "log": ler_log(execucao_id),
        "percentual": (progresso or {}).get("percentual"),
        "eta_segundos": (progresso or {}).get("eta_segundos"),
    })


@app.route("/api/agentes/progresso")
@requer_auth
def agentes_progresso():
    """Progresso (%/ETA) de todo agente RODANDO agora -- usado pelo
    polling da grade de Agentes (index.html), que não tinha nenhuma
    atualização ao vivo antes disso (pedido do Hugo, 24/08)."""
    resultado = {}
    for agente in AGENTES:
        ultima = buscar_ultima_execucao(agente["id"])
        if ultima and ultima["status"] == "RODANDO":
            progresso = progresso_execucao(ultima["id"]) or {}
            resultado[agente["id"]] = {
                "execucao_id": ultima["id"],
                "percentual": progresso.get("percentual"),
                "eta_segundos": progresso.get("eta_segundos"),
            }
    return jsonify(resultado)


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


@app.route("/api/sidebar/contadores")
@requer_auth(niveis=("total", "operador", "logistica", "leitura", "expedicao", "galpao", "atendimento"))
def api_sidebar_contadores():
    """Números dos badges do menu lateral (base.html). Chamado pelo JS
    depois do load de qualquer página e a cada poucos minutos -- nunca
    na renderização. Toda a lógica (o que é barato, o que é caro, o que
    cada nível pode ver) está em contadores_menu.py."""
    contadores = contadores_menu.contadores(g.nivel_acesso, _niveis_por_contador(), _identidade_demandas())
    resp = jsonify({"contadores": contadores, "avisos": _avisos_da_pessoa(contadores)})
    return _com_cookie_aparelho(resp)


def _identidade_demandas():
    """Quem esta logado, pro badge das Demandas (por usuario). Falha aqui
    so tira o badge, nunca derruba os outros contadores."""
    try:
        conn = demandas.conectar()
        try:
            return demandas.identidade(conn, session.get("usuario") or g.nivel_acesso, session.get("nivel_acesso"),
                                       g.get("telas_liberadas"),
                                       session.get("demandas_nome") or session.get("atendente_nome"))
        finally:
            conn.close()
    except Exception as e:
        logging.getLogger(__name__).warning(f"[contadores] Identidade das demandas falhou: {e}")
        return None


# Aviso de pendencias (spec 2026-10-24-aviso-pendencias-painel): a faixa no
# topo das telas vem junto do poll do menu; "ignorar" e por pessoa.
_NIVEIS_AVISO = ("total", "operador", "logistica", "leitura", "expedicao", "galpao", "atendimento")
_COOKIE_APARELHO = "aviso_aparelho"


def _pessoa_do_aviso() -> str:
    """Login nominal -> 'login:<usuario>'. Login de nivel (compartilhado) ->
    'aparelho:<uuid>' do cookie aviso_aparelho; sem cookie valido, gera um
    (o cookie vai na resposta, ver _com_cookie_aparelho)."""
    if acesso_telas.conta(session.get("usuario")):
        return f"login:{session.get('usuario')}"
    try:
        uid = str(uuid.UUID(request.cookies.get(_COOKIE_APARELHO, "")))
    except ValueError:
        uid = str(uuid.uuid4())
        g.aviso_aparelho_novo = uid
    return f"aparelho:{uid}"


def _com_cookie_aparelho(resp):
    uid = g.get("aviso_aparelho_novo")
    if uid:
        resp.set_cookie(_COOKIE_APARELHO, uid, max_age=365 * 24 * 3600, httponly=True, samesite="Lax",
                        secure=app.config["SESSION_COOKIE_SECURE"])
    return resp


def _avisos_da_pessoa(contadores: dict) -> list:
    """Avisos do perfil, sem os ignorados, com `destino` ja resolvido pelo
    url_for (prefixo do Caddy). Nunca derruba o poll do menu."""
    try:
        pessoa = _pessoa_do_aviso()
        telas = g.get("telas_liberadas")
        brutos = avisos_pendencias.montar(
            g.nivel_acesso, contadores, None if telas is None else set(telas),
            telas is not None and "aprovar_embarcadores" in telas)
        conn = avisos_pendencias.conectar()
        try:
            brutos = avisos_pendencias.filtrar_ignorados(conn, pessoa, brutos, datetime.now())
        finally:
            conn.close()
        itens = {i["chave"]: i for i in avisos_pendencias.CATALOGO}
        avisos = []
        for a in brutos:
            item = itens[a["chave"]]
            try:
                destino = url_for(item["endpoint_destino"])
            except BuildError:
                logging.getLogger(__name__).debug(f"[avisos] Tela '{item['endpoint_destino']}' ainda nao existe; aviso '{a['chave']}' omitido")
                continue
            if "?" in item["destino"]:
                destino += "?" + item["destino"].split("?", 1)[1]
            avisos.append({"chave": a["chave"], "texto": a["texto"], "qtd": a["qtd"],
                           "destino": destino, "vermelho": a["vermelho"]})
        return avisos
    except Exception as e:
        logging.getLogger(__name__).warning(f"[avisos] Falha ao montar os avisos: {e}")
        return []


@app.route("/api/avisos/ignorar", methods=["POST"])
@requer_auth(niveis=_NIVEIS_AVISO)
@exige_mesma_origem
def api_avisos_ignorar():
    """Esconde um aviso pra essa pessoa por IGNORAR_MINUTOS."""
    corpo = request.get_json(silent=True) or {}
    chave, qtd = corpo.get("chave"), corpo.get("qtd")
    if chave not in {i["chave"] for i in avisos_pendencias.CATALOGO}:
        return jsonify({"erro": "Aviso desconhecido."}), 400
    if not isinstance(qtd, int) or isinstance(qtd, bool) or qtd < 0:
        return jsonify({"erro": "qtd precisa ser um inteiro >= 0."}), 400
    agora = datetime.now()
    conn = avisos_pendencias.conectar()
    try:
        avisos_pendencias.ignorar(conn, _pessoa_do_aviso(), chave, qtd, agora)
    finally:
        conn.close()
    ate = agora + timedelta(minutes=avisos_pendencias.IGNORAR_MINUTOS)
    return _com_cookie_aparelho(jsonify({"ok": True, "ate": ate.isoformat(timespec="seconds")}))


# ── Pedidos do portal: executar ou recusar a solicitação do cliente ──────────

_NIVEIS_SOLICITACAO = ("total", "operador", "logistica")


def _criado_em_curto(valor) -> str:
    """'2026-10-24T09:05:00' -> '24/10 09:05' (devolve o texto cru se não casar)."""
    v = str(valor or "").replace("T", " ")
    if len(v) >= 16 and v[4] == "-" and v[7] == "-":
        return f"{v[8:10]}/{v[5:7]} {v[11:16]}"
    return v


@app.route("/solicitacoes-portal")
@requer_auth(niveis=_NIVEIS_SOLICITACAO)
def solicitacoes_portal():
    return render_template("solicitacoes_portal.html")


@app.route("/api/solicitacoes-portal")
@requer_auth(niveis=_NIVEIS_SOLICITACAO)
def api_solicitacoes_portal():
    from portal_cliente import envio_pedidos
    conn = envio_pedidos.conectar()
    try:
        linhas = envio_pedidos.listar_solicitacoes_pendentes(conn)
    finally:
        conn.close()
    # envio em CANCELANDO ainda esta na fila do worker (nao e acionavel aqui)
    linhas = [s for s in linhas if s.get("status_envio") != "CANCELANDO"]
    itens = [{
        "id": s["id"],
        "tipo": s["tipo"],
        "rotulo": envio_pedidos.ROTULOS_SOLICITACAO.get(s["tipo"], s["tipo"]),
        "embarcador": s.get("cnpj_embarcador") or "",
        "codigo_pedido": s.get("codigo_pedido") or "",
        "numero_nf": s.get("numero_nf") or "",
        "destinatario": s.get("destinatario_nome") or "",
        "criado_em": _criado_em_curto(s.get("criado_em")),
        "detalhe": s.get("detalhes") or "",
    } for s in linhas]
    return jsonify({"solicitacoes": itens})


@app.route("/api/solicitacoes-portal/<int:solicitacao_id>/executar", methods=["POST"])
@requer_auth(niveis=_NIVEIS_SOLICITACAO)
@exige_mesma_origem
def api_solicitacoes_portal_executar(solicitacao_id):
    """Síncrono (ação pontual de um operador; a trava da Stokki espera até 60 s)."""
    from portal_cliente import envio_pedidos, solicitacoes_execucao
    try:
        deps = solicitacoes_execucao.dependencias_reais(_carregar_config(), usuario=session.get("usuario") or "")
        conn = envio_pedidos.conectar()
        try:
            resultado = solicitacoes_execucao.executar(conn, solicitacao_id, deps)
        finally:
            conn.close()
    except Exception:
        logging.getLogger(__name__).exception(f"[solicitacoes-portal] Falha ao executar {solicitacao_id}")
        return jsonify({"ok": False, "mensagem": "Erro inesperado ao executar. Veja o log do painel."}), 500
    return jsonify(resultado)


@app.route("/api/solicitacoes-portal/<int:solicitacao_id>/recusar", methods=["POST"])
@requer_auth(niveis=_NIVEIS_SOLICITACAO)
@exige_mesma_origem
def api_solicitacoes_portal_recusar(solicitacao_id):
    from portal_cliente import envio_pedidos, solicitacoes_execucao
    corpo = request.get_json(silent=True) or {}
    resposta = corpo.get("resposta")
    resposta = resposta.strip() if isinstance(resposta, str) else ""
    if not resposta or len(resposta) > 500:
        return jsonify({"ok": False, "mensagem": "Informe a resposta ao cliente (até 500 caracteres)."}), 400
    conn = envio_pedidos.conectar()
    try:
        solicitacoes_execucao.recusar(conn, solicitacao_id, resposta)
    except ValueError as e:
        return jsonify({"ok": False, "mensagem": str(e)}), 400
    finally:
        conn.close()
    return jsonify({"ok": True, "mensagem": "Solicitação recusada."})


def _niveis_por_contador() -> dict:
    """Acesso personalizado: so os contadores das telas liberadas."""
    telas = g.get("telas_liberadas")
    if telas is None:
        return contadores_menu.NIVEIS_POR_CONTADOR
    return {c: (g.nivel_acesso,) if c in telas else () for c in contadores_menu.NIVEIS_POR_CONTADOR}


@app.route("/inicio")
@requer_auth(niveis=("total", "operador", "logistica", "leitura", "atendimento"))
def inicio():
    """Página inicial do painel (29/09): o que espera ação de quem logou,
    números do dia e, só pro total, as rotinas. O HTML sobe sem número
    nenhum; o JS busca /api/inicio/dados depois do load (mesma regra dos
    badges do menu: nunca esperar fonte cara na renderização)."""
    return render_template("inicio.html", mostra_rotinas=g.nivel_acesso == "total")


@app.route("/api/inicio/dados")
@requer_auth(niveis=("total", "operador", "logistica", "leitura", "atendimento"))
def api_inicio_dados():
    niveis = _niveis_por_contador()
    dados = pagina_inicial.montar_dados(
        g.nivel_acesso, url_for, niveis_por_contador=niveis,
        contadores=lambda nivel: contadores_menu.contadores(nivel, niveis))
    telas = g.get("telas_liberadas")
    if telas is not None and "aprovar_embarcadores" not in telas:
        dados.pop("cadastros_embarcador", None)
    return jsonify(dados)


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


@app.route("/expedicao")
@requer_auth(niveis=("total", "operador", "leitura", "expedicao", "atendimento"))
def expedicao():
    """Tela de impressão pro time de expedição (Hugo, 18/08): nome da
    rota, motorista e um botão por rota pra imprimir a papelada (NFs +
    boletos + canhoteira)."""
    data_alvo = _parse_data_param()
    try:
        rotas = listar_rotas_do_dia(data_alvo)
        erro = None
    except Exception as e:
        logging.getLogger(__name__).exception("Falha ao montar dados de expedição")
        rotas = []
        erro = str(e)
    return render_template(
        "expedicao.html", rotas=rotas, erro=erro, data_alvo_input=data_alvo.isoformat(),
        motivos_exclusao=MOTIVOS_EXCLUSAO,
        # "Excluir da Rota" só faz sentido de hoje em diante -- dia
        # passado é só consulta aqui (mesma regra de planejamento_rotas.
        # py, ver expedicao.excluir_pedido_da_rota) -- e nível "leitura"
        # não tem botão de ação nenhum, por definição (ver docstring de
        # requer_auth). Esconde a opção em vez de deixar o usuário
        # tentar e levar 401/erro do servidor.
        # Nível "atendimento" (28/09) também só consulta e imprime aqui.
        pode_excluir=data_alvo >= date.today() and g.nivel_acesso not in ("leitura", "atendimento"),
    )


@app.route("/api/expedicao/romaneio/<int:rota_id>")
@requer_auth(niveis=("total", "operador", "leitura", "expedicao", "atendimento"))
def api_expedicao_romaneio(rota_id):
    """Gera (sempre fresco) e serve o PDF de romaneio de uma rota já
    criada na VUUPT -- mesmo padrão do botão 'Imprimir rota' do
    planejamento, ver expedicao.py::gerar_romaneio_rota."""
    data_alvo = _parse_data_param()
    try:
        caminho = gerar_romaneio_rota(data_alvo, rota_id)
    except ValueError as e:
        return str(e), 404
    except Exception as e:
        logging.getLogger(__name__).exception(f"Falha ao gerar romaneio da rota {rota_id}")
        return f"Falha ao gerar romaneio: {e}", 500
    return send_file(caminho, mimetype="application/pdf", download_name=caminho.name)


@app.route("/api/expedicao/excluir-pedido", methods=["POST"])
@requer_auth(niveis=("total", "operador", "expedicao"))
@exige_mesma_origem
def api_expedicao_excluir_pedido():
    """Tira 1 pedido de uma rota já criada na VUUPT -- "Excluir da Rota"
    no menu de contexto de um chip de pedido da tela de expedição
    (Hugo, 23/08): motivo (dropdown fixo, ver expedicao.MOTIVOS_EXCLUSAO)
    + observação livre, gravados pra auditoria. Fora do nível "leitura"
    de propósito -- é uma ação que muda a rota na VUUPT. Ver
    expedicao.py::excluir_pedido_da_rota."""
    body = request.get_json(force=True)
    try:
        rota_id = int(body["rota_id"])
        service_id = int(body["service_id"])
        motivo = str(body["motivo"])
    except (KeyError, ValueError, TypeError) as e:
        return jsonify({"erro": str(e)}), 400
    observacao = (body.get("observacao") or "").strip()
    data_alvo = _parse_data_param()

    resultado = excluir_pedido_da_rota(data_alvo, rota_id, service_id, motivo, observacao,
                                       session.get("usuario", ""))
    if not resultado["ok"]:
        return jsonify({"erro": resultado["erro"]}), 400
    return jsonify(resultado)


# Consulta de pedido (Hugo, 02/10): na separação da rota o operador não
# acha um pedido e quer saber se ele deveria estar no galpão. Só leitura;
# o galpão também consulta. Os mesmos níveis estão no item do menu
# (_menu_lateral_nav.html) -- mexer nos dois lugares.
NIVEIS_CONSULTA_PEDIDO = ("total", "operador", "expedicao", "galpao")


@app.route("/expedicao/consulta")
@requer_auth(niveis=NIVEIS_CONSULTA_PEDIDO)
def expedicao_consulta():
    return render_template("consulta_pedido_expedicao.html")


@app.route("/api/expedicao/consulta")
@requer_auth(niveis=NIVEIS_CONSULTA_PEDIDO)
def api_expedicao_consulta():
    """Veredito de UM pedido cruzando Vuupt e Stokki ao vivo -- ver
    consulta_expedicao.py. 400 código inválido, 503 Vuupt fora."""
    try:
        return jsonify(consulta_expedicao.consultar(request.args.get("codigo", "")))
    except ValueError as e:
        return jsonify({"erro": str(e)}), 400
    except consulta_expedicao.ConsultaIndisponivel as e:
        return jsonify({"erro": str(e)}), 503
    except Exception as e:
        logging.getLogger(__name__).exception("Falha na consulta de pedido da expedição")
        return jsonify({"erro": f"Falha na consulta: {e}"}), 500


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
@requer_auth(niveis=("total", "operador", "logistica", "leitura", "atendimento"))
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
        data_alvo_input=data_alvo.isoformat(),
        # dia anterior a hoje é só consulta (pedido do Hugo, 23/08) --
        # some com toda a barra de ação/edição, independente do nível
        # de acesso (ver bloqueia_planejamento_passado pro reforço
        # equivalente no backend das rotas de mutação).
        pode_editar=g.nivel_acesso in ("total", "operador", "logistica") and data_alvo >= date.today(),
        justificativas=justificativas_sem_canhoto.JUSTIFICATIVAS,
    )


@app.route("/planejamento/mobile")
@requer_auth(niveis=("total", "operador", "logistica", "leitura", "atendimento"))
def planejamento_mobile():
    """Antiga versão mobile "lite" de /planejamento (17/08, só leitura +
    ações básicas, sem mapa). Desde 04/09 a própria tela /planejamento
    tem o modo celular completo (abas Pool / Rotas / Mapa / Mais, mesmo
    JS e endpoints do desktop) -- esta rota só redireciona pra lá, pra
    não quebrar a barra inferior da torre mobile e links salvos."""
    data_alvo = _parse_data_param()
    return redirect(url_for("planejamento", data=data_alvo.isoformat(), modo="mobile"))


# IDs dos agentes acionáveis pela barra de botões do planejamento --
# usada pra validar o agente_id recebido em /rodar (não deixa essa
# tela disparar qualquer agente do sistema, só os dela).
AGENTES_PLANEJAMENTO_IDS = tuple(e["agente_id"] for e in ETAPAS_AGENTES_PLANEJAMENTO)

# Subconjunto que entra na ordem de execução do "Executar tudo" --
# Expedição fica de fora (etapa "incluir_executar_tudo": False em
# planejamento_rotas.py: atua sobre pedidos já entregues, roda pela
# tarefa agendada própria, não faz parte do fluxo de criar rota pro
# dia seguinte).
AGENTES_PLANEJAMENTO_EXECUTAR_TUDO_IDS = tuple(
    e["agente_id"] for e in ETAPAS_AGENTES_PLANEJAMENTO if e.get("incluir_executar_tudo", True)
)


@app.route("/api/planejamento/agentes/etapas")
@requer_auth(niveis=("total", "operador", "logistica", "leitura", "atendimento"))
def api_planejamento_agentes_etapas():
    """Estado da barra de agentes do planejamento (leitura barata no
    SQLite) -- mesmo padrão de /api/torre/etapas, só que restrito aos
    agentes relevantes pra essa tela."""
    return jsonify({"etapas": montar_etapas_agentes_planejamento()})


@app.route("/api/planejamento/agentes/rodar", methods=["POST"])
@requer_auth(niveis=("total", "operador", "logistica"))
@exige_mesma_origem
def api_planejamento_agentes_rodar():
    """Dispara um agente da barra do planejamento. Importação aceita
    filtro (pedido/embarcador -> --pedido/--embarcador do pipeline.py)
    e Expedição aceita seleção de pedidos (30/08: "pedidos" com códigos
    separados por vírgula/espaço -> --pedido do expedir_pedidos.py, que
    roda o fluxo normal restrito só a eles; vazio = rodada completa) --
    tudo via argv extra, sem precisar de uma entrada nova em agentes.py
    pra cada combinação possível."""
    body = request.get_json(force=True)
    agente_id = body.get("agente_id", "")
    if agente_id not in AGENTES_PLANEJAMENTO_IDS:
        return jsonify({"erro": "Esse agente não faz parte da barra do planejamento."}), 400
    agente = buscar_agente(agente_id)
    if not agente:
        return jsonify({"erro": "Agente não encontrado."}), 404
    if ha_execucao_pendente(agente_id):
        return jsonify({"erro": "Essa etapa já está rodando ou na fila -- espera terminar antes de rodar de novo."}), 409

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
    elif agente_id == "somente_expedicao":
        # Aceita "PS-1, PS-2 PS-3" (vírgula, espaço ou ponto-e-vírgula) e
        # valida o formato aqui mesmo -- um token errado viraria só um
        # warning perdido no log do agente, melhor barrar antes de rodar.
        codigos = [c for c in re.split(r"[\s,;]+", (body.get("pedidos") or "").strip()) if c]
        invalidos = [c for c in codigos if not re.fullmatch(r"#?PS-?\d{4,6}(-[A-Za-z]\d+)*", c, re.IGNORECASE)]
        if invalidos:
            return jsonify({"erro": f"Código(s) de pedido inválido(s): {', '.join(invalidos)} "
                                    f"-- use o formato PS-XXXXX."}), 400
        if codigos:
            args_extra = ["--pedido"] + codigos
    elif agente.get("parametros"):
        # Agentes com campos declarados em agentes.py (hoje: Gerar
        # Romaneios com "data" -> --data). O front pode mandar a data
        # da tela (DATA_ALVO_ISO) pra gerar os PDFs do dia planejado.
        try:
            args_extra = montar_args_parametros(agente, body) or None
        except ValueError as e:
            return jsonify({"erro": str(e)}), 400

    # Se outra etapa da barra já está rodando (ou na fila), entra na
    # fila em vez de disparar em paralelo -- pedido do Hugo, 26/08:
    # clicar em duas etapas diferentes seguidas rodava as duas ao
    # mesmo tempo, arriscando concorrência no SQLite/sessão Stokki.
    execucao_id, na_fila = iniciar_execucao_com_fila(
        agente, modo_teste=False, ids_do_grupo=AGENTES_PLANEJAMENTO_IDS, args_extra=args_extra,
    )
    return jsonify({"ok": True, "execucao_id": execucao_id, "na_fila": na_fila})


@app.route("/api/planejamento/agentes/rodar-tudo", methods=["POST"])
@requer_auth(niveis=("total", "operador", "logistica"))
@exige_mesma_origem
def api_planejamento_agentes_rodar_tudo():
    """Botão "Executar tudo" da barra do planejamento: os agentes em
    sequência (Estação de Impressão → Importação sem filtro → Criar
    Rotas Diárias Rascunho → Incrementar Rotas → Processar Documentos
    → Gerar Romaneios), cada um esperando o anterior terminar
    (iniciar_sequencia). Expedição fica fora dessa sequência -- ver
    AGENTES_PLANEJAMENTO_EXECUTAR_TUDO_IDS."""
    if any(ha_execucao_pendente(agente_id) for agente_id in AGENTES_PLANEJAMENTO_IDS):
        return jsonify({"erro": "Já tem uma etapa rodando ou na fila -- espera terminar antes de rodar tudo."}), 409
    passos = [{"agente": buscar_agente(agente_id)} for agente_id in AGENTES_PLANEJAMENTO_EXECUTAR_TUDO_IDS]
    iniciar_sequencia(passos, ids_do_grupo=AGENTES_PLANEJAMENTO_IDS)
    return jsonify({"ok": True})


@app.route("/laboratorio-rotas")
@requer_auth(niveis=("total", "operador", "leitura"))
def laboratorio_rotas():
    """Laboratório de comparação visual de esquemas de roteirização --
    pedido do Hugo, 14/08. 100% leitura (ver laboratorio_rotas.py)."""
    data_alvo = _parse_data_param()
    particao = request.args.get("particao", "Todos")
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
@requer_auth(niveis=("total", "operador", "logistica", "leitura", "atendimento"))
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


@app.route("/vigia")
@requer_auth(niveis=("total", "operador", "leitura"))
def vigia_pedidos():
    """Vigia de pedidos abertos (Hugo, 28/09): cada pedido aberto com o
    estado, há quanto tempo e se o prazo venceu. Só leitura -- quem
    calcula é vigia/vigiar.py (timer de 15 min). Aba "fechamento" (05/10):
    resultado do batimento (batimento/bater.py, 07h25)."""
    aba = "fechamento" if (request.args.get("aba") or "").lower() == "fechamento" else "abertos"
    if aba == "fechamento":
        from batimento import consulta as batimento_consulta
        from batimento.regras import ROTULOS_DIVERGENCIA
        filtros = {
            "motivo": request.args.get("motivo", ""),
            "reais": request.args.get("reais", "") == "1",
            "tratadas": request.args.get("tratadas", "") == "1",
            "busca": request.args.get("busca", ""),
        }
        try:
            dados = batimento_consulta.fechamento(filtros["motivo"], filtros["reais"], filtros["tratadas"],
                                                  filtros["busca"])
            erro = None
        except Exception as e:
            logging.getLogger(__name__).exception("Falha ao ler o batimento")
            dados = {"linhas": [], "por_motivo": [], "total_abertas": 0, "ultima": None, "rodadas": [],
                     "atrasada": False}
            erro = str(e)
        return render_template("vigia_pedidos.html", aba=aba, dados=dados, filtros=filtros, erro=erro,
                               motivos=list(ROTULOS_DIVERGENCIA.items()),
                               pode_tratar=g.nivel_acesso in ("total", "operador"))

    from vigia import consulta as vigia_consulta
    filtros = {
        "estado": request.args.get("estado", ""),
        "vencidos": request.args.get("vencidos", "") == "1",
        "busca": request.args.get("busca", ""),
    }
    try:
        dados = vigia_consulta.listar(filtros["estado"], filtros["vencidos"], filtros["busca"])
        erro = None
    except Exception as e:
        logging.getLogger(__name__).exception("Falha ao ler o vigia")
        dados = {"linhas": [], "resumo": [], "total": 0, "vencidos": 0,
                 "ultima_rodada": None, "ultima_listagem_stokki": None}
        erro = str(e)
    from vigia.regras import ORDEM, ROTULOS
    return render_template("vigia_pedidos.html", aba=aba, dados=dados, filtros=filtros, erro=erro,
                           estados=[(e, ROTULOS[e]) for e in ORDEM])


@app.route("/api/batimento/tratar", methods=["POST"])
@requer_auth(niveis=("total", "operador"))
@exige_mesma_origem
def api_batimento_tratar():
    """Marca uma divergência do batimento como tratada (aba Fechamento)."""
    from batimento import consulta as batimento_consulta
    body = request.get_json(force=True) or {}
    try:
        batimento_consulta.tratar(str(body.get("codigo") or ""), session.get("usuario") or g.nivel_acesso,
                                  str(body.get("obs") or ""))
    except ValueError as e:
        return jsonify({"erro": str(e)}), 400
    return jsonify({"ok": True})


LIMITE_REQUISICAO_BAIXA = 60 * 1024 * 1024  # baixa sem app com canhotos (09/10)


def _rota_sem_app(route_id: int) -> tuple[dict | None, str | None]:
    """Rota da Vuupt + checagem de motorista SEM_APP. (rota, erro)."""
    import rotas_client
    from nucleo.sincronizar_vuupt import _rota_do_corpo
    from regras import preferencias_motoristas
    token = (_carregar_config().get("vuupt_api") or {}).get("token", "")
    # GET /routes/{id} vem embrulhado em {"route": {...}} (visto na prova de 09/10)
    rota = _rota_do_corpo(rotas_client.buscar_rota(token, route_id, include=["services"]))
    if not rota:
        return None, "Rota não encontrada na Vuupt."
    if rota.get("agent_id") not in preferencias_motoristas.agent_ids_sem_app():
        return rota, "Essa rota não é de motorista marcado SEM_APP: a baixa é pelo app do motorista."
    return rota, None


@app.route("/baixa-sem-app")
@requer_auth(niveis=("total", "operador"))
def baixa_sem_app():
    """Baixa de rota de motorista sem app (iPhone, Hugo 08/10): marca o que
    voltou e conclui na Vuupt (nucleo/baixa_sem_app.py)."""
    from mapa_util import extrair_servicos_da_rota
    from insucesso_entrega.motivos_falha import MOTIVOS_FALHA
    route_id = request.args.get("rota", type=int)
    rota, erro = _rota_sem_app(route_id) if route_id else (None, "Rota não informada.")
    servicos = []
    for s in (extrair_servicos_da_rota(rota) if rota else []):
        servicos.append({"id": s.get("id"), "codigo": (s.get("code") or "").lstrip("#"),
                         "titulo": s.get("title") or "", "endereco": s.get("address") or "",
                         "status": s.get("status") or "",
                         "fechado": (s.get("status") or "") in ("done", "canceled", "cancelled")})
    motivos = sorted(((k, v["texto"]) for k, v in MOTIVOS_FALHA.items()), key=lambda kv: kv[1])
    from nucleo.justificativas_sem_canhoto import JUSTIFICATIVAS
    return render_template("baixa_sem_app.html", rota=rota, erro=erro, servicos=servicos, motivos=motivos,
                           justificativas=JUSTIFICATIVAS, limite_bytes=LIMITE_REQUISICAO_BAIXA)


@app.route("/api/baixa-sem-app", methods=["POST"])
@requer_auth(niveis=("total", "operador"))
@exige_mesma_origem
def api_baixa_sem_app():
    import threading
    from mapa_util import extrair_servicos_da_rota
    from nucleo import baixa_sem_app as bsa, banco as nucleo_banco
    from vuupt_client import VuuptClient
    from insucesso_entrega.motivos_falha import MOTIVOS_FALHA
    # Recusa antes de ler: canhotos de 15 MB cada, nunca uma rota inteira de fotos gigantes.
    if (request.content_length or 0) > LIMITE_REQUISICAO_BAIXA:
        return jsonify({"erro": "Envio grande demais (mais de 60 MB no total): tire as fotos em resolução menor ou dê a baixa em duas etapas."}), 413
    # multipart quando vem canhoto (09/10): o JSON de antes vai no campo "dados"
    if (request.content_type or "").startswith("multipart/form-data"):
        try:
            body = json.loads(request.form.get("dados") or "{}")
        except ValueError:
            return jsonify({"erro": "dados inválidos"}), 400
    else:
        body = request.get_json(force=True) or {}
    try:
        route_id = int(body.get("rota"))
    except (TypeError, ValueError):
        return jsonify({"erro": "rota inválida"}), 400
    itens = body.get("itens")
    if not isinstance(itens, list) or not itens:
        return jsonify({"erro": "nenhum pedido"}), 400
    for i in itens:
        if not i.get("entregue") and int(i.get("failed_reason_id") or 0) not in MOTIVOS_FALHA:
            return jsonify({"erro": f"{i.get('codigo')}: escolha o motivo de quem voltou"}), 400
    rota, erro = _rota_sem_app(route_id)
    if erro:
        return jsonify({"erro": erro}), 400
    # O código do pedido vem da ROTA, nunca do navegador: vira nome de
    # arquivo do canhoto e entra na tratativa.
    codigo_por_id = {s.get("id"): (s.get("code") or "").lstrip("#") for s in extrair_servicos_da_rota(rota)}
    if any(int(i.get("service_id") or 0) not in codigo_por_id for i in itens):
        return jsonify({"erro": "pedido que não é dessa rota"}), 400
    for i in itens:
        i["codigo"] = codigo_por_id[int(i["service_id"])]
        i.pop("aviso", None)                     # o aviso e do servidor, nunca do navegador
        if not i.get("entregue"):
            i.pop("justificativa", None), i.pop("texto_outro", None)
    conn = nucleo_banco.conectar()
    try:
        if bsa.lote_em_andamento(conn, route_id):
            return jsonify({"erro": "Já tem uma baixa dessa rota em andamento. Aguarde terminar e recarregue."}), 409
        # Canhoto ANTES do lote: a expedicao roda a cada 30 min e a Stokki
        # nao aceita anexo em pedido ja expedido. Valida e converte tudo (em
        # memoria) antes de gravar qualquer coisa.
        from nucleo import canhotos_manuais, justificativas_sem_canhoto
        from vuupt_checklist import CAMPO_CANHOTO, CAMPO_NOTA_RETORNO, para_jpeg
        canhotos, fotos_por_servico = [], {}
        for i in itens:
            if not i.get("entregue"):
                continue
            sid = int(i.get("service_id") or 0)
            conteudos = {}
            for chave in ("canhoto", "nota"):
                arq = request.files.get(f"{chave}_{sid}")
                conteudo = arq.read(canhotos_manuais.LIMITE_BYTES + 1) if arq else b""
                if len(conteudo) > canhotos_manuais.LIMITE_BYTES:
                    return jsonify({"erro": f"{i.get('codigo')}: arquivo maior que 15 MB."}), 413
                conteudos[chave] = conteudo or None
            fotos, avisos = {}, []
            try:
                if conteudos["canhoto"]:
                    canhotos.append((i, canhotos_manuais.para_pdf(conteudos["canhoto"])))
                    if conteudos["canhoto"][:5] == b"%PDF-":
                        avisos.append("Canhoto em PDF: ficou só como cópia local; no checklist da VUUPT vão apenas fotos.")
                    else:
                        fotos[CAMPO_CANHOTO] = para_jpeg(conteudos["canhoto"])
            except (canhotos_manuais.CanhotoInvalido, ValueError) as e:
                return jsonify({"erro": f"{i.get('codigo')}: Canhoto: {e}"}), 400
            # Hugo, 30/10: a nota de retorno so vai junto da FOTO do canhoto (nunca sozinha nem com PDF)
            if conteudos["nota"] and CAMPO_CANHOTO not in fotos:
                conteudos["nota"] = None
                avisos.append(torre_controle.AVISO_NOTA_SEM_CANHOTO)
            if avisos:
                i["aviso"] = " ".join(avisos)
            try:
                if conteudos["nota"]:
                    fotos[CAMPO_NOTA_RETORNO] = para_jpeg(conteudos["nota"])
            except ValueError as e:
                msg = str(e)
                return jsonify({"erro": f"{i.get('codigo')}: Foto da nota de retorno: {msg[:1].lower()}{msg[1:]}"}), 400
            if conteudos["canhoto"]:
                i.pop("justificativa", None), i.pop("texto_outro", None)   # com canhoto a justificativa e ignorada
            else:
                try:
                    i["justificativa"], i["texto_outro"] = justificativas_sem_canhoto.validar(
                        i.get("justificativa"), i.get("texto_outro"))
                except ValueError as e:
                    return jsonify({"erro": f"{i.get('codigo')}: {e}"}), 400
            if fotos:
                fotos_por_servico[sid] = fotos
        por = session.get("usuario") or g.nivel_acesso
        for i, pdf in canhotos:
            canhotos_manuais.salvar(conn, i.get("codigo") or "", int(i["service_id"]), pdf, por, _carregar_config())
        lote = bsa.criar_lote(conn, route_id, rota.get("agent_id"), por, itens)
        try:
            bsa.gravar_fotos(conn, lote, fotos_por_servico)
        except Exception:  # noqa: BLE001 -- gravar_fotos ja marcou o lote como erro e logou o motivo
            return jsonify({"erro": "Não consegui guardar as fotos do checklist no servidor. Tente de novo."}), 500
    finally:
        conn.close()
    token = (_carregar_config().get("vuupt_api") or {}).get("token", "")
    threading.Thread(target=bsa.executar_lote, args=(lote, VuuptClient(token)), daemon=True).start()
    return jsonify({"lote": lote})


@app.route("/api/baixa-sem-app/<int:lote>")
@requer_auth(niveis=("total", "operador"))
def api_baixa_sem_app_status(lote: int):
    from nucleo import baixa_sem_app as bsa, banco as nucleo_banco
    conn = nucleo_banco.conectar()
    try:
        d = bsa.ler_lote(conn, lote)
    finally:
        conn.close()
    return (jsonify(d), 200) if d else (jsonify({"erro": "lote não encontrado"}), 404)


# Recebimento de retornos (Hugo, 23/10): insucessos e parciais que voltam
# ao galpão, conferidos pelo celular (retornos.py). Confirmar é de quem está
# no galpão ou opera; Atendimento e leitura só acompanham.
NIVEIS_RETORNOS_CONFIRMAR = ("total", "operador", "logistica", "galpao", "expedicao")
NIVEIS_RETORNOS_VER = NIVEIS_RETORNOS_CONFIRMAR + ("atendimento", "leitura")


@app.route("/retornos")
@requer_auth(niveis=NIVEIS_RETORNOS_VER)
def retornos_tela():
    # Tela única (mobile first, serve no desktop também): o "versão completa"
    # do base_mobile aponta pra ela mesma.
    return render_template("retornos.html", pode_confirmar=g.nivel_acesso in NIVEIS_RETORNOS_CONFIRMAR,
                           endpoint_desktop="retornos_tela")


@app.route("/api/retornos")
@requer_auth(niveis=NIVEIS_RETORNOS_VER)
def api_retornos():
    conn = retornos.conectar()
    try:
        motoristas = retornos.listar_pendentes(
            conn, date.today(), agent_ids_excluir=retornos.agent_ids_lalamove(_carregar_config()))
    finally:
        conn.close()
    return jsonify({"motoristas": motoristas, "motivos": retornos.motivos_validos()})


@app.route("/api/retornos/descarga", methods=["POST"])
@requer_auth(niveis=NIVEIS_RETORNOS_CONFIRMAR)
@exige_mesma_origem
def api_retornos_descarga():
    """Confirma uma descarga: fotos e assinatura vão pra
    dados/retornos/<id>/ antes de gravar; erro desfaz tudo (linha e pasta)."""
    try:
        dados = json.loads(request.form.get("dados") or "{}")
        itens = list(dados.get("itens") or [])
    except (ValueError, TypeError, AttributeError):
        return jsonify({"erro": "Dados da descarga inválidos."}), 400
    conn = retornos.conectar()
    descarga_id = retornos.reservar_descarga(conn)
    try:
        fotos_carga = [retornos.salvar_arquivo(descarga_id, "carga", f.read())
                       for f in request.files.getlist("foto_carga")]
        assinatura = request.files.get("assinatura")
        nome_assinatura = retornos.salvar_arquivo(descarga_id, "assinatura", assinatura.read()) if assinatura else ""
        for item in itens:
            pid = item.get("parada_id")
            item["fotos_avaria"] = [retornos.salvar_arquivo(descarga_id, f"avaria_{int(pid)}", f.read())
                                    for f in request.files.getlist(f"foto_avaria_{pid}")]
        resultado = retornos.registrar_descarga(
            conn, descarga_id=descarga_id, itens=itens, recebedor=dados.get("recebedor") or "",
            usuario=session.get("usuario") or g.nivel_acesso, fotos_carga=fotos_carga, assinatura=nome_assinatura)
    except (retornos.ErroRetorno, ValueError, TypeError) as e:
        retornos.descartar_descarga(conn, descarga_id)
        conn.close()
        msg = str(e) if isinstance(e, retornos.ErroRetorno) else "Dados da descarga inválidos."
        return jsonify({"erro": msg}), 400
    conn.close()

    # Cópia no GCS fora do request: no 4G do galpão, sete uploads em série
    # (ou o GCS fora do ar) deixavam o celular em "Enviando…" e geravam toque
    # repetido. O disco da VPS já tem tudo; o GCS é só cópia.
    threading.Thread(target=retornos.enviar_gcs, args=(_carregar_config(), descarga_id), daemon=True).start()
    texto = retornos.texto_alerta_descarga(resultado, " ".join((dados.get("recebedor") or "").split()))
    if texto:
        try:
            import grupos_equipe
            cg = grupos_equipe.conectar()
            try:
                retornos.postar_alerta(cg, texto)
            finally:
                cg.close()
        except Exception:
            logging.getLogger(__name__).exception("Retornos: descarga %s gravada, mas o alerta falhou", descarga_id)
    return jsonify({"ok": True, "descarga_id": descarga_id, "divergencias": len(resultado["divergentes"])})


@app.route("/api/retornos/descargas")
@requer_auth(niveis=NIVEIS_RETORNOS_VER)
def api_retornos_descargas():
    conn = retornos.conectar()
    try:
        return jsonify({"descargas": retornos.listar_descargas(conn)})
    finally:
        conn.close()


@app.route("/retornos/descarga/<int:descarga_id>")
@requer_auth(niveis=NIVEIS_RETORNOS_VER)
def retornos_descarga(descarga_id: int):
    conn = retornos.conectar()
    try:
        d = retornos.detalhar_descarga(conn, descarga_id)
    finally:
        conn.close()
    return jsonify(d) if d else (jsonify({"erro": "descarga não encontrada"}), 404)


@app.route("/retornos/arquivo/<int:descarga_id>/<nome>")
@requer_auth(niveis=NIVEIS_RETORNOS_VER)
def retornos_arquivo(descarga_id: int, nome: str):
    caminho = retornos.caminho_arquivo(descarga_id, nome)
    if not caminho:
        abort(404)
    return send_file(caminho, max_age=3600)


@app.route("/canhotos-pendentes")
@requer_auth(niveis=("total", "operador", "logistica"))
def canhotos_pendentes():
    """Pedidos entregues sem nenhum comprovante (Hugo, 09/10): sobe o
    canhoto e fica guardado conosco (servidor + bucket) e no batimento.
    Não vai pra Stokki: pedido já expedido não aceita anexo."""
    from datetime import date, timedelta
    from nucleo import banco as nucleo_banco, canhotos_manuais
    filtros = {
        "desde": request.args.get("desde") or (date.today() - timedelta(days=60)).isoformat(),
        "embarcador": request.args.get("embarcador", ""),
        "motorista": request.args.get("motorista", ""),
        "busca": request.args.get("busca", ""),
    }
    pagina = request.args.get("pagina", 1, type=int)
    conn = nucleo_banco.conectar()
    try:
        dados = canhotos_manuais.pendentes(conn, filtros["desde"], filtros["embarcador"], filtros["motorista"],
                                           filtros["busca"], pagina=pagina)
    finally:
        conn.close()
    return render_template("canhotos_pendentes.html", dados=dados, filtros=filtros)


@app.route("/api/canhotos-pendentes", methods=["POST"])
@requer_auth(niveis=("total", "operador", "logistica"))
@exige_mesma_origem
def api_canhotos_pendentes():
    from nucleo import banco as nucleo_banco, canhotos_manuais
    if (request.content_length or 0) > LIMITE_REQUISICAO_BAIXA:
        return jsonify({"erro": "Arquivo grande demais."}), 413
    arq = request.files.get("arquivo")
    if not arq:
        return jsonify({"erro": "Escolha o arquivo do canhoto."}), 400
    try:
        codigo = canhotos_manuais.codigos_validos(request.form.get("codigo") or "")[0]
        pdf = canhotos_manuais.para_pdf(arq.read())
    except canhotos_manuais.CanhotoInvalido as e:
        return jsonify({"erro": str(e)}), 400
    conn = nucleo_banco.conectar()
    try:
        # só pedido que está mesmo pendente (entregue, sem nenhum comprovante)
        achados = canhotos_manuais.pendentes(conn, "2000-01-01", busca=codigo, por_pagina=20)["linhas"]
        linha = next((l for l in achados if l["codigo"] == codigo), None)
        if not linha:
            return jsonify({"erro": f"{codigo} não está entre os entregues sem comprovante."}), 400
        canhotos_manuais.salvar(conn, codigo, linha["service_id"], pdf,
                                session.get("usuario") or g.nivel_acesso, _carregar_config())
    finally:
        conn.close()
    return jsonify({"ok": True, "codigo": codigo})


@app.route("/clientes-agenda")
@requer_auth(niveis=("total", "logistica", "atendimento"))
def clientes_agenda():
    """Fila de destinatários com data de agendamento informada esperando
    o Hugo autorizar a marcação AGENDA na BD_CLIENTES (30/09). Quem
    alimenta é marcar_clientes_agenda.py (sequências 18h/22h); o link
    chega pelo WhatsApp. Só nível total: a decisão grava na planilha
    (sem cópia por clique: o backup é o diário das 03h pro GCS)."""
    import marcar_clientes_agenda
    try:
        linhas = marcar_clientes_agenda.listar()
        erro = None
    except Exception as e:
        logging.getLogger(__name__).exception("Falha ao listar clientes com agendamento")
        linhas, erro = [], str(e)
    pendentes = [l for l in linhas if l["situacao"] == marcar_clientes_agenda.PENDENTE]
    decididos = [l for l in linhas if l["situacao"] != marcar_clientes_agenda.PENDENTE][:50]
    # Atendimento (21/10, Hugo) so ve a fila: decidir continua total/logistica.
    return render_template("clientes_agenda.html", pendentes=pendentes, decididos=decididos, erro=erro,
                           pode_decidir=g.nivel_acesso in ("total", "logistica"))


@app.route("/adesao-portal")
@requer_auth(niveis=("total", "operador", "logistica", "leitura", "atendimento"))
def adesao_portal():
    """Quem já entrou no portal do cliente e quem importa pedidos por lá
    (21/10). Lê o banco a cada abertura; a página se recarrega a cada 2h."""
    import adesao_portal as _adesao
    linhas = _adesao.listar()
    return render_template("adesao_portal.html", linhas=linhas, resumo=_adesao.resumo(linhas),
                           dias_ativo=_adesao.DIAS_ATIVO, gerado_em=datetime.now().strftime("%d/%m %H:%M"))


_NIVEIS_EDITA_TRANSPORTADORAS = ("total", "operador", "logistica")


@app.route("/transportadoras")
@requer_auth(niveis=("total", "operador", "logistica", "leitura", "atendimento"))
def transportadoras():
    """Cadastro de transportadoras (23/10): aba Pendentes (desconhecidas que
    seguraram pedidos, a equipe responde pelo cliente) e aba Todas. Quem
    grava e valida e transportadoras_cadastro; aqui so se mostra."""
    import transportadoras_tela as tt
    aba = "todas" if request.args.get("aba") == "todas" else "pendentes"
    conn = tt.conectar()
    try:
        linhas = tt.montar_lista(conn, aba, request.args.get("busca", ""), request.args.get("tipo", ""),
                                 request.args.get("situacao", ""))
    finally:
        conn.close()
    n_pendentes = tt.contar_pendentes()
    return render_template("transportadoras.html", linhas=linhas, aba=aba, n_pendentes=n_pendentes,
                           busca=request.args.get("busca", ""), tipo=request.args.get("tipo", ""),
                           situacao=request.args.get("situacao", ""), respostas=tt.RESPOSTAS,
                           pode_editar=g.nivel_acesso in _NIVEIS_EDITA_TRANSPORTADORAS)


def _corpo_dict() -> dict:
    corpo = request.get_json(force=True, silent=True)
    if not isinstance(corpo, dict):
        abort(Response(json.dumps({"erro": "Corpo da requisição inválido."}), 400, mimetype="application/json"))
    return corpo


def _autor_transportadoras() -> str:
    return session.get("usuario") or g.nivel_acesso


def _liberar_bloqueados_transportadora(transp: dict) -> dict:
    """Hugo 29/10: classificar como retirada/redespacho solta as NFs do portal
    presas no bloqueio de area por essa transportadora (bloqueio_area)."""
    from portal_cliente import bloqueio_area
    bloqueio_area.liberar_por_transportadora(transp, _autor_transportadoras())
    return transp


@app.route("/api/transportadoras/<int:tid>")
@requer_auth(niveis=("total", "operador", "logistica", "leitura", "atendimento"))
def api_transportadora_ficha(tid):
    import transportadoras_tela as tt
    conn = tt.conectar()
    try:
        f = tt.ficha(conn, tid)
    finally:
        conn.close()
    if f is None:
        return jsonify({"erro": "Transportadora não encontrada."}), 404
    return jsonify(f)


@app.route("/api/transportadoras/cep/<cep>")
@requer_auth(niveis=_NIVEIS_EDITA_TRANSPORTADORAS)
def api_transportadoras_cep(cep):
    from portal_cliente import cotacao
    achado = cotacao.consultar_cep(cep)
    if not achado:
        return jsonify({"erro": "CEP não encontrado."}), 404
    return jsonify(achado)


@app.route("/api/transportadoras", methods=["POST"])
@requer_auth(niveis=_NIVEIS_EDITA_TRANSPORTADORAS)
@exige_mesma_origem
def api_transportadora_criar():
    import transportadoras_cadastro as tc
    import transportadoras_tela as tt
    conn = tt.conectar()
    try:
        return jsonify(_liberar_bloqueados_transportadora(tc.criar(conn, tt.so_campos(_corpo_dict()), "TELA",
                                                                   _autor_transportadoras())))
    except tc.ErroCadastro as e:
        return jsonify({"erro": str(e)}), 400
    finally:
        conn.close()


@app.route("/api/transportadoras/<int:tid>", methods=["POST"])
@requer_auth(niveis=_NIVEIS_EDITA_TRANSPORTADORAS)
@exige_mesma_origem
def api_transportadora_atualizar(tid):
    """Edicao pela ficha. Tirar de PENDENTE (ATIVA/INATIVA) por aqui tambem
    fecha a consulta e libera os parados (canal 'tela')."""
    import consulta_transportadora as ct
    import transportadoras_cadastro as tc
    import transportadoras_tela as tt
    conn = tt.conectar()
    try:
        return jsonify(_liberar_bloqueados_transportadora(ct.atualizar_cadastro(conn, tid, tt.so_campos(_corpo_dict()),
                                                                                _autor_transportadoras(), "tela")))
    except tc.ErroCadastro as e:
        return jsonify({"erro": str(e)}), 400
    finally:
        conn.close()


@app.route("/api/transportadoras/<int:tid>/responder", methods=["POST"])
@requer_auth(niveis=_NIVEIS_EDITA_TRANSPORTADORAS)
@exige_mesma_origem
def api_transportadora_responder(tid):
    """A equipe responde pelo embarcador (canal 'tela'): vira ATIVA e libera
    os pedidos parados dela."""
    import consulta_transportadora as ct
    import transportadoras_cadastro as tc
    import transportadoras_tela as tt
    body = _corpo_dict()
    endereco = body.get("endereco")
    conn = tt.conectar()
    try:
        return jsonify(_liberar_bloqueados_transportadora(
            ct.responder(conn, tid, str(body.get("tipo") or ""), endereco if isinstance(endereco, dict) else {},
                         str(body.get("email") or "").strip(), _autor_transportadoras(), "tela")))
    except tc.ErroCadastro as e:
        return jsonify({"erro": str(e)}), 400
    finally:
        conn.close()


@app.route("/api/clientes-agenda/decidir", methods=["POST"])
@requer_auth(niveis=("total", "logistica"))
@exige_mesma_origem
def api_clientes_agenda_decidir():
    import marcar_clientes_agenda
    body = request.get_json(force=True) or {}
    acao = body.get("acao")
    if acao not in ("autorizar", "nao_marcar"):
        return jsonify({"erro": "ação inválida"}), 400
    caminho = _carregar_config().get("clientes_agendamento", {}).get("planilha", "")
    try:
        resultado = marcar_clientes_agenda.decidir(body.get("documento", ""), acao == "autorizar", caminho)
    except ValueError as e:
        return jsonify({"erro": str(e)}), 400
    except Exception as e:
        logging.getLogger(__name__).exception("Falha ao decidir cliente com agendamento")
        return jsonify({"erro": str(e)}), 500
    return jsonify({"ok": True, "situacao": resultado["situacao"]})


@app.route("/galpao-3d")
@requer_auth
def galpao_3d():
    """Galpao 3D (so nivel total, fora do catalogo de acesso_telas): casca
    da pagina; o JS estatico desenha o galpao com o JSON da API abaixo."""
    return render_template("galpao_3d.html")


@app.route("/api/galpao-3d/estado")
@requer_auth
def api_galpao_3d_estado():
    conn = galpao3d_banco.conectar()
    try:
        return jsonify(galpao3d_estado.montar_estado(conn, _carregar_config()))
    finally:
        conn.close()


@app.route("/acessos")
@requer_auth
def acessos():
    """Acesso personalizado por usuario (21/10, acesso_telas.py): o total
    escolhe as telas de cada login nominal. So nivel total de verdade --
    a tela nao esta no catalogo, entao nenhum personalizado chega aqui."""
    conn = acesso_telas.conectar()
    try:
        usuarios = acesso_telas.listar(conn)
        rodada = acesso_telas.rodada_sessoes(conn)
    finally:
        conn.close()
    grupos = {}
    for chave, rotulo, grupo, _ in acesso_telas.TELAS + acesso_telas.PERMISSOES:
        grupos.setdefault(grupo or "Início", []).append({"chave": chave, "rotulo": rotulo})
    return render_template("acessos.html", usuarios=usuarios, grupos=list(grupos.items()),
                           niveis=acesso_telas.NIVEIS, equipes=acesso_telas.EQUIPES,
                           senha_minima=acesso_telas.SENHA_MINIMA,
                           eu=session.get("usuario", ""), rodada=rodada)


@app.route("/api/acessos", methods=["POST"])
@requer_auth
@exige_mesma_origem
def api_acessos():
    """Salva um login nominal: telas e, quando vierem, nome e nivel (23/10)."""
    body = request.get_json(force=True, silent=True) or {}
    telas = body.get("telas")
    if telas is not None and not isinstance(telas, list):
        return jsonify({"erro": "telas inválidas"}), 400
    if "nivel" in body and body.get("nivel") not in acesso_telas.NIVEIS:
        return jsonify({"erro": "nível inválido"}), 400
    usuario = str(body.get("usuario") or "")
    por = session.get("usuario", "")
    conn = acesso_telas.conectar()
    try:
        acesso_telas.salvar(conn, usuario, telas, por)
        if "nivel" in body:
            acesso_telas.editar(conn, usuario, str(body.get("nome") or ""), body["nivel"], por,
                                str(body.get("equipe") or ""))
        if "portal_equipe" in body:
            acesso_telas.mudar_portal(conn, usuario, bool(body["portal_equipe"]), por)
    except ValueError as e:
        return jsonify({"erro": str(e)}), 400
    finally:
        conn.close()
    return jsonify({"ok": True})


@app.route("/api/acessos/novo", methods=["POST"])
@requer_auth
@exige_mesma_origem
def api_acessos_novo():
    """Cadastra login nominal com senha provisoria (troca no 1o acesso)."""
    body = request.get_json(force=True, silent=True) or {}
    cfg_painel = _carregar_config().get("painel_agentes", {})
    conn = acesso_telas.conectar()
    try:
        usuario = acesso_telas.criar(conn, str(body.get("usuario") or ""), str(body.get("nome") or ""),
                                     str(body.get("nivel") or ""), str(body.get("senha") or ""),
                                     session.get("usuario", ""), _logins_fixos(cfg_painel),
                                     str(body.get("equipe") or ""))
    except ValueError as e:
        return jsonify({"erro": str(e)}), 400
    finally:
        conn.close()
    return jsonify({"ok": True, "usuario": usuario})


@app.route("/api/acessos/derrubar-sessoes", methods=["POST"])
@requer_auth
@exige_mesma_origem
def api_acessos_derrubar_sessoes():
    """Derruba todas as sessoes abertas do painel, inclusive a de quem clicou."""
    conn = acesso_telas.conectar()
    try:
        acesso_telas.derrubar_sessoes(conn, session.get("usuario", ""))
    finally:
        conn.close()
    session.clear()
    return jsonify({"ok": True, "login": url_for("login")})


@app.route("/api/acessos/ativo", methods=["POST"])
@requer_auth
@exige_mesma_origem
def api_acessos_ativo():
    """Desativa (nao entra, sessao aberta cai) ou reativa um login nominal."""
    body = request.get_json(force=True, silent=True) or {}
    conn = acesso_telas.conectar()
    try:
        acesso_telas.mudar_ativo(conn, str(body.get("usuario") or ""), bool(body.get("ativo")),
                                 session.get("usuario", ""))
    except ValueError as e:
        return jsonify({"erro": str(e)}), 400
    finally:
        conn.close()
    return jsonify({"ok": True})


@app.route("/api/acessos/senha", methods=["POST"])
@requer_auth
@exige_mesma_origem
def api_acessos_senha():
    """Redefine a senha de outro login nominal (provisoria, troca no proximo acesso)."""
    body = request.get_json(force=True, silent=True) or {}
    conn = acesso_telas.conectar()
    try:
        acesso_telas.redefinir_senha(conn, str(body.get("usuario") or ""), str(body.get("senha") or ""),
                                     session.get("usuario", ""))
    except ValueError as e:
        return jsonify({"erro": str(e)}), 400
    finally:
        conn.close()
    return jsonify({"ok": True})


# --- Notificacoes (24/10): geral, tipo, excecao, piloto e e-mails do embarcador.
# So nivel total (sem niveis=) e fora do catalogo de acesso_telas. Quem decide e
# grava e controle_notificacoes; a tela e o que o Hugo usa no lugar do config.

def _autor_notificacoes() -> str:
    return session.get("usuario") or g.nivel_acesso


@app.route("/notificacoes")
@requer_auth
def notificacoes():
    import controle_notificacoes as cn
    import notificacoes_tela as nt
    conn = cn.conectar()
    try:
        return render_template("notificacoes.html", geral=cn.estado_geral(conn), lista=nt.montar_lista(conn),
                               piloto=cn.piloto(conn), historico=nt.historico_legivel(conn),
                               embarcadores=nt.lista_embarcadores(conn),
                               disparaveis={t: list(i["alvos"]) for t, i in nt.DISPARAVEIS.items()},
                               sem_disparo=nt.SEM_DISPARO,
                               so_pelo_timer=nt.SO_PELO_TIMER,
                               nota_teste=nt.NOTA_TESTE,
                               tipos={t: {"rotulo": i["rotulo"], "destino": i["destino"], "canais": list(i["canais"]),
                                          "quando": i["quando"]} for t, i in cn.TIPOS.items()})
    finally:
        conn.close()


@app.route("/api/notificacoes/tipo/<tipo>")
@requer_auth
def api_notificacoes_tipo(tipo):
    import controle_notificacoes as cn
    import notificacoes_tela as nt
    if tipo not in cn.TIPOS:
        return jsonify({"erro": "Tipo de notificação desconhecido."}), 404
    conn = cn.conectar()
    try:
        return jsonify(nt.detalhe_tipo(conn, tipo))
    finally:
        conn.close()


@app.route("/api/notificacoes/historico")
@requer_auth
def api_notificacoes_historico():
    import controle_notificacoes as cn
    import notificacoes_tela as nt
    conn = cn.conectar()
    try:
        return jsonify(nt.historico_legivel(conn))
    finally:
        conn.close()


@app.route("/api/notificacoes/geral", methods=["POST"])
@requer_auth
@exige_mesma_origem
def api_notificacoes_geral():
    """Desligar vale na hora; ligar o geral pede confirmacao (409 sem `confirmado`)."""
    import controle_notificacoes as cn
    corpo = _corpo_dict()
    ligado = corpo.get("ligado") is True
    conn = cn.conectar()
    try:
        if ligado and corpo.get("confirmado") is not True and cn.estado_geral(conn) != "LIGADO":
            ligados = sum(1 for t in cn.TIPOS if cn.estado_tipo(conn, t) == "LIGADO")
            return jsonify({"erro": "Confirme para ligar o geral.", "confirmar": True, "geral": True,
                            "tipos_ligados": ligados}), 409
        cn.definir_geral(conn, ligado, _autor_notificacoes())
    finally:
        conn.close()
    return jsonify({"ok": True})


@app.route("/api/notificacoes/estado", methods=["POST"])
@requer_auth
@exige_mesma_origem
def api_notificacoes_estado():
    """Troca o estado de um tipo. LIGADO (cliente recebe de verdade) responde
    409 com a contagem de destinatarios ate vir `confirmado: true`."""
    import controle_notificacoes as cn
    import notificacoes_tela as nt
    corpo = _corpo_dict()
    tipo, estado = str(corpo.get("tipo") or ""), str(corpo.get("estado") or "")
    if tipo not in cn.TIPOS or estado not in cn.ESTADOS:
        return jsonify({"erro": "Tipo ou estado inválido."}), 400
    conn = cn.conectar()
    try:
        if estado == "LIGADO" and corpo.get("confirmado") is not True and cn.estado_tipo(conn, tipo) != "LIGADO":
            return jsonify({"erro": "Confirme para ligar.", "confirmar": True, "tipo": tipo,
                            "destinatarios": nt.contar_destinatarios(conn, tipo)}), 409
        cn.definir_estado(conn, tipo, estado, _autor_notificacoes())
    except ValueError as e:
        return jsonify({"erro": str(e)}), 400
    finally:
        conn.close()
    return jsonify({"ok": True})


@app.route("/api/notificacoes/excecao", methods=["POST"])
@requer_auth
@exige_mesma_origem
def api_notificacoes_excecao():
    import controle_notificacoes as cn
    corpo = _corpo_dict()
    conn = cn.conectar()
    try:
        cn.definir_excecao(conn, str(corpo.get("tipo") or ""), str(corpo.get("destino") or ""),
                           corpo.get("regra"), _autor_notificacoes())
    except (ValueError, KeyError) as e:
        return jsonify({"erro": str(e)}), 400
    finally:
        conn.close()
    return jsonify({"ok": True})


@app.route("/api/notificacoes/piloto", methods=["POST"])
@requer_auth
@exige_mesma_origem
def api_notificacoes_piloto():
    import controle_notificacoes as cn
    corpo = _corpo_dict()
    conn = cn.conectar()
    try:
        cn.definir_piloto(conn, str(corpo.get("email") or ""), str(corpo.get("whatsapp") or ""),
                          _autor_notificacoes())
    except ValueError as e:
        return jsonify({"erro": str(e)}), 400
    finally:
        conn.close()
    return jsonify({"ok": True})


@app.route("/api/notificacoes/desfazer", methods=["POST"])
@requer_auth
@exige_mesma_origem
def api_notificacoes_desfazer():
    """Volta ao valor anterior. Se isso religa um tipo (ou o geral), pede a mesma
    confirmacao de ligar (409 com a contagem ate vir `confirmado: true`)."""
    import controle_notificacoes as cn
    import notificacoes_tela as nt
    corpo = _corpo_dict()
    conn = cn.conectar()
    try:
        hid = int(corpo.get("id"))
        pendente = nt.confirmacao_do_desfazer(conn, hid)   # tambem recusa item sem desfazer
        if pendente and corpo.get("confirmado") is not True:
            return jsonify(dict(pendente, erro="Confirme para ligar.", confirmar=True)), 409
        cn.desfazer(conn, hid, _autor_notificacoes())
    except (ValueError, TypeError) as e:
        return jsonify({"erro": str(e)}), 400
    finally:
        conn.close()
    return jsonify({"ok": True})


@app.route("/api/notificacoes/embarcador/<cnpj>")
@requer_auth
def api_notificacoes_embarcador(cnpj):
    """E-mails e WhatsApp do embarcador: a mesma lista que ele edita no portal
    (preferencias_notificacao)."""
    import notificacoes_tela as nt
    import preferencias_notificacao as pn
    conn = nt.conectar_preferencias()
    try:
        return jsonify(pn.ler(conn, cnpj))
    except ValueError as e:
        return jsonify({"erro": str(e)}), 400
    finally:
        conn.close()


@app.route("/api/notificacoes/embarcador/<cnpj>", methods=["POST"])
@requer_auth
@exige_mesma_origem
def api_notificacoes_embarcador_salvar(cnpj):
    """Grava e-mails e/ou WhatsApp (chave ausente = nao mexe) como `painel:<usuario>`
    e registra no historico do controle (so registro, sem desfazer)."""
    import notificacoes_tela as nt
    corpo = _corpo_dict()
    try:
        return jsonify(nt.salvar_contato(cnpj, corpo, "painel:" + _autor_notificacoes()))
    except ValueError as e:
        return jsonify({"erro": str(e)}), 400


@app.route("/api/notificacoes/embarcador/<cnpj>/situacao")
@requer_auth
def api_notificacoes_embarcador_situacao(cnpj):
    """Detalhe do embarcador: estado de cada tipo, a excecao dele e a preferencia no portal (so leitura)."""
    import controle_notificacoes as cn
    import notificacoes_tela as nt
    conn = cn.conectar()
    try:
        return jsonify(nt.situacao_embarcador(conn, cnpj))
    except ValueError as e:
        return jsonify({"erro": str(e)}), 404
    finally:
        conn.close()


@app.route("/api/notificacoes/disparo/<tipo>")
@requer_auth
def api_notificacoes_disparo_dados(tipo):
    """Dados da janela Disparar agora: alvos do tipo, quem ja recebeu hoje
    (so notas em rota e resumo), lista pro alvo "um" e os ultimos disparos."""
    import controle_notificacoes as cn
    import notificacoes_tela as nt
    if tipo not in nt.DISPARAVEIS:
        return jsonify({"erro": "Este tipo não tem disparo manual."}), 404
    alvos = nt.DISPARAVEIS[tipo]["alvos"]
    conn = cn.conectar()
    try:
        return jsonify({"alvos": list(alvos), "ja_recebeu_hoje": nt.ja_recebeu_hoje(conn, tipo),
                        "embarcadores": nt.embarcadores_disparo(conn) if "um" in alvos else [],
                        "ultimos": nt.ultimos_disparos(conn, tipo)})
    finally:
        conn.close()


@app.route("/api/notificacoes/disparo", methods=["POST"])
@requer_auth
@exige_mesma_origem
def api_notificacoes_disparo():
    """Roda a rotina do tipo em segundo plano (teste, todos ou um embarcador).
    409 se ja ha disparo do mesmo tipo rodando."""
    import controle_notificacoes as cn
    import notificacoes_tela as nt
    corpo = _corpo_dict()
    sender = corpo.get("sender_id")
    try:
        sender = int(sender) if sender not in (None, "") else None
    except (TypeError, ValueError):
        return jsonify({"erro": "Embarcador inválido."}), 400
    conn = cn.conectar()
    try:
        disparo_id = nt.iniciar_disparo(conn, str(corpo.get("tipo") or ""), str(corpo.get("alvo") or ""), sender,
                                        corpo.get("reenviar") is True, _autor_notificacoes())
    except ValueError as e:
        return jsonify({"erro": str(e)}), 400
    except RuntimeError as e:
        return jsonify({"erro": str(e)}), 409
    finally:
        conn.close()
    return jsonify({"id": disparo_id})


# Pedidos parados (24/10): galpao e expedicao entram pra REGISTRAR (aba
# Registrar, no lugar do Fresh Hub do Lovable) e veem a triagem so leitura.
NIVEIS_PP_VER = ("total", "operador", "logistica", "leitura", "atendimento", "galpao", "expedicao")
NIVEIS_PP_REGISTRAR = ("total", "operador", "logistica", "galpao", "expedicao")


@app.route("/pedidos-parados")
@requer_auth(niveis=NIVEIS_PP_VER)
def pedidos_parados():
    """Pedidos parados -- pedido do Hugo, 24/08. Aba Registrar (24/10:
    o galpao registra aqui, tabela pedidos_parados_registros) e aba
    Triagem. Página sobe só com a casca; os dados chegam por
    /api/pedidos-parados/* via JS (a Vuupt pode levar alguns segundos,
    não deve travar o load -- mesmo padrão da Torre)."""
    return render_template(
        "pedidos_parados_triagem.html",
        pode_editar=g.nivel_acesso in ("total", "operador", "logistica", "atendimento"),
        pode_registrar=g.nivel_acesso in NIVEIS_PP_REGISTRAR,
        aba_inicial="registrar" if g.nivel_acesso in ("galpao", "expedicao") else "triagem",
        encaminhamento_ativo=pedidos_parados_triagem.ENCAMINHAMENTO_DEMANDAS_ATIVO,
    )


@app.route("/api/pedidos-parados/registros-hoje")
@requer_auth(niveis=NIVEIS_PP_VER)
def api_pedidos_parados_registros_hoje():
    return jsonify(pedidos_parados_triagem.registros_hoje(session.get("usuario") or g.nivel_acesso))


@app.route("/api/pedidos-parados/registrar", methods=["POST"])
@requer_auth(niveis=NIVEIS_PP_REGISTRAR)
@exige_mesma_origem
def api_pedidos_parados_registrar():
    """Registra um pedido parado (multipart: order_number, volumes, foto
    opcional). Registrar de novo o mesmo pedido no dia e permitido (e o
    fluxo normal enquanto continua parado); a tela so avisa."""
    foto = request.files.get("foto")
    try:
        resultado = pedidos_parados_triagem.registrar(
            request.form.get("order_number", ""), request.form.get("volumes", ""),
            session.get("usuario") or g.nivel_acesso, foto.read() if foto else None,
        )
    except pedidos_parados_triagem.ErroRegistro as e:
        return jsonify({"erro": str(e)}), 400
    return jsonify(resultado)


@app.route("/api/pedidos-parados/registro/apagar", methods=["POST"])
@requer_auth(niveis=NIVEIS_PP_REGISTRAR)
@exige_mesma_origem
def api_pedidos_parados_registro_apagar():
    """So quem registrou, ate 15 min depois (erro de digitacao)."""
    body = request.get_json(force=True, silent=True) or {}
    try:
        pedidos_parados_triagem.apagar_registro(int(body.get("id") or 0), session.get("usuario") or g.nivel_acesso)
    except pedidos_parados_triagem.ErroRegistro as e:
        return jsonify({"erro": str(e)}), (404 if "não encontrado" in str(e) else 403)
    return jsonify({"ok": True})


@app.route("/pedidos-parados/foto/<int:registro_id>")
@requer_auth(niveis=NIVEIS_PP_VER)
def pedidos_parados_foto(registro_id: int):
    caminho = pedidos_parados_triagem.caminho_foto(registro_id)
    if not caminho:
        abort(404)
    return send_file(caminho, max_age=3600)


@app.route("/api/pedidos-parados/dados")
@requer_auth(niveis=NIVEIS_PP_VER)
def api_pedidos_parados_dados():
    try:
        return jsonify({"pedidos": pedidos_parados_triagem.listar_com_classificacao()})
    except Exception as e:
        logging.getLogger(__name__).exception("Falha ao montar dados de pedidos parados")
        return jsonify({"erro": str(e)}), 500


@app.route("/api/pedidos-parados/classificar", methods=["POST"])
@requer_auth(niveis=("total", "operador", "logistica", "atendimento"))
@exige_mesma_origem
def api_pedidos_parados_classificar():
    body = request.get_json(force=True)
    try:
        pedidos_parados_triagem.classificar(
            body["order_number"], body.get("freshhub_id", ""),
            body["classificacao"], session.get("usuario", "desconhecido"),
        )
    except KeyError as e:
        return jsonify({"erro": f"campo obrigatório ausente: {e}"}), 400
    except ValueError as e:
        return jsonify({"erro": str(e)}), 400
    except Exception as e:
        logging.getLogger(__name__).exception("Falha ao classificar pedido parado")
        return jsonify({"erro": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/api/pedidos-parados/buscar-vuupt")
@requer_auth(niveis=NIVEIS_PP_VER)
def api_pedidos_parados_buscar_vuupt():
    order_number = request.args.get("order_number", "")
    if not order_number:
        return jsonify({"erro": "parâmetro order_number ausente"}), 400
    try:
        resultado = pedidos_parados_triagem.buscar_sucesso_vuupt(order_number)
    except Exception as e:
        logging.getLogger(__name__).exception("Falha ao buscar pedido parado na Vuupt")
        return jsonify({"erro": str(e)}), 500
    return jsonify(resultado)


@app.route("/api/pedidos-parados/duplicar", methods=["POST"])
@requer_auth(niveis=("total", "operador", "logistica", "atendimento"))
@exige_mesma_origem
def api_pedidos_parados_duplicar():
    body = request.get_json(force=True)
    try:
        resultado = pedidos_parados_triagem.duplicar(
            body["order_number"], session.get("usuario", "desconhecido"),
        )
    except KeyError as e:
        return jsonify({"erro": f"campo obrigatório ausente: {e}"}), 400
    except (ValueError, RuntimeError) as e:
        return jsonify({"erro": str(e)}), 400
    except Exception as e:
        logging.getLogger(__name__).exception("Falha ao duplicar pedido parado")
        return jsonify({"erro": str(e)}), 500
    return jsonify(resultado)


@app.route("/api/pedidos-parados/equipes-demanda")
@requer_auth(niveis=("total", "operador", "logistica", "atendimento"))
def api_pedidos_parados_equipes_demanda():
    """Equipes ativas das Demandas pro botao Encaminhar escolher (26/10), com
    a equipe ja sugerida por classificacao (Verificar com Cliente -> Atendimento)."""
    conn = demandas.conectar()
    try:
        equipes = demandas.equipes_ativas(conn)
    finally:
        conn.close()
    return jsonify({"equipes": equipes, "padrao": pedidos_parados_triagem.EQUIPE_PADRAO_POR_CLASSIFICACAO})


@app.route("/api/pedidos-parados/encaminhar", methods=["POST"])
@requer_auth(niveis=("total", "operador", "logistica", "atendimento"))
@exige_mesma_origem
def api_pedidos_parados_encaminhar():
    if not pedidos_parados_triagem.ENCAMINHAMENTO_DEMANDAS_ATIVO:
        return jsonify({"erro": "Encaminhamento às Demandas pausado (Hugo, 24/10)."}), 409
    body = request.get_json(force=True)
    try:
        resultado = pedidos_parados_triagem.encaminhar_operacao(
            body["order_number"], body["classificacao"], session.get("usuario", "desconhecido"),
            equipe_id=body.get("equipe_id"), autor=g.get("nome_usuario") or session.get("atendente_nome"),
        )
    except KeyError as e:
        return jsonify({"erro": f"campo obrigatório ausente: {e}"}), 400
    except ValueError as e:
        return jsonify({"erro": str(e)}), 400
    except Exception as e:
        logging.getLogger(__name__).exception("Falha ao encaminhar pedido parado pra operação")
        return jsonify({"erro": str(e)}), 500
    return jsonify(resultado)


@app.route("/api/pedidos-parados/notificar-cliente-retira", methods=["POST"])
@requer_auth(niveis=("total", "operador", "logistica", "atendimento"))
@exige_mesma_origem
def api_pedidos_parados_notificar_cliente_retira():
    body = request.get_json(force=True)
    try:
        resultado = pedidos_parados_triagem.notificar_cliente_retira(
            body["order_number"], session.get("usuario", "desconhecido"),
        )
    except KeyError as e:
        return jsonify({"erro": f"campo obrigatório ausente: {e}"}), 400
    except (ValueError, RuntimeError) as e:
        return jsonify({"erro": str(e)}), 400
    except Exception as e:
        logging.getLogger(__name__).exception("Falha ao notificar cliente retira")
        return jsonify({"erro": str(e)}), 500
    return jsonify(resultado)


@app.route("/api/pedidos-parados/verificar-stokki", methods=["POST"])
@requer_auth(niveis=("total", "operador", "logistica", "atendimento"))
@exige_mesma_origem
def api_pedidos_parados_verificar_stokki():
    """Consulta status/transportadora de todos os pedidos em tela na
    Stokki e classifica sozinho Cancelados / Cliente Retira -- pedido do
    Hugo, 28/08."""
    body = request.get_json(force=True) or {}
    pedidos = body.get("pedidos") or []
    if not isinstance(pedidos, list) or not pedidos:
        return jsonify({"erro": "lista 'pedidos' vazia"}), 400
    try:
        resultado = pedidos_parados_triagem.verificar_na_stokki(
            pedidos, session.get("usuario", "desconhecido"),
        )
    except RuntimeError as e:
        return jsonify({"erro": str(e)}), 409
    except Exception as e:
        logging.getLogger(__name__).exception("Falha ao verificar pedidos parados na Stokki")
        return jsonify({"erro": str(e)}), 500
    return jsonify(resultado)


@app.route("/api/pedidos-parados/verificar-vuupt", methods=["POST"])
@requer_auth(niveis=("total", "operador", "logistica", "atendimento"))
@exige_mesma_origem
def api_pedidos_parados_verificar_vuupt():
    """Consulta status/agendamento de todos os pedidos em tela na Vuupt
    e classifica sozinho Em Rota / Agendado -- pedido do Hugo, 28/08."""
    body = request.get_json(force=True) or {}
    pedidos = body.get("pedidos") or []
    if not isinstance(pedidos, list) or not pedidos:
        return jsonify({"erro": "lista 'pedidos' vazia"}), 400
    try:
        resultado = pedidos_parados_triagem.verificar_na_vuupt(
            pedidos, session.get("usuario", "desconhecido"),
        )
    except RuntimeError as e:
        return jsonify({"erro": str(e)}), 409
    except Exception as e:
        logging.getLogger(__name__).exception("Falha ao verificar pedidos parados na Vuupt")
        return jsonify({"erro": str(e)}), 500
    return jsonify(resultado)


@app.route("/torre")
@requer_auth(niveis=("total", "operador", "logistica", "leitura", "atendimento"))
def torre():
    """Torre de Controle (cockpit) -- pedido do Hugo, 12/08. A página
    sobe só com a casca; os dados chegam por /api/torre/* via JS (a
    coleta na VUUPT leva alguns segundos e não deve segurar o load).

    Desde 15/09 (Hugo) a torre não recebe data: mostra as rotas ainda
    abertas de qualquer dia (torre_controle._coletar_rotas_abertas);
    consulta de dia passado é o Histórico (/consulta)."""
    gmaps_key = _carregar_config().get("google_maps", {}).get("api_key", "")
    pode_editar = g.nivel_acesso in ("total", "operador", "logistica")
    return render_template(
        "torre_controle.html", hoje_iso=date.today().isoformat(),
        google_maps_key=gmaps_key, pode_editar=pode_editar,
        url_regioes=URL_PAGINA_REGIOES,   # consulta interna dos dias de atendimento (Hugo, 14/10)
        url_cotacao=URL_PAGINA_COTACAO,
        # Nível "atendimento" (Hugo, 28/09): trata ocorrência (tratar,
        # desfazer, duplicar, notificar), mas não roda agente, não
        # encerra rota nem exclui pedido -- isso segue em pode_editar.
        pode_tratar=pode_editar or g.nivel_acesso == "atendimento",
        motivos_exclusao=MOTIVOS_EXCLUSAO,
        # "Excluir da Rota" do chip (25/08, mesmo botão da Expedição):
        # sem data na torre, toda rota mostrada é "de hoje em diante"
        # do ponto de vista da operação (ainda aberta).
        pode_excluir_pedido=pode_editar,
        justificativas=justificativas_sem_canhoto.JUSTIFICATIVAS,
    )


@app.route("/torre/mobile")
@requer_auth(niveis=("total", "operador", "logistica", "leitura", "atendimento"))
def torre_mobile():
    """Versão mobile da Torre de Controle (Hugo, 17/08) -- mesmo template
    "casca vazia + JS" do desktop, só que consumindo os mesmos
    /api/torre/* endpoints com uma renderização em cards (em vez de
    tabela/grid) pensada pra tela estreita. Nenhum endpoint novo."""
    gmaps_key = _carregar_config().get("google_maps", {}).get("api_key", "")
    pode_editar = g.nivel_acesso in ("total", "operador", "logistica")
    return render_template(
        "torre_mobile.html", hoje_iso=date.today().isoformat(),
        google_maps_key=gmaps_key, pode_editar=pode_editar,
        pode_tratar=pode_editar or g.nivel_acesso == "atendimento",
        endpoint_desktop="torre", url_regioes=URL_PAGINA_REGIOES, url_cotacao=URL_PAGINA_COTACAO,
        motivos_exclusao=MOTIVOS_EXCLUSAO,
        pode_excluir_pedido=pode_editar,
    )


@app.route("/api/torre/dados")
@requer_auth(niveis=("total", "operador", "logistica", "leitura", "atendimento"))
def api_torre_dados():
    try:
        return jsonify(torre_controle.buscar_dados_torre())
    except Exception as e:
        logging.getLogger(__name__).exception("Falha ao montar dados da torre")
        return jsonify({"erro": str(e)}), 500


@app.route("/api/torre/stokki")
@requer_auth(niveis=("total", "operador", "logistica", "leitura", "atendimento"))
def api_torre_stokki():
    """Funil outbound da Stokki -- endpoint separado do resto porque tem
    cache próprio (TTL 5 min) e trava de sessão (não consulta ao vivo
    com agente rodando)."""
    return jsonify(torre_controle.buscar_funil_stokki())


@app.route("/api/torre/excluir-pedido", methods=["POST"])
@requer_auth(niveis=("total", "operador", "logistica"))
@exige_mesma_origem
def api_torre_excluir_pedido():
    """Tira 1 pedido de uma rota já criada na VUUPT -- "Excluir da Rota"
    no menu de contexto de um chip pendente/em rota da Torre (Hugo,
    25/08): mesmo botão e mesmo motivo (dropdown fixo, ver
    expedicao.MOTIVOS_EXCLUSAO) da tela de Expedição, mas aqui a rota
    pode já estar rodando -- permitir_rota_em_andamento=True troca a
    trava de "rota não iniciada" pra "rota não encerrada" (ver
    expedicao.excluir_pedido_da_rota). Fora do nível "leitura" de
    propósito -- é uma ação que muda a rota na VUUPT."""
    body = request.get_json(force=True)
    try:
        rota_id = int(body["rota_id"])
        service_id = int(body["service_id"])
        motivo = str(body["motivo"])
    except (KeyError, ValueError, TypeError) as e:
        return jsonify({"erro": str(e)}), 400
    observacao = (body.get("observacao") or "").strip()
    # Torre acumulada (15/09): a rota pode ser de ontem e ainda estar
    # aberta. A exclusão é registrada com a data de HOJE (é quando ela
    # aconteceu, e excluir_pedido_da_rota recusa data passada); a torre
    # acha o chip excluído pelo rota_id, de qualquer dia
    # (expedicao._exclusoes_da_rota).
    resultado = excluir_pedido_da_rota(date.today(), rota_id, service_id, motivo, observacao,
                                       session.get("usuario", ""), permitir_rota_em_andamento=True)
    if not resultado["ok"]:
        return jsonify({"erro": resultado["erro"]}), 400
    return jsonify(resultado)


LIMITE_REQUISICAO_ENTREGUE = 32 * 1024 * 1024  # canhoto + nota de retorno, 15 MB cada (29/10)


@app.route("/api/torre/marcar-entregue", methods=["POST"])
@requer_auth(niveis=("total", "operador", "logistica"))
@exige_mesma_origem
def api_torre_marcar_entregue():
    """"Marcar como entregue" do menu de contexto do chip da Torre e da
    parada do Planejamento (Hugo, 08/10): fecha o servico na VUUPT na
    hora, por API, sem passar pelo app do motorista (ver
    torre_controle.marcar_entregue_manual). Mesmo nivel do "Excluir da
    rota" -- muda o pedido na VUUPT. Multipart: service_id, foto_canhoto
    e foto_nota_retorno (opcionais), justificativa e texto_outro."""
    from nucleo import canhotos_manuais
    # Recusa antes de ler: duas fotos de 15 MB no maximo (mesmo teto da baixa sem app).
    if (request.content_length or 0) > LIMITE_REQUISICAO_ENTREGUE:
        return jsonify({"erro": "Envio grande demais: mande fotos menores (até 15 MB cada)."}), 413
    try:
        service_id = int(request.form["service_id"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"erro": "service_id ausente ou inválido."}), 400
    # lote do Planejamento com foto: os itens 2..N mandam o 1o como referencia (Hugo, 30/10)
    try:
        lote_referencia = int(request.form["lote_referencia"]) if request.form.get("lote_referencia") else None
    except (TypeError, ValueError):
        return jsonify({"erro": "lote_referencia inválido."}), 400
    arquivos = {}
    for campo in ("foto_canhoto", "foto_nota_retorno"):
        arq = request.files.get(campo)
        conteudo = arq.read(canhotos_manuais.LIMITE_BYTES + 1) if arq else b""
        if len(conteudo) > canhotos_manuais.LIMITE_BYTES:
            return jsonify({"erro": "Arquivo maior que 15 MB."}), 413
        arquivos[campo] = conteudo or None
    resultado = torre_controle.marcar_entregue_manual(
        service_id, session.get("usuario", ""), arquivos["foto_canhoto"], arquivos["foto_nota_retorno"],
        request.form.get("justificativa") or None, request.form.get("texto_outro") or None,
        lote_referencia=lote_referencia)
    if not resultado.get("ok"):
        return jsonify({"erro": resultado.get("erro", "Falha ao marcar como entregue.")}), 400
    return jsonify(resultado)


@app.route("/api/torre/validar-lote-foto", methods=["POST"])
@requer_auth(niveis=("total", "operador", "logistica"))
@exige_mesma_origem
def api_torre_validar_lote_foto():
    """Foto em massa no Planejamento (Hugo, 30/10): so para pedidos do mesmo
    embarcador e do mesmo destinatario ou local de entrega
    (torre_controle.mesmo_destino_para_foto). A tela chama uma vez antes do
    laco de marcar-entregue; sem foto o lote segue livre. JSON {service_ids}."""
    ids = (request.get_json(silent=True) or {}).get("service_ids")
    try:
        if not isinstance(ids, list) or not ids:
            raise ValueError
        ids = [int(i) for i in ids]
    except (TypeError, ValueError):
        return jsonify({"erro": "service_ids ausente ou inválido."}), 400
    resultado = torre_controle.validar_lote_foto(ids)
    if not resultado.get("ok"):
        return jsonify({"erro": resultado["erro"], "fora": resultado.get("fora", [])}), 400
    return jsonify({"ok": True})


@app.route("/api/torre/encerrar-rota", methods=["POST"])
@requer_auth(niveis=("total", "operador", "logistica"))
@exige_mesma_origem
def api_torre_encerrar_rota():
    """Botão "Encerrar" de uma rota atrasada na torre acumulada (Hugo,
    15/09): tira a rota da tela sem mexer na VUUPT nem no núcleo -- ela
    continua no Histórico como estiver. Válvula de escape pra rota de
    dia anterior que nunca saiu (caso Rafael/Iago de 14/09) não ficar
    acumulando na torre até a janela passar."""
    body = request.get_json(force=True)
    try:
        rota_id = int(body["rota_id"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"erro": "rota_id ausente ou inválido."}), 400
    torre_controle.encerrar_rota_torre(
        rota_id, body.get("nome") or "", body.get("data_rota") or "", session.get("usuario", ""))
    return jsonify({"ok": True})


@app.route("/api/torre/etapas")
@requer_auth(niveis=("total", "operador", "logistica", "leitura", "atendimento"))
def api_torre_etapas():
    """Só o estado das etapas do stepper (leitura barata no SQLite) --
    o front consulta com frequência maior pra dar feedback rápido
    depois de um clique em 'rodar'."""
    return jsonify({"etapas": torre_controle.montar_etapas_pipeline()})


@app.route("/api/torre/rodar", methods=["POST"])
@requer_auth(niveis=("total", "operador", "logistica"))
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
    # Campos declarados em agentes.py["parametros"] (hoje: "data" do
    # Gerar Romaneios -> --data) -- pedido do Hugo, 07/10: o Reexecutar
    # da torre gerava sempre os PDFs de hoje, sem perguntar a data.
    try:
        args_extra = montar_args_parametros(agente, body) or None
    except ValueError as e:
        return jsonify({"erro": str(e)}), 400
    execucao_id = iniciar_execucao(agente, modo_teste=False, args_extra=args_extra)
    return jsonify({"ok": True, "execucao_id": execucao_id})


@app.route("/api/torre/tratar", methods=["POST"])
@requer_auth(niveis=("total", "operador", "logistica", "atendimento"))
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
            por=session.get("usuario") or g.nivel_acesso,
        )
    except KeyError as e:
        return jsonify({"erro": f"campo obrigatório ausente: {e}"}), 400
    return jsonify({"ok": True})


@app.route("/api/torre/tratar-lote", methods=["POST"])
@requer_auth(niveis=("total", "operador", "logistica", "atendimento"))
@exige_mesma_origem
def api_torre_tratar_lote():
    """Tratar por lote (30/09): varios itens da fila selecionados na
    tela, um motivo so pra todos. Mesmo efeito do tratar unitario."""
    body = request.get_json(force=True) or {}
    itens = body.get("itens")
    motivo = (body.get("motivo") or "").strip()
    if not isinstance(itens, list) or not itens:
        return jsonify({"erro": "nenhum item selecionado"}), 400
    if not motivo:
        return jsonify({"erro": "motivo obrigatório"}), 400
    qtd = torre_controle.marcar_excecoes_tratadas(itens, body.get("data_alvo", ""), motivo,
                                                  por=session.get("usuario") or g.nivel_acesso)
    return jsonify({"ok": True, "tratadas": qtd})


@app.route("/api/torre/destratar", methods=["POST"])
@requer_auth(niveis=("total", "operador", "logistica", "atendimento"))
@exige_mesma_origem
def api_torre_destratar():
    body = request.get_json(force=True)
    try:
        desfez = torre_controle.desfazer_excecao_tratada(body["id"])
    except KeyError as e:
        return jsonify({"erro": f"campo obrigatório ausente: {e}"}), 400
    return jsonify({"ok": True, "desfeito": desfez})


@app.route("/api/torre/duplicar", methods=["POST"])
@requer_auth(niveis=("total", "operador", "logistica", "atendimento"))
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


@app.route("/api/torre/notificar-ocorrencia", methods=["POST"])
@requer_auth(niveis=("total", "operador", "logistica", "atendimento"))
@exige_mesma_origem
def api_torre_notificar_ocorrencia():
    """Botão 'Notificar' da fila de ação -- dispara na hora a pergunta
    de reenvio ao remetente pra esse insucesso (as notificações
    automáticas de ocorrência foram desligadas, ver config.yaml:
    notificacoes_automaticas.ativo)."""
    body = request.get_json(force=True)
    try:
        service_id = int(body["service_id"])
        codigo = body["codigo"]
    except (KeyError, TypeError, ValueError):
        return jsonify({"erro": "service_id/codigo ausente ou inválido."}), 400
    resultado = torre_controle.notificar_ocorrencia_manual(service_id, codigo)
    if not resultado.get("ok"):
        return jsonify({"erro": resultado.get("erro", "Falha ao notificar.")}), 400
    return jsonify(resultado)


@app.route("/api/planejamento/pool")
@requer_auth(niveis=("total", "operador", "logistica", "leitura", "atendimento"))
def api_pool():
    """Busca ao vivo na VUUPT o pool de não alocados + resumo dos
    pedidos agendados (botão 'Atualizar' da tela) -- não mexe nos
    rascunhos/mapa já carregados, pra não perder o estado de edição em
    andamento. O resumo volta já renderizado (mesmo partial
    _resumo_agendados.html do load da página), a tela só troca o
    innerHTML do container. Com ?sincronizar=1 e fonte_pool=nucleo, roda
    antes a sincronização com a Vuupt (Hugo, 24/09); se ela falhar, o pool
    do núcleo é devolvido mesmo assim, com "aviso"."""
    data_alvo = _parse_data_param()
    aviso = None
    if request.args.get("sincronizar") == "1":
        try:
            sincronizar_pool_agora()
        except Exception as e:
            logging.getLogger(__name__).warning(f"Sincronização do pool antes de atualizar falhou: {e}")
            aviso = f"Não consegui sincronizar com a Vuupt agora ({e}); mostrando o pool do núcleo como está."
    try:
        resultado = buscar_pool_e_agendados(data_alvo)
    except Exception as e:
        return jsonify({"erro": str(e)}), 500
    return jsonify({
        "pool": resultado["pool"],
        "pool_retiradas": resultado.get("pool_retiradas", []),
        "retrato_stokki_em": resultado.get("retrato_stokki_em"),
        "resumo_html": render_template("_resumo_agendados.html", resumo_agendados=resultado["resumo_agendados"]),
        "aviso": aviso,
    })


@app.route("/api/planejamento/sem-rota-hoje")
@requer_auth(niveis=("total", "operador", "logistica", "leitura", "atendimento"))
def api_planejamento_sem_rota_hoje():
    """Número "Sem rota" de HOJE (mesmo cálculo da torre) pro chip do
    planejamento -- carregado async pelo template pra não segurar o
    load da página numa consulta ao vivo à VUUPT."""
    try:
        return jsonify(torre_controle.contar_sem_rota(date.today()))
    except Exception as e:
        logging.getLogger(__name__).exception("Falha ao contar sem rota de hoje")
        return jsonify({"erro": str(e)}), 500


@app.route("/api/planejamento/romaneio/<int:rascunho_id>")
@requer_auth(niveis=("total", "operador", "logistica", "leitura", "atendimento"))
def api_romaneio(rascunho_id):
    """Gera (sempre fresco, reflete o estado atual do rascunho) e serve
    o PDF de romaneio -- botão 'Imprimir rota', mesmo motor de
    roteirizacao/gerar_pdf_romaneios.py (capa + NFs + boletos +
    canhoteira) aplicado direto sobre o rascunho local.

    ?baixar=1 (botão 'Salvar rotas em PDF', 17/08: PDF pra mandar por
    WhatsApp) força o download em vez de abrir inline no navegador."""
    try:
        caminho = gerar_romaneio_pdf(rascunho_id)
    except ValueError as e:
        return str(e), 404
    except Exception as e:
        logging.getLogger(__name__).exception(f"Falha ao gerar romaneio do rascunho {rascunho_id}")
        return f"Falha ao gerar romaneio: {e}", 500
    baixar = request.args.get("baixar") == "1"
    nome_arquivo = caminho.name
    if baixar:
        rascunho = rascunhos_rota.buscar_rascunho(rascunho_id)
        if rascunho:
            motorista = rascunho.get("motorista_nome") or "sem motorista"
            nome_arquivo = f"Romaneio - {rascunho['nome']} - {motorista}.pdf"
    return send_file(caminho, mimetype="application/pdf", download_name=nome_arquivo, as_attachment=baixar)


@app.route("/api/planejamento/carregar-documentos", methods=["POST"])
@requer_auth(niveis=("total", "operador", "logistica"))
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
@requer_auth(niveis=("total", "operador", "logistica"))
@exige_mesma_origem
@bloqueia_planejamento_passado
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
@requer_auth(niveis=("total", "operador", "logistica"))
@exige_mesma_origem
@bloqueia_planejamento_passado
def api_reordenar():
    body = request.get_json(force=True)
    try:
        rascunhos_rota.reordenar_paradas(body["rascunho_id"], body["ordem_service_ids"])
    except (KeyError, ValueError) as e:
        return jsonify({"erro": str(e)}), 400
    return jsonify({"ok": True, "rascunho": _rascunho_ou_404(body["rascunho_id"])})


@app.route("/api/planejamento/inverter-rota", methods=["POST"])
@requer_auth(niveis=("total", "operador", "logistica"))
@exige_mesma_origem
@bloqueia_planejamento_passado
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
@requer_auth(niveis=("total", "operador", "logistica"))
@exige_mesma_origem
@bloqueia_planejamento_passado
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
@requer_auth(niveis=("total", "operador", "logistica"))
@exige_mesma_origem
@bloqueia_planejamento_passado
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
@requer_auth(niveis=("total", "operador", "logistica"))
@exige_mesma_origem
@bloqueia_planejamento_passado
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
@requer_auth(niveis=("total", "operador", "logistica"))
@exige_mesma_origem
@bloqueia_planejamento_passado
def api_adicionar_parada():
    body = request.get_json(force=True)
    try:
        rascunhos_rota.adicionar_parada(body["rascunho_id"], body["parada"], body.get("ordem"))
    except (KeyError, ValueError) as e:
        return jsonify({"erro": str(e)}), 400
    return jsonify({"ok": True, "rascunho": _rascunho_ou_404(body["rascunho_id"])})


@app.route("/api/planejamento/trocar-motorista", methods=["POST"])
@requer_auth(niveis=("total", "operador", "logistica"))
@exige_mesma_origem
@bloqueia_planejamento_passado
def api_trocar_motorista():
    body = request.get_json(force=True)
    try:
        rascunhos_rota.trocar_motorista(
            body["rascunho_id"], body.get("agent_id"), body.get("vehicle_id"), body.get("motorista_nome"),
        )
    except (KeyError, ValueError) as e:
        return jsonify({"erro": str(e)}), 400
    return jsonify({"ok": True, "rascunho": _rascunho_ou_404(body["rascunho_id"])})


@app.route("/api/planejamento/lalamove-veiculo", methods=["POST"])
@requer_auth(niveis=("total", "operador", "logistica"))
@exige_mesma_origem
@bloqueia_planejamento_passado
def api_lalamove_veiculo():
    """Seletor de veículo Lalamove no card (Hugo, 29/08) -- ver
    lalamove_integracao.py e config.yaml lalamove.veiculos."""
    body = request.get_json(force=True)
    try:
        rascunhos_rota.definir_lalamove_veiculo(body["rascunho_id"], body.get("codigo"),
                                                special_requests=body.get("special_requests"))
    except (KeyError, ValueError) as e:
        return jsonify({"erro": str(e)}), 400
    return jsonify({"ok": True, "rascunho": _rascunho_ou_404(body["rascunho_id"])})


@app.route("/api/planejamento/lalamove-horario", methods=["POST"])
@requer_auth(niveis=("total", "operador", "logistica"))
@exige_mesma_origem
@bloqueia_planejamento_passado
def api_lalamove_horario():
    """Campo "Lançar às" da caixa Lalamove do card (Hugo, 03/09): horário
    HH:MM do dia da rota em que o timer nucleo/lancar_lalamove_programados.py
    cria a corrida IMEDIATA (sem o agendamento da Lalamove). horario
    vazio/null limpa a programação."""
    body = request.get_json(force=True)
    try:
        lancar_em = rascunhos_rota.definir_lalamove_horario(body["rascunho_id"], body.get("horario"))
    except (KeyError, ValueError) as e:
        return jsonify({"erro": str(e)}), 400
    return jsonify({"ok": True, "lancar_em": lancar_em, "rascunho": _rascunho_ou_404(body["rascunho_id"])})


@app.route("/api/planejamento/lalamove-lancar", methods=["POST"])
@requer_auth(niveis=("total", "operador", "logistica"))
@exige_mesma_origem
@bloqueia_planejamento_passado
def api_lalamove_lancar():
    """Botão "Lançar na Lalamove" do card (Hugo, 30/08): cria a corrida
    na Lalamove pra rota do motorista virtual JÁ ENVIADA à VUUPT --
    separado do "Confirmar e enviar" (ver rascunhos_rota.lancar_lalamove)."""
    body = request.get_json(force=True)
    try:
        rascunho_id = int(body["rascunho_id"])
    except (KeyError, TypeError, ValueError) as e:
        return jsonify({"erro": str(e)}), 400
    token = _carregar_config().get("vuupt_api", {}).get("token", "")
    resultado = rascunhos_rota.lancar_lalamove(rascunho_id, token)
    if not resultado.get("ok"):
        return jsonify({"erro": resultado.get("erro") or "Falha ao lançar na Lalamove."}), 400
    return jsonify({"ok": True, "resultado": resultado})


@app.route("/api/planejamento/renomear-rota", methods=["POST"])
@requer_auth(niveis=("total", "operador", "logistica"))
@exige_mesma_origem
@bloqueia_planejamento_passado
def api_renomear_rota():
    body = request.get_json(force=True)
    try:
        rascunhos_rota.renomear_rascunho(body["rascunho_id"], body["nome"])
    except (KeyError, ValueError) as e:
        return jsonify({"erro": str(e)}), 400
    return jsonify({"ok": True, "rascunho": _rascunho_ou_404(body["rascunho_id"])})


@app.route("/api/planejamento/alocar-motoristas", methods=["POST"])
@requer_auth(niveis=("total", "operador", "logistica"))
@exige_mesma_origem
@bloqueia_planejamento_passado
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
@requer_auth(niveis=("total", "operador", "logistica"))
@exige_mesma_origem
@bloqueia_planejamento_passado
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


@app.route("/api/planejamento/publicar-oferta", methods=["POST"])
@requer_auth(niveis=("total", "operador", "logistica"))
@exige_mesma_origem
@bloqueia_planejamento_passado
def api_publicar_oferta():
    """Botão "Publicar para motoristas" de um card (Hugo, 22/08):
    publica o rascunho no marketplace de escolha aberta e já dispara o
    aviso (Chatwoot/e-mail automático + texto copiar/colar) pros
    motoristas elegíveis."""
    body = request.get_json(force=True)
    try:
        rascunho_id = body["rascunho_id"]
    except KeyError as e:
        return jsonify({"erro": str(e)}), 400

    resultado = publicar_oferta_rascunho(rascunho_id)
    if not resultado["ok"]:
        return jsonify(resultado)

    rascunho = _rascunho_ou_404(rascunho_id)
    config = _carregar_config()
    push_ofertas_vps(config.get("confirmacao_rotas", {}))
    aviso = notificar_oferta_motoristas(
        resultado["elegiveis"], date.fromisoformat(rascunho["data_alvo"]), config,
    )
    return jsonify({
        "ok": True, "rascunho": rascunho, "resumo": resultado["resumo"],
        "elegiveis": [{"agent_id": m.agent_id, "nome": m.nome} for m in resultado["elegiveis"]],
        "aviso": aviso,
    })


@app.route("/api/planejamento/publicar-ofertas-lote", methods=["POST"])
@requer_auth(niveis=("total", "operador", "logistica"))
@exige_mesma_origem
@bloqueia_planejamento_passado
def api_publicar_ofertas_lote():
    """Botão "Publicar pendentes" (Hugo, 22/08): publica de uma vez
    todo rascunho RASCUNHO sem motorista do lote ativo, e avisa (1 vez
    por motorista, não 1 por rota) todo motorista elegível pra pelo
    menos uma das rotas publicadas nesta chamada."""
    body = request.get_json(force=True)
    try:
        data_alvo = datetime.strptime(body["data_alvo"], "%Y-%m-%d").date()
    except (KeyError, ValueError) as e:
        return jsonify({"erro": str(e)}), 400

    resultado = publicar_ofertas_em_lote(data_alvo)

    config = _carregar_config()
    push_ofertas_vps(config.get("confirmacao_rotas", {}))
    motoristas_por_agent_id = {}
    for item in resultado["publicados"]:
        for m in item["elegiveis"]:
            motoristas_por_agent_id[m.agent_id] = m
    aviso = notificar_oferta_motoristas(list(motoristas_por_agent_id.values()), data_alvo, config)

    return jsonify({
        "ok": True,
        "publicados": [{"rascunho_id": p["rascunho_id"], "nome": p["nome"]} for p in resultado["publicados"]],
        "sem_elegivel": resultado["sem_elegivel"], "ja_tinham": resultado["ja_tinham"],
        "sem_paradas": resultado["sem_paradas"], "aviso": aviso,
    })


@app.route("/api/planejamento/despublicar-oferta", methods=["POST"])
@requer_auth(niveis=("total", "operador", "logistica"))
@exige_mesma_origem
@bloqueia_planejamento_passado
def api_despublicar_oferta():
    """Botão "Despublicar" de um card OFERTADA (Hugo, 22/08)."""
    body = request.get_json(force=True)
    try:
        rascunho_id = body["rascunho_id"]
    except KeyError as e:
        return jsonify({"erro": str(e)}), 400

    resultado = despublicar_oferta_rascunho(rascunho_id)
    if not resultado["ok"]:
        return jsonify(resultado)
    push_ofertas_vps(_carregar_config().get("confirmacao_rotas", {}))
    return jsonify({"ok": True, "rascunho": _rascunho_ou_404(rascunho_id)})


@app.route("/api/planejamento/despublicar-ofertas-lote", methods=["POST"])
@requer_auth(niveis=("total", "operador", "logistica"))
@exige_mesma_origem
@bloqueia_planejamento_passado
def api_despublicar_ofertas_lote():
    """Botão "Cancelar publicações" (Hugo, 23/08): despublica de uma vez
    toda rota OFERTADA do lote ativo."""
    body = request.get_json(force=True)
    try:
        data_alvo = datetime.strptime(body["data_alvo"], "%Y-%m-%d").date()
    except (KeyError, ValueError) as e:
        return jsonify({"erro": str(e)}), 400

    resultado = despublicar_ofertas_em_lote(data_alvo)
    push_ofertas_vps(_carregar_config().get("confirmacao_rotas", {}))
    return jsonify({"ok": True, **resultado})


@app.route("/api/planejamento/disponibilidade-motoristas", methods=["POST"])
@requer_auth(niveis=("total", "operador", "logistica"))
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
@requer_auth(niveis=("total", "operador", "logistica"))
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
@requer_auth(niveis=("total", "operador", "logistica"))
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
@requer_auth(niveis=("total", "operador", "logistica", "leitura"))
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
    return render_template("motoristas.html", dados=dados, erro=erro, pode_editar=g.nivel_acesso in ("total", "operador", "logistica"),
                           pode_aprovar=g.nivel_acesso == "total")


# ── Auto-cadastro pelo app (Hugo, 08/10): aprovação só do nível total ────────
def _cadastro_conn():
    from nucleo import banco as nucleo_banco
    return nucleo_banco.conectar()


@app.route("/api/motoristas/cadastros/<int:cadastro_id>/documento/<tipo>")
@requer_auth(niveis=("total",))
def api_cadastro_documento(cadastro_id, tipo):
    from nucleo import cadastro_motorista as cad
    conn = _cadastro_conn()
    try:
        p = cad.caminho_documento(conn, cadastro_id, tipo)
    finally:
        conn.close()
    if not p:
        abort(404)
    return send_file(p, max_age=0)


@app.route("/api/motoristas/cadastros/<int:cadastro_id>/aprovar", methods=["POST"])
@requer_auth(niveis=("total",))
@exige_mesma_origem
def api_cadastro_aprovar(cadastro_id):
    """Linha na BD_MOTORISTAS (precisa do agente Vuupt criado à mão) +
    login no app com PIN provisório. Devolve o PIN pra mandar ao motorista."""
    from nucleo import cadastro_motorista as cad
    body = request.get_json(force=True) or {}
    conn = _cadastro_conn()
    try:
        r = cad.aprovar(conn, cadastro_id, _carregar_config(), agent_id=body.get("agent_id"),
                        zonas=body.get("zonas"), dias=body.get("dias"), tipo_veiculo=body.get("tipo_veiculo"),
                        aceita_viagens=bool(body.get("aceita_viagens")), revisado_por=session.get("usuario") or g.nivel_acesso,
                        dono_veiculo=body.get("dono_veiculo"))
    except cad.CadastroInvalido as e:
        return jsonify({"erro": e.mensagem}), e.codigo
    except ValueError as e:
        return jsonify({"erro": str(e)}), 400
    except PermissionError as e:
        return jsonify({"erro": str(e)}), 409
    except Exception as e:
        logging.getLogger(__name__).exception("Falha ao aprovar cadastro de motorista")
        return jsonify({"erro": str(e)}), 500
    finally:
        conn.close()
    return jsonify({"ok": True, **r})


@app.route("/api/motoristas/<cpf>/ve-financeiro", methods=["POST"])
@requer_auth(niveis=("total",))
@exige_mesma_origem
def api_motorista_ve_financeiro(cpf):
    """Chave "Vê financeiro" da tabela (Hugo, 08/10): quem dirige carro de
    outro não vê o extrato no app. Pedágio continua."""
    from nucleo import cadastro_motorista as cad
    body = request.get_json(force=True) or {}
    conn = _cadastro_conn()
    try:
        m = cad.definir_ve_financeiro(conn, cpf, bool(body.get("ve")))
    except cad.CadastroInvalido as e:
        return jsonify({"erro": e.mensagem}), e.codigo
    finally:
        conn.close()
    return jsonify({"ok": True, "cpf": m["cpf"], "ve_financeiro": bool(m["ve_financeiro"])})


@app.route("/api/motoristas/cadastros/<int:cadastro_id>/recusar", methods=["POST"])
@requer_auth(niveis=("total",))
@exige_mesma_origem
def api_cadastro_recusar(cadastro_id):
    from nucleo import cadastro_motorista as cad
    body = request.get_json(force=True) or {}
    conn = _cadastro_conn()
    try:
        c = cad.recusar(conn, cadastro_id, body.get("motivo") or "", session.get("usuario") or g.nivel_acesso)
    except cad.CadastroInvalido as e:
        return jsonify({"erro": e.mensagem}), e.codigo
    finally:
        conn.close()
    return jsonify({"ok": True, "cadastro": c})


# ── Pre-cadastro de embarcador da Stokki (10/10): aprovação só do nível total ──
@app.route("/api/cadastro-embarcador/<cnpj>/aprovar", methods=["POST"])
@requer_auth(niveis=("total",))
@exige_mesma_origem
def api_cadastro_embarcador_aprovar(cnpj):
    """Grava em interno.habilidade o tipo de carga que o cliente informou no
    portal, ou o do corpo ({"habilidade": ...}) se a equipe corrigir."""
    import cadastro_embarcador as cad_emb
    from nucleo import banco as nucleo_banco
    body = request.get_json(silent=True) or {}
    conn = nucleo_banco.conectar()
    try:
        c = cad_emb.aprovar(conn, cnpj, session.get("usuario") or g.nivel_acesso, habilidade=body.get("habilidade"))
    except cad_emb.CadastroInvalido as e:
        return jsonify({"erro": e.mensagem}), e.codigo
    finally:
        conn.close()
    return jsonify({"ok": True, "cadastro": c})


# ── Pedágios dos motoristas (app de motoristas; Hugo, 11/09) ─────────────────
# O motorista informa valor + foto do recibo no app (nucleo_pedagios,
# PENDENTE). Aqui o Hugo aprova (com o valor que vale) ou rejeita; só o
# APROVADO entra no extrato (nucleo/financeiro.py). Lógica em
# nucleo/operacao.py; aqui só HTTP.

@app.route("/financeiro/pedagios")
@requer_auth(niveis=("total", "operador", "logistica", "leitura"))
def pedagios():
    from nucleo import banco as nucleo_banco, operacao as nucleo_operacao
    status = (request.args.get("status") or "PENDENTE").upper()
    if status == "TODOS":
        status_filtro = None
    elif status in (nucleo_banco.PEDAGIO_PENDENTE, nucleo_banco.PEDAGIO_APROVADO, nucleo_banco.PEDAGIO_REJEITADO,
                    nucleo_banco.PEDAGIO_CANCELADO):
        status_filtro = status
    else:
        status, status_filtro = "PENDENTE", nucleo_banco.PEDAGIO_PENDENTE

    # Filtros opcionais: período da ROTA (ISO) e motorista (agent_id).
    # Valor inválido é ignorado em vez de quebrar a tela.
    def _data(chave):
        bruto = (request.args.get(chave) or "").strip()
        try:
            return date.fromisoformat(bruto).isoformat() if bruto else None
        except ValueError:
            return None
    data_inicio, data_fim = _data("de"), _data("ate")
    if data_inicio and data_fim and data_fim < data_inicio:
        data_inicio, data_fim = data_fim, data_inicio
    try:
        agent_id = int(request.args.get("motorista") or "") or None
    except ValueError:
        agent_id = None

    conn = nucleo_banco.conectar()
    try:
        itens = nucleo_operacao.listar_pedagios_painel(conn, status_filtro, data_inicio=data_inicio,
                                                       data_fim=data_fim, agent_id=agent_id)
        contagem = nucleo_operacao.contar_pedagios_painel(conn, data_inicio, data_fim, agent_id)
        motoristas = nucleo_operacao.motoristas_com_pedagio(conn)
    finally:
        conn.close()
    filtros = {"de": data_inicio or "", "ate": data_fim or "", "motorista": agent_id or ""}
    resumo = {
        "quantidade": len(itens),
        "informado": sum(float(p["valor_informado"] or 0) for p in itens),
        "aprovado": sum(float(p["valor_aprovado"] or 0) for p in itens if p["status"] == nucleo_banco.PEDAGIO_APROVADO),
    }
    return render_template("pedagios.html", itens=itens, status=status, contagem=contagem, motoristas=motoristas,
                           filtros=filtros, resumo=resumo, pode_editar=g.nivel_acesso in ("total", "operador", "logistica"))


@app.route("/financeiro/pedagios/<int:pedagio_id>/foto")
@requer_auth(niveis=("total", "operador", "logistica", "leitura", "atendimento"))
def pedagio_foto(pedagio_id):
    from nucleo import banco as nucleo_banco
    conn = nucleo_banco.conectar()
    try:
        row = conn.execute("SELECT caminho_local FROM nucleo_pedagios WHERE id = ?", (pedagio_id,)).fetchone()
    finally:
        conn.close()
    if not row or not row["caminho_local"]:
        abort(404)
    caminho = Path(row["caminho_local"])
    if not caminho.is_absolute():
        caminho = _RAIZ / caminho
    if not caminho.exists():
        abort(404)
    return send_file(caminho)


@app.route("/api/financeiro/pedagios/<int:pedagio_id>/revisar", methods=["POST"])
@requer_auth(niveis=("total", "operador", "logistica"))
def revisar_pedagio(pedagio_id):
    from nucleo import banco as nucleo_banco, operacao as nucleo_operacao
    from nucleo.operacao import OperacaoInvalida
    body = request.get_json(silent=True) or {}
    valor = body.get("valor_aprovado")
    try:
        valor_f = None if valor in (None, "") else float(str(valor).replace(",", "."))
    except ValueError:
        return jsonify({"erro": "Valor aprovado inválido."}), 400
    conn = nucleo_banco.conectar()
    try:
        resultado = nucleo_operacao.revisar_pedagio(
            conn, pedagio_id, body.get("status", ""), session.get("usuario") or g.nivel_acesso,
            valor_f, (body.get("observacao") or "").strip() or None,
        )
    except OperacaoInvalida as e:
        return jsonify({"erro": e.mensagem}), e.codigo
    except Exception as e:
        logging.getLogger(__name__).exception("Falha ao revisar pedágio")
        return jsonify({"erro": str(e)}), 500
    finally:
        conn.close()
    return jsonify({"ok": True, "pedagio": resultado})


# ── Canhotos: conferência das fotos do app (Hugo, 12/09) ─────────────────────
# A validação automática (nucleo/validacao_fotos.py) marca cada foto de
# canhoto APROVADO/REPROVADO. Aqui o Hugo vê as reprovadas (e as que
# chegaram sem conferência) e dá a palavra final. Enquanto a validação
# estiver desligada no config, a aba "Sem conferência" tem tudo.

@app.route("/canhotos")
@requer_auth(niveis=("total", "operador", "logistica", "leitura", "atendimento"))
def canhotos():
    from nucleo import banco as nucleo_banco, operacao as nucleo_operacao
    filtro = (request.args.get("resultado") or "REPROVADO").upper()
    if filtro == "TODOS":
        resultado = None
    elif filtro in ("REPROVADO", "APROVADO", "PENDENTE"):
        resultado = filtro
    else:
        filtro, resultado = "REPROVADO", "REPROVADO"
    conn = nucleo_banco.conectar()
    try:
        itens = nucleo_operacao.listar_comprovantes_painel(conn, resultado)
        contagem = {(r[0] or "PENDENTE"): r[1] for r in conn.execute(
            "SELECT resultado_validacao, COUNT(*) FROM nucleo_comprovantes GROUP BY resultado_validacao")}
    finally:
        conn.close()
    return render_template("canhotos.html", itens=itens, resultado=filtro, contagem=contagem,
                           pode_editar=g.nivel_acesso in ("total", "operador", "logistica"))


@app.route("/canhotos/<int:comprovante_id>/foto")
@requer_auth(niveis=("total", "operador", "logistica", "leitura", "atendimento"))
def canhoto_foto(comprovante_id):
    from nucleo import banco as nucleo_banco
    conn = nucleo_banco.conectar()
    try:
        row = conn.execute("SELECT caminho_local FROM nucleo_comprovantes WHERE id = ?", (comprovante_id,)).fetchone()
    finally:
        conn.close()
    if not row or not row["caminho_local"]:
        abort(404)
    caminho = Path(row["caminho_local"])
    if not caminho.is_absolute():
        caminho = _RAIZ / caminho
    if not caminho.exists():
        abort(404)
    return send_file(caminho)


@app.route("/api/canhotos/<int:comprovante_id>/revisar", methods=["POST"])
@requer_auth(niveis=("total", "operador", "logistica"))
def revisar_canhoto(comprovante_id):
    from nucleo import banco as nucleo_banco, operacao as nucleo_operacao
    from nucleo.operacao import OperacaoInvalida
    body = request.get_json(silent=True) or {}
    conn = nucleo_banco.conectar()
    try:
        resultado = nucleo_operacao.revisar_comprovante(
            conn, comprovante_id, body.get("resultado", ""), session.get("usuario") or g.nivel_acesso,
            (body.get("observacao") or "").strip() or None,
        )
    except OperacaoInvalida as e:
        return jsonify({"erro": e.mensagem}), e.codigo
    except Exception as e:
        logging.getLogger(__name__).exception("Falha ao revisar canhoto")
        return jsonify({"erro": str(e)}), 500
    finally:
        conn.close()
    return jsonify({"ok": True, "comprovante": resultado})


# ── Consulta de rotas e pedidos (Hugo, 12/09) ────────────────────────────────
# A Torre e o Planejamento são sempre do DIA. Esta tela é o HISTÓRICO:
# "o que aconteceu com o pedido PS-12345 na semana passada?". Lê só o
# núcleo próprio (dados/dados.db), que o timer do sincronizar_vuupt vem
# enchendo a cada 30 min -- não bate na VUUPT, então é instantânea.
# Toda a lógica de busca/recorte está em nucleo/consulta.py; aqui só HTTP.

def _arg_data(chave: str) -> str | None:
    """Data ISO do query string; valor inválido é ignorado em vez de
    quebrar a tela (mesmo critério da tela de pedágios)."""
    bruto = (request.args.get(chave) or "").strip()
    try:
        return date.fromisoformat(bruto).isoformat() if bruto else None
    except ValueError:
        return None


@app.route("/consulta")
@requer_auth(niveis=("total", "operador", "logistica", "leitura", "atendimento"))
def consulta():
    from nucleo import banco as nucleo_banco, consulta as nucleo_consulta
    termo = (request.args.get("q") or "").strip()
    aba = "pedidos" if (request.args.get("aba") or "").lower() == "pedidos" else "rotas"
    de, ate = _arg_data("de"), _arg_data("ate")
    status = (request.args.get("status") or "").strip().upper() or None
    provedor = (request.args.get("provedor") or "").strip().upper() or None
    try:
        agent_id = int(request.args.get("motorista") or "") or None
    except ValueError:
        agent_id = None

    conn = nucleo_banco.conectar()
    try:
        cobertura = nucleo_consulta.cobertura(conn=conn)
        motoristas = nucleo_consulta.motoristas_com_rota(de, ate, conn=conn)
        if termo:
            # Busca manda na tela: ignora aba e filtros de listagem
            resultado = nucleo_consulta.buscar(termo, de=de, ate=ate, conn=conn)
        elif aba == "pedidos":
            resultado = nucleo_consulta.listar_pedidos(de, ate, status=status, conn=conn)
            resultado["rotas"] = []
        else:
            resultado = nucleo_consulta.listar_rotas(de, ate, status=status, provedor=provedor,
                                                     agent_id=agent_id, conn=conn)
            resultado["pedidos"] = []
    finally:
        conn.close()

    filtros = {"q": termo, "de": de or "", "ate": ate or "", "status": status or "",
               "provedor": provedor or "", "motorista": agent_id or ""}
    return render_template("consulta.html", r=resultado, filtros=filtros, aba=aba,
                           cobertura=cobertura, motoristas=motoristas)


@app.route("/consulta/rota/<int:rota_id>")
@requer_auth(niveis=("total", "operador", "logistica", "leitura", "atendimento"))
def consulta_rota(rota_id):
    from nucleo import consulta as nucleo_consulta
    rota = nucleo_consulta.detalhar_rota(rota_id)
    if not rota:
        abort(404)
    return render_template("consulta_rota.html", rota=rota)


@app.route("/consulta/pedido/<codigo>")
@requer_auth(niveis=("total", "operador", "logistica", "leitura", "atendimento"))
def consulta_pedido(codigo):
    from nucleo import consulta as nucleo_consulta
    detalhe = nucleo_consulta.detalhar_pedido(codigo)
    if not detalhe:
        abort(404)
    # Retorno conferido no galpão (tela /retornos); leitura nunca derruba a consulta.
    try:
        conn_r = retornos.conectar()
        try:
            no_galpao = retornos.status_por_codigo(conn_r, detalhe["codigo"])
        finally:
            conn_r.close()
    except Exception:
        logging.getLogger(__name__).exception("Consulta: falha ao ler retornos de %s", codigo)
        no_galpao = []
    return render_template("consulta_pedido.html", d=detalhe, no_galpao=no_galpao)


@app.route("/api/motoristas/vuupt-disponiveis")
@requer_auth(niveis=("total", "operador", "logistica"))
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
@requer_auth(niveis=("total", "operador", "logistica"))
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


@app.route("/api/motoristas/<int:motorista_id>", methods=["PUT"])
@requer_auth(niveis=("total", "operador", "logistica"))
@exige_mesma_origem
def api_editar_motorista(motorista_id):
    """Edita uma linha da bd_motoristas (botão Editar da tela /motoristas);
    chave ausente = não mexe, "" = limpa. Histórico e repasse ao login do app em regras/cadastro_motoristas.py."""
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return jsonify({"erro": "Corpo da requisição inválido: esperado um objeto JSON."}), 400
    try:
        resultado = editar_motorista(_carregar_config(), motorista_id, body, session.get("usuario") or g.nivel_acesso)
    except MotoristaNaoEncontrado as e:
        return jsonify({"erro": str(e)}), 404
    except (KeyError, ValueError) as e:
        return jsonify({"erro": str(e)}), 400
    except Exception as e:
        logging.getLogger(__name__).exception("Falha ao editar motorista")
        return jsonify({"erro": str(e)}), 500
    return jsonify({"ok": True, "mudancas": resultado["mudancas"], "motorista": resultado["motorista"]})


@app.route("/api/planejamento/nova-rota", methods=["POST"])
@requer_auth(niveis=("total", "operador", "logistica"))
@exige_mesma_origem
@bloqueia_planejamento_passado
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
@requer_auth(niveis=("total", "operador", "logistica"))
@exige_mesma_origem
@bloqueia_planejamento_passado
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
@requer_auth(niveis=("total", "operador", "logistica"))
@exige_mesma_origem
@bloqueia_planejamento_passado
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


@app.route("/api/planejamento/incrementar-selecionados", methods=["POST"])
@requer_auth(niveis=("total", "operador", "logistica"))
@exige_mesma_origem
@bloqueia_planejamento_passado
def api_incrementar_selecionados():
    """Botão "Incrementar" da seleção do pool (Hugo, 10/09): põe cada
    pedido selecionado no rascunho (status RASCUNHO, nunca rota já
    enviada à VUUPT) mais próximo que o comporte, com as travas do
    incremento automático (até 15 pedidos por rota comum). Recebe os
    itens do pool como a tela já os tem (mesmo contrato de
    /adicionar-parada)."""
    body = request.get_json(force=True)
    try:
        data_alvo = datetime.strptime(body["data_alvo"], "%Y-%m-%d").date()
        paradas = body["paradas"]
        if not paradas:
            return jsonify({"erro": "Nenhum pedido selecionado."}), 400
        resultado = incrementar_rascunhos_com_selecionados(data_alvo, paradas)
    except (KeyError, ValueError) as e:
        return jsonify({"erro": str(e)}), 400
    except Exception as e:
        logging.getLogger(__name__).exception("Falha ao incrementar rascunhos com a seleção")
        return jsonify({"erro": str(e)}), 500
    return jsonify({"ok": True, **resultado})


@app.route("/api/planejamento/otimizar-sequencia", methods=["POST"])
@requer_auth(niveis=("total", "operador", "logistica"))
@exige_mesma_origem
@bloqueia_planejamento_passado
def api_otimizar_sequencia():
    body = request.get_json(force=True)
    try:
        rascunhos_rota.otimizar_sequencia(body["rascunho_id"])
    except (KeyError, ValueError) as e:
        return jsonify({"erro": str(e)}), 400
    return jsonify({"ok": True, "rascunho": _rascunho_ou_404(body["rascunho_id"])})


@app.route("/api/planejamento/duplicar-rota", methods=["POST"])
@requer_auth(niveis=("total", "operador", "logistica"))
@exige_mesma_origem
@bloqueia_planejamento_passado
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
@requer_auth(niveis=("total", "operador", "logistica"))
@exige_mesma_origem
@bloqueia_planejamento_passado
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
@requer_auth(niveis=("total", "operador", "logistica"))
@exige_mesma_origem
@bloqueia_planejamento_passado
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
@requer_auth(niveis=("total", "operador", "logistica"))
@exige_mesma_origem
@bloqueia_planejamento_passado
def api_cancelar_rota():
    """Cancela na VUUPT a(s) rota(s) já enviada(s) indicada(s) -- botão
    "Cancelar rota"/"Cancelar todas as rotas" da tela. Só funciona pra
    rota que ainda não iniciou deslocamento (checado ao vivo contra a
    API dentro de cancelar_rota_enviada). O rascunho volta a ser
    RASCUNHO editável (não é descartado) -- mesmo padrão de
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


@app.route("/api/planejamento/cancelar-e-notificar", methods=["POST"])
@requer_auth(niveis=("total", "operador", "logistica"))
@exige_mesma_origem
@bloqueia_planejamento_passado
def api_cancelar_e_notificar():
    """Botão "Cancelar e notificar" do card (Hugo, 27/10): cancela igual ao
    "Cancelar rota" e, só se cancelou, avisa o grupo "Rotas canceladas" do
    /atendimento e os embarcadores pelo chat do portal (aviso_rota_cancelada)."""
    import aviso_rota_cancelada
    body = request.get_json(force=True)
    motivo = " ".join(str(body.get("motivo") or "").split())[:300]
    try:
        rascunho = rascunhos_rota.buscar_rascunho(int(body["rascunho_id"]))
    except (KeyError, TypeError, ValueError) as e:
        return jsonify({"erro": str(e)}), 400
    if not motivo:
        return jsonify({"erro": "Informe o motivo do cancelamento."}), 400
    if not rascunho:
        return jsonify({"erro": "Rota não encontrada."}), 404

    config = _carregar_config()
    resultado = rascunhos_rota.cancelar_rota_enviada(rascunho["id"], config.get("vuupt_api", {}).get("token", ""))
    if not resultado["ok"]:
        return jsonify({"ok": False, "erro": resultado["erro"]})
    aviso = aviso_rota_cancelada.avisar(rascunho, motivo, session.get("usuario") or g.nivel_acesso, config)
    return jsonify({"ok": True, "aviso": aviso})


@app.route("/api/planejamento/cancelar-pedido", methods=["POST"])
@requer_auth(niveis=("total", "operador", "logistica"))
@exige_mesma_origem
@bloqueia_planejamento_passado
def api_cancelar_pedido():
    """Cancela DE VERDADE um pedido na VUUPT (PUT /services/{id}/cancel) --
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
@requer_auth(niveis=("total", "operador", "logistica"))
@exige_mesma_origem
@bloqueia_planejamento_passado
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


@app.route("/api/planejamento/reagendar-pedidos", methods=["POST"])
@requer_auth(niveis=("total", "operador", "logistica"))
@exige_mesma_origem
@bloqueia_planejamento_passado
def api_reagendar_pedidos():
    """Agenda/reagenda em lote (MESMA janela de data/horário pra todos)
    -- botão "Agendar" da barra de seleção múltipla da tela de
    planejamento, tanto pra seleção do pool quanto pra seleção dentro de
    rotas (ver planejamento_rotas.reagendar_pedidos)."""
    body = request.get_json(force=True)
    try:
        itens = [{"service_id": int(it["service_id"])} for it in body["itens"]]
        data = body["data"]
        hora_inicio = body["hora_inicio"]
        hora_fim = body["hora_fim"]
    except (KeyError, ValueError, TypeError) as e:
        return jsonify({"erro": str(e)}), 400

    try:
        resultado = reagendar_pedidos(itens, data, hora_inicio, hora_fim)
    except Exception as e:
        # rede de segurança: o loop em si já isola falha por item (ver
        # planejamento_rotas.reagendar_pedidos), isso é só pra nunca
        # devolver um 500 cru (HTML) pro fetch() do navegador, que
        # quebraria tentando fazer resp.json() -- mesmo padrão de
        # api_roteirizar_selecionados.
        logging.getLogger(__name__).exception("Falha ao reagendar em lote")
        return jsonify({"erro": str(e)}), 500
    if not resultado["ok"]:
        return jsonify({"erro": resultado["erro"]}), 400
    return jsonify({"ok": True, "falhas": resultado["falhas"]})


@app.route("/api/planejamento/checar-dia-fixo", methods=["POST"])
@requer_auth(niveis=("total", "operador", "logistica"))
@exige_mesma_origem
def api_checar_dia_fixo():
    """Antes de reagendar (um ou lote): quais pedidos ficam fora do dia de
    visita da região, com o valor da calculadora de frete dedicado (Hugo,
    03/10 -- spec dias fixos v2, 5.4). Mesma permissão do reagendar. Só lê."""
    body = request.get_json(force=True, silent=True) or {}
    try:
        service_ids = [int(s) for s in body.get("service_ids") or []]
    except (TypeError, ValueError) as e:
        return jsonify({"erro": str(e)}), 400
    try:
        resultado = checar_reagendamento_dia_fixo(service_ids, str(body.get("data") or ""))
    except Exception as e:
        logging.getLogger(__name__).exception("Falha ao checar dia fixo do reagendamento")
        return jsonify({"erro": str(e)}), 500
    if not resultado["ok"]:
        return jsonify({"erro": resultado["erro"]}), 400
    return jsonify(resultado)


@app.route("/api/planejamento/editar-endereco", methods=["POST"])
@requer_auth(niveis=("total", "operador", "logistica"))
@exige_mesma_origem
@bloqueia_planejamento_passado
def api_editar_endereco():
    """Edita o endereço de um pedido direto na VUUPT -- opção "Editar
    endereço" do menu de contexto (ver
    planejamento_rotas.editar_endereco_pedido)."""
    body = request.get_json(force=True)
    try:
        service_id = int(body["service_id"])
        endereco = body["endereco"]
    except (KeyError, ValueError) as e:
        return jsonify({"erro": str(e)}), 400
    rascunho_id = body.get("rascunho_id")
    rascunho_id = int(rascunho_id) if rascunho_id is not None else None

    resultado = editar_endereco_pedido(service_id, endereco, rascunho_id)
    if not resultado["ok"]:
        return jsonify({"erro": resultado["erro"]}), 400
    return jsonify({"ok": True})


@app.route("/api/planejamento/editar-nivel-horario", methods=["POST"])
@requer_auth(niveis=("total", "operador", "logistica"))
@exige_mesma_origem
@bloqueia_planejamento_passado
def api_editar_nivel_horario():
    """Corrige nível de dificuldade e horário de atendimento (padrão de
    recebimento) do destinatário -- opção "Nível / horário de
    atendimento" do menu de contexto (ver
    planejamento_rotas.editar_nivel_horario_pedido)."""
    body = request.get_json(force=True)
    try:
        service_id = int(body["service_id"])
        nivel = int(body["nivel"])
        horario_inicio = body["horario_inicio"]
        horario_fim = body["horario_fim"]
    except (KeyError, ValueError) as e:
        return jsonify({"erro": str(e)}), 400
    rascunho_id = body.get("rascunho_id")
    rascunho_id = int(rascunho_id) if rascunho_id is not None else None

    resultado = editar_nivel_horario_pedido(service_id, nivel, horario_inicio, horario_fim, rascunho_id)
    if not resultado["ok"]:
        return jsonify({"erro": resultado["erro"]}), 400
    return jsonify({"ok": True})


@app.route("/api/planejamento/editar-endereco-lote", methods=["POST"])
@requer_auth(niveis=("total", "operador", "logistica"))
@exige_mesma_origem
@bloqueia_planejamento_passado
def api_editar_endereco_lote():
    """Edita o MESMO endereço em lote -- botão "Editar endereço" da
    barra de seleção múltipla da tela de planejamento, tanto pra seleção
    do pool quanto pra seleção dentro de rotas (ver
    planejamento_rotas.editar_endereco_pedidos)."""
    body = request.get_json(force=True)
    try:
        itens = [
            {"service_id": int(it["service_id"]),
             "rascunho_id": int(it["rascunho_id"]) if it.get("rascunho_id") is not None else None}
            for it in body["itens"]
        ]
        endereco = body["endereco"]
    except (KeyError, ValueError, TypeError) as e:
        return jsonify({"erro": str(e)}), 400

    try:
        resultado = editar_endereco_pedidos(itens, endereco)
    except Exception as e:
        # rede de segurança: o loop em si já isola falha por item (ver
        # planejamento_rotas.editar_endereco_pedidos), isso é só pra
        # nunca devolver um 500 cru (HTML) pro fetch() do navegador, que
        # quebraria tentando fazer resp.json() -- mesmo padrão de
        # api_roteirizar_selecionados.
        logging.getLogger(__name__).exception("Falha ao editar endereço em lote")
        return jsonify({"erro": str(e)}), 500
    if not resultado["ok"]:
        return jsonify({"erro": resultado["erro"]}), 400
    return jsonify({"ok": True, "falhas": resultado["falhas"]})


@app.route("/api/planejamento/transportadoras-terceiros")
@requer_auth(niveis=("total", "operador", "logistica"))
def api_transportadoras_terceiros():
    """Opções do modal "Transportadora (redespacho)" da tela de
    planejamento: TERCEIROS do cadastro de transportadoras com endereço de galpão
    (ver planejamento_rotas.listar_transportadoras_terceiros)."""
    return jsonify({"transportadoras": listar_transportadoras_terceiros()})


@app.route("/api/planejamento/editar-transportadora", methods=["POST"])
@requer_auth(niveis=("total", "operador", "logistica"))
@exige_mesma_origem
@bloqueia_planejamento_passado
def api_editar_transportadora():
    """Troca o endereço dos pedidos pelo galpão da transportadora TERCEIROS
    escolhida -- opção "Transportadora (redespacho)" do menu de contexto
    e das barras de seleção (ver
    planejamento_rotas.editar_transportadora_pedidos). Mesmo contrato do
    editar-endereco-lote: itens + falhas por pedido."""
    body = request.get_json(force=True)
    try:
        itens = [
            {"service_id": int(it["service_id"]),
             "rascunho_id": int(it["rascunho_id"]) if it.get("rascunho_id") is not None else None}
            for it in body["itens"]
        ]
        transportadora = body["transportadora"]
    except (KeyError, ValueError, TypeError) as e:
        return jsonify({"erro": str(e)}), 400

    try:
        resultado = editar_transportadora_pedidos(itens, transportadora)
    except Exception as e:
        logging.getLogger(__name__).exception("Falha ao editar transportadora")
        return jsonify({"erro": str(e)}), 500
    if not resultado["ok"]:
        return jsonify({"erro": resultado["erro"]}), 400
    return jsonify({"ok": True, "falhas": resultado["falhas"], "endereco": resultado["endereco"]})


@app.route("/api/planejamento/dedicado", methods=["POST", "DELETE"])
@requer_auth(niveis=("total", "operador", "logistica"))
@exige_mesma_origem
def api_planejamento_dedicado():
    """Marca (POST {itens, valor}) ou desmarca (DELETE {codigos}) pedidos
    dedicados -- Hugo, 23/09. Nada vai pra Vuupt; a marca é nossa e alimenta
    a roteirização (fica fora da rota compartilhada) e o e-mail quinzenal
    do financeiro (notificar_dedicados_financeiro.py)."""
    body = request.get_json(force=True, silent=True) or {}
    por = session.get("usuario") or g.nivel_acesso
    try:
        if request.method == "DELETE":
            return jsonify(remover_dedicados([str(c) for c in body.get("codigos") or []], por))
        from atendimento_chamados import valor_brl
        return jsonify(marcar_dedicados(body.get("itens") or [], valor_brl(body.get("valor")), por))
    except ValueError as e:
        return jsonify({"erro": str(e)}), 400
    except Exception as e:
        logging.getLogger(__name__).exception("Falha em dedicado")
        return jsonify({"erro": str(e)}), 500


# ── Galpão: endereçamento de produtos (WMS, Hugo 04/09) ─────────────────────
# Tela mobile única (/wms, PWA instalável no aparelho compartilhado do
# galpão) + APIs em /api/wms/*. Lógica e tabelas em wms.py; aqui só HTTP.
# Níveis: total e operador (também veem no menu) e "galpao" (login fixo do
# aparelho). Quem opera se identifica por nome + PIN, guardado na sessão
# por SESSAO_OPERADOR_HORAS (renova a cada movimento).

_NIVEIS_WMS = ("total", "operador", "galpao")
_NIVEIS_WMS_ADMIN = ("total", "operador")
# Rotas de SO LEITURA que tanto o app do galpao quanto a tela da equipe
# (/wms/estoque, niveis "total"/"operador"/"leitura", sem "galpao") usam --
# uniao dos dois publicos. Rotas que MUDAM estado (trocar lote, registrar
# recebimento) continuam em _NIVEIS_WMS: "leitura" nunca opera o estoque.
_NIVEIS_WMS_CONSULTA = ("total", "operador", "leitura", "galpao")


def _wms_operador_atual():
    op = session.get("wms_operador")
    if not op:
        return None
    try:
        em = datetime.fromisoformat(op.get("em", ""))
    except ValueError:
        return None
    if datetime.now() - em > timedelta(hours=wms.SESSAO_OPERADOR_HORAS):
        session.pop("wms_operador", None)
        return None
    return {"id": op["id"], "nome": op["nome"]}


def _wms_gravar_operador(op: dict):
    session["wms_operador"] = {"id": op["id"], "nome": op["nome"], "em": datetime.now().isoformat(timespec="seconds")}


def _wms_exige_operador():
    op = _wms_operador_atual()
    if not op:
        abort(Response(jsonify({"erro": "Identifique o operador (nome + PIN) antes de registrar.", "sem_operador": True}).get_data(),
                       status=409, mimetype="application/json"))
    _wms_gravar_operador(op)
    return op


def _wms_json_erro(e, status=400):
    return jsonify({"erro": str(e)}), status


def _wms_bootstrap(conn) -> dict:
    return {
        "operador": _wms_operador_atual(),
        "operadores": wms.listar_operadores(conn),
        "areas": wms.listar_areas(conn),
        "resumo": wms.resumo_dia(conn),
        "produtos": wms.contar_produtos(conn),
        "nivel": g.nivel_acesso,
        "pode_administrar": g.nivel_acesso in _NIVEIS_WMS_ADMIN,
        "sessao_operador_horas": wms.SESSAO_OPERADOR_HORAS,
    }


@app.route("/wms", endpoint="wms")
@requer_auth(niveis=_NIVEIS_WMS)
def tela_wms():
    conn = wms.conectar()
    try:
        wms.semear_estrutura_inicial(conn)
        boot = _wms_bootstrap(conn)
    finally:
        conn.close()
    return render_template("wms.html", boot=boot)


@app.route("/wms/manifest.webmanifest")
def wms_manifest():
    raiz = request.script_root or ""
    manifesto = {
        "name": "Galpão Freshlog", "short_name": "Galpão", "lang": "pt-BR",
        "start_url": f"{raiz}/wms", "scope": f"{raiz}/wms", "display": "standalone",
        "orientation": "portrait", "background_color": "#141428", "theme_color": "#141428",
        "icons": [{"src": url_for("static", filename="apple-touch-icon.png"), "sizes": "180x180", "type": "image/png", "purpose": "any"}],
    }
    return Response(json.dumps(manifesto, ensure_ascii=False), mimetype="application/manifest+json")


@app.route("/wms/sw.js")
def wms_service_worker():
    """Service worker mínimo (exigido pra "instalar" a PWA): rede primeiro,
    cache da casca só como fallback quando a rede cai. API nunca é cacheada."""
    raiz = request.script_root or ""
    js = """
const CACHE = 'wms-casca-v1';
const CASCA = ['%(raiz)s/wms'];
self.addEventListener('install', (e) => { self.skipWaiting(); });
self.addEventListener('activate', (e) => { e.waitUntil(self.clients.claim()); });
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.pathname.includes('/api/')) return;
  if (!CASCA.includes(url.pathname)) return;
  e.respondWith(
    fetch(e.request).then((resp) => {
      if (resp.ok) caches.open(CACHE).then((c) => c.put(e.request, resp.clone()));
      return resp;
    }).catch(() => caches.match(e.request))
  );
});
""" % {"raiz": raiz}
    resp = Response(js, mimetype="application/javascript")
    resp.headers["Service-Worker-Allowed"] = f"{raiz}/wms"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/api/wms/bootstrap")
@requer_auth(niveis=_NIVEIS_WMS)
def api_wms_bootstrap():
    conn = wms.conectar()
    try:
        return jsonify(_wms_bootstrap(conn))
    finally:
        conn.close()


@app.route("/api/wms/operador/entrar", methods=["POST"])
@requer_auth(niveis=_NIVEIS_WMS)
@exige_mesma_origem
def api_wms_operador_entrar():
    body = request.get_json(force=True) or {}
    conn = wms.conectar()
    try:
        op = wms.autenticar_operador(conn, int(body.get("operador_id") or 0), body.get("pin", ""))
    except (wms.ErroWMS, ValueError) as e:
        return _wms_json_erro(e, 401)
    finally:
        conn.close()
    _wms_gravar_operador(op)
    return jsonify({"ok": True, "operador": op})


@app.route("/api/wms/operador/sair", methods=["POST"])
@requer_auth(niveis=_NIVEIS_WMS)
@exige_mesma_origem
def api_wms_operador_sair():
    session.pop("wms_operador", None)
    return jsonify({"ok": True})


@app.route("/api/wms/ler")
@requer_auth(niveis=_NIVEIS_WMS)
def api_wms_ler():
    conn = wms.conectar()
    try:
        return jsonify(wms.ler_codigo(conn, request.args.get("codigo", "")))
    finally:
        conn.close()


@app.route("/api/wms/produtos")
@requer_auth(niveis=_NIVEIS_WMS)
def api_wms_produtos():
    conn = wms.conectar()
    try:
        return jsonify({"produtos": wms.buscar_produtos_texto(conn, request.args.get("q", ""))})
    finally:
        conn.close()


@app.route("/api/wms/produtos/<int:produto_id>")
@requer_auth(niveis=_NIVEIS_WMS)
def api_wms_produto(produto_id):
    conn = wms.conectar()
    try:
        p = wms.obter_produto(conn, produto_id)
        if not p:
            return _wms_json_erro("Produto não encontrado.", 404)
        p["saldos"] = wms.saldos_produto(conn, produto_id)
        p["total"] = round(sum(s["quantidade"] for s in p["saldos"]), 3)
        p["movimentos"] = wms.listar_movimentos(conn, 20, produto_id=produto_id)
        return jsonify(p)
    finally:
        conn.close()


@app.route("/api/wms/produtos/manual", methods=["POST"])
@requer_auth(niveis=_NIVEIS_WMS)
@exige_mesma_origem
def api_wms_produto_manual():
    body = request.get_json(force=True) or {}
    conn = wms.conectar()
    try:
        qtd = body.get("qtd_por_caixa")
        p = wms.criar_produto_manual(conn, body.get("ean", ""), body.get("descricao", ""), body.get("embarcador", ""),
                                     body.get("unidade") or "UN", float(qtd) if qtd not in (None, "") else None)
        return jsonify({"ok": True, "produto": p})
    except (wms.ErroWMS, ValueError) as e:
        return _wms_json_erro(e)
    finally:
        conn.close()


@app.route("/api/wms/produtos/<int:produto_id>/controla-validade", methods=["POST"])
@requer_auth(niveis=_NIVEIS_WMS_ADMIN)
@exige_mesma_origem
def api_wms_produto_controla_validade(produto_id):
    body = request.get_json(force=True) or {}
    conn = wms.conectar()
    try:
        return jsonify({"ok": True, "produto": wms.definir_controla_validade(conn, produto_id, bool(body.get("controla", True)))})
    finally:
        conn.close()


def _wms_registrar(conn, item: dict, op: dict) -> dict:
    return wms.registrar_movimento(
        conn, tipo=item.get("tipo", ""), produto_id=int(item.get("produto_id") or 0),
        quantidade=item.get("quantidade"), lote=item.get("lote", ""), validade=item.get("validade"),
        origem=item.get("origem"), destino=item.get("destino"), operador=op, uuid=item.get("uuid"),
        observacao=item.get("observacao", ""), dispositivo=(request.user_agent.string or "")[:80],
        criado_em=item.get("criado_em"))


@app.route("/api/wms/movimentos", methods=["GET"])
@requer_auth(niveis=_NIVEIS_WMS)
def api_wms_movimentos():
    conn = wms.conectar()
    try:
        return jsonify({"movimentos": wms.listar_movimentos(
            conn, request.args.get("limite", 50, type=int), produto_id=request.args.get("produto_id", type=int),
            posicao=request.args.get("posicao"), operador_id=request.args.get("operador_id", type=int))})
    finally:
        conn.close()


@app.route("/api/wms/movimentos", methods=["POST"])
@requer_auth(niveis=_NIVEIS_WMS)
@exige_mesma_origem
def api_wms_registrar_movimento():
    op = _wms_exige_operador()
    body = request.get_json(force=True) or {}
    conn = wms.conectar()
    try:
        m = _wms_registrar(conn, body, op)
    except (wms.ErroWMS, ValueError) as e:
        return _wms_json_erro(e)
    finally:
        conn.close()
    return jsonify({"ok": True, "movimento": m})


@app.route("/api/wms/movimentos/lote", methods=["POST"])
@requer_auth(niveis=_NIVEIS_WMS)
@exige_mesma_origem
def api_wms_movimentos_lote():
    """Reenvio da fila offline: cada item leva o uuid gerado no aparelho e o
    operador que estava logado na hora; o servidor ignora uuid repetido."""
    op = _wms_exige_operador()
    body = request.get_json(force=True) or {}
    conn = wms.conectar()
    resultados = []
    try:
        for item in body.get("movimentos") or []:
            op_item = item.get("operador") if isinstance(item.get("operador"), dict) and item["operador"].get("id") else op
            try:
                m = _wms_registrar(conn, item, op_item)
                resultados.append({"uuid": item.get("uuid"), "ok": True, "movimento": m})
            except (wms.ErroWMS, ValueError) as e:
                resultados.append({"uuid": item.get("uuid"), "ok": False, "erro": str(e)})
    finally:
        conn.close()
    return jsonify({"resultados": resultados})


@app.route("/api/wms/posicoes")
@requer_auth(niveis=_NIVEIS_WMS)
def api_wms_posicoes():
    conn = wms.conectar()
    try:
        return jsonify({"posicoes": wms.listar_posicoes(conn, request.args.get("area"),
                                                        apenas_ativas=request.args.get("todas") != "1")})
    finally:
        conn.close()


@app.route("/api/wms/posicoes/<codigo>")
@requer_auth(niveis=_NIVEIS_WMS)
def api_wms_posicao(codigo):
    conn = wms.conectar()
    try:
        p = wms.obter_posicao(conn, codigo)
        if not p:
            return _wms_json_erro("Posição não existe.", 404)
        p["conteudo"] = wms.conteudo_posicao(conn, p["codigo"])
        p["movimentos"] = wms.listar_movimentos(conn, 20, posicao=p["codigo"])
        return jsonify(p)
    finally:
        conn.close()


@app.route("/api/wms/posicoes", methods=["POST"])
@requer_auth(niveis=_NIVEIS_WMS)
@exige_mesma_origem
def api_wms_criar_posicao():
    body = request.get_json(force=True) or {}
    op = _wms_operador_atual()
    conn = wms.conectar()
    try:
        p = wms.criar_posicao(conn, body.get("codigo", ""), (op or {}).get("nome") or session.get("usuario"))
        return jsonify({"ok": True, "posicao": p})
    except wms.ErroWMS as e:
        return _wms_json_erro(e)
    finally:
        conn.close()


@app.route("/api/wms/posicoes/<codigo>/ativo", methods=["POST"])
@requer_auth(niveis=_NIVEIS_WMS_ADMIN)
@exige_mesma_origem
def api_wms_posicao_ativo(codigo):
    body = request.get_json(force=True) or {}
    conn = wms.conectar()
    try:
        return jsonify({"ok": True, "posicao": wms.desativar_posicao(conn, codigo, bool(body.get("ativo", False)))})
    except wms.ErroWMS as e:
        return _wms_json_erro(e)
    finally:
        conn.close()


@app.route("/api/wms/posicoes/<codigo>", methods=["DELETE"])
@requer_auth(niveis=_NIVEIS_WMS_ADMIN)
@exige_mesma_origem
def api_wms_excluir_posicao(codigo):
    """Exclui de verdade uma posição VAZIA (sem saldo). Mesmo nível de quem
    pode desativar; a regra de "vazia" fica em wms.excluir_posicao."""
    conn = wms.conectar()
    try:
        return jsonify({"ok": True, "posicao": wms.excluir_posicao(conn, codigo)})
    except wms.ErroWMS as e:
        return _wms_json_erro(e)
    finally:
        conn.close()


@app.route("/api/wms/areas", methods=["POST"])
@requer_auth(niveis=_NIVEIS_WMS)
@exige_mesma_origem
def api_wms_criar_area():
    """Cria (ou atualiza) uma área e, opcionalmente, gera as posições em
    lote: estantes × níveis + pallets."""
    body = request.get_json(force=True) or {}
    op = _wms_operador_atual()
    conn = wms.conectar()
    try:
        area = wms.criar_area(conn, body.get("codigo", ""), body.get("tipo", "CONTAINER"), body.get("nome"), body.get("temperatura"))
        geradas = wms.gerar_posicoes(conn, area["codigo"], body.get("estantes") or 0, body.get("niveis") or 0,
                                     body.get("pallets") or 0, (op or {}).get("nome") or session.get("usuario"))
        return jsonify({"ok": True, "area": area, "geradas": geradas, "areas": wms.listar_areas(conn)})
    except (wms.ErroWMS, ValueError) as e:
        return _wms_json_erro(e)
    finally:
        conn.close()


# ── WMS fase 2: pedidos com reserva por lote, telas (Hugo 22/09) ──────────
# Modulo de dados em wms_pedidos.py (Tasks 1-9, sem interface). Aqui so HTTP.
# Rotas de leitura (_NIVEIS_WMS_CONSULTA) servem tanto o app do galpao
# (aba Separar) quanto a tela da equipe (/wms/estoque). Trocar lote e
# registrar/enderecar recebimento continuam _NIVEIS_WMS (quem opera).

@app.route("/api/wms/pedidos")
@requer_auth(niveis=_NIVEIS_WMS_CONSULTA)
def api_wms_pedidos():
    conn = wms_pedidos.conectar()
    try:
        return jsonify({"pedidos": wms_pedidos.listar_pedidos_wms(
            conn, estado=request.args.get("estado"), limite=request.args.get("limite", 50, type=int))})
    finally:
        conn.close()


@app.route("/api/wms/pedidos/<int:pedido_id>")
@requer_auth(niveis=_NIVEIS_WMS_CONSULTA)
def api_wms_pedido(pedido_id):
    conn = wms_pedidos.conectar()
    try:
        pedido = conn.execute("SELECT * FROM wms_pedidos WHERE id = ?", (pedido_id,)).fetchone()
        if not pedido:
            return _wms_json_erro("Pedido não encontrado.", 404)
        itens = [dict(r) for r in conn.execute(
            "SELECT * FROM wms_pedido_itens WHERE pedido_id = ? ORDER BY linha", (pedido_id,))]
        return jsonify({"pedido": dict(pedido), "itens": itens,
                        "separacao": wms_pedidos.separacao_do_pedido(conn, pedido_id)})
    finally:
        conn.close()


@app.route("/api/wms/reservas/<int:reserva_id>/trocar-lote", methods=["POST"])
@requer_auth(niveis=_NIVEIS_WMS)
@exige_mesma_origem
def api_wms_trocar_lote(reserva_id):
    _wms_exige_operador()
    body = request.get_json(force=True) or {}
    conn = wms_pedidos.conectar()
    try:
        nova = wms_pedidos.trocar_lote_reserva(
            conn, reserva_id, body.get("posicao"), body.get("lote"), body.get("validade"))
    except (wms.ErroWMS, ValueError) as e:
        return _wms_json_erro(e)
    finally:
        conn.close()
    return jsonify({"ok": True, "reserva": nova})


@app.route("/api/wms/pendencias")
@requer_auth(niveis=_NIVEIS_WMS_CONSULTA)
def api_wms_pendencias():
    conn = wms_pedidos.conectar()
    try:
        return jsonify({"pendencias": wms_pedidos.pendencias(
            conn, request.args.get("limite", 50, type=int))})
    finally:
        conn.close()


@app.route("/api/wms/reservas-antigas")
@requer_auth(niveis=_NIVEIS_WMS_CONSULTA)
def api_wms_reservas_antigas():
    """Reserva ATIVA parada ha muitos dias -- sintoma de baixa que nunca
    veio (ver wms_pedidos.reservas_antigas). O corte em dias vem do
    config.yaml (wms.reserva_antiga_dias); sem a chave vale o padrao do
    modulo, e mudar la nao exige deploy."""
    dias = (_carregar_config().get("wms", {}) or {}).get(
        "reserva_antiga_dias", wms_pedidos.RESERVA_ANTIGA_DIAS)
    try:
        dias = int(dias)
    except (TypeError, ValueError):
        dias = wms_pedidos.RESERVA_ANTIGA_DIAS
    conn = wms_pedidos.conectar()
    try:
        return jsonify({"dias": dias, "pedidos": wms_pedidos.reservas_antigas(
            conn, dias, limite=request.args.get("limite", 50, type=int))})
    finally:
        conn.close()


@app.route("/api/wms/pedidos/<int:pedido_id>/liberar-reservas", methods=["POST"])
@requer_auth(niveis=("total", "operador"))
@exige_mesma_origem
def api_wms_liberar_reservas(pedido_id):
    """
    Saída manual pra reserva que escapou das duas varreduras automáticas
    (a baixa do que já foi expedido e a liberação do que sumiu da Stokki).
    Devolve o que está reservado pro disponível.

    Escrita de equipe interna ("total"/"operador"): nem "leitura" nem o
    aparelho do galpão ("galpao") liberam reserva. O motivo é obrigatório
    e fica gravado no pedido junto com quem pediu -- liberar por engano
    devolve ao disponível mercadoria que já foi embora, e seis meses
    depois "por que este pedido está CANCELADO" precisa ter resposta.
    """
    body = request.get_json(force=True) or {}
    motivo = " ".join(str(body.get("motivo") or "").split())
    if not motivo:
        return _wms_json_erro("Diga o motivo da liberação -- ele fica gravado no pedido.")
    conn = wms_pedidos.conectar()
    try:
        pedido = conn.execute("SELECT * FROM wms_pedidos WHERE id = ?", (pedido_id,)).fetchone()
        if not pedido:
            return _wms_json_erro("Pedido não encontrado.", 404)
        quem = session.get("usuario") or "equipe"
        liberadas = wms_pedidos.cancelar_reservas(
            conn, pedido_id, f"Liberado manualmente por {quem}: {motivo}")
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True, "liberadas": liberadas, "codigo_ps": pedido["codigo_ps"]})


@app.route("/api/wms/produtos/<int:produto_id>/saldo")
@requer_auth(niveis=_NIVEIS_WMS_CONSULTA)
def api_wms_produto_saldo(produto_id):
    conn = wms_pedidos.conectar()
    try:
        return jsonify({"lotes": wms_pedidos.disponivel_por_lote(conn, produto_id)})
    finally:
        conn.close()


@app.route("/api/wms/recebimentos")
@requer_auth(niveis=_NIVEIS_WMS)
def api_wms_recebimentos():
    """Recebimentos esperados (Task 8) pra aba Receber do app do galpao.
    Sem ?estado, so mostra ESPERADO -- o que falta enderecar."""
    conn = wms_pedidos.conectar()
    try:
        estado = request.args.get("estado", "ESPERADO")
        sql = ("SELECT r.*, (SELECT COUNT(*) FROM wms_recebimento_itens i "
               "WHERE i.recebimento_id = r.id) AS itens FROM wms_recebimentos r")
        params = []
        if estado and estado != "TODOS":
            sql += " WHERE r.estado = ?"
            params.append(estado)
        sql += " ORDER BY r.id DESC LIMIT ?"
        params.append(request.args.get("limite", 50, type=int))
        rows = conn.execute(sql, params).fetchall()
        return jsonify({"recebimentos": [dict(r) for r in rows]})
    finally:
        conn.close()


@app.route("/api/wms/recebimentos/<int:recebimento_id>")
@requer_auth(niveis=_NIVEIS_WMS)
def api_wms_recebimento(recebimento_id):
    conn = wms_pedidos.conectar()
    try:
        rec = conn.execute("SELECT * FROM wms_recebimentos WHERE id = ?", (recebimento_id,)).fetchone()
        if not rec:
            return _wms_json_erro("Recebimento não encontrado.", 404)
        # A unidade vem do produto do catálogo (wms_recebimento_itens não tem
        # essa coluna -- a tela mostrava "undefined" no rótulo da quantidade).
        return jsonify({"recebimento": dict(rec),
                        "itens": wms_pedidos.itens_do_recebimento(conn, recebimento_id)})
    finally:
        conn.close()


@app.route("/api/wms/recebimentos/<int:recebimento_id>/itens/<int:item_id>/enderecar", methods=["POST"])
@requer_auth(niveis=_NIVEIS_WMS)
@exige_mesma_origem
def api_wms_recebimento_enderecar_item(recebimento_id, item_id):
    """
    Chamada depois que a ENTRADA em si ja foi aceita por /api/wms/movimentos
    (esta rota nao move estoque, so contabiliza o quanto desta linha do
    recebimento ja foi endereçado). Soma ao qtd_enderecada da linha e fecha
    o recebimento (estado=ENDERECADO) quando toda linha RESOLVIDA (produto
    identificado) bateu a quantidade do pedido -- linha que ficou pendencia
    (produto nao encontrado no catalogo) nunca entra nessa conta: ela so
    aparece na tela como aviso, nunca trava o fechamento do recebimento.

    Idempotente pelo `uuid` do movimento de ENTRADA que o aparelho já
    mandou: resposta perdida + nova tentativa não conta duas vezes (a
    lógica está em wms_pedidos.contabilizar_enderecamento).

    registrar_recebimento (Task 8) nunca reverte ENDERECADO pra ESPERADO;
    esta rota so anda pra frente, pelo mesmo motivo.
    """
    _wms_exige_operador()
    body = request.get_json(force=True) or {}
    conn = wms_pedidos.conectar()
    try:
        r = wms_pedidos.contabilizar_enderecamento(
            conn, recebimento_id, item_id, body.get("qtd") or 0, body.get("uuid") or "")
    except (wms.ErroWMS, ValueError) as e:
        return _wms_json_erro(e)
    finally:
        conn.close()
    return jsonify({"ok": True, "item": r["item"], "recebimento": r["recebimento"],
                    "duplicado": r["duplicado"]})


@app.route("/api/wms/recebimentos/<int:recebimento_id>/encerrar-divergencia", methods=["POST"])
@requer_auth(niveis=_NIVEIS_WMS)
@exige_mesma_origem
def api_wms_recebimento_encerrar_divergencia(recebimento_id):
    """
    O operador terminou a descarga e faltou mercadoria: encerra o
    recebimento assumindo a falta e manda o relatório pro cliente.

    Sem isto, recebimento que chegou parcial nunca alcança o esperado,
    nunca fecha sozinho e fica pra sempre na lista do galpão. O
    fechamento automático do caso normal continua onde estava
    (contabilizar_enderecamento) -- este botão é só pro caso em que falta.

    NÃO mexe em estoque: o que chegou já entrou pelas ENTRADAs do
    endereçamento e o que faltou nunca existiu.

    O e-mail vai DEPOIS do encerramento, que já está commitado: falha de
    SMTP não pode desfazer a conferência do galpão. O resultado do envio
    volta no JSON pra tela poder dizer a verdade ao operador (e o e-mail
    nasce redirecionado pro interno enquanto o Hugo não ligar o envio
    real -- ver wms_faltas_recebimento).

    Se o envio falhar, NÃO há reenvio automático: quem remanda é a equipe,
    por `wms_faltas_recebimento.py --listar/--reenviar`. A falta fica
    congelada em falta_un, então o relatório é remontado idêntico depois.

    Repetir a chamada (resposta perdida no tablet, o operador aperta de
    novo) é SUCESSO, não erro: o trabalho dele já está gravado. Nesse caso
    a rota ainda tenta o e-mail -- se o primeiro deu certo, a idempotência
    segura; se falhou, esta é uma segunda chance de graça.
    """
    op = _wms_exige_operador()
    body = request.get_json(force=True) or {}
    conn = wms_pedidos.conectar()
    try:
        ja_encerrado = False
        try:
            r = wms_pedidos.encerrar_com_divergencia(
                conn, recebimento_id, body.get("observacao") or "", operador=op)
        except (wms.ErroWMS, ValueError) as e:
            rec = conn.execute("SELECT * FROM wms_recebimentos WHERE id = ?", (recebimento_id,)).fetchone()
            if not (rec and rec["estado"] == "DIVERGENCIA"):
                return _wms_json_erro(e)
            ja_encerrado = True
            r = {"recebimento": dict(rec),
                 "faltas": wms_pedidos.faltas_congeladas(conn, recebimento_id)}
        try:
            envio = wms_faltas_recebimento.notificar_faltas(conn, recebimento_id, _carregar_config())
        except Exception as e:  # noqa: BLE001 -- e-mail nunca derruba o encerramento
            logging.getLogger(__name__).exception("Falha no relatório de faltas do recebimento %s", recebimento_id)
            envio = {"enviado": False, "motivo": "erro_inesperado", "detalhe": str(e)}
    finally:
        conn.close()
    return jsonify({"ok": True, "ja_encerrado": ja_encerrado, "recebimento": r["recebimento"],
                    "faltas": r["faltas"], "envio": envio})


@app.route("/wms/etiqueta-produto.pdf")
@requer_auth(niveis=_NIVEIS_WMS)
def wms_etiqueta_produto_pdf():
    """PDF da etiqueta de mercadoria (produto + lote + validade, QR FL|...),
    imprimida na aba Receber. Mesma calibracao de midia da etiqueta de
    posicao vizinha (config.yaml wms.etiqueta_*) -- ver wms_etiquetas_pdf."""
    conn = wms_pedidos.conectar()
    cfg_wms = _carregar_config().get("wms", {}) or {}
    orientacao = request.args.get("orientacao") or cfg_wms.get("etiqueta_orientacao") or "paisagem"
    padrao = {"margem": cfg_wms.get("etiqueta_margem_mm", 0), "dx": cfg_wms.get("etiqueta_dx_mm", 0),
              "dy": cfg_wms.get("etiqueta_dy_mm", 0)}

    def _mm(nome):
        bruto = request.args.get(nome)
        if bruto in (None, ""):
            bruto = padrao[nome]
        try:
            return float(str(bruto).replace(",", "."))
        except ValueError:
            abort(400, f"Parâmetro {nome} inválido.")
    margem, dx, dy = _mm("margem"), _mm("dx"), _mm("dy")
    try:
        pdf = wms_etiqueta_produto.gerar_etiquetas_produto_pdf(
            conn, request.args.get("produto_id", type=int), request.args.get("lote", ""),
            request.args.get("validade") or None, copias=request.args.get("copias", 1, type=int),
            orientacao=orientacao, margem_mm=margem, desloc_x_mm=dx, desloc_y_mm=dy)
    except wms.ErroWMS as e:
        abort(400, str(e))
    finally:
        conn.close()
    return Response(pdf, mimetype="application/pdf",
                    headers={"Content-Disposition": "inline; filename=etiqueta-produto.pdf"})


@app.route("/wms/estoque")
@requer_auth(niveis=("total", "operador", "leitura"))
def wms_estoque():
    return render_template("wms_estoque.html")


@app.route("/wms/etiquetas.pdf")
@requer_auth(niveis=_NIVEIS_WMS)
def wms_etiquetas_pdf():
    """PDF com uma etiqueta por página no tamanho da mídia (5 × 10 cm em pé
    por padrão, ver wms.gerar_etiquetas_pdf). ?area=C5 imprime a área toda,
    ?codigos=C5-E3-N2,C5-P1 imprime só essas; ?orientacao=retrato|retrato-inv|
    paisagem muda o giro."""
    conn = wms.conectar()
    # Ajuste fino pra térmica (Elgin L42 Pro do galpão): ?orientacao=paisagem|
    # retrato|retrato-inv, ?margem=2 (mm nos 4 lados), ?dx=1&dy=-1 (deslocamento
    # em mm). Aceita vírgula decimal. Sem parâmetro na URL vale o padrão do
    # config.yaml (wms.etiqueta_orientacao, etiqueta_margem_mm, etiqueta_dx_mm,
    # etiqueta_dy_mm), calibrado com o Hugo em 04/09 (mídia deitada 10 × 5 cm)
    # -- assim os botões da tela já saem certos e afinar não exige deploy.
    cfg_wms = _carregar_config().get("wms", {}) or {}
    orientacao = request.args.get("orientacao") or cfg_wms.get("etiqueta_orientacao") or "paisagem"
    padrao = {"margem": cfg_wms.get("etiqueta_margem_mm", 0), "dx": cfg_wms.get("etiqueta_dx_mm", 0),
              "dy": cfg_wms.get("etiqueta_dy_mm", 0)}

    def _mm(nome):
        bruto = request.args.get(nome)
        if bruto in (None, ""):
            bruto = padrao[nome]
        try:
            return float(str(bruto).replace(",", "."))
        except ValueError:
            abort(400, f"Parâmetro {nome} inválido.")
    margem, dx, dy = _mm("margem"), _mm("dx"), _mm("dy")
    try:
        area = request.args.get("area")
        if area:
            codigos = [p["codigo"] for p in wms.listar_posicoes(conn, area)]
            nome = f"etiquetas_{wms.normalizar_codigo(area)}.pdf"
        else:
            codigos = [c for c in (request.args.get("codigos") or "").split(",") if c.strip()]
            nome = "etiquetas_" + (wms.normalizar_codigo(codigos[0]) if codigos else "vazio") + ".pdf"
        try:
            pdf = wms.gerar_etiquetas_pdf(conn, codigos, orientacao, margem, dx, dy)
        except wms.ErroWMS as e:
            abort(400, str(e))
    finally:
        conn.close()
    return Response(pdf, mimetype="application/pdf", headers={"Content-Disposition": f'inline; filename="{nome}"'})


# ── Atendimento (chat + chamados do portal do cliente), 09/09 ─────────────────
# Rotas em atendimento_chamados.py (mesmo padrão do portal: módulo separado
# que recebe os decoradores daqui; "atendimento" a seco colide com o
# pacote atendimento/ da raiz, o WhatsApp+e-mail).
import atendimento_chamados as _atendimento_web
_atendimento_web.registrar(app, requer_auth=requer_auth, exige_mesma_origem=exige_mesma_origem, carregar_config=_carregar_config)

# ── Demandas (tarefas pro time, por equipe e pessoa), 25/10 ───────────────────
import demandas
demandas.registrar(app, requer_auth=requer_auth, exige_mesma_origem=exige_mesma_origem,
                   carregar_config=_carregar_config)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8070, debug=False)
