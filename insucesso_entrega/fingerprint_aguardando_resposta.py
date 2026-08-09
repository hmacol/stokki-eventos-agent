# -*- coding: utf-8 -*-
"""
fingerprint_aguardando_resposta.py

Registro local de insucessos que foram notificados com uma pergunta
específica e estão aguardando resposta do remetente (pedido do Hugo,
03/08) -- alguns motivos de falha (ver motivos_falha.py::
aguarda_resposta) não duplicam automaticamente, precisam de uma
resposta antes.

Rate-limit diário (mesmo padrão de agendamento_confirmacao.py): no
máximo 1 e-mail por dia por pedido, até que status vire algo diferente
de PENDENTE (a leitura/parsing automático da resposta ainda não existe
-- por ora, status só muda manualmente ou por um mecanismo futuro
parecido com ler_respostas_agendamento.py).
"""
import sqlite3
from datetime import date, datetime
from pathlib import Path

_RAIZ = Path(__file__).parent.parent  # sobe de insucesso_entrega/ pra raiz do projeto
DB_PATH = _RAIZ / "dados" / "dados.db"


def _conectar():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS insucessos_aguardando_resposta (
            service_id           INTEGER PRIMARY KEY,
            code                  TEXT,
            failed_reason_id      INTEGER,
            sender_id             INTEGER,
            status                TEXT NOT NULL DEFAULT 'PENDENTE',
            primeira_notificacao_em TEXT NOT NULL,
            ultima_notificacao_em TEXT NOT NULL,
            resposta_texto        TEXT,
            respondido_em         TEXT,
            duplicado_apos_resposta INTEGER
        )
    """)
    conn.commit()
    return conn


def pode_notificar(service_id: int) -> bool:
    """
    True se: nunca foi notificado ainda, OU já foi notificado antes
    mas não HOJE e o status continua PENDENTE (ainda sem resposta).
    False se já respondido (status != PENDENTE) ou já notificado hoje.
    """
    conn = _conectar()
    row = conn.execute(
        "SELECT status, ultima_notificacao_em FROM insucessos_aguardando_resposta WHERE service_id = ?",
        (service_id,),
    ).fetchone()
    conn.close()

    if row is None:
        return True
    if row["status"] and row["status"].upper() != "PENDENTE":
        return False
    hoje = date.today().isoformat()
    return row["ultima_notificacao_em"][:10] != hoje


def marcar_notificado(service_id: int, failed_reason_id, sender_id=None, code=None):
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _conectar()
    conn.execute("""
        INSERT INTO insucessos_aguardando_resposta
            (service_id, code, failed_reason_id, sender_id, status, primeira_notificacao_em, ultima_notificacao_em)
        VALUES (?, ?, ?, ?, 'PENDENTE', ?, ?)
        ON CONFLICT(service_id) DO UPDATE SET
            ultima_notificacao_em = excluded.ultima_notificacao_em
    """, (service_id, code, failed_reason_id, sender_id, agora, agora))
    conn.commit()
    conn.close()


def buscar_pendentes_por_grupo(sender_id: int, failed_reason_id: int) -> list[dict]:
    """Todos os service_id ainda PENDENTES (sem resposta) desse grupo
    (mesmo remetente + mesmo motivo) -- usado quando uma resposta
    chega, pra aplicar o resultado a TODOS os pedidos daquele grupo,
    já que foram notificados juntos no mesmo e-mail."""
    conn = _conectar()
    rows = conn.execute("""
        SELECT * FROM insucessos_aguardando_resposta
        WHERE sender_id = ? AND failed_reason_id = ? AND status = 'PENDENTE'
    """, (sender_id, failed_reason_id)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def marcar_respondido(service_id: int, resposta_texto: str, duplicado: bool):
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _conectar()
    conn.execute("""
        UPDATE insucessos_aguardando_resposta
        SET status = 'RESPONDIDO', resposta_texto = ?, respondido_em = ?, duplicado_apos_resposta = ?
        WHERE service_id = ?
    """, (resposta_texto[:1000], agora, 1 if duplicado else 0, service_id))
    conn.commit()
    conn.close()
