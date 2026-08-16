# -*- coding: utf-8 -*-
"""
confirmacao_motoristas/app.py

App público (roda numa VPS, fora da rede local) onde o motorista
confirma ou recusa a rota do dia que já foi designada a ele. Pedido do
Hugo, 16/08 -- ver DOC_EXECUCAO_CLAUDE_NOTIFICACAO_MOTORISTAS.md
("Fase 2: confirmação interativa de leitura").

Projeto standalone e pequeno de propósito: não compartilha nada com o
resto do repositório (nem config.yaml, nem dados.db) porque roda numa
máquina diferente, sem acesso à rede local. Todo o estado que ele
precisa chega por push da máquina local (POST /api/sync/upsert) e toda
resposta do motorista sai por pull (GET /api/sync/respostas) -- ver
roteirizacao/avisar_motoristas_rotas.py::push_confirmacoes_vps e
roteirizacao/sincronizar_respostas_confirmacao.py no repo principal.

O link em si é assinado (itsdangerous) com o MESMO token_secret salvo
em config.yaml (confirmacao_rotas.token_secret) na máquina local --
isso garante que só um link gerado por lá é aceito aqui, mesmo que
alguém adivinhe o formato da URL. O segredo dos endpoints /api/sync/*
(SYNC_SECRET) é outro par, separado do token_secret.

CONFIGURAÇÃO (variáveis de ambiente, ver infra/env.exemplo):
    TOKEN_SECRET   -- idêntico a confirmacao_rotas.token_secret no config.yaml local
    SYNC_SECRET    -- idêntico a confirmacao_rotas.sync_secret no config.yaml local
    DB_PATH        -- caminho do sqlite local (padrão: confirmacoes.db ao lado deste arquivo)
    PORT           -- porta HTTP (padrão: 8090; Caddy é quem expõe 443 na frente)

COMO RODAR (dev):
    py -3 app.py
COMO RODAR (produção -- ver infra/confirmacao-motoristas.service):
    waitress-serve --host=127.0.0.1 --port=8090 app:app
"""
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, abort, g, jsonify, render_template, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

_RAIZ = Path(__file__).parent
_DB_PATH = Path(os.environ.get("DB_PATH", _RAIZ / "confirmacoes.db"))
_TOKEN_SECRET = os.environ["TOKEN_SECRET"]
_SYNC_SECRET = os.environ["SYNC_SECRET"]
_MAX_AGE_SEGUNDOS = 72 * 3600  # link válido por 72h -- mesma janela do aviso "amanhã" + folga
_MAX_TENTATIVAS = 5

STATUS_AGUARDANDO = "AGUARDANDO"
STATUS_CONFIRMADO = "CONFIRMADO"
STATUS_RECUSADO = "RECUSADO"

app = Flask(__name__)
_serializer = URLSafeTimedSerializer(_TOKEN_SECRET, salt="confirmacao-rota")

CAMPOS_SYNC = (
    "token", "vuupt_route_id", "agent_id", "data_rota", "nome_motorista", "zona",
    "horario_previsto", "qtd_entregas", "telefone_ultimos4",
)


def _conectar():
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS confirmacoes (
            token              TEXT PRIMARY KEY,
            vuupt_route_id     INTEGER NOT NULL,
            agent_id           INTEGER NOT NULL,
            data_rota          TEXT NOT NULL,
            nome_motorista     TEXT,
            zona               TEXT,
            horario_previsto   TEXT,
            qtd_entregas       INTEGER,
            telefone_ultimos4  TEXT,
            status             TEXT NOT NULL DEFAULT 'AGUARDANDO',
            motivo_recusa      TEXT,
            tentativas         INTEGER NOT NULL DEFAULT 0,
            recebido_em        TEXT NOT NULL,
            respondido_em      TEXT
        )
    """)
    conn.commit()
    return conn


def _agora_iso() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _exige_segredo_sync(f):
    """Protege os endpoints de sincronização com a máquina local --
    comparação de tempo constante (hmac.compare_digest-like via ==
    não é seguro contra timing attack, mas aqui o segredo já viaja só
    entre dois hosts que a gente controla; suficiente pra esse caso)."""
    import functools
    import hmac

    @functools.wraps(f)
    def decorado(*args, **kwargs):
        recebido = request.headers.get("X-Sync-Secret", "")
        if not hmac.compare_digest(recebido, _SYNC_SECRET):
            abort(401)
        return f(*args, **kwargs)
    return decorado


def _validar_token(token: str) -> dict:
    """Retorna {'estado': 'ok', 'payload': {...}} ou
    {'estado': 'expirado'|'invalido'}. Não consulta o banco -- só
    confere a assinatura/validade do link em si."""
    try:
        payload = _serializer.loads(token, max_age=_MAX_AGE_SEGUNDOS)
        return {"estado": "ok", "payload": payload}
    except SignatureExpired:
        return {"estado": "expirado"}
    except BadSignature:
        return {"estado": "invalido"}


@app.route("/r/<token>", methods=["GET", "POST"])
def confirmar_rota(token):
    validacao = _validar_token(token)
    if validacao["estado"] == "invalido":
        return render_template("confirmar.html", estado="invalido"), 404
    if validacao["estado"] == "expirado":
        return render_template("confirmar.html", estado="expirado"), 410

    conn = _conectar()
    try:
        linha = conn.execute("SELECT * FROM confirmacoes WHERE token = ?", (token,)).fetchone()
        if linha is None:
            # Link válido (assinatura ok) mas ainda não chegou o push da
            # máquina local -- acontece se o motorista clicar nos
            # primeiros segundos após o envio. Não é erro do usuário.
            return render_template("confirmar.html", estado="nao_encontrado"), 404

        rota = dict(linha)
        erro = None

        if request.method == "POST":
            if rota["tentativas"] >= _MAX_TENTATIVAS:
                return render_template("confirmar.html", estado="bloqueado", rota=rota), 429

            acao = request.form.get("acao")
            ultimos4_informado = re.sub(r"\D", "", request.form.get("ultimos4", ""))
            exige_checagem = bool(rota["telefone_ultimos4"])

            if exige_checagem and ultimos4_informado != rota["telefone_ultimos4"]:
                conn.execute("UPDATE confirmacoes SET tentativas = tentativas + 1 WHERE token = ?", (token,))
                conn.commit()
                rota["tentativas"] += 1
                erro = "Os 4 últimos dígitos não conferem com o telefone cadastrado."
            elif acao not in ("confirmar", "recusar"):
                erro = "Ação inválida."
            else:
                novo_status = STATUS_CONFIRMADO if acao == "confirmar" else STATUS_RECUSADO
                motivo = request.form.get("motivo", "").strip()[:500] or None if acao == "recusar" else None
                conn.execute("""
                    UPDATE confirmacoes
                    SET status = ?, motivo_recusa = ?, respondido_em = ?
                    WHERE token = ?
                """, (novo_status, motivo, _agora_iso(), token))
                conn.commit()
                rota["status"] = novo_status
                rota["motivo_recusa"] = motivo

        return render_template("confirmar.html", estado="formulario", rota=rota, erro=erro)
    finally:
        conn.close()


@app.route("/api/sync/upsert", methods=["POST"])
@_exige_segredo_sync
def api_sync_upsert():
    """Recebe da máquina local as confirmações pendentes (novas ou com
    dados de rota atualizados). Só toca nos campos descritivos -- nunca
    sobrescreve status/resposta já registrados aqui, pra um reenvio de
    aviso não apagar a resposta que o motorista já deu."""
    corpo = request.get_json(silent=True) or {}
    itens = corpo.get("confirmacoes") or []
    conn = _conectar()
    try:
        agora = _agora_iso()
        for item in itens:
            if not all(campo in item for campo in ("token", "vuupt_route_id", "agent_id", "data_rota")):
                continue
            conn.execute(f"""
                INSERT INTO confirmacoes ({", ".join(CAMPOS_SYNC)}, status, recebido_em)
                VALUES ({", ".join("?" * len(CAMPOS_SYNC))}, 'AGUARDANDO', ?)
                ON CONFLICT(token) DO UPDATE SET
                    vuupt_route_id    = excluded.vuupt_route_id,
                    agent_id          = excluded.agent_id,
                    data_rota         = excluded.data_rota,
                    nome_motorista    = excluded.nome_motorista,
                    zona              = excluded.zona,
                    horario_previsto  = excluded.horario_previsto,
                    qtd_entregas      = excluded.qtd_entregas,
                    telefone_ultimos4 = excluded.telefone_ultimos4
            """, tuple(item.get(campo) for campo in CAMPOS_SYNC) + (agora,))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"recebidas": len(itens)})


@app.route("/api/sync/respostas", methods=["GET"])
@_exige_segredo_sync
def api_sync_respostas():
    """Devolve pra máquina local as respostas dos motoristas
    (respondido_em preenchido). Pull completo a cada chamada -- volume
    é baixo (poucas dezenas de confirmações por dia), não precisa de
    cursor incremental."""
    conn = _conectar()
    try:
        linhas = conn.execute(
            "SELECT token, status, motivo_recusa, respondido_em FROM confirmacoes "
            "WHERE respondido_em IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    return jsonify({"respostas": [dict(linha) for linha in linhas]})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 8090)), debug=False)
