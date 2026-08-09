# -*- coding: utf-8 -*-
"""
adicionar_agendamento.py

Cria a infraestrutura de banco para o fluxo de confirmação de
agendamento de pedidos (data + horário específico de entrega, por
pedido individual) — portado do agente_relatorio (adicionar_agendamento.py
de lá), adaptado para o caminho de banco deste projeto (dados/dados.db).

1. Tabela 'agendamentos_pedido' — guarda a data/horário definitivo de
   cada pedido que precisou de agendamento confirmado pelo embarcador
   (rastreio da solicitação enviada + da resposta recebida).
2. Tabela 'emails_processados_respostas' — evita reprocessar o mesmo
   e-mail de resposta em execuções futuras do leitor de IMAP
   (ler_respostas_agendamento.py), pelo Message-ID.

Diferente do agente_relatorio: lá o agendamento é vinculado a um
cadastro geral de clientes (tabela 'clientes' com coluna
requer_agendamento); aqui esse papel é feito pelo cadastro em
regras/clientes_agendamento.py (planilha por CNPJ/CPF), então essa
parte da migração original não se aplica.

Execute uma vez: py -3.11 adicionar_agendamento.py
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "dados" / "dados.db"

DB_PATH.parent.mkdir(parents=True, exist_ok=True)
conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

# ── Tabela agendamentos_pedido ────────────────────────────────────────────
c.execute("""
    CREATE TABLE IF NOT EXISTS agendamentos_pedido (
        id                       INTEGER PRIMARY KEY AUTOINCREMENT,
        pedido                   TEXT NOT NULL,
        cnpj_destinatario        TEXT NOT NULL,
        nome_destinatario        TEXT,
        cnpj_embarcador          TEXT NOT NULL,
        numero_nf                TEXT,
        email_embarcador         TEXT,
        status                   TEXT NOT NULL DEFAULT 'PENDENTE'
                                  CHECK(status IN ('PENDENTE','RESPONDIDO','IGNORADO')),
        data_agendada            TEXT,          -- formato DD/MM/YYYY
        horario_inicio_agendado  TEXT,          -- formato HH:MM
        horario_fim_agendado     TEXT,          -- formato HH:MM
        resposta_texto           TEXT,
        solicitado_em            TEXT,
        enviado_em               TEXT DEFAULT (datetime('now','localtime')),
        respondido_em            TEXT,
        UNIQUE(pedido)
    )
""")
print("OK: tabela 'agendamentos_pedido' criada (ou já existia).")

# ── Tabela emails_processados_respostas ──────────────────────────────────
# Referenciada por ler_respostas_agendamento.py (_ja_processado/
# _marcar_processado) para não reprocessar a mesma resposta em execuções
# futuras. 'origem' permite reaproveitar a mesma tabela para outros
# leitores de resposta no futuro (ex: horário padrão, endereço), cada um
# com seu próprio namespace de Message-ID.
c.execute("""
    CREATE TABLE IF NOT EXISTS emails_processados_respostas (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id       TEXT NOT NULL,
        origem           TEXT NOT NULL,
        remetente_email  TEXT,
        processado_em    TEXT DEFAULT (datetime('now','localtime')),
        UNIQUE(message_id, origem)
    )
""")
print("OK: tabela 'emails_processados_respostas' criada (ou já existia).")

conn.commit()
conn.close()
print(f"Banco atualizado: {DB_PATH.resolve()}")
