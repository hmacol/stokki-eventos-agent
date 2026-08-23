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
import json
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
# Marketplace de rotas (Hugo, 22/08): salt PRÓPRIO -- token aqui é por
# motorista+dia ({"agent_id", "data_rota"}), não por rota fixa como o de
# cima, então nunca pode ser confundido/reaproveitado entre os dois usos.
_serializer_oferta = URLSafeTimedSerializer(_TOKEN_SECRET, salt="escolha-rota")

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
    # Marketplace de rotas (Hugo, 22/08): oferta = rascunho publicado
    # pro grupo elegível escolher. rascunho_id é a chave (não token --
    # o token aqui é por motorista+dia, não por oferta).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ofertas (
            rascunho_id    INTEGER PRIMARY KEY,
            data_rota      TEXT NOT NULL,
            resumo_json    TEXT NOT NULL,
            status         TEXT NOT NULL DEFAULT 'ABERTA',
            escolhido_por  INTEGER,
            escolhido_em   TEXT,
            recebido_em    TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ofertas_elegibilidade (
            rascunho_id        INTEGER NOT NULL,
            agent_id           INTEGER NOT NULL,
            telefone_ultimos4  TEXT,
            cpf                TEXT,
            PRIMARY KEY (rascunho_id, agent_id)
        )
    """)
    # Migração pra banco criado antes de 22/08 (identificação por CPF
    # na página compartilhada /escolher, sem token pessoal -- ver rota
    # mais abaixo).
    colunas_elegibilidade = {row["name"] for row in conn.execute("PRAGMA table_info(ofertas_elegibilidade)")}
    if "cpf" not in colunas_elegibilidade:
        conn.execute("ALTER TABLE ofertas_elegibilidade ADD COLUMN cpf TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ofertas_elegibilidade_cpf ON ofertas_elegibilidade(cpf)")
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


def _validar_token(token: str, serializer: URLSafeTimedSerializer = None, max_age: int = _MAX_AGE_SEGUNDOS) -> dict:
    """Retorna {'estado': 'ok', 'payload': {...}} ou
    {'estado': 'expirado'|'invalido'}. Não consulta o banco -- só
    confere a assinatura/validade do link em si. `serializer` default
    (_serializer, salt 'confirmacao-rota') mantém o comportamento de
    sempre pra /r/<token>; /escolher/<token> passa _serializer_oferta."""
    try:
        payload = (serializer or _serializer).loads(token, max_age=max_age)
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


def _tentar_escolher(conn, agent_id: int, rascunho_id: int, ultimos4_informado: str,
                      verificar_telefone: bool = True) -> str | None:
    """Claim de uma oferta pelo agent_id JÁ CONFIRMADO (do token
    assinado ou do CPF revalidado no banco -- nunca de um campo de
    formulário cru). Atômico -- UPDATE ... WHERE status='ABERTA' --
    garante que só o primeiro clique vence quando dois motoristas
    tentam a mesma rota quase ao mesmo tempo. Retorna a mensagem de
    erro, ou None se a escolha deu certo.

    `verificar_telefone=False` (página compartilhada por CPF) pula a
    checagem de últimos 4 dígitos -- o CPF completo já é o fator forte
    ali, exigir também o telefone (dado raro, ver
    regras/preferencias_motoristas.py) só adicionaria fricção sem
    reforçar segurança nenhuma."""
    elegibilidade = conn.execute(
        "SELECT telefone_ultimos4 FROM ofertas_elegibilidade WHERE rascunho_id = ? AND agent_id = ?",
        (rascunho_id, agent_id),
    ).fetchone()
    if elegibilidade is None:
        return "Essa rota não está mais disponível pra você."

    exige_checagem = verificar_telefone and bool(elegibilidade["telefone_ultimos4"])
    if exige_checagem and ultimos4_informado != elegibilidade["telefone_ultimos4"]:
        return "Os 4 últimos dígitos não conferem com o telefone cadastrado."

    cur = conn.execute("""
        UPDATE ofertas SET status = 'ESCOLHIDA', escolhido_por = ?, escolhido_em = ?
        WHERE rascunho_id = ? AND status = 'ABERTA'
    """, (agent_id, _agora_iso(), rascunho_id))
    conn.commit()
    if cur.rowcount == 0:
        return "Essa rota não está mais disponível -- outro motorista já escolheu (ou ela foi retirada)."
    return None


def _listar_ofertas_abertas(conn, agent_id: int, data_rota: str | None = None) -> list[dict]:
    """Ofertas ABERTA elegíveis pra esse agent_id -- `data_rota` filtra
    pro dia do link pessoal (/escolher/<token>); None mostra QUALQUER
    oferta aberta pra ele, usado pela página compartilhada por CPF
    (/escolher), que não carrega data nenhuma."""
    sql = """
        SELECT o.rascunho_id, o.resumo_json, oe.telefone_ultimos4
        FROM ofertas o
        JOIN ofertas_elegibilidade oe ON oe.rascunho_id = o.rascunho_id
        WHERE oe.agent_id = ? AND o.status = 'ABERTA'
    """
    parametros = [agent_id]
    if data_rota is not None:
        sql += " AND o.data_rota = ?"
        parametros.append(data_rota)
    sql += " ORDER BY o.recebido_em"
    linhas = conn.execute(sql, parametros).fetchall()
    return [
        {"rascunho_id": linha["rascunho_id"], "resumo": json.loads(linha["resumo_json"]),
         "exige_telefone": bool(linha["telefone_ultimos4"])}
        for linha in linhas
    ]


@app.route("/escolher/<token>", methods=["GET", "POST"])
def escolher_rota(token):
    """Marketplace de rotas (Hugo, 22/08): lista as rotas ABERTA
    elegíveis pro motorista do token (agent_id+data_rota, não uma rota
    fixa) e deixa ele escolher UMA. Link pessoal -- só chega em quem
    tem telefone/e-mail cadastrado (ver /escolher sem token, a versão
    compartilhada por CPF, pra quem não tem)."""
    validacao = _validar_token(token, serializer=_serializer_oferta)
    if validacao["estado"] == "invalido":
        return render_template("escolher_rota.html", estado="invalido"), 404
    if validacao["estado"] == "expirado":
        return render_template("escolher_rota.html", estado="expirado"), 410

    agent_id = validacao["payload"]["agent_id"]
    data_rota = validacao["payload"]["data_rota"]

    conn = _conectar()
    try:
        mensagem = erro = None
        if request.method == "POST":
            rascunho_id = request.form.get("rascunho_id", type=int)
            ultimos4_informado = re.sub(r"\D", "", request.form.get("ultimos4", ""))
            erro = _tentar_escolher(conn, agent_id, rascunho_id, ultimos4_informado)
            if erro is None:
                mensagem = "Rota escolhida! Aguarde o contato da Freshlog com os detalhes."

        ofertas = _listar_ofertas_abertas(conn, agent_id, data_rota)
        return render_template("escolher_rota.html", estado="formulario",
                                ofertas=ofertas, mensagem=mensagem, erro=erro)
    finally:
        conn.close()


@app.route("/escolher", methods=["GET", "POST"])
def escolher_rota_por_cpf():
    """Marketplace de rotas -- página COMPARTILHADA (Hugo, 22/08):
    mesma escolha de /escolher/<token>, mas sem link pessoal nenhum por
    trás. Pedido do Hugo: dar a mesma oportunidade de escolher pra todo
    motorista elegível, não só quem tem telefone/e-mail cadastrado pro
    aviso individual (hoje é minoria) -- um único link, postado uma vez
    só (grupo/canal que já chega a todo mundo), nunca expira.

    Sem token assinado por trás, o CPF completo (11 dígitos, não só os
    4 últimos como no link pessoal) É o fator de identificação -- por
    isso exige o CPF de novo em CADA submit (inclusive no clique de
    escolher), nunca confia num agent_id vindo direto do formulário.
    Resolve contra ofertas_elegibilidade JÁ FILTRADO por oferta ABERTA
    -- CPF sem nenhuma oferta aberta no momento dá a MESMA mensagem de
    'nada disponível' de CPF desconhecido, de propósito (não confirma
    nem nega se aquele CPF é de motorista cadastrado)."""
    conn = _conectar()
    try:
        cpf_informado = re.sub(r"\D", "", request.form.get("cpf", ""))
        mensagem = erro = None
        ofertas = []
        identificado = False

        if request.method == "POST" and cpf_informado:
            agent_ids = [row["agent_id"] for row in conn.execute(
                "SELECT DISTINCT oe.agent_id FROM ofertas_elegibilidade oe "
                "JOIN ofertas o ON o.rascunho_id = oe.rascunho_id "
                "WHERE oe.cpf = ? AND o.status = 'ABERTA'",
                (cpf_informado,),
            ).fetchall()]

            if len(agent_ids) > 1:
                # CPF duplicado entre motoristas diferentes na planilha
                # de origem -- erro de cadastro, não dá pra saber qual é
                # qual com segurança. Loga pro Hugo investigar, não
                # escolhe um dos dois arbitrariamente.
                app.logger.warning(f"CPF ambíguo (mais de 1 agent_id elegível): terminado em ...{cpf_informado[-4:]}")
            elif len(agent_ids) == 1:
                identificado = True
                agent_id = agent_ids[0]

                if request.form.get("rascunho_id"):
                    rascunho_id = request.form.get("rascunho_id", type=int)
                    erro = _tentar_escolher(conn, agent_id, rascunho_id, "", verificar_telefone=False)
                    if erro is None:
                        mensagem = "Rota escolhida! Aguarde o contato da Freshlog com os detalhes."

                ofertas = _listar_ofertas_abertas(conn, agent_id)
            if not identificado:
                erro = "Nenhuma rota disponível pra esse CPF no momento."

        return render_template("escolher_cpf.html", identificado=identificado,
                                ofertas=ofertas, mensagem=mensagem, erro=erro,
                                cpf_informado=cpf_informado if identificado else "")
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


@app.route("/api/sync/ofertas/upsert", methods=["POST"])
@_exige_segredo_sync
def api_sync_ofertas_upsert():
    """Recebe da máquina local as ofertas do marketplace publicadas (ou
    despublicadas) desde o último push. Nunca sobrescreve uma oferta já
    ESCOLHIDA aqui (guarda WHERE ofertas.status != 'ESCOLHIDA', mesmo
    padrão de api_sync_upsert) -- se um motorista ganhou a corrida antes
    do Hugo clicar 'Despublicar', a escolha dele prevalece. IMPORTANTE:
    a guarda não pode ser WHERE status = 'ABERTA' -- bloquearia também
    o caso normal de republicar depois de despublicar (CANCELADA ->
    ABERTA), já que o status ATUAL na VPS não é 'ABERTA' nesse momento
    (achado 22/08, revisão do Hugo)."""
    corpo = request.get_json(silent=True) or {}
    itens = corpo.get("ofertas") or []
    conn = _conectar()
    try:
        agora = _agora_iso()
        for item in itens:
            if not all(campo in item for campo in ("rascunho_id", "data_alvo", "resumo_json", "status")):
                continue
            conn.execute("""
                INSERT INTO ofertas (rascunho_id, data_rota, resumo_json, status, recebido_em)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(rascunho_id) DO UPDATE SET
                    data_rota   = excluded.data_rota,
                    resumo_json = excluded.resumo_json,
                    status      = excluded.status,
                    recebido_em = excluded.recebido_em
                WHERE ofertas.status != 'ESCOLHIDA'
            """, (item["rascunho_id"], item["data_alvo"], item["resumo_json"], item["status"], agora))

            conn.execute("DELETE FROM ofertas_elegibilidade WHERE rascunho_id = ?", (item["rascunho_id"],))
            for elegivel in json.loads(item.get("agent_ids_elegiveis") or "[]"):
                conn.execute("""
                    INSERT OR REPLACE INTO ofertas_elegibilidade (rascunho_id, agent_id, telefone_ultimos4, cpf)
                    VALUES (?, ?, ?, ?)
                """, (item["rascunho_id"], elegivel["agent_id"], elegivel.get("telefone_ultimos4"), elegivel.get("cpf")))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"recebidas": len(itens)})


@app.route("/api/sync/ofertas/escolhidas", methods=["GET"])
@_exige_segredo_sync
def api_sync_ofertas_escolhidas():
    """Devolve pra máquina local as ofertas já ESCOLHIDA -- pull
    completo a cada chamada, mesmo padrão de api_sync_respostas."""
    conn = _conectar()
    try:
        linhas = conn.execute(
            "SELECT rascunho_id, escolhido_por, escolhido_em FROM ofertas WHERE status = 'ESCOLHIDA'"
        ).fetchall()
    finally:
        conn.close()
    return jsonify({"ofertas": [dict(linha) for linha in linhas]})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 8090)), debug=False)
