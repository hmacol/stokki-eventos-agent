# -*- coding: utf-8 -*-
"""
dashboard_embarcadores.py

App Flask do dashboard web de comparativo de embarcadores (pedido do
Hugo, 30/07): pedidos por semana desde o início do ano, fluxo mensal
por embarcador, destinatários mais recorrentes, resumo -- e os filtros
interativos pedidos em seguida (30/07): destinatários recorrentes por
ano/mês, pedidos por semana filtrado por embarcador (barras), pedidos
por embarcador filtrado por ano/mês/semana (rosca/pizza).

Roda como serviço (mesmo estilo do dashboard já existente no
agente_relatorio) — porta 8060 (5060 é bloqueada pelo Chrome/SIP,
ERR_UNSAFE_PORT).

Este app NUNCA consulta o VUUPT diretamente — só lê os arquivos
dados/dashboard_atual.json (resumo) e dados/registros_dashboard.json
(lista enriquecida, usada pelas rotas de filtro pra agregar sob
demanda), pré-computados 1x/dia por atualizar_dashboard.py.

AUTENTICAÇÃO (pedido do Hugo, 30/07: expor pra fora via túnel, precisa
de senha): HTTP Basic Auth simples, usuário/senha lidos de
config.yaml (seção dashboard_embarcadores.usuario/senha, na raiz do
projeto). Aplicada em TODAS as rotas, inclusive as /api/... (elas
também expõem dado de negócio). Sem HTTPS o Basic Auth sozinho não
seria seguro (a senha viaja em texto quase plano) — mas rodando atrás
de Cloudflare Tunnel, o HTTPS já vem de graça (o túnel cuida disso),
então é seguro o bastante nesse cenário.

SERVIDOR: pra expor de verdade (fora de teste local), NÃO use este
arquivo direto com `py -3.11 dashboard_embarcadores.py` — isso roda o
servidor de desenvolvimento do Flask, que ele mesmo avisa não ser
adequado pra produção. Use waitress (`pip install waitress`):
    waitress-serve --host=0.0.0.0 --port=8060 dashboard_embarcadores:app
(setup_tarefas.ps1 já foi atualizado pra rodar assim.)

COMO USAR (só teste local rápido, sem waitress):
    py -3.11 dashboard_embarcadores.py
    (abre em http://localhost:8060 -- vai pedir usuário/senha)
"""
import functools
import json
import logging
import sys
from pathlib import Path

import yaml
from flask import Flask, Response, jsonify, render_template, request, send_file

from capturar_dashboard import capturar_dashboard
from dashboard_dados import (
    filtrar_destinatarios_recorrentes,
    opcoes_filtro,
    pedidos_por_embarcador_periodo,
    pedidos_semana_por_embarcador,
)

_RAIZ_LOCAL = Path(__file__).parent
_RAIZ_PROJETO = Path(__file__).parent.parent
ARQUIVO_DASHBOARD = _RAIZ_LOCAL / "dados" / "dashboard_atual.json"
ARQUIVO_REGISTROS = _RAIZ_LOCAL / "dados" / "registros_dashboard.json"
PORTA = 8060

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)


def _carregar_credenciais() -> dict:
    """
    Lê usuário/senha de config.yaml (seção dashboard_embarcadores).
    Sem essa seção configurada, o app RECUSA subir (evita expor o
    dashboard sem senha nenhuma por esquecimento de configuração).
    """
    caminho_config = _RAIZ_PROJETO / "config.yaml"
    with open(caminho_config, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    cfg = config.get("dashboard_embarcadores", {})
    usuario, senha = cfg.get("usuario"), cfg.get("senha")
    if not usuario or not senha:
        raise RuntimeError(
            "config.yaml precisa ter dashboard_embarcadores.usuario e "
            ".senha configurados antes de rodar o dashboard exposto."
        )
    return {"usuario": usuario, "senha": senha}


_CREDENCIAIS = _carregar_credenciais()


def requer_login(func):
    @functools.wraps(func)
    def decorada(*args, **kwargs):
        auth = request.authorization
        if not auth or auth.username != _CREDENCIAIS["usuario"] or auth.password != _CREDENCIAIS["senha"]:
            return Response(
                "Acesso restrito — informe usuário e senha.", 401,
                {"WWW-Authenticate": 'Basic realm="Dashboard Freshlog"'},
            )
        return func(*args, **kwargs)
    return decorada


def _carregar_dados() -> dict:
    if not ARQUIVO_DASHBOARD.exists():
        return {
            "atualizado_em": None,
            "total_pedidos": 0,
            "total_embarcadores": 0,
            "total_destinatarios": 0,
            "clientes_recorrentes": 0,
            "semanas": [],
            "pedidos_por_semana": [],
            "meses": [],
            "fluxo_embarcadores": [],
            "top_destinatarios": [],
            "sem_dados": True,
        }
    dados = json.loads(ARQUIVO_DASHBOARD.read_text(encoding="utf-8"))
    dados["sem_dados"] = False
    return dados


def _carregar_registros() -> list[dict]:
    if not ARQUIVO_REGISTROS.exists():
        return []
    return json.loads(ARQUIVO_REGISTROS.read_text(encoding="utf-8"))


def _int_ou_none(valor):
    return int(valor) if valor else None


@app.route("/")
@requer_login
def index():
    dados = _carregar_dados()
    opcoes = opcoes_filtro(_carregar_registros()) if not dados["sem_dados"] else {
        "anos": [], "meses": [], "semanas": [], "embarcadores": []
    }
    return render_template("dashboard.html", dados=dados, opcoes=opcoes)


@app.route("/exportar/<formato>")
@requer_login
def exportar(formato):
    """
    Botão "Tirar foto" / "Exportar PDF" (pedido do Hugo, 30/07) — captura
    o dashboard AGORA (Playwright headless, mesma técnica da rotina
    semanal por e-mail) e devolve o arquivo pra download.
    """
    if formato not in ("png", "pdf"):
        return "Formato inválido — use png ou pdf.", 400

    pasta_export = _RAIZ_LOCAL / "dados" / "exportacoes"
    caminho_png = pasta_export / "dashboard.png"
    caminho_pdf = pasta_export / "dashboard.pdf"
    url_interna = f"http://127.0.0.1:{PORTA}/"

    try:
        capturar_dashboard(url_interna, _CREDENCIAIS["usuario"], _CREDENCIAIS["senha"],
                          caminho_png, caminho_pdf)
    except Exception as e:
        logger.error(f"Falha ao exportar dashboard: {e}")
        return f"Falha ao gerar {formato}: {e}", 500

    caminho = caminho_png if formato == "png" else caminho_pdf
    tipo_mime = "image/png" if formato == "png" else "application/pdf"
    return send_file(caminho, mimetype=tipo_mime, as_attachment=True,
                     download_name=f"dashboard_embarcadores.{formato}")


@app.route("/api/dados")
@requer_login
def api_dados():
    """Endpoint JSON cru do resumo — útil pra debug ou integração futura."""
    return jsonify(_carregar_dados())


@app.route("/api/destinatarios_recorrentes")
@requer_login
def api_destinatarios_recorrentes():
    """Top destinatários recorrentes, filtrado por ?ano= e/ou ?mes= (AAAA-MM)."""
    ano = _int_ou_none(request.args.get("ano"))
    mes = request.args.get("mes") or None
    registros = _carregar_registros()
    return jsonify(filtrar_destinatarios_recorrentes(registros, ano=ano, mes=mes))


@app.route("/api/pedidos_semana_embarcador")
@requer_login
def api_pedidos_semana_embarcador():
    """Pedidos por semana pra UM embarcador (?embarcador=Nome), pro gráfico de barras."""
    embarcador = request.args.get("embarcador", "")
    registros = _carregar_registros()
    return jsonify(pedidos_semana_por_embarcador(registros, embarcador))


@app.route("/api/pedidos_por_embarcador")
@requer_login
def api_pedidos_por_embarcador():
    """
    Total de pedidos por embarcador, pro gráfico de rosca/pizza.
    Filtro mais específico tem prioridade: ?semana= > ?mes= > ?ano=.
    """
    ano = _int_ou_none(request.args.get("ano"))
    mes = request.args.get("mes") or None
    semana = request.args.get("semana") or None
    registros = _carregar_registros()
    return jsonify(pedidos_por_embarcador_periodo(registros, ano=ano, mes=mes, semana=semana))


if __name__ == "__main__":
    logger.info(f"Dashboard de embarcadores em http://localhost:{PORTA} (uso de teste local — "
               f"pra expor de verdade, use waitress, ver docstring deste arquivo)")
    app.run(host="0.0.0.0", port=PORTA, debug=False)
