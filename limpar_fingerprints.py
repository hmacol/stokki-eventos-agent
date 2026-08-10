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
    py -3.11 limpar_fingerprints.py               # dry-run: só mostra quantos seriam apagados
    py -3.11 limpar_fingerprints.py --confirmar    # apaga de verdade

Sem --confirmar, o script é só uma checagem (não apaga nada) -- proteção
contra disparo acidental, já que este DELETE é irreversível e força o
reenvio/regeocodificação de todo pedido não atribuído na próxima rodada.
"""
import argparse
import sqlite3
from pathlib import Path

DB = Path(__file__).parent / "dados" / "dados.db"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirmar", action="store_true",
                        help="Confirma a exclusão de verdade (sem isso, só mostra a contagem)")
    args = parser.parse_args()

    if not DB.exists():
        print(f"Banco não encontrado: {DB}")
        raise SystemExit(1)

    conn = sqlite3.connect(DB)
    try:
        antes = conn.execute(
            "SELECT COUNT(*) FROM fingerprint_importacao_vuupt"
        ).fetchone()[0]

        if not args.confirmar:
            print(f"[DRY-RUN] {antes} fingerprint(s) seriam removido(s). "
                  f"Rode de novo com --confirmar para apagar de verdade.")
            return

        conn.execute("DELETE FROM fingerprint_importacao_vuupt")
        conn.commit()
        print(f"OK: {antes} fingerprint(s) removido(s). "
              f"Próxima rodada do pipeline reenviará todos os pedidos não atribuídos.")
    except sqlite3.OperationalError as e:
        print(f"Erro: {e} — a tabela fingerprint_importacao_vuupt existe no dados.db?")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
