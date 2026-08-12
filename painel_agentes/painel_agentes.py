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
from flask import Flask, Response, abort, redirect, render_template, request, url_for, jsonify, send_file

from agentes import AGENTES, buscar_agente, categorias_ordenadas
from executor import (
    iniciar_execucao, buscar_execucao, buscar_ultima_execucao,
    listar_execucoes_recentes, ha_execucao_rodando, ler_log, limpar_execucoes_travadas,
    encerrar_todas_execucoes,
)
from mapa_rotas import buscar_rotas_para_mapa
from planejamento_rotas import (
    buscar_dados_planejamento, buscar_pool_nao_alocados, gerar_romaneio_pdf,
    carregar_documentos_do_rascunho,
)
import rascunhos_rota

app = Flask(__name__)

# Roda uma vez, assim que o painel sobe -- destrava qualquer execução
# que ficou presa em RODANDO por causa de um encerramento à força do
# processo anterior (ex: Ctrl+C no meio de uma execução, pedido do
# Hugo 04/08). Nada pode estar genuinamente rodando nesse momento.
limpar_execucoes_travadas()


def _carregar_config() -> dict:
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def requer_auth(f):
    @wraps(f)
    def decorado(*args, **kwargs):
        config = _carregar_config()
        cfg_painel = config.get("painel_agentes", {})
        usuario_esperado = cfg_painel.get("usuario")
        senha_esperada = cfg_painel.get("senha")
        if not usuario_esperado or not senha_esperada:
            return (
                "Painel de agentes desabilitado: configure painel_agentes.usuario "
                "e painel_agentes.senha no config.yaml antes de subir.", 500,
            )
        auth = request.authorization
        if not auth or not (
            hmac.compare_digest(auth.username, usuario_esperado)
            and hmac.compare_digest(auth.password, senha_esperada)
        ):
            return Response(
                "Autenticação necessária", 401,
                {"WWW-Authenticate": 'Basic realm="Painel de Agentes"'},
            )
        return f(*args, **kwargs)
    return decorado


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
@requer_auth
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
@requer_auth
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
    )


@app.route("/api/planejamento/pool")
@requer_auth
def api_pool():
    """Busca ao vivo na VUUPT só o pool de não alocados (botão
    'Atualizar' da tela) -- não mexe nos rascunhos/mapa já carregados,
    pra não perder o estado de edição em andamento."""
    data_alvo = _parse_data_param()
    try:
        pool = buscar_pool_nao_alocados(data_alvo)
    except Exception as e:
        return jsonify({"erro": str(e)}), 500
    return jsonify({"pool": pool})


@app.route("/api/planejamento/romaneio/<int:rascunho_id>")
@requer_auth
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
@requer_auth
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
@requer_auth
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
@requer_auth
@exige_mesma_origem
def api_reordenar():
    body = request.get_json(force=True)
    try:
        rascunhos_rota.reordenar_paradas(body["rascunho_id"], body["ordem_service_ids"])
    except (KeyError, ValueError) as e:
        return jsonify({"erro": str(e)}), 400
    return jsonify({"ok": True, "rascunho": _rascunho_ou_404(body["rascunho_id"])})


@app.route("/api/planejamento/remover-parada", methods=["POST"])
@requer_auth
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
@requer_auth
@exige_mesma_origem
def api_adicionar_parada():
    body = request.get_json(force=True)
    try:
        rascunhos_rota.adicionar_parada(body["rascunho_id"], body["parada"], body.get("ordem"))
    except (KeyError, ValueError) as e:
        return jsonify({"erro": str(e)}), 400
    return jsonify({"ok": True, "rascunho": _rascunho_ou_404(body["rascunho_id"])})


@app.route("/api/planejamento/trocar-motorista", methods=["POST"])
@requer_auth
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


@app.route("/api/planejamento/nova-rota", methods=["POST"])
@requer_auth
@exige_mesma_origem
def api_nova_rota():
    body = request.get_json(force=True)
    try:
        data_alvo = datetime.strptime(body["data_alvo"], "%Y-%m-%d").date()
        rascunhos_do_dia = rascunhos_rota.listar_rascunhos_do_dia(data_alvo)
        if not rascunhos_do_dia:
            return jsonify({"erro": "Nenhum lote ativo para essa data -- rode o pipeline em modo rascunho primeiro."}), 400
        lote_id = rascunhos_do_dia[0]["lote_id"]
        referencia = rascunhos_do_dia[0]
        rascunho_id = rascunhos_rota.criar_rascunho_vazio(
            data_alvo, lote_id, body.get("particao") or referencia["particao"],
            body.get("tipo_rota") or referencia["tipo_rota"],
            referencia["start_location_base_id"], referencia["end_location_base_id"],
            referencia["start_at"],
        )
    except (KeyError, ValueError) as e:
        return jsonify({"erro": str(e)}), 400
    return jsonify({"ok": True, "rascunho": _rascunho_ou_404(rascunho_id)})


@app.route("/api/planejamento/otimizar-sequencia", methods=["POST"])
@requer_auth
@exige_mesma_origem
def api_otimizar_sequencia():
    body = request.get_json(force=True)
    try:
        rascunhos_rota.otimizar_sequencia(body["rascunho_id"])
    except (KeyError, ValueError) as e:
        return jsonify({"erro": str(e)}), 400
    return jsonify({"ok": True, "rascunho": _rascunho_ou_404(body["rascunho_id"])})


@app.route("/api/planejamento/descartar-rota", methods=["POST"])
@requer_auth
@exige_mesma_origem
def api_descartar_rota():
    body = request.get_json(force=True)
    try:
        rascunhos_rota.descartar_rascunho(body["rascunho_id"])
    except (KeyError, ValueError) as e:
        return jsonify({"erro": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/planejamento/confirmar-envio", methods=["POST"])
@requer_auth
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8070, debug=False)
