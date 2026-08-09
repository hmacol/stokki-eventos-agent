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

import yaml
from flask import Flask, Response, redirect, render_template, request, url_for, jsonify

from agentes import AGENTES, buscar_agente, categorias_ordenadas
from executor import (
    iniciar_execucao, buscar_execucao, buscar_ultima_execucao,
    listar_execucoes_recentes, ha_execucao_rodando, ler_log, limpar_execucoes_travadas,
    encerrar_todas_execucoes,
)
from mapa_rotas import buscar_rotas_para_mapa

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
        if not auth or auth.username != usuario_esperado or auth.password != senha_esperada:
            return Response(
                "Autenticação necessária", 401,
                {"WWW-Authenticate": 'Basic realm="Painel de Agentes"'},
            )
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8070, debug=False)
