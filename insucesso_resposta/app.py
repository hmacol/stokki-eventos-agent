# -*- coding: utf-8 -*-
"""
insucesso_resposta/app.py

App público (roda numa VPS, fora da rede local) onde o embarcador
responde ao e-mail de insucesso na entrega -- pedido do Hugo (18/08):
substitui os botões mailto: (dependiam de abrir o cliente de e-mail e
enviar, e só eram lidos pelo job de IMAP a cada 30 min) por um link
único que já grava a resposta no clique.

Mesmo molde de confirmacao_motoristas/app.py: projeto standalone, sem
acesso à rede local (nem config.yaml, nem dados.db/VUUPT) -- só guarda a
ESCOLHA do embarcador. Quem decide a quais pedidos ela se aplica e quem
liga no VUUPT é sempre a máquina local, via
insucesso_entrega/sincronizar_respostas_insucesso.py (pull) e
insucesso_entrega/notificar_insucesso_aguardando_resposta.py (push) --
ver esses dois arquivos no repo principal.

O link é assinado (itsdangerous) com o MESMO token_secret salvo em
config.yaml (resposta_insucesso.token_secret) na máquina local. O
segredo dos endpoints /api/sync/* (SYNC_SECRET) é outro par.

Diferente do app de motoristas: aqui a resposta liga no VUUPT
(cancelar/duplicar/reagendar) e NÃO é idempotente sem os fingerprints
locais -- por isso existe /api/sync/ack (fecha o ciclo depois que a
máquina local aplicou a decisão) e o upsert só reescreve o token de um
grupo que ainda não foi respondido (ou cujo ciclo anterior já foi
totalmente aplicado, ou cujo token antigo já expirou) -- nunca de um
grupo respondido e ainda não puxado, pra não perder a decisão do
embarcador.

CONFIGURAÇÃO (variáveis de ambiente, ver infra/env.exemplo):
    TOKEN_SECRET   -- idêntico a resposta_insucesso.token_secret no config.yaml local
    SYNC_SECRET    -- idêntico a resposta_insucesso.sync_secret no config.yaml local
    DB_PATH        -- caminho do sqlite local (padrão: respostas.db ao lado deste arquivo)
    PORT           -- porta HTTP (padrão: 8091; Caddy é quem expõe 443 na frente)

COMO RODAR (dev):
    py -3 app.py
COMO RODAR (produção -- ver infra/insucesso-resposta.service):
    waitress-serve --host=127.0.0.1 --port=8091 app:app
"""
import json
import os
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

_RAIZ = Path(__file__).parent
_DB_PATH = Path(os.environ.get("DB_PATH", _RAIZ / "respostas.db"))
_TOKEN_SECRET = os.environ["TOKEN_SECRET"]
_SYNC_SECRET = os.environ["SYNC_SECRET"]
# Link válido por 30 dias -- resposta de insucesso não é tão urgente
# quanto confirmar a rota do dia seguinte (72h no app de motoristas).
_MAX_AGE_SEGUNDOS = 30 * 24 * 3600

ACOES_VALIDAS = ("manter", "reagendar", "cancelar")

app = Flask(__name__)
_serializer = URLSafeTimedSerializer(_TOKEN_SECRET, salt="resposta-insucesso")


def _conectar():
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS respostas (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            token                 TEXT NOT NULL UNIQUE,
            sender_id             INTEGER NOT NULL,
            failed_reason_id      INTEGER NOT NULL,
            motivo_texto          TEXT,
            pergunta_texto        TEXT,
            prazo_texto           TEXT,
            aviso_dia_fixo_texto  TEXT,
            pedidos_json          TEXT NOT NULL,
            status                TEXT NOT NULL DEFAULT 'AGUARDANDO',
            acao                  TEXT,
            data_pedida           TEXT,
            recebido_em           TEXT NOT NULL,
            respondido_em         TEXT,
            aplicado_em           TEXT,
            UNIQUE (sender_id, failed_reason_id)
        )
    """)
    conn.commit()
    return conn


def _agora_iso() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _exige_segredo_sync(f):
    """Protege os endpoints de sincronização com a máquina local (mesmo
    padrão de confirmacao_motoristas/app.py::_exige_segredo_sync)."""
    import functools
    import hmac

    @functools.wraps(f)
    def decorado(*args, **kwargs):
        recebido = request.headers.get("X-Sync-Secret", "")
        if not hmac.compare_digest(recebido, _SYNC_SECRET):
            abort(401)
        return f(*args, **kwargs)
    return decorado


def _token_ainda_valido(token: str) -> bool:
    try:
        _serializer.loads(token, max_age=_MAX_AGE_SEGUNDOS)
        return True
    except (BadSignature, SignatureExpired):
        return False


def _validar_token(token: str) -> dict:
    """Retorna {'estado': 'ok'} ou {'estado': 'expirado'|'invalido'} --
    só confere a assinatura/validade do link, não consulta o banco."""
    try:
        _serializer.loads(token, max_age=_MAX_AGE_SEGUNDOS)
        return {"estado": "ok"}
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


@app.route("/r/<token>", methods=["GET", "POST"])
def responder(token):
    validacao = _validar_token(token)
    if validacao["estado"] == "invalido":
        return render_template("resposta.html", estado="invalido"), 404
    if validacao["estado"] == "expirado":
        return render_template("resposta.html", estado="expirado"), 410

    conn = _conectar()
    try:
        linha = conn.execute("SELECT * FROM respostas WHERE token = ?", (token,)).fetchone()
        if linha is None:
            # Link válido (assinatura ok) mas o push da máquina local
            # ainda não chegou -- acontece se o embarcador clicar nos
            # primeiros segundos após o envio, ou se o grupo já foi
            # respondido/aplicado e reiniciado com um token novo.
            return render_template("resposta.html", estado="nao_encontrado"), 404

        grupo = dict(linha)
        grupo["pedidos"] = json.loads(grupo["pedidos_json"])
        erro = None

        if request.method == "POST":
            if grupo["aplicado_em"]:
                erro = "Sua resposta já foi processada -- não é mais possível alterar por aqui."
            else:
                acao = request.form.get("acao")
                data_pedida = (request.form.get("data_pedida") or "").strip()
                if acao not in ACOES_VALIDAS:
                    erro = "Ação inválida."
                elif acao == "reagendar" and not _data_valida(data_pedida):
                    erro = "Informe uma data válida (a partir de hoje) para o reagendamento."
                else:
                    agora = _agora_iso()
                    data_final = data_pedida if acao == "reagendar" else None
                    conn.execute("""
                        UPDATE respostas
                        SET status = 'RESPONDIDO', acao = ?, data_pedida = ?, respondido_em = ?
                        WHERE token = ?
                    """, (acao, data_final, agora, token))
                    conn.commit()
                    grupo.update(status="RESPONDIDO", acao=acao, data_pedida=data_final, respondido_em=agora)

        return render_template("resposta.html", estado="formulario", grupo=grupo, erro=erro)
    finally:
        conn.close()


@app.route("/api/sync/upsert", methods=["POST"])
@_exige_segredo_sync
def api_sync_upsert():
    """Recebe da máquina local o conteúdo de um grupo (remetente +
    motivo) pra exibir na página. Se o grupo já existe e está
    RESPONDIDO mas ainda não foi puxado/aplicado (aplicado_em nulo),
    ignora silenciosamente -- não sobrescreve uma decisão pendente de
    aplicar. Se já existe e está AGUARDANDO com um token ainda válido,
    mantém o MESMO token (o link de um e-mail antigo continua
    funcionando mesmo depois de um novo aviso pro mesmo grupo) e só
    atualiza o conteúdo exibido."""
    corpo = request.get_json(silent=True) or {}
    sender_id = corpo.get("sender_id")
    failed_reason_id = corpo.get("failed_reason_id")
    token_novo = corpo.get("token")
    pedidos = corpo.get("pedidos") or []
    if sender_id is None or failed_reason_id is None or not token_novo or not pedidos:
        abort(400)

    conn = _conectar()
    try:
        linha = conn.execute(
            "SELECT token, status, aplicado_em FROM respostas WHERE sender_id = ? AND failed_reason_id = ?",
            (sender_id, failed_reason_id),
        ).fetchone()

        if linha is not None and linha["status"] == "RESPONDIDO" and linha["aplicado_em"] is None:
            return jsonify({"token": linha["token"], "ignorado": True})

        pode_reescrever_token = (
            linha is None
            or linha["aplicado_em"] is not None
            or not _token_ainda_valido(linha["token"])
        )
        token_final = token_novo if pode_reescrever_token else linha["token"]

        conn.execute("""
            INSERT INTO respostas
                (token, sender_id, failed_reason_id, motivo_texto, pergunta_texto,
                 prazo_texto, aviso_dia_fixo_texto, pedidos_json, status, recebido_em)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'AGUARDANDO', ?)
            ON CONFLICT(sender_id, failed_reason_id) DO UPDATE SET
                token                = excluded.token,
                motivo_texto         = excluded.motivo_texto,
                pergunta_texto       = excluded.pergunta_texto,
                prazo_texto          = excluded.prazo_texto,
                aviso_dia_fixo_texto = excluded.aviso_dia_fixo_texto,
                pedidos_json         = excluded.pedidos_json,
                status               = 'AGUARDANDO',
                acao                 = NULL,
                data_pedida          = NULL,
                recebido_em          = excluded.recebido_em,
                respondido_em        = NULL,
                aplicado_em          = NULL
        """, (
            token_final, sender_id, failed_reason_id,
            corpo.get("motivo_texto"), corpo.get("pergunta_texto"),
            corpo.get("prazo_texto"), corpo.get("aviso_dia_fixo_texto"),
            json.dumps(pedidos, ensure_ascii=False), _agora_iso(),
        ))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"token": token_final})


@app.route("/api/sync/respostas", methods=["GET"])
@_exige_segredo_sync
def api_sync_respostas():
    """Devolve pra máquina local os grupos já respondidos e ainda não
    aplicados. Pull completo a cada chamada -- volume baixo, não
    precisa de cursor incremental (mesmo padrão do app de motoristas)."""
    conn = _conectar()
    try:
        linhas = conn.execute("""
            SELECT token, sender_id, failed_reason_id, acao, data_pedida, respondido_em
            FROM respostas
            WHERE status = 'RESPONDIDO' AND aplicado_em IS NULL
        """).fetchall()
    finally:
        conn.close()
    return jsonify({"respostas": [dict(linha) for linha in linhas]})


@app.route("/api/sync/ack", methods=["POST"])
@_exige_segredo_sync
def api_sync_ack():
    """Marca os grupos como aplicados (a máquina local já executou a
    decisão no VUUPT) -- libera um ciclo novo pro mesmo grupo na
    próxima notificação."""
    corpo = request.get_json(silent=True) or {}
    tokens = corpo.get("tokens") or []
    if not tokens:
        return jsonify({"confirmados": 0})
    conn = _conectar()
    try:
        marcadores = ",".join("?" * len(tokens))
        conn.execute(
            f"UPDATE respostas SET aplicado_em = ? WHERE token IN ({marcadores})",
            (_agora_iso(), *tokens),
        )
        conn.commit()
    finally:
        conn.close()
    return jsonify({"confirmados": len(tokens)})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 8091)), debug=False)
