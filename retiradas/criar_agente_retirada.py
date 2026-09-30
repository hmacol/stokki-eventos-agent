"""
Cria (uma vez) o agente fixo de retiradas na VUUPT via POST /users
(doc pública: vuupt.stoplight.io "Cadastrar um novo Usuário") e imprime
o id pra colocar em config.yaml -> retiradas.agent_id.

Uso:
  python retiradas/criar_agente_retirada.py            # só mostra o que faria
  python retiradas/criar_agente_retirada.py --criar    # cria de verdade

ATENÇÃO: agente novo pode contar como licença/assento no plano da VUUPT --
confirmar com a VUUPT antes de criar (Hugo, 28/08).
"""
import argparse
import secrets
import sys
from pathlib import Path

import requests
import yaml

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))

from vuupt_client import API_BASE_URL  # noqa: E402

NOME = "RETIRADA - TERCEIROS"
EMAIL = "retiradas@freshlogbr.com"
CODE = "RETIRADA"


def main(criar: bool):
    cfg = yaml.safe_load(open(_RAIZ / "config.yaml", encoding="utf-8")) or {}
    token = cfg["vuupt_api"]["token"]
    h = {"Authorization": f"Bearer {token}", "Accept": "application/json", "Content-Type": "application/json"}

    # já existe?
    r = requests.get(f"{API_BASE_URL}/agents", headers=h, params={"per_page": 100}, timeout=30)
    r.raise_for_status()
    for a in r.json().get("data", []):
        if (a.get("name") or "").strip().upper() == NOME or (a.get("code") or "") == CODE:
            print(f"Já existe: agent_id={a['id']} name={a['name']!r} code={a.get('code')!r} -> config retiradas.agent_id: {a['id']}")
            return

    payload = {
        "name": NOME, "email": EMAIL, "code": CODE,
        "password": secrets.token_urlsafe(16),
        "is_user": "N", "is_user_readonly": "N", "is_admin": "N", "is_driver": "Y",
    }
    if not criar:
        print("Criaria (POST /users):", {k: v for k, v in payload.items() if k != "password"})
        print("Rode com --criar pra criar de verdade.")
        return
    r = requests.post(f"{API_BASE_URL}/users", headers=h, json=payload, timeout=30)
    print(r.status_code, r.text[:500])
    r.raise_for_status()
    d = r.json()
    user = d.get("user") or d.get("data") or d
    print(f"\nAgente criado: id={user.get('id')} -> coloque em config.yaml: retiradas.agent_id: {user.get('id')}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--criar", action="store_true")
    main(p.parse_args().criar)
