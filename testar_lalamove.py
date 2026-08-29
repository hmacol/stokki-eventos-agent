# -*- coding: utf-8 -*-
"""
testar_lalamove.py -- valida as credenciais da Lalamove no config.yaml
sem criar pedido nenhum.

    python testar_lalamove.py            # cidades/serviços + cotação de teste (base -> Av. Paulista)
    python testar_lalamove.py cidades    # só GET /v3/cities
"""
import logging
import sys

from lalamove_integracao import ENDERECO_BASE, _carregar_config, _coords_base, cfg_lalamove, cliente_de_config
from lalamove_client import montar_stop

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


def main() -> int:
    config = _carregar_config()
    cfg = cfg_lalamove(config)
    if not cfg.get("api_key") or not cfg.get("api_secret"):
        print("Preencha lalamove.api_key / api_secret no config.yaml (Partner Portal > Developers).")
        return 1
    cli = cliente_de_config(cfg)
    print(f"Ambiente: {'SANDBOX' if cli.sandbox else 'PRODUÇÃO'} ({cli.base_url}), market {cli.market}")

    cidades = cli.info_cidades()
    print("\nCidades / tipos de serviço disponíveis:")
    for c in cidades:
        tipos = [s.get("key") for s in c.get("services", [])]
        print(f"  {c.get('locode')} {c.get('name')}: {', '.join(tipos)}")
        for s in c.get("services", []):
            extras = [r.get("name") for r in s.get("specialRequests", [])]
            if extras:
                print(f"      {s.get('key')}: specialRequests = {', '.join(extras)}")
    print(f"\nservice_type configurado: {cfg.get('service_type')}")

    if len(sys.argv) > 1 and sys.argv[1] == "cidades":
        return 0

    lat, lng = _coords_base(config)
    stops = [montar_stop(lat, lng, ENDERECO_BASE),
             montar_stop(-23.561414, -46.655881, "Av. Paulista, 1578, Bela Vista, São Paulo")]
    cot = cli.cotar(stops, str(cfg.get("service_type") or "VAN"))
    pb = cot.get("priceBreakdown") or {}
    print(f"\nCotação de teste OK: {pb.get('total')} {pb.get('currency')} -- "
          f"{(cot.get('distance') or {}).get('value')} m, quotationId {cot.get('quotationId')} (expira {cot.get('expiresAt')})")
    print("Nenhum pedido foi criado.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
