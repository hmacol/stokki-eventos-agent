# -*- coding: utf-8 -*-
"""
enviar_rascunhos_pendentes.py

Rede de segurança do fluxo de rascunho (pedido do Hugo, 13/08): desde
que criar_rotas_diarias.py passou a rodar com --gerar-rascunho na
sequência de produção das 18h (rodar_sequencial.ps1), as rotas do dia
não vão mais direto pra VUUPT -- ficam paradas em rascunhos_rota até
alguém confirmar em /planejamento ("Confirmar e Enviar").

incrementar_rotas.py (sequência da noite, 22h) só sabe encontrar "as
rotas de hoje" perguntando pra própria VUUPT (listar_rotas) -- se um
rascunho ainda não foi confirmado manualmente até lá, ele fica
invisível pro incremento, que cria rotas novas do zero em cima do que
já está desenhado no rascunho (duplicidade).

Esse script cobria esse buraco rodando como PRIMEIRO passo da
sequência da noite, antes do incrementar_rotas.py, e enviava pra
VUUPT de verdade qualquer rascunho que ainda estivesse pendente
(status RASCUNHO) na data alvo do dia -- mesma enviar_rascunho() que
o botão "Confirmar e Enviar" da tela usa.

CANCELADO da sequência automática (pedido do Hugo, 14/08): não roda
mais sozinho às 22h (ver rodar_sequencial_noite.ps1). Rascunho não
confirmado a tempo em /planejamento agora faz incrementar_rotas.py
FALHAR de propósito (e-mail de alerta), em vez de duplicar a rota por
cima do rascunho como antes. Esse script continua funcional pra rodar
manualmente se algum dia fizer sentido de novo.

COMO USAR:
    py -3.11 enviar_rascunhos_pendentes.py                # execução normal
    py -3.11 enviar_rascunhos_pendentes.py --modo-teste    # só lista o que enviaria
"""
import argparse
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

_RAIZ_LOCAL   = Path(__file__).parent
_RAIZ_PROJETO = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ_PROJETO))
sys.path.insert(0, str(_RAIZ_LOCAL))
sys.path.insert(0, str(_RAIZ_PROJETO / "painel_agentes"))
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
        logging.FileHandler(_RAIZ_LOCAL / "dados" / "enviar_rascunhos_pendentes.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("enviar_rascunhos_pendentes")

import yaml

from notificar_execucao_agente import notificar_execucao
from criar_rotas_diarias import TZ_BRASILIA, _data_alvo_rotas
from rascunhos_rota import listar_rascunhos_do_dia, enviar_rascunho, STATUS_RASCUNHO


def _carregar_config() -> dict:
    with open(_RAIZ_PROJETO / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def main(modo_teste: bool = False):
    inicio = time.time()
    logger.info(f"{'[MODO TESTE] ' if modo_teste else ''}Envio de rascunhos pendentes iniciado.")

    config = _carregar_config()
    token = config.get("vuupt_api", {}).get("token", "")
    resumo_etapas = {}

    try:
        data_alvo = _data_alvo_rotas(datetime.now(TZ_BRASILIA))
        data_alvo_br = data_alvo.strftime("%d/%m/%Y")

        rascunhos = listar_rascunhos_do_dia(data_alvo)
        pendentes = [r for r in rascunhos if r["status"] == STATUS_RASCUNHO]

        if not pendentes:
            logger.info(f"Nenhum rascunho pendente pra {data_alvo_br} -- tudo já foi confirmado manualmente ou não há rascunho pra hoje.")
            resumo_etapas["Envio de rascunhos pendentes"] = {
                "status": "ok",
                "detalhe": f"Nenhum rascunho pendente pra {data_alvo_br}.",
            }
        else:
            enviados, com_erro = [], []
            for rascunho in pendentes:
                prefixo = "[TESTE] " if modo_teste else ""
                if modo_teste:
                    logger.info(f"{prefixo}Enviaria '{rascunho['nome']}' ({len(rascunho['paradas'])} parada(s)).")
                    enviados.append(rascunho["nome"])
                    continue

                resultado = enviar_rascunho(rascunho["id"], token)
                if resultado.get("ok"):
                    aviso_removidos = (f" (removidos por conflito: {resultado['codigos_removidos']})"
                                       if resultado.get("codigos_removidos") else "")
                    logger.info(f"Rascunho '{rascunho['nome']}' enviado -- rota VUUPT id={resultado['vuupt_route_id']}{aviso_removidos}.")
                    enviados.append(rascunho["nome"])
                else:
                    logger.error(f"Falha ao enviar rascunho '{rascunho['nome']}': {resultado.get('erro')}")
                    com_erro.append(f"{rascunho['nome']} ({resultado.get('erro')})")

            detalhe = f"{prefixo}{len(enviados)} rascunho(s) pendente(s) enviado(s) pra {data_alvo_br} (não confirmados a tempo em /planejamento)."
            if com_erro:
                detalhe += f" [ALERTA_ENVIO_RASCUNHO] {len(com_erro)} falha(s): {'; '.join(com_erro)}."
            resumo_etapas["Envio de rascunhos pendentes"] = {
                "status": "ok" if not com_erro else "erro",
                "detalhe": detalhe,
            }

    except Exception as e:
        logger.exception(f"Erro no envio de rascunhos pendentes: {e}")
        resumo_etapas["Envio de rascunhos pendentes"] = {"status": "erro", "detalhe": str(e)}

    duracao = time.time() - inicio
    logger.info(f"Envio de rascunhos pendentes finalizado em {duracao:.1f}s.")

    try:
        notificar_execucao(resumo_etapas, duracao, modo_teste, config)
    except Exception as e:
        logger.warning(f"Falha ao notificar execução (não afeta o resultado): {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Envia pra VUUPT qualquer rascunho de rota ainda pendente na data alvo do dia")
    parser.add_argument("--modo-teste", action="store_true",
                        help="Mostra o que seria enviado, sem chamar a API de verdade")
    args = parser.parse_args()
    main(modo_teste=args.modo_teste)
