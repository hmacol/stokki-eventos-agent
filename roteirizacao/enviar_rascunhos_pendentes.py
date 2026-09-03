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
VUUPT de verdade QUALQUER rascunho que ainda estivesse pendente
(status RASCUNHO) na data alvo do dia -- mesma enviar_rascunho() que
o botão "Confirmar e Enviar" da tela usa.

CANCELADO da sequência automática em 14/08 (envio cego demais) e
REVIVIDO em 23/08 como Fase 3 do roadmap de roteirização (portão
automático, pedido do Hugo): volta a rodar às 22h, mas agora
CONDICIONADO à nota de qualidade -- só manda sozinho o rascunho
pendente que estiver com ZERO badges (reaproveita _badges_trava e
_badges_lote de painel_agentes/planejamento_rotas.py, os mesmos avisos
que já aparecem na tela: paradas/caixas/tempo estimado fora da trava,
nível 4 dividindo rota, distância par-a-par, isolamento e disparidade
vs. o lote do dia -- ver Fase 0) E motorista atribuído. Rascunho
reprovado continua em RASCUNHO, esperando revisão manual em
/planejamento -- entra no e-mail de execução apontando o motivo, pra
o Hugo saber exatamente qual rota parar e por quê (não precisa mais
revisar toda rota do dia de olho, só as que a nota reprovou).

COMO USAR:
    py -3.11 enviar_rascunhos_pendentes.py                # execução normal
    py -3.11 enviar_rascunhos_pendentes.py --modo-teste    # só lista o que enviaria/reprovaria
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

# _badges_trava/_badges_lote são os mesmos cálculos que alimentam os
# badges da tela /planejamento (Fase 0) -- reaproveitados aqui como a
# própria nota de qualidade da Fase 3, em vez de uma pontuação nova.
# Import protegido de propósito: planejamento_rotas.py teve 3 rounds de
# incidente de deploy em 22/08 por causa de import de nível de módulo
# quebrando em cima dele (marketplace de rotas ainda não deployado) --
# se acontecer de novo, essa rede de segurança não pode ficar refém
# disso. Falha aqui SÓ desativa a nota de qualidade (volta pro
# comportamento cego pré-Fase 3 -- ninguém envia sozinho, tudo escala
# pro Hugo), nunca derruba o script (o script roda com "set -e" na
# sequência da noite, na frente do incrementar_rotas.py).
try:
    from planejamento_rotas import _badges_trava, _badges_lote
except Exception:
    logger.exception(
        "Falha ao importar _badges_trava/_badges_lote de planejamento_rotas -- "
        "nota de qualidade desativada nesta execução, nenhum rascunho será enviado sozinho."
    )
    _badges_trava = _badges_lote = None


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
        if _badges_trava is not None:
            # badges calculados sobre o LOTE INTEIRO (não só os
            # pendentes), mesma ordem de painel_agentes.
            # planejamento_rotas.buscar_dados_planejamento --
            # _badges_lote precisa do lote completo pra calcular
            # mediana/isolamento.
            for r in rascunhos:
                r["badges"] = _badges_trava(r)
            _badges_lote(rascunhos)
        else:
            # nota de qualidade indisponível (falha no import lá em
            # cima) -- todo pendente entra como "reprovado" (mesmo
            # comportamento manual-só de antes da Fase 3).
            for r in rascunhos:
                r["badges"] = ["nota de qualidade indisponível nesta execução"]

        pendentes = [r for r in rascunhos if r["status"] == STATUS_RASCUNHO]

        if not pendentes:
            logger.info(f"Nenhum rascunho pendente pra {data_alvo_br} -- tudo já foi confirmado manualmente ou não há rascunho pra hoje.")
            resumo_etapas["Envio de rascunhos pendentes"] = {
                "status": "ok",
                "detalhe": f"Nenhum rascunho pendente pra {data_alvo_br}.",
            }
        else:
            aprovados, reprovados = [], []
            for rascunho in pendentes:
                motivos = list(rascunho["badges"])
                if not rascunho.get("agent_id"):
                    motivos.append("sem motorista atribuído")
                (reprovados if motivos else aprovados).append((rascunho, motivos))

            prefixo = "[TESTE] " if modo_teste else ""
            for rascunho, motivos in reprovados:
                logger.info(
                    f"{prefixo}Reprovado na nota de qualidade, fica em RASCUNHO: "
                    f"'{rascunho['nome']}' -- {'; '.join(motivos)}."
                )

            enviados, com_erro, lalamove_sem_corrida = [], [], []
            for rascunho, _ in aprovados:
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
                    # Lançamento na Lalamove virou botão separado do envio
                    # (Hugo, 30/08) -- corrida NUNCA sai automática daqui,
                    # só avisa que ficou pendente em /planejamento.
                    try:
                        from lalamove_integracao import rascunho_e_lalamove
                        if rascunho_e_lalamove(rascunho) and rascunho.get("lalamove_lancar_em"):
                            # Horário escolhido no card (Hugo, 03/09): o timer
                            # lancar_lalamove_programados cuida do lançamento.
                            logger.info(f"Rota LALAMOVE '{rascunho['nome']}' enviada à VUUPT; corrida programada "
                                        f"pra {rascunho['lalamove_lancar_em']} (timer lancar_lalamove_programados).")
                        elif rascunho_e_lalamove(rascunho):
                            logger.warning(f"Rota LALAMOVE '{rascunho['nome']}' enviada à VUUPT SEM corrida na "
                                           f"Lalamove e sem horário programado -- lançar pelo botão do card "
                                           f"em /planejamento ou escolher o horário na caixa Lalamove.")
                            lalamove_sem_corrida.append(rascunho["nome"])
                    except Exception as e:
                        logger.warning(f"Não deu pra checar se '{rascunho['nome']}' é rota LALAMOVE: {e}")
                else:
                    logger.error(f"Falha ao enviar rascunho '{rascunho['nome']}': {resultado.get('erro')}")
                    com_erro.append(f"{rascunho['nome']} ({resultado.get('erro')})")

            detalhe = (
                f"{prefixo}{len(enviados)}/{len(pendentes)} rota(s) pendente(s) aprovada(s) na nota de "
                f"qualidade e enviada(s) pra {data_alvo_br} (não confirmadas a tempo em /planejamento)."
            )
            if reprovados:
                motivos_fmt = "; ".join(f"{r['nome']} ({', '.join(m)})" for r, m in reprovados)
                detalhe += (
                    f" [ALERTA_ROTA_PENDENTE_QUALIDADE] {len(reprovados)} rota(s) reprovada(s), "
                    f"continuam em RASCUNHO esperando revisão manual em /planejamento: {motivos_fmt}."
                )
            if com_erro:
                detalhe += f" [ALERTA_ENVIO_RASCUNHO] {len(com_erro)} falha(s): {'; '.join(com_erro)}."
            if lalamove_sem_corrida:
                detalhe += (
                    f" [ALERTA_LALAMOVE_PENDENTE] {len(lalamove_sem_corrida)} rota(s) LALAMOVE enviada(s) à VUUPT "
                    f"sem corrida na Lalamove (lançar pelo botão do card em /planejamento): "
                    f"{'; '.join(lalamove_sem_corrida)}."
                )
            resumo_etapas["Envio de rascunhos pendentes"] = {
                "status": "ok" if not (com_erro or reprovados) else "erro",
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
