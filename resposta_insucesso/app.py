# -*- coding: utf-8 -*-
"""
resposta_insucesso/app.py

Página pública, hospedada na MESMA VPS que já roda expedir_pedidos.py
(desde a migração de 17/08 -- ver DOC_EXECUCAO_CLAUDE_MIGRACAO_VPS.md),
onde o embarcador responde ao e-mail de insucesso na entrega -- pedido
do Hugo (18/08): substitui os botões mailto: (dependiam de abrir o
cliente de e-mail e enviar, e só eram lidos por IMAP a cada 30 min) por
um link único que já aplica a resposta no clique.

Mora dentro do próprio repositório (/opt/stokki-eventos), reaproveita o
MESMO venv/config.yaml/dados.db que o resto do projeto -- diferente de
confirmacao_motoristas/ (roda numa VPS separada, sem acesso à rede
local, por isso precisa de sincronização por push/pull). Aqui não há
nada pra sincronizar: o token do link carrega {sender_id,
failed_reason_id} assinado (itsdangerous) e cada acesso consulta
fingerprint_aguardando_resposta.buscar_pendentes_por_grupo AO VIVO --
ver insucesso_entrega/aplicar_resposta_insucesso.py, que aplica a
decisão no VUUPT na hora do POST.

Publicado em app.freshhub.com.br/insucesso via Caddy `handle_path` (ver
infra/Caddyfile-insucesso) -- mesmo padrão técnico validado em
painel_agentes/painel_agentes.py (Fase 1 da migração): ProxyFix(x_prefix=1)
faz url_for()/redirect() respeitarem o prefixo /insucesso. Sem proxy na
frente (dev local) isso é no-op.

COMO RODAR (dev):
    py -3 app.py
COMO RODAR (produção -- ver infra/resposta-insucesso.service):
    waitress-serve --host=127.0.0.1 --port=8072 app:app
"""
import sys
from datetime import date
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
# ORDEM IMPORTA: insucesso_entrega/ tem uma cópia desatualizada de
# expedir_pedidos.py (achado conhecido, ver DOC_EXECUCAO_CLAUDE_MIGRACAO_VPS.md)
# -- a raiz precisa vir ANTES no sys.path, senão "from expedir_pedidos
# import ..." (usado por aplicar_resposta_insucesso.py) resolve pra
# cópia errada. Por isso insert(0, raiz) e só depois insert(1, ...),
# nunca os dois com insert(0, ...).
sys.path.insert(0, str(_RAIZ))
sys.path.insert(1, str(_RAIZ / "insucesso_entrega"))

import yaml
from flask import Flask, render_template, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.middleware.proxy_fix import ProxyFix

from fingerprint_aguardando_resposta import buscar_pendentes_por_grupo
from motivos_falha import texto_do_motivo
import aplicar_resposta_insucesso as logica

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_prefix=1, x_proto=1, x_for=1, x_host=1)

ACOES_VALIDAS = ("manter", "reagendar", "cancelar")
_MAX_AGE_SEGUNDOS = 30 * 24 * 3600  # link válido por 30 dias


def _carregar_config() -> dict:
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


_CONFIG = _carregar_config()
_TOKEN_SECRET = _CONFIG.get("resposta_insucesso", {}).get("token_secret")
if not _TOKEN_SECRET:
    raise RuntimeError(
        "resposta_insucesso.token_secret ausente no config.yaml -- gere uma string "
        "aleatória forte antes de subir esta página."
    )
_serializer = URLSafeTimedSerializer(_TOKEN_SECRET, salt="resposta-insucesso")


def _validar_token(token: str) -> dict:
    """Retorna {'estado': 'ok', 'payload': {...}} ou
    {'estado': 'expirado'|'invalido'}."""
    try:
        payload = _serializer.loads(token, max_age=_MAX_AGE_SEGUNDOS)
        return {"estado": "ok", "payload": payload}
    except SignatureExpired:
        return {"estado": "expirado"}
    except BadSignature:
        return {"estado": "invalido"}


def _data_valida(valor: str) -> bool:
    try:
        d = date.fromisoformat(valor)
    except ValueError:
        return False
    return d >= date.today()


def _codigo(code: str | None) -> str:
    """Normaliza o código do pedido pra exibição -- mesmo padrão usado
    nos e-mails (evita '##1234' se o valor salvo já tiver o #)."""
    return "#" + (code or "").lstrip("#")


@app.route("/r/<token>", methods=["GET", "POST"])
def responder(token):
    validacao = _validar_token(token)
    if validacao["estado"] == "invalido":
        return render_template("resposta.html", estado="invalido"), 404
    if validacao["estado"] == "expirado":
        return render_template("resposta.html", estado="expirado"), 410

    sender_id = validacao["payload"]["sender_id"]
    failed_reason_id = validacao["payload"]["failed_reason_id"]
    motivo_texto = texto_do_motivo(failed_reason_id)

    erro = None
    if request.method == "POST":
        acao = request.form.get("acao")
        data_pedida_raw = (request.form.get("data_pedida") or "").strip()
        nova_data = None
        if acao not in ACOES_VALIDAS:
            erro = "Ação inválida."
        elif acao == "reagendar":
            if not _data_valida(data_pedida_raw):
                erro = "Informe uma data válida (a partir de hoje) para o reagendamento."
            else:
                nova_data = date.fromisoformat(data_pedida_raw)

        if not erro:
            resultados = logica.aplicar_decisao(sender_id, failed_reason_id, acao, nova_data, _CONFIG)
            if not resultados:
                return render_template("resposta.html", estado="resolvido")
            resultados_view = [{**r, "code": _codigo(r["code"])} for r in resultados]
            return render_template("resposta.html", estado="aplicado", resultados=resultados_view)

    pendentes = buscar_pendentes_por_grupo(sender_id, failed_reason_id)
    if not pendentes:
        return render_template("resposta.html", estado="resolvido")
    codigos = [_codigo(p.get("code")) for p in pendentes]
    return render_template("resposta.html", estado="formulario",
                           motivo_texto=motivo_texto, codigos=codigos, erro=erro)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8072, debug=False)
