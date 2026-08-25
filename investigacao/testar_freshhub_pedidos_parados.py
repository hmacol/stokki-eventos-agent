# -*- coding: utf-8 -*-
"""
investigacao/testar_freshhub_pedidos_parados.py

Script de validação SÓ DE LEITURA -- não cria nada em Demandas, não
duplica nada na Vuupt. Confirma que o login + leitura de pedidos
parados no Fresh Hub (freshhub/auth.py + freshhub/pedidos_parados.py)
funciona de ponta a ponta.

Também lista as task_categories e task_areas cadastradas no Fresh Hub,
pra ajudar a decidir a categoria/área da tratativa "Devolução Parcial"
(pendência em aberto, ver TRATATIVAS_PEDIDOS_PARADOS.md na raiz).

Execute (da raiz do projeto):
  py -3.11 investigacao\testar_freshhub_pedidos_parados.py
"""
import logging
import sys
from pathlib import Path

import yaml

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))

from freshhub.auth import SUPABASE_URL, FreshHubSession
from freshhub.pedidos_parados import listar_pedidos_parados

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

CONFIG_PATH = _RAIZ / "config.yaml"


def main():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    sessao = FreshHubSession(config)

    pedidos = listar_pedidos_parados(sessao)
    print(f"\n{len(pedidos)} pedido(s) parado(s) encontrados no total.")

    nao_resolvidos = [p for p in pedidos if not p.get("resolved_at")]
    print(f"{len(nao_resolvidos)} ainda sem resolved_at (== 'Pendente' na UI).\n")

    print("Amostra (5 mais recentes):")
    for p in pedidos[:5]:
        print(
            f"  - pedido={p['order_number']:>10}  vol={p['volumes']:<3}  "
            f"criado_em={p['created_at']}  resolved_at={p['resolved_at']}"
        )
    if len(pedidos) > 5:
        print(f"  ... e mais {len(pedidos) - 5}")

    # Duplicados por order_number -- confirma o achado do HAR (mesmo
    # pedido registrado como parado mais de uma vez em datas diferentes).
    vistos = {}
    for p in pedidos:
        vistos.setdefault(p["order_number"], []).append(p["created_at"])
    duplicados = {k: v for k, v in vistos.items() if len(v) > 1}
    if duplicados:
        print(f"\n{len(duplicados)} order_number(s) com mais de um registro:")
        for numero, datas in list(duplicados.items())[:10]:
            print(f"  - {numero}: {len(datas)}x ({', '.join(datas)})")

    print("\n--- task_categories cadastradas (pra decidir a categoria de 'Devolução Parcial') ---")
    resp = sessao.get(
        f"{SUPABASE_URL}/rest/v1/task_categories",
        params={"select": "id,label", "active": "eq.true", "order": "label.asc"},
    )
    resp.raise_for_status()
    for cat in resp.json():
        print(f"  - {cat['label']}")

    print("\n--- task_areas cadastradas ---")
    resp = sessao.get(
        f"{SUPABASE_URL}/rest/v1/task_areas",
        params={"select": "id,label", "active": "eq.true", "order": "label.asc"},
    )
    resp.raise_for_status()
    for area in resp.json():
        print(f"  - {area['label']}")


if __name__ == "__main__":
    main()
