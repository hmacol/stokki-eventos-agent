# -*- coding: utf-8 -*-
"""
atualizar_dashboard.py

Atualiza os dados do dashboard web de comparativo de embarcadores —
roda 1x/dia via tarefa agendada (ver setup_tarefas.ps1). Busca o mês
corrente fresco do VUUPT, reaproveita o cache dos meses já fechados
(ver dashboard_dados.py), monta as agregações e salva DOIS arquivos —
o app Flask (dashboard_embarcadores.py) só lê esses arquivos, nunca
consulta o VUUPT diretamente:
  - dashboard_atual.json — resumo pronto (cards, gráficos, tabelas)
  - registros_dashboard.json — lista enriquecida, usada pelas rotas
    de filtro interativo (/api/...) pra agregar sob demanda

COMO USAR:
    py -3.11 atualizar_dashboard.py
"""
import json
import logging
import sys
from pathlib import Path

_RAIZ_LOCAL   = Path(__file__).parent
_RAIZ_PROJETO = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ_PROJETO))  # pra achar vuupt_client.py, na raiz do projeto
(_RAIZ_LOCAL / "dados").mkdir(parents=True, exist_ok=True)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_RAIZ_LOCAL / "dados" / "atualizar_dashboard.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("atualizar_dashboard")

import yaml
from vuupt_client import VuuptClient
from dashboard_dados import atualizar_cache, montar_dashboard

ARQUIVO_DASHBOARD = _RAIZ_LOCAL / "dados" / "dashboard_atual.json"
ARQUIVO_REGISTROS = _RAIZ_LOCAL / "dados" / "registros_dashboard.json"


def main():
    with open(_RAIZ_PROJETO / "config.yaml", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    token = config.get("vuupt_api", {}).get("token", "")
    if not token:
        logger.error("vuupt_api.token ausente no config.yaml")
        sys.exit(1)

    vuupt = VuuptClient(token)

    logger.info("Atualizando cache mensal (meses fechados vêm do cache, mês corrente é buscado fresco)...")
    registros = atualizar_cache(vuupt)
    logger.info(f"Total de registros combinados (ano até hoje): {len(registros)}")

    logger.info("Montando agregações do dashboard...")
    dashboard, enriquecidos = montar_dashboard(registros)

    ARQUIVO_DASHBOARD.write_text(json.dumps(dashboard, ensure_ascii=False, indent=2), encoding="utf-8")
    ARQUIVO_REGISTROS.write_text(json.dumps(enriquecidos, ensure_ascii=False), encoding="utf-8")
    logger.info(f"Dashboard atualizado: {ARQUIVO_DASHBOARD.resolve()}")
    logger.info(f"Registros enriquecidos (p/ filtros): {ARQUIVO_REGISTROS.resolve()}")
    logger.info(
        f"Resumo: {dashboard['total_pedidos']} pedido(s), "
        f"{dashboard['total_embarcadores']} embarcador(es), "
        f"{dashboard['total_destinatarios']} destinatário(s), "
        f"{dashboard['clientes_recorrentes']} recorrente(s)."
    )


if __name__ == "__main__":
    main()
