# -*- coding: utf-8 -*-
"""
ler_respostas_insucesso.py

Lê a caixa de entrada do Gmail (via IMAP), identifica respostas aos
e-mails de "insucesso na entrega -- aguardando retorno" (ver
notificar_insucesso_aguardando_resposta.py) e usa a API da Anthropic
(Claude) pra decidir, a partir do texto livre da resposta, o que fazer.

NOVA REGRA (pedido do Hugo, 11/08): como TODO insucesso agora é
duplicado NA HORA e o e-mail é um AVISO ("já duplicamos; responda se
quiser cancelar"), a decisão aqui virou CANCELAR, REAGENDAR ou MANTER
(12/08: o e-mail ganhou botões mailto: de "Cancelar reenvio" e
"Reagendar para outra data" que abrem uma resposta pré-preenchida --
ver notificar_insucesso_aguardando_resposta.py::_botoes_resposta; a
resposta cai aqui no fluxo normal):
  - Remetente pediu cancelamento -> cancela a reentrega no VUUPT
    (serviço duplicado, ou o agendamento se ainda não venceu), marca
    no fingerprint -- o insucesso nunca mais é duplicado -- e avisa o
    ATENDIMENTO por e-mail (config email.email_atendimento).
  - Remetente pediu REAGENDAMENTO com data -> agenda a reentrega pra
    data pedida (scheduled_start no VUUPT; duplica na hora se ainda não
    existia) -- a roteirização só pega o pedido no dia certo
    (roteirizacao_dados.py::elegivel_para_data). Cidade com dia fixo de
    entrega (regioes_dia_fixo.py, ex.: Sorocaba = terça) tem a data
    ajustada pra próxima ocorrência do dia da região.
  - Remetente confirmou/aceitou o reenvio -> mantém a reentrega.
    (Transição: se for pendência do fluxo antigo de pergunta, em que
    nada foi duplicado ainda, duplica agora.)

Portado de ler_respostas_agendamento.py (mesmo padrão: IMAP + marcador
oculto + Claude) -- só a extração e a ação final são diferentes (aqui
é uma decisão sim/não de duplicar, não uma data/horário).

Marcador oculto: [[INSUCESSO_GRUPO:<sender_id>:<failed_reason_id>]] --
uma resposta pode valer pra VÁRIOS pedidos de uma vez (o e-mail original
já lista todos os pedidos daquele grupo remetente+motivo).

COMO USAR (standalone -- normalmente chamado a partir de executar_tudo.py):
    py -3.11 ler_respostas_insucesso.py
"""
import email
import imaplib
import json
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

_RAIZ = Path(__file__).parent.parent  # sobe de insucesso_entrega/ pra raiz do projeto
sys.path.insert(0, str(_RAIZ))

import requests
import yaml

from email_utils import (
    envelope_html, enviar_email, COR_PRIMARIA, COR_TEXTO, COR_BORDA, COR_FUNDO, COR_ERRO,
)

from email_leitura_utils import (
    fetch_em_lote as _fetch_em_lote,
    remover_acentos as _remover_acentos,
    decodificar_header as _decodificar_header,
    remover_texto_citado as _remover_texto_citado,
    extrair_texto_corpo as _extrair_texto_corpo,
)
from vuupt_client import VuuptClient
from motivos_falha import texto_do_motivo
from fingerprint_aguardando_resposta import buscar_pendentes_por_grupo, marcar_respondido
import tratativas

logger = logging.getLogger(__name__)

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
DB_PATH = _RAIZ / "dados" / "dados.db"
ORIGEM_EMAIL_PROCESSADO = "INSUCESSO"

# Trava contra execução SIMULTÂNEA (11/08): a leitura passou a rodar
# também dentro do expedir_pedidos.py (tarefa de 30 em 30 min), e os
# disparos de 10:00/13:00 coincidem com o executar_tudo. Sem a trava,
# os dois processos leriam o mesmo e-mail de resposta ao mesmo tempo
# (o fingerprint de message_id só é gravado DEPOIS de processar) e
# aplicariam a ação duas vezes.
_LOCK_PATH = _RAIZ / "dados" / "ler_respostas_insucesso.lock"
_LOCK_IDADE_MAX_S = 600  # trava mais velha que isso = processo morto, pode roubar


def _adquirir_trava() -> bool:
    try:
        if _LOCK_PATH.exists():
            if time.time() - _LOCK_PATH.stat().st_mtime < _LOCK_IDADE_MAX_S:
                return False
            _LOCK_PATH.unlink()  # trava órfã de um processo que morreu
        fd = os.open(str(_LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False
    except Exception as e:
        logger.debug(f"Trava de leitura indisponível ({e}) -- seguindo sem trava.")
        return True  # na dúvida não deixa a leitura parar pra sempre


def _liberar_trava():
    try:
        _LOCK_PATH.unlink()
    except Exception:
        pass


def _ja_processado(message_id: str) -> bool:
    if not message_id:
        return False
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT 1 FROM emails_processados_respostas WHERE message_id = ? AND origem = ?",
        (message_id, ORIGEM_EMAIL_PROCESSADO),
    ).fetchone()
    conn.close()
    return row is not None


def _marcar_email_processado(message_id: str, remetente_email: str):
    if not message_id:
        return
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR IGNORE INTO emails_processados_respostas (message_id, origem, remetente_email) VALUES (?, ?, ?)",
        (message_id, ORIGEM_EMAIL_PROCESSADO, remetente_email),
    )
    conn.commit()
    conn.close()


def _extrair_grupo_do_corpo(corpo: str) -> tuple[int, int] | None:
    """Procura o marcador oculto [[INSUCESSO_GRUPO:sender_id:failed_reason_id]]."""
    match = re.search(r"\[\[INSUCESSO_GRUPO:(\d+):(\d+)\]\]", corpo)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


_DIAS_SEMANA_PT = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira",
                   "sexta-feira", "sábado", "domingo"]


def _extrair_decisao_via_claude(texto_resposta: str, motivo_texto: str,
                                codigos: list[str], api_key: str) -> dict:
    """
    Usa a API da Anthropic pra decidir, a partir da resposta em texto
    livre do remetente (ou do rascunho pré-preenchido dos botões do
    e-mail), o que fazer com a REENTREGA já criada/agendada: cancelar,
    reagendar pra uma data específica, ou manter.

    Retorna dict: {"acao": "cancelar"|"reagendar"|"manter",
                   "data": "YYYY-MM-DD"|None, "resumo": str,
                   "nao_entendido": bool}
    """
    hoje = date.today()
    prompt = f"""Você vai analisar a resposta de um embarcador a um e-mail sobre um INSUCESSO NA ENTREGA.

Motivo do insucesso: "{motivo_texto}"
Pedido(s) afetado(s): {", ".join(codigos)}
Hoje é {_DIAS_SEMANA_PT[hoje.weekday()]}, {hoje.strftime("%d/%m/%Y")}.

O e-mail original AVISOU o embarcador de que esses pedidos JÁ FORAM DUPLICADOS para uma nova
tentativa de entrega (reentrega) no próximo dia útil, e ofereceu dois botões de resposta rápida:
"CANCELAR o reenvio" e "REAGENDAR o reenvio" (com um campo de data pra preencher). O embarcador
pode ter usado um dos botões ou escrito livremente.

Resposta do embarcador:
\"\"\"
{texto_resposta.strip()[:2000]}
\"\"\"

Decida a ação sobre a reentrega:
- "cancelar": ele pediu pra não reenviar, disse que o pedido foi cancelado, que vai resolver
  por outro meio, ou recusou a nova tentativa de qualquer forma.
- "reagendar": ele pediu que a nova tentativa aconteça em uma DATA específica. Preencha "data"
  no formato YYYY-MM-DD, resolvendo datas relativas ("sexta que vem", "semana que vem") a partir
  de hoje. Se ele pediu reagendamento mas NÃO deu a data (ex.: mandou o modelo ___/___/______
  sem preencher), retorne nao_entendido=true.
- "manter": ele confirmou/agradeceu o reenvio, deu aval, ou não pediu mudança nenhuma.

Responda APENAS com um JSON válido neste formato exato, sem texto antes ou depois:
{{"acao": "cancelar", "data": null, "resumo": "breve resumo de 1 frase da resposta", "nao_entendido": false}}

Se não conseguir entender a resposta o suficiente pra decidir, retorne:
{{"acao": "manter", "data": null, "resumo": "", "nao_entendido": true}}
"""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 300,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        texto_resposta_ia = resp.json()["content"][0]["text"].strip()
        texto_resposta_ia = re.sub(r"^```json\s*|\s*```$", "", texto_resposta_ia.strip())
        return json.loads(texto_resposta_ia)
    except Exception as e:
        logger.error(f"Erro ao extrair decisão via Claude: {e}")
        return {"acao": "manter", "data": None, "resumo": "", "nao_entendido": True}


def _cancelar_reentrega(pendente: dict, vuupt: "VuuptClient") -> bool:
    """
    Cancela a reentrega de um insucesso cujo remetente respondeu
    pedindo cancelamento (pedido do Hugo, 11/08):

      - Se o serviço duplicado JÁ existe no VUUPT: DELETE nele
        (cancelar_servico) e marca cancelado_em no fingerprint de
        duplicação -- a linha fica lá, então ja_duplicado() continua
        True e o insucesso nunca é duplicado/importado de novo.
      - Se a duplicação estava só AGENDADA (motivo com atraso, ainda
        não venceu): cancela o agendamento.

    Retorna True se cancelou algo (serviço ou agendamento).
    """
    import fingerprint_duplicacao_insucesso
    import fingerprint_duplicacao_agendada

    service_id = pendente["service_id"]
    code = pendente.get("code") or str(service_id)

    novo_code = fingerprint_duplicacao_insucesso.buscar_novo_code(service_id)
    if novo_code:
        try:
            duplicado = vuupt.buscar_servico_por_code(novo_code)
            if duplicado:
                vuupt.cancelar_servico(duplicado["id"])
                fingerprint_duplicacao_insucesso.marcar_cancelado(service_id)
                logger.info(f"  Reentrega de {code} cancelada no VUUPT ({novo_code}).")
                return True
            # Duplicado sumiu do VUUPT (cancelado manualmente?) -- marca
            # cancelado no fingerprint mesmo assim, o efeito desejado
            # (não reenviar, não duplicar de novo) já está garantido.
            fingerprint_duplicacao_insucesso.marcar_cancelado(service_id)
            logger.warning(f"  Reentrega {novo_code} de {code} não encontrada no VUUPT -- "
                           "marcada como cancelada no fingerprint.")
            return True
        except Exception as e:
            logger.error(f"  Falha ao cancelar reentrega {novo_code} de {code}: {e}")
            return False

    if fingerprint_duplicacao_agendada.cancelar_agendamento(service_id):
        # Bloqueia também a duplicação imediata futura: com a regra nova
        # ("todo insucesso duplica"), sem esta marca o insucesso ainda
        # na janela de busca seria duplicado na próxima execução.
        fingerprint_duplicacao_insucesso.marcar_duplicado(service_id, "")
        fingerprint_duplicacao_insucesso.marcar_cancelado(service_id)
        logger.info(f"  Duplicação agendada de {code} cancelada antes de vencer.")
        return True

    # Nada duplicado nem agendado (pendência do fluxo antigo de
    # pergunta). Registra como duplicado+cancelado no fingerprint pra
    # que a regra nova ("todo insucesso duplica") NÃO crie a reentrega
    # que o remetente acabou de recusar.
    fingerprint_duplicacao_insucesso.marcar_duplicado(service_id, "")
    fingerprint_duplicacao_insucesso.marcar_cancelado(service_id)
    logger.info(f"  {code}: nada a cancelar (sem duplicado nem agendamento) -- "
                "registrado no fingerprint pra nunca ser duplicado.")
    return True


def _ajustar_data_por_dia_fixo(servico: dict, nova_data: date) -> tuple[date, str | None]:
    """
    Regras de dia fixo de entrega (regioes_dia_fixo.py -- cidade da
    região OU endereço/galpão cadastrado; ex.: Sorocaba só recebe às
    terças, ABCD às segundas/quartas/sextas, galpão Transfrios às
    segundas/quartas) valem também pro reagendamento pedido pelo
    remetente (pedido do Hugo, 12/08): se a data pedida cai num dia
    fora da regra, empurra pra PRÓXIMA data válida a partir dela.
    (13/08: o cálculo em si virou o helper central
    regioes_dia_fixo.ajustar_data_por_dia_fixo, compartilhado com o
    pipeline e o agente de agendamentos confirmados -- aqui só resta
    montar o texto do aviso.)

    Retorna (data_final, aviso) -- aviso é None quando nada mudou.
    """
    from roteirizacao.regioes_dia_fixo import ajustar_data_por_dia_fixo, nomes_dias

    data_final, regra = ajustar_data_por_dia_fixo(servico, nova_data)
    if not regra:
        return nova_data, None
    aviso = (f"{regra['nome']} só recebe às {nomes_dias(regra['dias'])} -- "
             f"data pedida {nova_data.strftime('%d/%m')} ajustada pra "
             f"{data_final.strftime('%d/%m/%Y')}")
    return data_final, aviso


def _reagendar_reentrega(pendente: dict, nova_data: date, vuupt: "VuuptClient") -> date | None:
    """
    Reagenda a reentrega de um insucesso pra data pedida pelo remetente
    (botão 'Reagendar para outra data' -- pedido do Hugo, 12/08):

      - Regra de dia fixo (cidade da região ou endereço/galpão): a data
        pedida é ajustada pra próxima data válida (_ajustar_data_por_dia_fixo).
      - Se o serviço duplicado JÁ existe no VUUPT: grava scheduled_start/
        scheduled_end na data (08h-16h, mesmo padrão de
        regioes_dia_fixo.py) -- a roteirização só pega o pedido no dia
        certo (roteirizacao_dados.py::elegivel_para_data).
      - Senão (duplicação agendada pra frente, ou nada ainda): duplica
        AGORA já com scheduled_start na data, e cancela o agendamento
        local se havia um (a data explícita do remetente substitui o
        prazo automático do motivo). Criar já com a data -- em vez de
        só mover a data do agendamento local -- garante que a
        roteirização pega o pedido exatamente no dia pedido; um
        duplicado criado sem scheduled_start só entraria em rota no
        ciclo seguinte à data movida.

    Retorna a DATA final agendada (pode diferir da pedida por causa do
    dia fixo), ou None se não conseguiu reagendar -- quem chama usa a
    data pra confirmar ao remetente, e o None pra avisar o atendimento.
    """
    import fingerprint_duplicacao_insucesso
    import fingerprint_duplicacao_agendada
    from expedir_pedidos import duplicar_servico_por_insucesso

    service_id = pendente["service_id"]
    code = pendente.get("code") or str(service_id)

    def _agendamento(data: date) -> dict:
        return {
            "scheduled_start": f"{data.isoformat()}T08:00:00-03:00",
            "scheduled_end": f"{data.isoformat()}T16:00:00-03:00",
        }

    novo_code = fingerprint_duplicacao_insucesso.buscar_novo_code(service_id)
    if novo_code:
        try:
            duplicado = vuupt.buscar_servico_por_code(novo_code)
            if not duplicado:
                logger.warning(f"  Reentrega {novo_code} de {code} não encontrada no VUUPT -- "
                               "nada reagendado (verificar manualmente).")
                return None
            data_final, aviso = _ajustar_data_por_dia_fixo(duplicado, nova_data)
            if aviso:
                logger.info(f"  {code}: {aviso}.")
            vuupt.atualizar_servico(duplicado["id"], _agendamento(data_final))
            logger.info(f"  Reentrega de {code} ({novo_code}) reagendada no VUUPT "
                        f"pra {data_final.strftime('%d/%m/%Y')}.")
            return data_final
        except Exception as e:
            logger.error(f"  Falha ao reagendar reentrega {novo_code} de {code}: {e}")
            return None

    servico_original = vuupt.buscar_servico_por_code(pendente.get("code") or "")
    if not servico_original:
        logger.warning(f"  Não achei o serviço {code} no VUUPT pra duplicar com a nova data -- pulando.")
        return None
    data_final, aviso = _ajustar_data_por_dia_fixo(servico_original, nova_data)
    if aviso:
        logger.info(f"  {code}: {aviso}.")

    novo = duplicar_servico_por_insucesso(vuupt, servico_original)
    if not novo:
        return None
    if fingerprint_duplicacao_agendada.cancelar_agendamento(service_id):
        logger.info(f"  {code}: duplicação agendada cancelada (substituída pela data pedida).")
    fingerprint_duplicacao_insucesso.marcar_duplicado(service_id, novo.get("code", ""))
    try:
        vuupt.atualizar_servico(novo["id"], _agendamento(data_final))
        logger.info(f"  {code} duplicado ({novo.get('code')}) já agendado "
                    f"pra {data_final.strftime('%d/%m/%Y')}.")
        return data_final
    except Exception as e:
        # A reentrega existe, só ficou sem a data -- a roteirização
        # aplica o dia fixo da região sozinha (aplicar_regioes_dia_fixo)
        # ou roteiriza no próximo ciclo. Retorna None pra o atendimento
        # ser avisado e confirmar a data manualmente.
        logger.error(f"  {code} duplicado, mas falha ao gravar o agendamento: {e}")
        return None


def _notificar_atendimento_cancelamento(config_email: dict, motivo_texto: str,
                                        itens: list[tuple[str, bool]],
                                        resumo: str, remetente_email: str):
    """
    Avisa o ATENDIMENTO que um embarcador pediu o cancelamento do
    reenvio (botão 'Cancelar reenvio' -- pedido do Hugo, 12/08).
    `itens`: [(code, cancelou_ok)] -- itens com falha aparecem
    destacados pra tratamento manual. Destinatário: config
    email.email_atendimento (fallback: email_responsavel, remetente).
    """
    destino = (config_email.get("email_atendimento")
               or config_email.get("email_responsavel")
               or config_email.get("remetente"))
    if not destino:
        logger.warning("Sem destinatário de atendimento configurado -- notificação de cancelamento não enviada.")
        return

    import html as _html
    linhas = ""
    for code, ok in itens:
        status = ("Reentrega cancelada no Vuupt" if ok
                  else "FALHA ao cancelar — verificar manualmente no Vuupt")
        cor = COR_TEXTO if ok else COR_ERRO
        linhas += f"""
    <tr>
      <td style="padding:8px 14px;border-bottom:1px solid {COR_BORDA};">{_html.escape('#' + (code or '').lstrip('#'))}</td>
      <td style="padding:8px 14px;border-bottom:1px solid {COR_BORDA};color:{cor};">{status}</td>
    </tr>"""

    conteudo = f"""
<p style="margin:0 0 4px 0;font-size:12px;font-weight:800;color:{COR_ERRO};letter-spacing:0.5px;">
  ATENDIMENTO — CANCELAMENTO DE REENVIO
</p>
<p style="margin:0 0 16px 0;font-size:20px;font-weight:800;color:{COR_PRIMARIA};">
  Embarcador pediu o cancelamento da reentrega
</p>
<p style="margin:0 0 20px 0;font-size:14px;color:{COR_TEXTO};line-height:1.6;">
  O remetente <strong>{_html.escape(remetente_email)}</strong> respondeu ao aviso de insucesso
  (motivo: {_html.escape(motivo_texto)}) pedindo o <strong>cancelamento do reenvio</strong>.<br>
  Resumo da resposta: {_html.escape(resumo or '(sem resumo)')}
</p>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
      style="border:1px solid {COR_BORDA};border-radius:8px;overflow:hidden;">
<thead><tr style="background:{COR_FUNDO};">
<th style="padding:8px 14px;text-align:left;font-size:11px;color:{COR_PRIMARIA};">Pedido</th>
<th style="padding:8px 14px;text-align:left;font-size:11px;color:{COR_PRIMARIA};">Situação</th>
</tr></thead><tbody>{linhas}</tbody></table>
"""
    falhas = sum(1 for _, ok in itens if not ok)
    assunto = f"[Freshlog] Atendimento: cancelamento de reenvio — {len(itens)} pedido(s)"
    if falhas:
        assunto += f" ({falhas} com FALHA)"
    corpo = envelope_html(conteudo, rodape="Mensagem automática — Agente Stokki Eventos.",
                          cor_acento=COR_ERRO)
    enviar_email([destino], assunto, corpo, config_email)


def _notificar_remetente_reagendamento(config_email: dict, remetente_email: str,
                                       motivo_texto: str,
                                       itens: list[tuple[str, "date | None"]],
                                       data_pedida: date):
    """
    Confirma ao remetente a data em que cada reentrega foi agendada
    após o pedido de reagendamento (pedido do Hugo, 12/08: "notificação
    aos clientes que o pedido deles foi agendado para a data correta")
    -- em especial quando a data pedida caiu num dia sem entrega pra
    região/ponto e foi ajustada pra próxima data válida.
    `itens`: [(code, data_final|None)].
    """
    if not remetente_email:
        return

    import html as _html
    linhas = ""
    algum_ajuste = False
    for code, data_final in itens:
        if data_final:
            texto_data = f"<strong>{data_final.strftime('%d/%m/%Y')}</strong>"
            if data_final != data_pedida:
                algum_ajuste = True
                texto_data += " (ajustada para o dia de entrega da região)"
        else:
            texto_data = "não foi possível reagendar — nossa equipe entrará em contato"
        linhas += f"""
    <tr>
      <td style="padding:8px 14px;border-bottom:1px solid {COR_BORDA};">{_html.escape('#' + (code or '').lstrip('#'))}</td>
      <td style="padding:8px 14px;border-bottom:1px solid {COR_BORDA};">{texto_data}</td>
    </tr>"""

    obs_ajuste = ""
    if algum_ajuste:
        obs_ajuste = (
            f"<br>A data solicitada ({data_pedida.strftime('%d/%m/%Y')}) cai num dia em que a "
            "região (ou ponto de entrega) não recebe — nesses casos, o pedido foi agendado "
            "para a <strong>próxima data de entrega válida</strong>."
        )

    conteudo = f"""
<p style="margin:0 0 4px 0;font-size:12px;font-weight:800;color:{COR_PRIMARIA};letter-spacing:0.5px;">
  REAGENDAMENTO — {_html.escape(motivo_texto.upper())}
</p>
<p style="margin:0 0 16px 0;font-size:20px;font-weight:800;color:{COR_PRIMARIA};">
  Reentrega reagendada
</p>
<p style="margin:0 0 20px 0;font-size:14px;color:{COR_TEXTO};line-height:1.6;">
  Recebemos sua solicitação de reagendamento e os pedidos abaixo foram agendados
  conforme a tabela.{obs_ajuste}
</p>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
      style="border:1px solid {COR_BORDA};border-radius:8px;overflow:hidden;">
<thead><tr style="background:{COR_FUNDO};">
<th style="padding:8px 14px;text-align:left;font-size:11px;color:{COR_PRIMARIA};">Pedido</th>
<th style="padding:8px 14px;text-align:left;font-size:11px;color:{COR_PRIMARIA};">Nova data de entrega</th>
</tr></thead><tbody>{linhas}</tbody></table>
<p style="margin:20px 0 0 0;font-size:14px;color:{COR_TEXTO};line-height:1.6;">
  Atenciosamente,<br><strong>Freshlog Logística</strong>
</p>
"""
    assunto = f"[Freshlog] Reentrega reagendada — {len(itens)} pedido(s)"
    corpo = envelope_html(conteudo, rodape="Mensagem automática — Agente Stokki Eventos.")
    enviar_email([remetente_email], assunto, corpo, config_email)


def _notificar_atendimento_falha_reagendamento(config_email: dict, motivo_texto: str,
                                               codes_falha: list[str],
                                               remetente_email: str, data_pedida: date):
    """
    Avisa o atendimento quando algum reagendamento pedido pelo
    remetente NÃO pôde ser aplicado (o e-mail de confirmação ao
    remetente promete que "nossa equipe entrará em contato" -- alguém
    precisa de fato assumir o caso).
    """
    destino = (config_email.get("email_atendimento")
               or config_email.get("email_responsavel")
               or config_email.get("remetente"))
    if not destino:
        logger.warning("Sem destinatário de atendimento configurado -- falha de reagendamento não notificada.")
        return

    import html as _html
    lista = "".join(f"<li>{_html.escape('#' + (c or '').lstrip('#'))}</li>" for c in codes_falha)
    conteudo = f"""
<p style="margin:0 0 4px 0;font-size:12px;font-weight:800;color:{COR_ERRO};letter-spacing:0.5px;">
  ATENDIMENTO — FALHA NO REAGENDAMENTO
</p>
<p style="margin:0 0 16px 0;font-size:20px;font-weight:800;color:{COR_PRIMARIA};">
  Reagendamento pedido pelo embarcador não pôde ser aplicado
</p>
<p style="margin:0 0 12px 0;font-size:14px;color:{COR_TEXTO};line-height:1.6;">
  O remetente <strong>{_html.escape(remetente_email)}</strong> pediu o reagendamento da
  reentrega (motivo do insucesso: {_html.escape(motivo_texto)}) para
  <strong>{data_pedida.strftime('%d/%m/%Y')}</strong>, mas os pedidos abaixo não puderam
  ser reagendados automaticamente — verificar no Vuupt e confirmar a data com o cliente:
</p>
<ul style="margin:0 0 8px 0;font-size:14px;color:{COR_TEXTO};line-height:1.8;">{lista}</ul>
"""
    assunto = f"[Freshlog] Atendimento: falha no reagendamento — {len(codes_falha)} pedido(s)"
    corpo = envelope_html(conteudo, rodape="Mensagem automática — Agente Stokki Eventos.",
                          cor_acento=COR_ERRO)
    enviar_email([destino], assunto, corpo, config_email)


def processar_respostas_insucesso(config: dict) -> dict:
    """
    Conecta no Gmail via IMAP, busca respostas aos e-mails de aviso de
    duplicação por insucesso, decide via Claude se a reentrega deve ser
    CANCELADA, REAGENDADA (pra data pedida) ou mantida, aplica a decisão
    (cancelamento no VUUPT + aviso ao atendimento, reagendamento no
    VUUPT/agendamento local, ou duplicação tardia pra pendências do
    fluxo antigo) e marca o grupo como respondido.

    Retorna {"processados", "grupos_atualizados", "duplicados",
    "cancelados", "reagendados", "nao_entendidos"}
    """
    if not _adquirir_trava():
        logger.info("Outra leitura de respostas de insucesso em andamento -- pulando este ciclo.")
        return {"processados": 0, "grupos_atualizados": 0, "duplicados": 0, "cancelados": 0, "reagendados": 0, "nao_entendidos": 0}

    try:
        return _processar_respostas_insucesso_travado(config)
    finally:
        _liberar_trava()


def _processar_respostas_insucesso_travado(config: dict) -> dict:
    """Corpo real de processar_respostas_insucesso -- só roda segurando a trava."""
    cfg_email = config.get("email", {})
    cfg_anthropic = config.get("anthropic", {})
    cfg_vuupt = config.get("vuupt_api", {})

    usuario_imap = cfg_email.get("remetente", "")
    senha_app = cfg_email.get("senha_app", "")
    api_key = cfg_anthropic.get("api_key", "")
    vuupt_token = cfg_vuupt.get("token", "")

    if not usuario_imap or not senha_app:
        logger.warning("IMAP desativado — remetente/senha_app não configurados em config.yaml.")
        return {"processados": 0, "grupos_atualizados": 0, "duplicados": 0, "cancelados": 0, "reagendados": 0, "nao_entendidos": 0}
    if not api_key or api_key == "SUA_CHAVE_AQUI":
        logger.warning("Chave da API Anthropic não configurada em config.yaml (seção anthropic).")
        return {"processados": 0, "grupos_atualizados": 0, "duplicados": 0, "cancelados": 0, "reagendados": 0, "nao_entendidos": 0}

    from expedir_pedidos import duplicar_servico_por_insucesso
    import fingerprint_duplicacao_insucesso
    import fingerprint_duplicacao_agendada

    vuupt = VuuptClient(vuupt_token)

    processados = 0
    grupos_atualizados = 0
    duplicados = 0
    cancelados = 0
    reagendados = 0
    nao_entendidos = 0

    try:
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=30)
        mail.login(usuario_imap, senha_app)
        mail.select("INBOX")

        dias_retroativos = cfg_email.get("dias_buscar_respostas", 7)
        data_limite = (datetime.now() - timedelta(days=dias_retroativos)).strftime("%d-%b-%Y")
        status, dados = mail.search(None, f"(SINCE {data_limite})")
        if status != "OK":
            logger.warning("Falha ao buscar e-mails no IMAP.")
            return {"processados": 0, "grupos_atualizados": 0, "duplicados": 0, "cancelados": 0, "reagendados": 0, "nao_entendidos": 0}

        ids = dados[0].split()
        logger.info(f"E-mails encontrados nos últimos {dias_retroativos} dia(s): {len(ids)} (filtrando por assunto a seguir)")

        # Fetch em lote (email_leitura_utils.py, compartilhado com
        # ler_respostas_agendamento.py) em vez de 1 round-trip IMAP por
        # e-mail -- primeiro só os cabeçalhos, pra filtrar por assunto
        # e já-processado antes de baixar o corpo completo dos que sobram.
        cabecalhos_por_id = _fetch_em_lote(mail, ids, "(BODY.PEEK[HEADER])")

        ids_para_processar = []
        for msg_id in ids:
            msg_id_str = msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)
            entrada = cabecalhos_por_id.get(msg_id_str)
            if not entrada:
                continue

            msg_header = email.message_from_bytes(entrada[1])
            assunto = _decodificar_header(msg_header.get("Subject", ""))
            assunto_normalizado = _remover_acentos(assunto.lower())
            if "aguardando retorno" not in assunto_normalizado:
                continue

            message_id = msg_header.get("Message-ID", "")
            if _ja_processado(message_id):
                continue

            ids_para_processar.append(msg_id)

        corpos_por_id = _fetch_em_lote(mail, ids_para_processar, "(RFC822)") if ids_para_processar else {}

        for msg_id in ids_para_processar:
            msg_id_str = msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)
            entrada = corpos_por_id.get(msg_id_str)
            if not entrada:
                continue
            try:
                msg = email.message_from_bytes(entrada[1])
            except Exception:
                continue

            assunto = _decodificar_header(msg.get("Subject", ""))
            message_id = msg.get("Message-ID", "")

            remetente_raw = _decodificar_header(msg.get("From", ""))
            remetente_email_match = re.search(r"[\w\.\-+]+@[\w\.\-]+", remetente_raw)
            remetente_email = remetente_email_match.group(0).lower() if remetente_email_match else ""

            eh_resposta = bool(re.match(r"^\s*(re|res|fwd|fw)\s*:", assunto, re.IGNORECASE))
            nosso_email = (cfg_email.get("remetente", "") or "").lower()
            if remetente_email == nosso_email and not eh_resposta:
                continue

            corpo = _extrair_texto_corpo(msg)
            corpo_sem_citacao = _remover_texto_citado(corpo)
            processados += 1

            grupo = _extrair_grupo_do_corpo(corpo)
            if not grupo:
                logger.info(f"E-mail de {remetente_email} sem marcador de grupo reconhecível — ignorando.")
                _marcar_email_processado(message_id, remetente_email)
                continue

            sender_id, failed_reason_id = grupo
            pendentes = buscar_pendentes_por_grupo(sender_id, failed_reason_id)
            if not pendentes:
                logger.info(f"Nenhum insucesso pendente pro grupo {grupo} — já respondido antes, ignorando.")
                _marcar_email_processado(message_id, remetente_email)
                continue

            motivo_texto = texto_do_motivo(failed_reason_id)
            codigos = [p.get("code") or str(p["service_id"]) for p in pendentes]

            decisao = _extrair_decisao_via_claude(
                corpo_sem_citacao, motivo_texto, codigos, api_key
            )

            if decisao.get("nao_entendido"):
                logger.warning(f"Não foi possível entender a resposta de {remetente_email} pro grupo {grupo}.")
                nao_entendidos += 1
                _marcar_email_processado(message_id, remetente_email)
                continue

            acao = decisao.get("acao") or "manter"
            resumo = decisao.get("resumo", "")

            nova_data = None
            if acao == "reagendar":
                try:
                    nova_data = date.fromisoformat(str(decisao.get("data") or ""))
                except ValueError:
                    nova_data = None
                if not nova_data or nova_data < date.today():
                    # Sem data utilizável -- trata como não entendido: o
                    # grupo continua pendente pra uma nova resposta.
                    logger.warning(f"Reagendamento de {remetente_email} pro grupo {grupo} "
                                   f"sem data válida ({decisao.get('data')!r}).")
                    nao_entendidos += 1
                    _marcar_email_processado(message_id, remetente_email)
                    continue

            itens_cancelamento = []
            itens_reagendamento = []
            for p in pendentes:
                marcar_respondido(p["service_id"], corpo_sem_citacao, acao != "cancelar")
                grupos_atualizados += 1
                if p.get("code"):
                    tratativas.registrar_evento(
                        p["code"], "INSUCESSO_ENTREGA", "RESPOSTA_RECEBIDA",
                        service_id=p.get("service_id"), motivo_id=failed_reason_id,
                        motivo_texto=motivo_texto, decisao=acao,
                        remetente_email=remetente_email, texto=corpo_sem_citacao[:2000],
                    )

                if acao == "cancelar":
                    ok = _cancelar_reentrega(p, vuupt)
                    if ok:
                        cancelados += 1
                    itens_cancelamento.append((p.get("code") or str(p["service_id"]), ok))
                elif acao == "reagendar":
                    data_final = _reagendar_reentrega(p, nova_data, vuupt)
                    if data_final:
                        reagendados += 1
                    itens_reagendamento.append((p.get("code") or str(p["service_id"]), data_final))
                else:
                    # Mantém a reentrega já criada. Transição do fluxo
                    # antigo (pergunta antes de duplicar): se nada foi
                    # duplicado nem agendado pra este insucesso ainda,
                    # a resposta positiva duplica agora.
                    if (p.get("code")
                            and not fingerprint_duplicacao_insucesso.ja_duplicado(p["service_id"])
                            and not fingerprint_duplicacao_agendada.ja_agendado(p["service_id"])):
                        servico_original = vuupt.buscar_servico_por_code(p["code"])
                        if servico_original:
                            novo = duplicar_servico_por_insucesso(vuupt, servico_original)
                            if novo:
                                fingerprint_duplicacao_insucesso.marcar_duplicado(p["service_id"], novo.get("code", ""))
                                duplicados += 1
                        else:
                            logger.warning(f"  Não achei o serviço {p['code']} no VUUPT pra duplicar — pulando.")

            if acao == "cancelar":
                # Aviso ao atendimento (pedido do Hugo, 12/08) -- inclui
                # itens com falha de cancelamento, que precisam de ação manual.
                _notificar_atendimento_cancelamento(cfg_email, motivo_texto,
                                                    itens_cancelamento, resumo, remetente_email)
            elif acao == "reagendar":
                # Confirma ao remetente a data efetivamente agendada
                # (pode ter sido ajustada pro dia fixo da região) e, se
                # algum pedido falhou, aciona o atendimento.
                _notificar_remetente_reagendamento(cfg_email, remetente_email,
                                                   motivo_texto, itens_reagendamento, nova_data)
                codes_falha = [c for c, d in itens_reagendamento if not d]
                if codes_falha:
                    _notificar_atendimento_falha_reagendamento(cfg_email, motivo_texto,
                                                               codes_falha, remetente_email,
                                                               nova_data)

            logger.info(
                f"Grupo {grupo} ({motivo_texto}) respondido: acao={acao}"
                + (f" (nova data {nova_data.strftime('%d/%m/%Y')})" if nova_data else "")
                + f" -- \"{resumo}\" ({len(pendentes)} pedido(s))"
            )
            _marcar_email_processado(message_id, remetente_email)

        mail.logout()

    except Exception as e:
        logger.exception(f"Erro ao processar respostas de insucesso: {e}")

    logger.info(
        f"Leitura de respostas de insucesso concluída: {processados} e-mail(s) processado(s), "
        f"{grupos_atualizados} pedido(s) atualizado(s), {duplicados} duplicado(s), "
        f"{cancelados} reentrega(s) cancelada(s), {reagendados} reagendada(s), "
        f"{nao_entendidos} não entendido(s)."
    )
    return {
        "processados": processados, "grupos_atualizados": grupos_atualizados,
        "duplicados": duplicados, "cancelados": cancelados,
        "reagendados": reagendados, "nao_entendidos": nao_entendidos,
    }


def main():
    (_RAIZ / "dados").mkdir(parents=True, exist_ok=True)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(_RAIZ / "dados" / "ler_respostas_insucesso.log", encoding="utf-8"),
        ],
    )
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    resultado = processar_respostas_insucesso(config)
    logger.info(f"Resultado: {resultado}")


if __name__ == "__main__":
    main()
