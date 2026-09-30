# -*- coding: utf-8 -*-
"""
regras/confirmacao_rotas.py

Tabela local de confirmação de rota pelo motorista -- pedido do Hugo,
16/08: página web (hospedada numa VPS pública, fora da rede local) onde
o motorista confirma ou recusa a rota já designada a ele.

Como a máquina local não tem exposição de entrada à internet, essa
tabela é o lado local de uma sincronização por push/pull com a VPS
(ver roteirizacao/avisar_motoristas_rotas.py::push_confirmacoes_vps e
roteirizacao/sincronizar_respostas_confirmacao.py). A fonte de verdade
da rota em si continua sendo o VUUPT -- essa tabela é só o estado da
confirmação, chaveada por (vuupt_route_id, agent_id), independente de
rascunhos_rota (que é só o rascunho pré-envio).

Mesmo padrão de regras/disponibilidade_motoristas.py: tabela própria
em dados/dados.db, CREATE TABLE IF NOT EXISTS idempotente, upsert via
ON CONFLICT DO UPDATE.
"""
import sqlite3
from datetime import date, datetime
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
_DB_PATH = _RAIZ / "dados" / "dados.db"

STATUS_AGUARDANDO = "AGUARDANDO"
STATUS_CONFIRMADO = "CONFIRMADO"
STATUS_RECUSADO = "RECUSADO"


def _conectar():
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS confirmacoes_rota (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            vuupt_route_id     INTEGER NOT NULL,
            agent_id           INTEGER NOT NULL,
            data_rota          TEXT NOT NULL,
            nome_motorista     TEXT,
            zona               TEXT,
            horario_previsto   TEXT,
            qtd_entregas       INTEGER,
            telefone_ultimos4  TEXT,
            token              TEXT NOT NULL,
            status             TEXT NOT NULL DEFAULT 'AGUARDANDO',
            motivo_recusa      TEXT,
            criado_em          TEXT NOT NULL,
            respondido_em      TEXT,
            sincronizado_em    TEXT,
            UNIQUE (vuupt_route_id, agent_id)
        )
    """)
    conn.commit()
    return conn


def criar_ou_atualizar(vuupt_route_id: int, agent_id: int, data_rota: date, token: str,
                       nome_motorista: str | None = None, zona: str | None = None,
                       horario_previsto: str | None = None, qtd_entregas: int | None = None,
                       telefone_ultimos4: str | None = None) -> int:
    """Registra uma confirmação pendente pra essa rota+motorista (ou
    atualiza o token/dados se já existir uma pendente da mesma rota --
    ex. reenvio do aviso). Não mexe em status/resposta já gravados --
    se o motorista já confirmou/recusou, um reenvio do aviso não reabre
    a pendência silenciosamente. Retorna o id da linha."""
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _conectar()
    try:
        conn.execute("""
            INSERT INTO confirmacoes_rota
                (vuupt_route_id, agent_id, data_rota, nome_motorista, zona,
                 horario_previsto, qtd_entregas, telefone_ultimos4, token, status, criado_em)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'AGUARDANDO', ?)
            ON CONFLICT(vuupt_route_id, agent_id) DO UPDATE SET
                token              = excluded.token,
                nome_motorista     = excluded.nome_motorista,
                zona               = excluded.zona,
                horario_previsto   = excluded.horario_previsto,
                qtd_entregas       = excluded.qtd_entregas,
                telefone_ultimos4  = excluded.telefone_ultimos4,
                sincronizado_em    = NULL
            WHERE confirmacoes_rota.status = 'AGUARDANDO'
        """, (vuupt_route_id, agent_id, data_rota.isoformat(), nome_motorista, zona,
              horario_previsto, qtd_entregas, telefone_ultimos4, token, agora))
        conn.commit()
        linha = conn.execute(
            "SELECT id FROM confirmacoes_rota WHERE vuupt_route_id = ? AND agent_id = ?",
            (vuupt_route_id, agent_id),
        ).fetchone()
        return linha["id"]
    finally:
        conn.close()


def marcar_sincronizadas(ids: list[int]) -> None:
    """Marca as linhas como já empurradas pra VPS -- evita reenviar no
    próximo push tudo que já foi entregue com sucesso."""
    if not ids:
        return
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _conectar()
    try:
        marcadores = ",".join("?" * len(ids))
        conn.execute(
            f"UPDATE confirmacoes_rota SET sincronizado_em = ? WHERE id IN ({marcadores})",
            (agora, *ids),
        )
        conn.commit()
    finally:
        conn.close()


def listar_pendentes_de_envio() -> list[dict]:
    """Linhas ainda não empurradas pra VPS (sincronizado_em nulo) --
    tanto as recém-criadas quanto as que foram atualizadas por um
    reenvio de aviso."""
    conn = _conectar()
    try:
        linhas = conn.execute(
            "SELECT * FROM confirmacoes_rota WHERE sincronizado_em IS NULL"
        ).fetchall()
    finally:
        conn.close()
    return [dict(linha) for linha in linhas]


def aplicar_resposta_remota(token: str, status: str, motivo_recusa: str | None,
                            respondido_em: str) -> bool:
    """Aplica a resposta do motorista (vinda do pull na VPS) na linha
    correspondente ao token. Retorna False se o token não corresponder
    a nenhuma linha local (ex. dessincronia) -- quem chama decide como
    logar isso."""
    if status not in (STATUS_CONFIRMADO, STATUS_RECUSADO):
        raise ValueError(f"Status de resposta inválido: {status!r}")
    conn = _conectar()
    try:
        cur = conn.execute("""
            UPDATE confirmacoes_rota
            SET status = ?, motivo_recusa = ?, respondido_em = ?
            WHERE token = ?
        """, (status, motivo_recusa, respondido_em, token))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def listar_do_dia(data_alvo: date) -> dict[int, dict]:
    """{agent_id: {"status":..., "respondido_em":..., "motivo_recusa":...}}
    pra essa data -- usado pelo badge em /planejamento. Se o motorista
    tiver mais de uma rota no dia (raro), reflete a rota mais recente."""
    conn = _conectar()
    try:
        linhas = conn.execute(
            "SELECT agent_id, status, respondido_em, motivo_recusa FROM confirmacoes_rota "
            "WHERE data_rota = ? ORDER BY id",
            (data_alvo.isoformat(),),
        ).fetchall()
    finally:
        conn.close()
    return {
        linha["agent_id"]: {
            "status": linha["status"],
            "respondido_em": linha["respondido_em"],
            "motivo_recusa": linha["motivo_recusa"],
        }
        for linha in linhas
    }
