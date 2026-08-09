# -*- coding: utf-8 -*-
"""
fingerprint_duplicacao_agendada.py

Rastreia insucessos cuja duplicação foi AGENDADA pra alguns dias úteis
no futuro (não na hora, nem esperando resposta) -- pedido do Hugo,
06/08: "loja ou câmara em manutenção duplica automático para 3 dias
úteis depois da tentativa".

Ciclo: insucesso detectado -> agenda a duplicação pra N dias úteis
depois -> em cada execução seguinte de expedir_pedidos.py, checa se
alguma duplicação agendada já venceu -> se sim, duplica de verdade.
"""
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
DB_PATH = _RAIZ / "dados" / "dados.db"


def _conectar():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS duplicacoes_agendadas (
            service_id      INTEGER PRIMARY KEY,
            code            TEXT NOT NULL,
            failed_reason_id INTEGER,
            data_deteccao   TEXT NOT NULL,
            data_agendada   TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'PENDENTE',
            novo_code       TEXT,
            executado_em    TEXT
        )
    """)
    conn.commit()
    return conn


def adicionar_dias_uteis(data_inicial: date, dias_uteis: int) -> date:
    """Soma N dias ÚTEIS (pula sábado e domingo) a partir de uma data."""
    data = data_inicial
    dias_adicionados = 0
    while dias_adicionados < dias_uteis:
        data += timedelta(days=1)
        if data.weekday() < 5:  # 0=segunda ... 4=sexta
            dias_adicionados += 1
    return data


def ja_agendado(service_id: int) -> bool:
    conn = _conectar()
    row = conn.execute(
        "SELECT 1 FROM duplicacoes_agendadas WHERE service_id = ?", (service_id,)
    ).fetchone()
    conn.close()
    return row is not None


def agendar(service_id: int, code: str, failed_reason_id: int, dias_uteis: int,
           data_base: date | None = None):
    """Agenda a duplicação de um serviço pra N dias úteis a partir de
    hoje (ou de data_base, se informado -- usado nos testes)."""
    hoje = data_base or date.today()
    data_agendada = adicionar_dias_uteis(hoje, dias_uteis)
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = _conectar()
    conn.execute("""
        INSERT INTO duplicacoes_agendadas
            (service_id, code, failed_reason_id, data_deteccao, data_agendada, status)
        VALUES (?, ?, ?, ?, ?, 'PENDENTE')
        ON CONFLICT(service_id) DO NOTHING
    """, (service_id, code, failed_reason_id, agora, data_agendada.isoformat()))
    conn.commit()
    conn.close()
    return data_agendada


def buscar_pendentes_vencidas(data_referencia: date | None = None) -> list[dict]:
    """Duplicações agendadas cuja data já chegou (<=  hoje) e ainda não
    foram executadas -- essas devem ser duplicadas AGORA."""
    hoje = (data_referencia or date.today()).isoformat()
    conn = _conectar()
    rows = conn.execute(
        "SELECT * FROM duplicacoes_agendadas WHERE status = 'PENDENTE' AND data_agendada <= ?",
        (hoje,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def marcar_executada(service_id: int, novo_code: str):
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _conectar()
    conn.execute(
        "UPDATE duplicacoes_agendadas SET status = 'EXECUTADO', novo_code = ?, executado_em = ? WHERE service_id = ?",
        (novo_code, agora, service_id),
    )
    conn.commit()
    conn.close()


def marcar_falha(service_id: int, motivo: str):
    """Se a duplicação vencida falhar (ex: serviço não achado mais no
    VUUPT), marca como FALHOU em vez de ficar tentando pra sempre a
    cada execução."""
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _conectar()
    conn.execute(
        "UPDATE duplicacoes_agendadas SET status = 'FALHOU', novo_code = ?, executado_em = ? WHERE service_id = ?",
        (motivo[:500], agora, service_id),
    )
    conn.commit()
    conn.close()
