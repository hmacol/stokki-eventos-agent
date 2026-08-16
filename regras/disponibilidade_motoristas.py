# -*- coding: utf-8 -*-
"""
regras/disponibilidade_motoristas.py

Ajustes pontuais de disponibilidade de motorista por data -- pedido do
Hugo, 16/08: uma tabela pra selecionar quem está disponível pra rota
num dia (ou período, ex. férias/atestado), que a alocação automática
(roteirizacao/alocacao_motoristas.py::selecionar_motorista_equitativo)
sempre respeita.

Complementa, não substitui, o padrão semanal recorrente já existente
(MotoristaPreferencias.dias_disponiveis, regras/preferencias_motoristas.py):
ausência de ajuste pra um (agent_id, data) mantém o comportamento de
sempre (dia da semana dentro do padrão do motorista). Um ajuste aqui é
uma EXCEÇÃO explícita pra aquela data -- disponivel=0 bloqueia mesmo
que o dia da semana esteja no padrão; disponivel=1 libera mesmo que
não esteja (ex. motorista topa cobrir um dia fora da escala normal).
Lançamento por período (férias/atestado) é só uma forma de gravar a
mesma linha em várias datas de uma vez -- mesma tabela, sem estrutura
separada.

Tabela própria em dados/dados.db, mesmo padrão de
painel_agentes/torre_controle.py::_conectar_tratadas (CREATE TABLE IF
NOT EXISTS idempotente, upsert via ON CONFLICT DO UPDATE).
"""
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
_DB_PATH = _RAIZ / "dados" / "dados.db"


def _conectar():
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS disponibilidade_motoristas (
            agent_id      INTEGER NOT NULL,
            data          TEXT NOT NULL,
            disponivel    INTEGER NOT NULL,
            motivo        TEXT,
            atualizado_em TEXT NOT NULL,
            PRIMARY KEY (agent_id, data)
        )
    """)
    conn.commit()
    return conn


def _intervalo_datas(data_inicio: date, data_fim: date) -> list[date]:
    if data_fim < data_inicio:
        data_inicio, data_fim = data_fim, data_inicio
    dias = (data_fim - data_inicio).days
    return [data_inicio + timedelta(days=i) for i in range(dias + 1)]


def carregar_ajustes_dia(data_alvo: date) -> dict[int, dict]:
    """{agent_id: {"disponivel": bool, "motivo": str|None}} -- só os
    ajustes existentes pra `data_alvo` (agent_id ausente = sem ajuste,
    usa o padrão semanal de sempre). Usado tanto pra montar a tela
    quanto pelo filtro de alocação."""
    conn = _conectar()
    try:
        linhas = conn.execute(
            "SELECT agent_id, disponivel, motivo FROM disponibilidade_motoristas WHERE data = ?",
            (data_alvo.isoformat(),),
        ).fetchall()
    finally:
        conn.close()
    return {
        linha["agent_id"]: {"disponivel": bool(linha["disponivel"]), "motivo": linha["motivo"]}
        for linha in linhas
    }


def definir_disponibilidade_dia(data_alvo: date, ajustes: dict[int, tuple[bool, str | None]]) -> None:
    """Grava o snapshot completo de ajustes enviado pela tela (um
    upsert por motorista marcado/desmarcado no dia). `ajustes` --
    {agent_id: (disponivel, motivo)}."""
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _conectar()
    try:
        for agent_id, (disponivel, motivo) in ajustes.items():
            conn.execute("""
                INSERT INTO disponibilidade_motoristas (agent_id, data, disponivel, motivo, atualizado_em)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(agent_id, data) DO UPDATE SET
                    disponivel = excluded.disponivel,
                    motivo = excluded.motivo,
                    atualizado_em = excluded.atualizado_em
            """, (agent_id, data_alvo.isoformat(), int(bool(disponivel)), motivo, agora))
        conn.commit()
    finally:
        conn.close()


def definir_disponibilidade_periodo(agent_id: int, data_inicio: date, data_fim: date,
                                    disponivel: bool, motivo: str | None) -> int:
    """Marca o mesmo motorista com o mesmo status em todas as datas do
    intervalo (inclusive) -- ex. férias/atestado. Retorna a quantidade
    de dias gravados."""
    datas = _intervalo_datas(data_inicio, data_fim)
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _conectar()
    try:
        for dia in datas:
            conn.execute("""
                INSERT INTO disponibilidade_motoristas (agent_id, data, disponivel, motivo, atualizado_em)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(agent_id, data) DO UPDATE SET
                    disponivel = excluded.disponivel,
                    motivo = excluded.motivo,
                    atualizado_em = excluded.atualizado_em
            """, (agent_id, dia.isoformat(), int(bool(disponivel)), motivo, agora))
        conn.commit()
    finally:
        conn.close()
    return len(datas)


def limpar_ajuste(agent_id: int, data_alvo: date) -> bool:
    """Remove o ajuste daquele motorista naquela data -- volta a valer
    o padrão semanal (DIAS_DISPONIVEIS). Retorna True se havia algo pra
    remover."""
    conn = _conectar()
    try:
        cur = conn.execute(
            "DELETE FROM disponibilidade_motoristas WHERE agent_id = ? AND data = ?",
            (agent_id, data_alvo.isoformat()),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
