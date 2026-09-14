# -*- coding: utf-8 -*-
"""
nucleo/banco.py

Esquema e conexão do núcleo próprio (seção 3.2 do
DOC_EXECUCAO_CLAUDE_APP_MOTORISTAS.md). Tudo em dados/dados.db, o mesmo
banco operacional do resto do projeto -- prefixo `nucleo_` pra não colidir
com as tabelas `rotas`/`rotas_paradas` do desenho de junho/2026 (vazias,
ficam intocadas até o Hugo autorizar o DROP).

Padrões (mesmos de painel_agentes/rascunhos_rota.py):
  - CREATE TABLE IF NOT EXISTS idempotente
  - migração aditiva por PRAGMA table_info (nunca DROP, nunca renomeia)
  - timestamps TEXT em datetime('now','localtime')

A tabela `motoristas` (cpf + PIN com hash, criada em junho e vazia) é
reaproveitada como cadastro de login do app -- ganha colunas novas por
ALTER TABLE, sem mexer nas existentes.

journal_mode NÃO é alterado aqui (é uma mudança persistente no arquivo,
feita uma vez na VPS em 11/09/2026: `PRAGMA journal_mode=WAL` como
www-data -- ver seção 2.6 do doc). Só se aplica busy_timeout pra conviver
com os jobs. Scripts manuais na VPS: sempre `sudo -u www-data`, senão o
-wal/-shm nasce de root e os serviços ficam "readonly database".
"""
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_RAIZ = Path(__file__).parent.parent
DB_PATH = _RAIZ / "dados" / "dados.db"

# ── Status ─────────────────────────────────────────────────────────────────────
PROVEDOR_VUUPT = "VUUPT"
PROVEDOR_APP = "APP"

# nucleo_rotas.status
ROTA_PLANEJADA = "PLANEJADA"
ROTA_ACEITA = "ACEITA"
ROTA_EM_ROTA = "EM_ROTA"
ROTA_CONCLUIDA = "CONCLUIDA"
ROTA_CANCELADA = "CANCELADA"

# nucleo_paradas.situacao
PARADA_PENDENTE = "PENDENTE"
PARADA_EM_DESLOCAMENTO = "EM_DESLOCAMENTO"   # motorista a caminho (evento DESLOCAMENTO, app)
PARADA_EM_ROTA = "EM_ROTA"                   # motorista no local (CHEGADA no app / on_route na VUUPT)
PARADA_ENTREGUE = "ENTREGUE"
PARADA_PARCIAL = "PARCIAL"
PARADA_INSUCESSO = "INSUCESSO"
PARADA_CANCELADA = "CANCELADA"

# nucleo_pedidos.status
PEDIDO_ABERTO = "ABERTO"
PEDIDO_EM_ROTA = "EM_ROTA"
PEDIDO_ENTREGUE = "ENTREGUE"
PEDIDO_INSUCESSO = "INSUCESSO"
PEDIDO_CANCELADO = "CANCELADO"

# nucleo_pedagios.status (Hugo, 11/09: pedágio reembolsado à parte,
# informado com foto no app e aprovado no painel antes de entrar no extrato)
PEDAGIO_PENDENTE = "PENDENTE"
PEDAGIO_APROVADO = "APROVADO"
PEDAGIO_REJEITADO = "REJEITADO"
PEDAGIO_CANCELADO = "CANCELADO"   # o próprio motorista desistiu (só de PENDENTE); não entra no extrato

# nucleo_pedagios.tipo (Hugo, 14/09): além do pedágio, despesas adicionais
# da rota com o MESMO fluxo (foto + aprovação no painel + extrato).
# OUTROS exige descrição; ESTACIONAMENTO e DESCARGA exigem o pedido de
# referência (parada da própria rota).
DESPESA_PEDAGIO = "PEDAGIO"
DESPESA_ESTACIONAMENTO = "ESTACIONAMENTO"
DESPESA_DESCARGA = "DESCARGA"
DESPESA_OUTROS = "OUTROS"
TIPOS_DESPESA = {DESPESA_PEDAGIO: "Pedágio", DESPESA_ESTACIONAMENTO: "Estacionamento",
                 DESPESA_DESCARGA: "Descarga", DESPESA_OUTROS: "Outros"}
DESPESAS_COM_PEDIDO = {DESPESA_ESTACIONAMENTO, DESPESA_DESCARGA}

# nucleo_eventos.origem
ORIGEM_APP = "APP"
ORIGEM_VUUPT_SYNC = "VUUPT_SYNC"
ORIGEM_PAINEL = "PAINEL"
ORIGEM_PIPELINE = "PIPELINE"

_DDL = """
CREATE TABLE IF NOT EXISTS nucleo_pedidos (
    codigo                  TEXT PRIMARY KEY,               -- PS-XXXXX (code da VUUPT = id do Stokki)
    vuupt_service_id        INTEGER,
    titulo                  TEXT,
    tipo                    TEXT NOT NULL DEFAULT 'delivery',
    destinatario_nome       TEXT,
    destinatario_codigo     TEXT,                           -- CNPJ/CPF formatado (customer.code)
    destinatario_telefone   TEXT,
    endereco                TEXT,
    latitude                REAL,
    longitude               REAL,
    horario_inicio          TEXT,                           -- janela de atendimento do destinatário
    horario_fim             TEXT,
    remetente_nome          TEXT,
    remetente_codigo        TEXT,
    sender_id               INTEGER,
    caixas                  INTEGER,                        -- dimension_3 (ponderado, ver gerar_pdf_romaneios.py)
    agendamento_inicio      TEXT,
    agendamento_fim         TEXT,
    status                  TEXT NOT NULL DEFAULT 'ABERTO',
    origem                  TEXT,                           -- PIPELINE | VUUPT_SYNC | APP | PAINEL
    dados_json              TEXT,                           -- payload/serviço bruto (nunca perder campo)
    criado_em               TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    atualizado_em           TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_nucleo_pedidos_service ON nucleo_pedidos(vuupt_service_id);
CREATE INDEX IF NOT EXISTS idx_nucleo_pedidos_status ON nucleo_pedidos(status);

CREATE TABLE IF NOT EXISTS nucleo_rotas (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    data_rota               TEXT NOT NULL,                  -- YYYY-MM-DD
    nome                    TEXT,
    provedor                TEXT NOT NULL DEFAULT 'VUUPT',  -- VUUPT | APP (uma fonte de verdade por rota)
    vuupt_route_id          INTEGER,
    rascunho_id             INTEGER,                        -- rascunhos_rota.id de origem, quando houver
    agent_id                INTEGER,
    vehicle_id              INTEGER,
    motorista_nome          TEXT,
    motorista_cpf           TEXT,
    tipo_veiculo            TEXT,                           -- classificação da ROTA (veículo grande), não do motorista
    start_at                TEXT,
    start_location_base_id  INTEGER,
    end_location_base_id    INTEGER,
    km_estimado             REAL,
    km_real                 REAL,
    km_fonte                TEXT,                           -- ESTIMADO | GPS_APP | INFORMADO
    status                  TEXT NOT NULL DEFAULT 'PLANEJADA',
    status_provedor         TEXT,                           -- status bruto da VUUPT
    total_paradas           INTEGER NOT NULL DEFAULT 0,
    entregues               INTEGER NOT NULL DEFAULT 0,
    insucessos              INTEGER NOT NULL DEFAULT 0,
    iniciada_em             TEXT,
    concluida_em            TEXT,
    cancelada_em            TEXT,
    dados_json              TEXT,
    criado_em               TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    atualizado_em           TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_nucleo_rotas_vuupt
    ON nucleo_rotas(vuupt_route_id) WHERE vuupt_route_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_nucleo_rotas_data ON nucleo_rotas(data_rota, status);
CREATE INDEX IF NOT EXISTS idx_nucleo_rotas_agent ON nucleo_rotas(agent_id, data_rota);

CREATE TABLE IF NOT EXISTS nucleo_paradas (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    rota_id                 INTEGER NOT NULL REFERENCES nucleo_rotas(id) ON DELETE CASCADE,
    ordem                   INTEGER NOT NULL,
    service_id              INTEGER,
    codigo                  TEXT,
    titulo                  TEXT,
    destinatario_nome       TEXT,
    endereco                TEXT,
    latitude                REAL,
    longitude               REAL,
    sender_id               INTEGER,
    remetente_nome          TEXT,
    nivel_dificuldade       INTEGER,
    volume_caixas           INTEGER,
    janela_inicio           TEXT,
    janela_fim              TEXT,
    situacao                TEXT NOT NULL DEFAULT 'PENDENTE',
    status_provedor         TEXT,                           -- status bruto do serviço na VUUPT
    status_done_provedor    TEXT,                           -- status_done bruto (failed | ...)
    motivo_id               INTEGER,                        -- motivos_ocorrencia.id
    motivo_texto            TEXT,
    failed_reason_id        INTEGER,
    started_at              TEXT,                           -- saiu pra parada (DESLOCAMENTO)
    arrived_at              TEXT,                           -- chegou (CHEGADA)
    completed_at            TEXT,                           -- resultado (ENTREGUE/PARCIAL/INSUCESSO)
    tempo_deslocamento_s    INTEGER,                        -- arrived_at - started_at (Hugo, 26/08)
    tempo_no_local_s        INTEGER,                        -- completed_at - arrived_at: quanto demora pra receber
    customer_id             INTEGER,                        -- destinatário na VUUPT (clientes.customer_id)
    reagendado_para         TEXT,                           -- retorno marcado pelo motorista (REAGENDAR)
    tentativas              INTEGER NOT NULL DEFAULT 0,
    dados_json              TEXT,
    criado_em               TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    atualizado_em           TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_nucleo_paradas_rota_service
    ON nucleo_paradas(rota_id, service_id) WHERE service_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_nucleo_paradas_rota ON nucleo_paradas(rota_id, ordem);
CREATE INDEX IF NOT EXISTS idx_nucleo_paradas_codigo ON nucleo_paradas(codigo);

CREATE TABLE IF NOT EXISTS nucleo_eventos (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid                    TEXT UNIQUE,                    -- gerado no aparelho: fila offline reenvia sem duplicar
    rota_id                 INTEGER,
    parada_id               INTEGER,
    agent_id                INTEGER,
    tipo                    TEXT NOT NULL,                  -- ROTA_ENVIADA, ROTA_ACEITA, ROTA_EM_ROTA, CHEGADA, ENTREGUE, INSUCESSO, GPS, FOTO...
    origem                  TEXT NOT NULL,                  -- APP | VUUPT_SYNC | PAINEL | PIPELINE
    ocorrido_em             TEXT NOT NULL,                  -- relógio de quem gerou (aparelho / VUUPT)
    recebido_em             TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    latitude                REAL,
    longitude               REAL,
    precisao_m              REAL,
    dados_json              TEXT
);
CREATE INDEX IF NOT EXISTS idx_nucleo_eventos_rota ON nucleo_eventos(rota_id, ocorrido_em);
CREATE INDEX IF NOT EXISTS idx_nucleo_eventos_parada ON nucleo_eventos(parada_id, ocorrido_em);
CREATE INDEX IF NOT EXISTS idx_nucleo_eventos_tipo ON nucleo_eventos(tipo, ocorrido_em);

CREATE TABLE IF NOT EXISTS nucleo_comprovantes (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid                    TEXT UNIQUE,
    rota_id                 INTEGER,
    parada_id               INTEGER NOT NULL REFERENCES nucleo_paradas(id) ON DELETE CASCADE,
    tipo                    TEXT NOT NULL,                  -- CANHOTO | ASSINATURA | NF_DEVOLUCAO | PRODUTO | OCORRENCIA
    caminho_gcs             TEXT,                           -- gs://bucket/pedidos/{codigo}/...
    caminho_local           TEXT,
    sha256                  TEXT,
    tamanho_bytes           INTEGER,
    capturado_em            TEXT,
    enviado_em              TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    validado_em             TEXT,
    validado_por            TEXT,                           -- IA | HUMANO
    resultado_validacao     TEXT,                           -- APROVADO | REPROVADO + motivo em dados_json
    dados_json              TEXT
);
CREATE INDEX IF NOT EXISTS idx_nucleo_comprovantes_parada ON nucleo_comprovantes(parada_id);

CREATE TABLE IF NOT EXISTS nucleo_pedagios (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid                    TEXT UNIQUE,                    -- gerado no aparelho (fila offline reenvia sem duplicar)
    rota_id                 INTEGER NOT NULL REFERENCES nucleo_rotas(id) ON DELETE CASCADE,
    agent_id                INTEGER,
    valor_informado         REAL NOT NULL,                  -- digitado pelo motorista
    tipo                    TEXT NOT NULL DEFAULT 'PEDAGIO', -- PEDAGIO | ESTACIONAMENTO | DESCARGA | OUTROS (TIPOS_DESPESA)
    descricao               TEXT,                           -- obrigatória em OUTROS
    parada_id               INTEGER,                        -- pedido de referência (obrigatório em ESTACIONAMENTO/DESCARGA)
    caminho_local           TEXT,                           -- foto do comprovante (disco)
    caminho_gcs             TEXT,                           -- foto no bucket (best-effort)
    sha256                  TEXT,
    tamanho_bytes           INTEGER,
    capturado_em            TEXT,
    enviado_em              TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    status                  TEXT NOT NULL DEFAULT 'PENDENTE',   -- PENDENTE | APROVADO | REJEITADO | CANCELADO (pelo motorista)
    valor_aprovado          REAL,                           -- o que entra no extrato (pode diferir do informado)
    revisado_em             TEXT,
    revisado_por            TEXT,                           -- usuário do painel
    observacao_revisao      TEXT,
    dados_json              TEXT
);
CREATE INDEX IF NOT EXISTS idx_nucleo_pedagios_rota ON nucleo_pedagios(rota_id);
CREATE INDEX IF NOT EXISTS idx_nucleo_pedagios_status ON nucleo_pedagios(status, enviado_em);

CREATE TABLE IF NOT EXISTS tarifas_motorista (
    tipo_veiculo            TEXT PRIMARY KEY,               -- FIORINO | VAN_HR | VUC | TRES_QUARTOS | TRUCK
    nome                    TEXT,
    valor_base              REAL NOT NULL,
    km_franquia             REAL NOT NULL,
    valor_km_adicional      REAL NOT NULL,
    ativo                   INTEGER NOT NULL DEFAULT 1,
    vigencia_inicio         TEXT,
    observacoes             TEXT,
    atualizado_em           TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS motoristas (
    cpf                     TEXT PRIMARY KEY,               -- somente dígitos
    nome                    TEXT NOT NULL,
    pin_hash                TEXT NOT NULL,
    pin_salt                TEXT NOT NULL,
    telefone                TEXT,
    ativo                   INTEGER NOT NULL DEFAULT 1,
    criado_em               TEXT DEFAULT (datetime('now','localtime')),
    atualizado_em           TEXT DEFAULT (datetime('now','localtime')),
    ultimo_login_em         TEXT
);
"""

# Colunas novas de `motoristas` (tabela de junho) -- migração aditiva.
_COLUNAS_MOTORISTAS_NOVAS = [
    ("agent_id", "INTEGER"),          # AGENT_ID_VUUPT da planilha -- chave usada por todo o resto do projeto
    ("vehicle_id", "INTEGER"),
    ("tipo_veiculo", "TEXT"),         # veículo DO MOTORISTA (tarifa) -- FIORINO | VAN_HR | ...
    ("email", "TEXT"),
    ("push_token", "TEXT"),           # Expo push token (Fase B)
    ("tentativas_pin", "INTEGER NOT NULL DEFAULT 0"),
    ("bloqueado_ate", "TEXT"),
    ("perfil", "TEXT NOT NULL DEFAULT 'MOTORISTA'"),  # MOTORISTA | TESTE (usuário do Hugo no piloto)
]


# Colunas novas de nucleo_paradas (bancos criados antes de 26/08) -- migração aditiva.
_COLUNAS_PARADAS_NOVAS = [
    ("tempo_deslocamento_s", "INTEGER"),
    ("tempo_no_local_s", "INTEGER"),
    ("customer_id", "INTEGER"),   # destinatário na VUUPT (casa com clientes.customer_id) -- chave da média por destinatário
    ("reagendado_para", "TEXT"),  # motorista reagendou o retorno (evento REAGENDAR): "YYYY-MM-DD HH:MM" ou 'FIM' (depois das outras)
    ("tentativas", "INTEGER NOT NULL DEFAULT 0"),   # quantas vezes o motorista já esteve no local sem concluir
]


# Colunas novas de nucleo_rotas (11/09): km estimado rodoviário com o
# trecho de volta separado -- a volta só é paga com insucesso/parcial ou
# parada fora da Grande SP (regras/km_cobrado.py).
_COLUNAS_ROTAS_NOVAS = [
    ("km_volta_estimado", "REAL"),      # última parada -> base (mesma fonte do km_estimado)
    ("km_fonte_estimativa", "TEXT"),    # GOOGLE_ROUTES | HAVERSINE
]


def agora() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _migrar_colunas(conn: sqlite3.Connection, tabela: str, colunas: list[tuple[str, str]]):
    existentes = {row[1] for row in conn.execute(f"PRAGMA table_info({tabela})")}
    for nome, tipo in colunas:
        if nome not in existentes:
            conn.execute(f"ALTER TABLE {tabela} ADD COLUMN {nome} {tipo}")
            logger.info(f"nucleo: coluna {tabela}.{nome} adicionada.")


def garantir_esquema(conn: sqlite3.Connection):
    """Cria o que falta e aplica migrações aditivas. Seguro rodar a cada
    conexão (mesmo padrão de rascunhos_rota._conectar)."""
    conn.executescript(_DDL)
    _migrar_colunas(conn, "motoristas", _COLUNAS_MOTORISTAS_NOVAS)
    _migrar_colunas(conn, "nucleo_paradas", _COLUNAS_PARADAS_NOVAS)
    _migrar_colunas(conn, "nucleo_rotas", _COLUNAS_ROTAS_NOVAS)
    _migrar_colunas(conn, "nucleo_pedagios", [("tipo", "TEXT NOT NULL DEFAULT 'PEDAGIO'"), ("descricao", "TEXT"),
                                              ("parada_id", "INTEGER")])
    conn.execute("CREATE INDEX IF NOT EXISTS idx_motoristas_agent ON motoristas(agent_id)")
    # documentos_processados é de outro módulo (documentos_pedido/), mas o
    # núcleo lê a NF do pedido por codigo_pedido a cada parada montada
    # (validacao_fotos.nfs_do_pedido) -- sem índice isso vira varredura
    # da tabela inteira por parada. Só cria se a tabela já existir.
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='documentos_processados'").fetchone():
        conn.execute("CREATE INDEX IF NOT EXISTS idx_documentos_codigo_pedido ON documentos_processados(codigo_pedido)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_documentos_nf ON documentos_processados(numero_nf)")
    conn.commit()


def conectar(caminho: Path | str | None = None) -> sqlite3.Connection:
    """Conexão com row_factory=Row, foreign keys ligadas e busy_timeout de
    5s (os timers da VPS escrevem no mesmo arquivo). `caminho` só é usado
    em teste; produção sempre usa DB_PATH."""
    db = Path(caminho) if caminho else DB_PATH
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    garantir_esquema(conn)
    return conn