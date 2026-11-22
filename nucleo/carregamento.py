# -*- coding: utf-8 -*-
"""
carregamento.py

Checklist de carregamento do app dos motoristas: antes de sair, o motorista
fotografa o bau carregado e desmarca as paradas que nao foram carregadas.
Esta etapa: config, quando a rota exige o checklist e leitura do registro.
Chave `api_motorista.checklist_carregamento.ativo` DESLIGADA por padrao.
"""
import sqlite3

from nucleo import banco, operacao, ponte_vuupt


def config_carregamento(config: dict | None = None) -> dict:
    bruto = ((config if config is not None else ponte_vuupt._config_projeto()).get("api_motorista") or {}) \
        .get("checklist_carregamento") or {}
    return {
        "ativo": bool(bruto.get("ativo")),
        "pilotos": {int(a) for a in (bruto.get("pilotos") or [])},
    }


def exigido(rota: sqlite3.Row, cfg: dict) -> bool:
    """O checklist so trava rota editavel ainda nao iniciada, de piloto (ou de todos se `pilotos` vazio)."""
    return bool(cfg["ativo"] and operacao.rota_editavel(rota)
                and rota["status"] in (banco.ROTA_PLANEJADA, banco.ROTA_ACEITA)
                and (not cfg["pilotos"] or rota["agent_id"] in cfg["pilotos"]))


def carregamento_da_rota(conn: sqlite3.Connection, rota_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM nucleo_carregamentos WHERE rota_id = ?", (rota_id,)).fetchone()
    return dict(row) if row else None
