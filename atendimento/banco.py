# -*- coding: utf-8 -*-
"""
atendimento/banco.py

Esquema e conexão da central de atendimento (WhatsApp via Evolution API,
ver atendimento/app.py e integracao_evolution.py). Banco próprio,
dados/atendimento.db -- separado do dados/dados.db compartilhado porque
esse subsistema escreve a cada mensagem trocada (volume bem maior e
orientado a webhook, não a lote/cron como o resto do projeto).

Mesmos padrões de nucleo/banco.py:
  - CREATE TABLE IF NOT EXISTS idempotente
  - migração aditiva por PRAGMA table_info (nunca DROP, nunca renomeia)
  - timestamps TEXT em datetime('now','localtime')
"""
import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

_RAIZ = Path(__file__).parent.parent
DB_PATH = _RAIZ / "dados" / "atendimento.db"

# times possíveis de uma conversa (roteamento manual, sem bot de triagem ainda)
TIMES = ("DESTINATARIOS", "MOTORISTAS", "COMERCIAL_FINANCEIRO")

# Limiares das métricas em tempo real (barra do topo, ver app.py::api_metricas
# e base.html) -- fixos por enquanto, virar configurável em config.yaml se o
# Hugo pedir depois de ver como se comporta na prática.
LIMITE_SLA_FILA_MIN = 15   # conversa não atribuída esperando há mais que isso = SLA estourado
LIMITE_PARADO_MIN = 20     # conversa atribuída sem resposta ao cliente há mais que isso = parada

# Fila de reenvio de mensagens OUT que falharam ao enviar (canal Evolution API
# instável -- ver integracao_evolution.py e atendimento/reenviar_pendentes.py).
# Backoff exponencial com teto: 30s, 1min, 2min, 4min, 8min, 15min, 15min, 15min --
# depois de MAX_TENTATIVAS_ENVIO desiste e marca FALHOU (dispara e-mail de alerta).
MAX_TENTATIVAS_ENVIO = 8

_DDL = """
CREATE TABLE IF NOT EXISTS usuarios (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    login           TEXT UNIQUE NOT NULL,
    nome            TEXT NOT NULL,
    senha_hash      TEXT NOT NULL,
    papel           TEXT NOT NULL DEFAULT 'atendente',      -- admin | atendente
    ativo           INTEGER NOT NULL DEFAULT 1,
    criado_em       TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    ultimo_login_em TEXT
);

CREATE TABLE IF NOT EXISTS contatos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    telefone_e164   TEXT UNIQUE NOT NULL,
    nome            TEXT,
    criado_em       TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS conversas (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    protocolo               TEXT UNIQUE,                    -- AT-YYYYMMDD-00042, preenchido logo após o INSERT
    contato_id              INTEGER NOT NULL REFERENCES contatos(id),
    time                    TEXT,                            -- ver TIMES acima; NULL = não classificada ainda
    status                  TEXT NOT NULL DEFAULT 'ABERTA',  -- ABERTA | RESOLVIDA
    atendente_id            INTEGER REFERENCES usuarios(id), -- NULL = ainda não assumida
    assumida_em             TEXT,
    aberta_em               TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    encerrada_em            TEXT,
    ultima_mensagem_em      TEXT,
    ultima_mensagem_preview TEXT
);
CREATE INDEX IF NOT EXISTS idx_conversas_status ON conversas(status, atendente_id);
CREATE INDEX IF NOT EXISTS idx_conversas_contato ON conversas(contato_id);

CREATE TABLE IF NOT EXISTS mensagens (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    conversa_id             INTEGER NOT NULL REFERENCES conversas(id) ON DELETE CASCADE,
    direcao                 TEXT NOT NULL,                   -- IN | OUT
    atendente_id            INTEGER REFERENCES usuarios(id), -- quem respondeu pela UI; NULL se IN ou se veio do próprio celular vinculado
    corpo                   TEXT,
    evolution_message_id    TEXT UNIQUE,                     -- data.key.id do webhook -- dedupe de eco/retry
    criado_em               TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_mensagens_conversa ON mensagens(conversa_id, criado_em);

CREATE TABLE IF NOT EXISTS notas_internas (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    conversa_id             INTEGER NOT NULL REFERENCES conversas(id) ON DELETE CASCADE,
    atendente_id            INTEGER REFERENCES usuarios(id),
    corpo                   TEXT NOT NULL,
    criado_em               TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_notas_conversa ON notas_internas(conversa_id, criado_em);

CREATE TABLE IF NOT EXISTS respostas_rapidas (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    titulo                  TEXT NOT NULL,
    corpo                   TEXT NOT NULL,
    criado_em               TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- Linha única (id sempre 1) com o último estado conhecido da sessão do
-- WhatsApp -- base do alerta por e-mail em monitorar_saude_evolution.py,
-- que só avisa na transição (edge-triggered), não a cada execução do timer.
CREATE TABLE IF NOT EXISTS estado_evolution (
    id        INTEGER PRIMARY KEY CHECK (id = 1),
    conectado INTEGER NOT NULL DEFAULT 1,
    mudou_em  TEXT
);
"""


def conectar() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    garantir_esquema(conn)
    return conn


# Coluna nova de `conversas` (tabela já existia antes da barra de métricas) --
# migração aditiva, mesmo padrão de nucleo/banco.py.
_COLUNAS_CONVERSAS_NOVAS = [
    ("ultima_mensagem_direcao", "TEXT"),  # IN | OUT -- base da métrica "atendimentos parados"
    # Bot de triagem (ver app.py::webhook_evolution) -- motivo_contato é só
    # informativo (chip na UI), independente de `time` (fila do time humano).
    ("motivo_contato", "TEXT"),          # status_pedido | canhoto | cotacao | outro
    ("bot_aguardando_menu", "INTEGER NOT NULL DEFAULT 0"),
    ("bot_tentativas", "INTEGER NOT NULL DEFAULT 0"),
]

# Colunas novas de `mensagens` (tabela já existia antes da fila de reenvio) --
# DEFAULT 'ENVIADA' garante que mensagens antigas (e as IN, que nunca passam por
# aqui) não entrem na fila de retry por engano.
_COLUNAS_MENSAGENS_NOVAS = [
    ("status", "TEXT NOT NULL DEFAULT 'ENVIADA'"),  # ENVIADA | PENDENTE | FALHOU
    ("tentativas", "INTEGER NOT NULL DEFAULT 0"),
    ("proxima_tentativa_em", "TEXT"),
    # Mídia recebida baixada de verdade (ver app.py::_baixar_e_salvar_midia) --
    # todas NULL quando a mensagem não é mídia ou o download falhou (cai de
    # volta pro rótulo de texto em `corpo`, comportamento de antes).
    ("midia_categoria", "TEXT"),        # imagem | video | audio | documento | figurinha
    ("midia_mime", "TEXT"),
    ("midia_caminho", "TEXT"),          # relativo à raiz do projeto
    ("midia_nome_original", "TEXT"),
]


def _migrar_colunas(conn: sqlite3.Connection, tabela: str, colunas: list[tuple[str, str]]):
    existentes = {row[1] for row in conn.execute(f"PRAGMA table_info({tabela})")}
    for nome, tipo in colunas:
        if nome not in existentes:
            conn.execute(f"ALTER TABLE {tabela} ADD COLUMN {nome} {tipo}")
            logger.info(f"atendimento: coluna {tabela}.{nome} adicionada.")


def garantir_esquema(conn: sqlite3.Connection):
    conn.executescript(_DDL)
    _migrar_colunas(conn, "conversas", _COLUNAS_CONVERSAS_NOVAS)
    _migrar_colunas(conn, "mensagens", _COLUNAS_MENSAGENS_NOVAS)
    conn.execute("INSERT OR IGNORE INTO estado_evolution (id, conectado) VALUES (1, 1)")
    conn.commit()


def buscar_ou_criar_contato(conn: sqlite3.Connection, telefone_e164: str, nome: str | None = None) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM contatos WHERE telefone_e164 = ?", (telefone_e164,)).fetchone()
    if row:
        if nome and not row["nome"]:
            conn.execute("UPDATE contatos SET nome = ? WHERE id = ?", (nome, row["id"]))
            conn.commit()
        return conn.execute("SELECT * FROM contatos WHERE id = ?", (row["id"],)).fetchone()
    cur = conn.execute(
        "INSERT INTO contatos (telefone_e164, nome) VALUES (?, ?)", (telefone_e164, nome),
    )
    conn.commit()
    return conn.execute("SELECT * FROM contatos WHERE id = ?", (cur.lastrowid,)).fetchone()


def conversa_aberta_do_contato(conn: sqlite3.Connection, contato_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM conversas WHERE contato_id = ? AND status = 'ABERTA' ORDER BY id DESC LIMIT 1",
        (contato_id,),
    ).fetchone()


def abrir_conversa(conn: sqlite3.Connection, contato_id: int) -> sqlite3.Row:
    """Abre uma conversa nova e já grava o protocolo (AT-AAAAMMDD-<id com
    5 dígitos>) -- precisa do id gerado pelo INSERT, por isso o UPDATE
    logo em seguida, mesmo padrão de código auto-numerado usado alhures
    no projeto."""
    cur = conn.execute("INSERT INTO conversas (contato_id) VALUES (?)", (contato_id,))
    conversa_id = cur.lastrowid
    protocolo = f"AT-{datetime_agora_str()[:10].replace('-', '')}-{conversa_id:05d}"
    conn.execute("UPDATE conversas SET protocolo = ? WHERE id = ?", (protocolo, conversa_id))
    conn.commit()
    return conn.execute("SELECT * FROM conversas WHERE id = ?", (conversa_id,)).fetchone()


def datetime_agora_str() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def assumir_conversa(conn: sqlite3.Connection, conversa_id: int, atendente_id: int) -> bool:
    """UPDATE atômico: só assume se ainda não tinha atendente. SQLite já
    serializa escritores (busy_timeout cobre a espera), então rowcount==1
    decide quem ganhou a corrida sem precisar de lock explícito."""
    cur = conn.execute(
        "UPDATE conversas SET atendente_id = ?, assumida_em = datetime('now','localtime') "
        "WHERE id = ? AND atendente_id IS NULL",
        (atendente_id, conversa_id),
    )
    conn.commit()
    return cur.rowcount == 1


# ── Bot de triagem (ver app.py::webhook_evolution) ──────────────────────────

_BOT_TRIAGEM_LOGIN = "_bot_triagem"


def marcar_bot_aguardando_menu(conn: sqlite3.Connection, conversa_id: int, aguardando: bool) -> None:
    conn.execute(
        "UPDATE conversas SET bot_aguardando_menu = ? WHERE id = ?",
        (int(aguardando), conversa_id),
    )
    conn.commit()


def classificar_conversa_pelo_bot(conn: sqlite3.Connection, conversa_id: int,
                                   time: str | None, motivo: str) -> None:
    """Cliente respondeu uma opção válida do menu -- grava o time (fila
    humana) e o motivo (informativo) e encerra a participação do bot nessa
    conversa (bot_aguardando_menu volta a 0)."""
    conn.execute(
        "UPDATE conversas SET time = ?, motivo_contato = ?, bot_aguardando_menu = 0 WHERE id = ?",
        (time, motivo, conversa_id),
    )
    conn.commit()


def incrementar_tentativas_bot(conn: sqlite3.Connection, conversa_id: int) -> int:
    """Resposta não reconhecida como opção do menu -- incrementa o contador
    e devolve o total, pra quem chama decidir se já esgotou MAX_TENTATIVAS_MENU
    (ver app.py) e deve desistir."""
    conn.execute("UPDATE conversas SET bot_tentativas = bot_tentativas + 1 WHERE id = ?", (conversa_id,))
    conn.commit()
    return conn.execute(
        "SELECT bot_tentativas FROM conversas WHERE id = ?", (conversa_id,),
    ).fetchone()["bot_tentativas"]


def desistir_bot(conn: sqlite3.Connection, conversa_id: int) -> None:
    """Esgotou as tentativas de entender a resposta -- desiste, cai na fila
    geral (sem `time`) pra um atendente resolver na mão."""
    conn.execute(
        "UPDATE conversas SET bot_aguardando_menu = 0, motivo_contato = 'outro' WHERE id = ?",
        (conversa_id,),
    )
    conn.commit()


def usuario_bot_id(conn: sqlite3.Connection) -> int:
    """Id do usuário reservado que representa o bot de triagem nas mensagens
    OUT que ele manda (mensagens.atendente_id) -- só existe pra popular o
    nome do remetente na UI via o LEFT JOIN que já existe em
    _SELECT_CONVERSAS/api_mensagens; nunca loga de verdade (ativo=0, senha
    aleatória). Cria a linha na primeira chamada (idempotente)."""
    row = conn.execute("SELECT id FROM usuarios WHERE login = ?", (_BOT_TRIAGEM_LOGIN,)).fetchone()
    if row:
        return row["id"]
    import secrets
    from werkzeug.security import generate_password_hash
    cur = conn.execute(
        "INSERT INTO usuarios (login, nome, senha_hash, papel, ativo) VALUES (?, ?, ?, 'bot', 0)",
        (_BOT_TRIAGEM_LOGIN, "Bot de triagem", generate_password_hash(secrets.token_hex(32))),
    )
    conn.commit()
    return cur.lastrowid


def registrar_mensagem(conn: sqlite3.Connection, conversa_id: int, direcao: str, corpo: str | None,
                        atendente_id: int | None = None, evolution_message_id: str | None = None,
                        status: str = "ENVIADA", midia: dict | None = None) -> int | None:
    """Insere a mensagem e atualiza o resumo da conversa (preview + hora).
    Idempotente por evolution_message_id: se o id já existe (eco/retry do
    webhook), não duplica -- retorna None nesse caso.

    status="PENDENTE" é usado quando o envio pela Evolution API falhou na
    hora (ver api_responder em app.py): a mensagem já aparece na thread em
    vez de sumir, e reenviar_pendentes.py assume dali.

    midia (opcional): dict com categoria/mime/caminho/nome_original quando o
    webhook baixou a mídia recebida com sucesso (ver
    app.py::_baixar_e_salvar_midia). None (default) grava tudo NULL."""
    if evolution_message_id:
        ja = conn.execute(
            "SELECT id FROM mensagens WHERE evolution_message_id = ?", (evolution_message_id,),
        ).fetchone()
        if ja:
            return None
    midia = midia or {}
    cur = conn.execute(
        "INSERT INTO mensagens (conversa_id, direcao, atendente_id, corpo, evolution_message_id, status, "
        "midia_categoria, midia_mime, midia_caminho, midia_nome_original) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (conversa_id, direcao, atendente_id, corpo, evolution_message_id, status,
         midia.get("categoria"), midia.get("mime"), midia.get("caminho"), midia.get("nome_original")),
    )
    preview = (corpo or "")[:120]
    conn.execute(
        "UPDATE conversas SET ultima_mensagem_em = datetime('now','localtime'), "
        "ultima_mensagem_preview = ?, ultima_mensagem_direcao = ? WHERE id = ?",
        (preview, direcao, conversa_id),
    )
    conn.commit()
    return cur.lastrowid


def _proximo_backoff_segundos(tentativas: int) -> int:
    """Backoff exponencial com teto de 15min -- ver MAX_TENTATIVAS_ENVIO."""
    return min(30 * 2 ** (tentativas - 1), 900)


_SELECT_MENSAGENS_PENDENTES = """
    SELECT m.id, m.corpo, m.tentativas, c.id AS conversa_id, c.protocolo,
           ct.telefone_e164
    FROM mensagens m
    JOIN conversas c ON c.id = m.conversa_id
    JOIN contatos ct ON ct.id = c.contato_id
    WHERE m.status = 'PENDENTE'
      AND (m.proxima_tentativa_em IS NULL OR m.proxima_tentativa_em <= datetime('now','localtime'))
    ORDER BY m.id ASC
"""


def mensagens_pendentes_para_retry(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Mensagens OUT que falharam ao enviar e já podem tentar de novo --
    usado por reenviar_pendentes.py (timer a cada 1min)."""
    return conn.execute(_SELECT_MENSAGENS_PENDENTES).fetchall()


def marcar_envio_sucesso(conn: sqlite3.Connection, mensagem_id: int, evolution_message_id: str | None) -> None:
    conn.execute(
        "UPDATE mensagens SET status = 'ENVIADA', evolution_message_id = ?, proxima_tentativa_em = NULL "
        "WHERE id = ?",
        (evolution_message_id, mensagem_id),
    )
    conn.commit()


def marcar_envio_falha_ou_esgotado(conn: sqlite3.Connection, mensagem_id: int, tentativas_atuais: int) -> bool:
    """Incrementa a tentativa; se esgotou MAX_TENTATIVAS_ENVIO marca FALHOU
    (definitivo), senão agenda a próxima com backoff. Retorna True quando
    esgotou -- reenviar_pendentes.py usa isso pra decidir se manda o e-mail
    de alerta (uma vez só, não a cada tentativa)."""
    tentativas = tentativas_atuais + 1
    if tentativas >= MAX_TENTATIVAS_ENVIO:
        conn.execute(
            "UPDATE mensagens SET status = 'FALHOU', tentativas = ?, proxima_tentativa_em = NULL WHERE id = ?",
            (tentativas, mensagem_id),
        )
        conn.commit()
        return True
    conn.execute(
        "UPDATE mensagens SET tentativas = ?, "
        "proxima_tentativa_em = datetime('now','localtime', ?) WHERE id = ?",
        (tentativas, f"+{_proximo_backoff_segundos(tentativas)} seconds", mensagem_id),
    )
    conn.commit()
    return False


def atualizar_estado_evolution(conn: sqlite3.Connection, conectado: bool) -> bool:
    """Atualiza o estado conhecido da sessão do WhatsApp; retorna True só
    quando o valor mudou desde a última checagem (edge-triggered) -- usado
    por monitorar_saude_evolution.py pra só mandar e-mail na transição."""
    atual = conn.execute("SELECT conectado FROM estado_evolution WHERE id = 1").fetchone()
    mudou = atual is None or bool(atual["conectado"]) != conectado
    if mudou:
        conn.execute(
            "UPDATE estado_evolution SET conectado = ?, mudou_em = datetime('now','localtime') WHERE id = 1",
            (int(conectado),),
        )
        conn.commit()
    return mudou


def estado_evolution_atual(conn: sqlite3.Connection) -> dict:
    """Último estado conhecido pelo monitor (não faz nenhuma chamada de rede
    -- é só o que monitorar_saude_evolution.py gravou na última execução do
    timer). Usado na tela /admin/whatsapp ao lado da checagem ao vivo."""
    row = conn.execute("SELECT conectado, mudou_em FROM estado_evolution WHERE id = 1").fetchone()
    if row is None:
        return {"conectado": None, "mudou_em": None}
    return {"conectado": bool(row["conectado"]), "mudou_em": row["mudou_em"]}


def contagem_fila_reenvio(conn: sqlite3.Connection) -> dict:
    """Quantas mensagens estão esperando reenvio (PENDENTE, ver
    reenviar_pendentes.py) e quantas esgotaram as tentativas e nunca foram
    entregues (FALHOU) -- total histórico, não só as de hoje."""
    pendentes = conn.execute("SELECT COUNT(*) AS n FROM mensagens WHERE status = 'PENDENTE'").fetchone()["n"]
    falhou = conn.execute("SELECT COUNT(*) AS n FROM mensagens WHERE status = 'FALHOU'").fetchone()["n"]
    return {"pendentes": pendentes, "falhou": falhou}


def calcular_metricas(conn: sqlite3.Connection) -> dict:
    """Métricas da barra do topo (ver app.py::api_metricas e base.html) --
    pensadas pro atendente acompanhar em tempo real, não só o admin:
    'fila' e 'fila_sla_estourado' medem a porta de entrada (conversa sem
    ninguém ainda), 'parados' mede acompanhamento (conversa já assumida
    mas o cliente escreveu por último e ninguém respondeu ainda),
    'resolvidas_hoje' é a métrica "de produtividade"."""
    fila = conn.execute(
        "SELECT COUNT(*) AS n FROM conversas WHERE status = 'ABERTA' AND atendente_id IS NULL",
    ).fetchone()["n"]
    fila_sla_estourado = conn.execute(
        "SELECT COUNT(*) AS n FROM conversas WHERE status = 'ABERTA' AND atendente_id IS NULL "
        "  AND aberta_em <= datetime('now', 'localtime', ?)",
        (f"-{LIMITE_SLA_FILA_MIN} minutes",),
    ).fetchone()["n"]
    parados = conn.execute(
        "SELECT COUNT(*) AS n FROM conversas WHERE status = 'ABERTA' AND atendente_id IS NOT NULL "
        "  AND ultima_mensagem_direcao = 'IN' AND ultima_mensagem_em <= datetime('now', 'localtime', ?)",
        (f"-{LIMITE_PARADO_MIN} minutes",),
    ).fetchone()["n"]
    resolvidas_hoje = conn.execute(
        "SELECT COUNT(*) AS n FROM conversas WHERE status = 'RESOLVIDA' "
        "  AND encerrada_em >= date('now', 'localtime')",
    ).fetchone()["n"]
    return {
        "fila": fila,
        "fila_sla_estourado": fila_sla_estourado,
        "parados": parados,
        "resolvidas_hoje": resolvidas_hoje,
    }
