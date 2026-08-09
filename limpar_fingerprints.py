# -*- coding: utf-8 -*-
"""
limpar_fingerprints.py

Apaga os fingerprints de importação (tabela fingerprint_importacao_vuupt
do dados/dados.db) para forçar o reenvio de todos os pedidos na próxima
rodada do pipeline — necessário após correções que mudam o payload
(ex: coordenadas no nível do serviço), já que pedidos com hash salvo
dariam 'pulado_sem_alteracao' e nunca receberiam a correção.

Pedidos já atribuídos/concluídos no VUUPT continuam protegidos pela
trava de status do pipeline — limpar fingerprint NÃO mexe neles.

Uso:
    py -3.11 limpar_fingerprints.py
"""
import sqlite3
from pathlib import Path

DB = Path(__file__).parent / "dados" / "dados.db"

if not DB.exists():
    print(f"Banco não encontrado: {DB}")
    raise SystemExit(1)

conn = sqlite3.connect(DB)
try:
    antes = conn.execute(
        "SELECT COUNT(*) FROM fingerprint_importacao_vuupt"
    ).fetchone()[0]
    conn.execute("DELETE FROM fingerprint_importacao_vuupt")
    conn.commit()
    print(f"OK: {antes} fingerprint(s) removido(s). "
          f"Próxima rodada do pipeline reenviará todos os pedidos não atribuídos.")
except sqlite3.OperationalError as e:
    print(f"Erro: {e} — a tabela fingerprint_importacao_vuupt existe no dados.db?")
finally:
    conn.close()
