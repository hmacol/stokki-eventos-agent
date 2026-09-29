# -*- coding: utf-8 -*-
"""
notificar_whatsapp.py

Aviso curto no grupo interno de WhatsApp para notificacoes que ja saem
por e-mail (pedido do Hugo, 28/09/2026). O e-mail continua sendo o
registro completo; aqui vai so o essencial: contagens e nome da rotina,
NUNCA nome de cliente, endereco, NF ou log.

Tres origens chamam este modulo, sempre DEPOIS do e-mail:
    notificar_execucao_agente.notificar_execucao  -> avisar_execucao
    alertar_falha_job.main                        -> avisar_falha_job
    verificar_entregues_nao_expedidos.main        -> avisar_nao_expedidos

O numero que envia e o do proprio Hugo, por um gateway nao-oficial
(integracao_openwa.py). Por isso: desligado por padrao, teto diario,
intervalo minimo, sem repeticao e SEM reenvio automatico (licao do erro
463 em 27/08: insistir piora).

Falha aqui NUNCA derruba a rotina que chamou.

config.yaml:
    whatsapp_notificacoes:
      ativo: false
      base_url: "http://127.0.0.1:2785/api"
      api_key: "..."
      sessao: "..."
      grupo_id: "...@g.us"
      sempre_avisar: [cancelar_rotas_sem_motorista, criar_rotas_diarias, pipeline]
      teto_diario: 20
      intervalo_min_seg: 20
      janela_repeticao_min: 120
      falhas_para_alerta: 3
"""
import logging
import re
from datetime import datetime

logger = logging.getLogger(__name__)

MAX_DETALHE = 200
MAX_ETAPAS_ERRO = 3


# --- Texto --------------------------------------------------------------------

def _uma_linha(texto, limite: int = MAX_DETALHE) -> str:
    """Uma linha so, sem os caracteres de formatacao do WhatsApp, cortada."""
    limpo = re.sub(r"[*_~`]", "", str(texto or ""))
    limpo = re.sub(r"\s+", " ", limpo).strip()
    return limpo if len(limpo) <= limite else limpo[:limite - 1].rstrip() + "…"


def _plural(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


def texto_execucao(resumo_etapas: dict, duracao_seg: float, titulo: str | None = None,
                   agora: datetime | None = None) -> str:
    from notificar_execucao_agente import (etapas_com_erro, formatar_duracao, resumir_execucao,
                                           titulo_da_rotina)
    resumo_etapas = resumo_etapas or {}
    erros = etapas_com_erro(resumo_etapas)
    nome = _uma_linha(titulo_da_rotina(resumo_etapas, titulo), 80)
    quando = (agora or datetime.now()).strftime("%d/%m %H:%M")
    resumo = resumir_execucao(resumo_etapas).rstrip(".")
    if not erros:
        return f"✅ *{nome}* · {quando}\n{resumo} ({formatar_duracao(duracao_seg)})"
    linhas = [f"❌ *{nome}* · {quando}", resumo]
    for etapa in erros[:MAX_ETAPAS_ERRO]:
        detalhe = _uma_linha((resumo_etapas.get(etapa) or {}).get("detalhe"))
        if detalhe:
            linhas.append(f"{_uma_linha(etapa, 60)}: {detalhe}")
    linhas.append("Detalhes completos no e-mail.")
    return "\n".join(linhas)


def texto_falha_job(unidade: str, info: dict, agora: datetime | None = None) -> str:
    info = info or {}
    quando = (agora or datetime.now()).strftime("%d/%m %H:%M")
    return "\n".join([
        "🚨 *Job da VPS falhou*",
        _uma_linha(unidade, 80),
        f"Resultado: {_uma_linha(info.get('Result', '?'), 40)} "
        f"(código {_uma_linha(info.get('ExecMainStatus', '?'), 10)}) · {quando}",
        "Log completo no e-mail.",
    ])


def texto_nao_expedidos(n_alertas: int, n_rotas: int, n_retiradas: int) -> str:
    linhas = ["⚠️ *Checagem da expedição*"]
    if n_alertas:
        linhas.append(f"{_plural(n_alertas, 'pedido entregue', 'pedidos entregues')} sem expedição na Stokki")
    partes = []
    if n_rotas:
        partes.append(f"{_plural(n_rotas, 'rota', 'rotas')} sem terminar")
    if n_retiradas:
        partes.append(f"{_plural(n_retiradas, 'retirada parada', 'retiradas paradas')} há mais de 7 dias")
    if partes:
        linhas.append(" · ".join(partes))
    linhas.append("Lista completa no e-mail.")
    return "\n".join(linhas)
