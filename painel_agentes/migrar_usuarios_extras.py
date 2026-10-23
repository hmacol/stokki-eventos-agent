# -*- coding: utf-8 -*-
"""
Copia os logins nominais (painel_agentes.usuarios_extras do config.yaml)
pra tabela painel_usuarios, uma vez (23/10, Hugo). Cada um entra com a
mesma senha e o mesmo nivel, sem troca obrigatoria. Quem ja esta na tabela
nao e tocado, entao rodar de novo nao muda nada. Depois disso o painel so
le a tabela; a lista do config pode ser apagada quando o Hugo quiser.

    py -3.11 painel_agentes/migrar_usuarios_extras.py                      (local)
    sudo -u www-data venv/bin/python painel_agentes/migrar_usuarios_extras.py  (VPS)
"""
import sys
from pathlib import Path

import yaml

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).parent))
import acesso_telas  # noqa: E402

_RAIZ = Path(__file__).parent.parent


def main() -> None:
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        cfg_painel = (yaml.safe_load(f) or {}).get("painel_agentes", {})
    conn = acesso_telas.conectar()
    try:
        importados = acesso_telas.importar_do_config(conn, cfg_painel)
        total = conn.execute("SELECT COUNT(*) FROM painel_usuarios").fetchone()[0]
    finally:
        conn.close()
    print(f"importados: {len(importados)} ({', '.join(importados) or '-'}); na tabela agora: {total}")


if __name__ == "__main__":
    main()
