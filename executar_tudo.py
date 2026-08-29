# -*- coding: utf-8 -*-
"""
executar_tudo.py

Executa o fluxo completo em sequencia:
  1. Agendamento -- le respostas de confirmacao de agendamento por e-mail
                    (IMAP) e atualiza os pedidos confirmados, ANTES do
                    pipeline rodar (pra ja usar confirmacoes novas nesta
                    mesma execucao)
  2. Impressao    -- move pedidos faturados de "Em espera" para
                     "Aguardando Transportador" na Estacao de Impressao
  3. Pipeline     -- importa pedidos pendentes do Stokki para o VUUPT

NOTA (06/08, pedido do Hugo): a Expedicao (expedir_pedidos.py) SAIU
desse fluxo -- passou a rodar sozinha, a cada 30 minutos (8h-19h35),
pra detectar insucesso na entrega o mais perto possivel da ocorrencia
(antes so rodava aqui dentro, 3x/dia, deixando pouco tempo pro
embarcador responder antes das 20h). Continua existindo como comando
avulso ("Somente Expedicao" no painel) -- so nao faz mais parte deste
fluxo combinado.

Execute:
  py -3.11 executar_tudo.py
  py -3.11 executar_tudo.py --modo-teste
  py -3.11 executar_tudo.py --sem-impressao   # pula a etapa de impressao
  py -3.11 executar_tudo.py --sem-agendamento # pula a leitura de respostas
"""
import argparse
import logging
import os
import sys
import time
from pathlib import Path

import yaml

_RAIZ = Path(__file__).parent
sys.path.insert(0, str(_RAIZ))
(_RAIZ / "dados").mkdir(parents=True, exist_ok=True)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

def _handlers_logging():
    """Console sempre; arquivo se der. O executar_tudo.log às vezes está
    aberto por outro processo sem compartilhamento de escrita e o open
    falha com PermissionError -- isso derrubava a execução inteira antes
    de qualquer etapa rodar (visto 10/08 às 10h, ~10x no histórico).
    Nesse caso escreve num arquivo alternativo com o PID no nome; em
    último caso segue só com o console."""
    handlers = [logging.StreamHandler()]
    aviso = None
    destino = _RAIZ / "dados" / "executar_tudo.log"
    alternativo = destino.with_name(f"executar_tudo_{os.getpid()}.log")
    for caminho in (destino, alternativo):
        try:
            handlers.append(logging.FileHandler(caminho, encoding="utf-8"))
            if caminho is alternativo:
                aviso = (f"Log padrão indisponível ({destino}); "
                         f"gravando esta execução em {alternativo}.")
            break
        except OSError as e:
            aviso = (f"Sem acesso a nenhum arquivo de log ({e}); "
                     f"seguindo só com o console.")
    return handlers, aviso


_handlers, _aviso_log = _handlers_logging()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=_handlers,
)
logger = logging.getLogger("executar_tudo")
if _aviso_log:
    logger.warning(_aviso_log)

import pipeline as pipeline_mod
from stokki.estacao_impressao import imprimir_pedidos_pendentes
from ler_respostas_agendamento import processar_respostas_agendamento
from ler_planilha_entregas_nuu import processar_planilhas_entregas
from notificar_execucao_agente import notificar_execucao
import historico


def _carregar_config():
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def main(modo_teste: bool = False,
        sem_impressao: bool = False, sem_agendamento: bool = False):
    inicio = time.time()
    logger.info("=" * 60)
    logger.info(f"{'[MODO TESTE] ' if modo_teste else ''}Execucao completa iniciada.")
    logger.info("=" * 60)

    resumo_etapas = {}
    config = _carregar_config()  # carregada uma vez -- não muda durante a execução

    # ── Etapa 1: Leitura de respostas de agendamento (IMAP) ───────────────────
    if not sem_agendamento:
        logger.info("\n>>> ETAPA 1: Leitura de respostas de confirmacao de agendamento")
        logger.info("-" * 60)
        try:
            resultado_agendamento = processar_respostas_agendamento(config)
            logger.info(
                f"Respostas: {resultado_agendamento['processados']} processada(s), "
                f"{resultado_agendamento['atualizados']} pedido(s) confirmado(s), "
                f"{resultado_agendamento['nao_entendidos']} nao entendida(s)."
            )
            resumo_etapas["Agendamento"] = {
                "status": "ok",
                "detalhe": f"{resultado_agendamento['processados']} processada(s), "
                          f"{resultado_agendamento['atualizados']} confirmada(s)",
            }
        except Exception as e:
            logger.error(f"Erro na leitura de respostas de agendamento: {e}")
            resumo_etapas["Agendamento"] = {"status": "erro", "detalhe": str(e)}

        logger.info("-" * 60)
        time.sleep(2)
    else:
        logger.info("\n>>> ETAPA 1: Leitura de respostas de agendamento -- PULADA (--sem-agendamento)")
        resumo_etapas["Agendamento"] = {"status": "ok", "detalhe": "Pulada (--sem-agendamento)"}

    # ── Etapa 1a: Planilha de entregas da NUU/Maria Dolores (IMAP) ─────────────
    # Mesmo racional da Etapa 1: roda ANTES do Pipeline (Etapa 3) pra
    # pipeline.py já enxergar, na mesma execução, o agendamento que a
    # planilha confirmou (via agendamento_confirmacao.buscar_confirmacao,
    # consultada na criação do serviço no VUUPT) -- pedido do Hugo, 11/08.
    if not sem_agendamento:
        logger.info("\n>>> ETAPA 1a: Leitura da planilha de entregas (NUU/Maria Dolores)")
        logger.info("-" * 60)
        try:
            resultado_entregas_nuu = processar_planilhas_entregas(config, modo_teste=modo_teste)
            logger.info(
                f"Planilha NUU: {resultado_entregas_nuu['planilhas']} planilha(s), "
                f"{resultado_entregas_nuu['casados']}/{resultado_entregas_nuu['linhas']} NF(s) casada(s) "
                f"com pedido, {resultado_entregas_nuu['agendamentos_aplicados']} agendamento(s), "
                f"{resultado_entregas_nuu['enderecos_novos']} endereço(s) divergente(s)."
            )
            resumo_etapas["Planilha de entregas (NUU)"] = {
                "status": "ok",
                "detalhe": f"{resultado_entregas_nuu['casados']}/{resultado_entregas_nuu['linhas']} NF(s) casada(s), "
                          f"{resultado_entregas_nuu['agendamentos_aplicados']} agendamento(s), "
                          f"{resultado_entregas_nuu['enderecos_novos']} endereço(s) divergente(s)",
            }
        except Exception as e:
            logger.error(f"Erro na leitura da planilha de entregas da NUU: {e}")
            resumo_etapas["Planilha de entregas (NUU)"] = {"status": "erro", "detalhe": str(e)}

        logger.info("-" * 60)
        time.sleep(2)
    else:
        logger.info("\n>>> ETAPA 1a: Leitura da planilha de entregas (NUU) -- PULADA (--sem-agendamento)")
        resumo_etapas["Planilha de entregas (NUU)"] = {"status": "ok", "detalhe": "Pulada (--sem-agendamento)"}

    # Respostas de insucesso na entrega NÃO entram mais aqui (18/08): a
    # página pública (app.freshhub.com.br/insucesso, ver
    # resposta_insucesso/app.py) já aplica a decisão do embarcador na
    # hora do clique, direto no mesmo dados.db -- não há mais nada pra
    # sincronizar/ler em lote.

    # ── Etapa 2: Impressao (Em espera -> Aguardando Transportador) ────────────
    if not sem_impressao:
        logger.info("\n>>> ETAPA 2: Estacao de Impressao (Em espera -> Aguardando Transportador)")
        logger.info("-" * 60)
        try:
            resultado_impressao = imprimir_pedidos_pendentes(config, dry_run=modo_teste)
            if modo_teste:
                logger.info(
                    f"[MODO TESTE] {resultado_impressao['pendentes']} pedido(s) na fila "
                    f"da Estacao de Impressao (nenhum clique realizado)."
                )
                resumo_etapas["Impressão"] = {
                    "status": "ok",
                    "detalhe": f"[Teste] {resultado_impressao['pendentes']} pedido(s) na fila",
                }
            else:
                logger.info(
                    f"Processados: {resultado_impressao['processados']}/"
                    f"{resultado_impressao['pendentes']} pedido(s)."
                )
                detalhe_impressao = f"{resultado_impressao['processados']}/{resultado_impressao['pendentes']} processado(s)"
                if resultado_impressao.get("ignorados"):
                    logger.info(
                        f"{len(resultado_impressao['ignorados'])} ignorado(s) de propósito: "
                        f"{resultado_impressao['ignorados']}"
                    )
                    detalhe_impressao += f", {len(resultado_impressao['ignorados'])} ignorado(s)"
                if resultado_impressao["falhas"]:
                    logger.warning(
                        f"{len(resultado_impressao['falhas'])} falha(s): "
                        f"{resultado_impressao['falhas']}"
                    )
                    detalhe_impressao += f", {len(resultado_impressao['falhas'])} falha(s)"
                resumo_etapas["Impressão"] = {"status": "ok", "detalhe": detalhe_impressao}
        except Exception as e:
            logger.error(f"Erro na etapa de impressao: {e}")
            resumo_etapas["Impressão"] = {"status": "erro", "detalhe": str(e)}

        logger.info("-" * 60)
        logger.info("Impressao concluida. Aguardando 5s antes da expedicao...")
        time.sleep(5)
    else:
        logger.info("\n>>> ETAPA 2: Estacao de Impressao -- PULADA (--sem-impressao)")
        resumo_etapas["Impressão"] = {"status": "ok", "detalhe": "Pulada (--sem-impressao)"}

    # Expedição SAIU deste fluxo (06/08) -- roda separada, a cada 30 min
    # (tarefa StokkiEventos_ExpedicaoFrequente, ver
    # criar_tarefa_expedicao_frequente.ps1). ATENÇÃO: entre 06/08 e 11/08
    # essa tarefa não existia (nunca foi criada) e a expedição ficou sem
    # rodar -- ~570 pedidos entregues ficaram represados em "Aguardando
    # Transportador" até a rodada retroativa de 11/08.

    # ── Etapa 3: Pipeline de importacao ───────────────────────────────────────
    logger.info("\n>>> ETAPA 3: Pipeline de importacao Stokki -> VUUPT")
    logger.info("-" * 60)
    try:
        pipeline_mod.main(modo_teste=modo_teste)
        ultimo = historico.ultima_execucao()
        if ultimo:
            contagem = ultimo.get("contagem", {})
            partes = [f"{historico.ROTULOS.get(k, k)}: {v}" for k, v in contagem.items() if v]
            resumo_etapas["Pipeline"] = {
                "status": "ok",
                "detalhe": f"{ultimo.get('total', 0)} pedido(s) — " + (", ".join(partes) if partes else "nenhuma ação"),
            }
        else:
            resumo_etapas["Pipeline"] = {"status": "ok", "detalhe": "Concluído (sem histórico disponível)"}
    except Exception as e:
        logger.error(f"Erro no pipeline: {e}")
        resumo_etapas["Pipeline"] = {"status": "erro", "detalhe": str(e)}

    # ── Etapa 4: Retiradas no galpão (Hugo, 28/08) ────────────────────────────
    # Fecha na VUUPT (entregue com sucesso) as retiradas que a Stokki já
    # marcou "Enviado" e cancela as que a Stokki cancelou. Roda DEPOIS do
    # pipeline, na mesma sequência, pra reaproveitar a sessão Stokki sem
    # login concorrente. Também roda sozinho a cada 30 min em horário
    # comercial (infra/stokki-acompanhar-retiradas.timer).
    logger.info("\n>>> ETAPA 4: Acompanhar retiradas no galpão (Stokki -> VUUPT)")
    logger.info("-" * 60)
    try:
        from retiradas.acompanhar_retiradas import acompanhar as acompanhar_retiradas
        resumo_ret = acompanhar_retiradas(config, modo_teste=modo_teste, ignorar_trava_painel=True)
        resumo_etapas["Retiradas"] = {
            "status": "erro" if resumo_ret.get("erros") else "ok",
            "detalhe": (f"Concluídas: {len(resumo_ret.get('concluidos', []))}, "
                        f"canceladas: {len(resumo_ret.get('cancelados', []))}, "
                        f"aguardando: {len(resumo_ret.get('aguardando', []))}, "
                        f"erros: {len(resumo_ret.get('erros', []))}"),
        }
    except Exception as e:
        logger.error(f"Erro no acompanhamento de retiradas: {e}")
        resumo_etapas["Retiradas"] = {"status": "erro", "detalhe": str(e)}

    logger.info("-" * 60)
    elapsed = time.time() - inicio
    logger.info(f"\nExecucao completa finalizada em {elapsed/60:.1f} minutos.")
    logger.info("=" * 60)

    try:
        notificar_execucao(resumo_etapas, elapsed, modo_teste, config)
    except Exception as e:
        logger.warning(f"Falha ao notificar execução por e-mail (não afeta o resultado): {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Executa agendamento + impressao + pipeline em sequencia"
    )
    parser.add_argument("--modo-teste", action="store_true",
                        help="Simula sem gravar nada (impressao: so conta, nao clica)")
    parser.add_argument("--sem-impressao", action="store_true",
                        help="Pula a etapa de Estacao de Impressao")
    parser.add_argument("--sem-agendamento", action="store_true",
                        help="Pula a leitura de respostas de confirmacao de agendamento")
    args = parser.parse_args()
    main(modo_teste=args.modo_teste,
        sem_impressao=args.sem_impressao, sem_agendamento=args.sem_agendamento)
