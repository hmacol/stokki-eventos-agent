# -*- coding: utf-8 -*-
"""
sincronizar_respostas_insucesso.py

Puxa da VPS pública (insucesso_resposta/app.py) as respostas que os
embarcadores já deram na página web (Sim, reenviar / Reagendar / Não
reenviar -- ver notificar_insucesso_aguardando_resposta.py::
publicar_grupo/_botoes_resposta) e aplica no VUUPT -- pedido do Hugo,
18/08: substitui ler_respostas_insucesso.py (que lia respostas por
e-mail via IMAP + Claude), removido.

REGRA ATUAL (herdada de 15/08, "nenhum insucesso duplica antes de
perguntar" -- só muda o CANAL da resposta, não a regra): a decisão do
embarcador é CANCELAR, REAGENDAR ou MANTER (nome interno mantido por
conveniência -- "manter" aqui significa "confirmar o reenvio", já que
nada é duplicado antes da resposta):
  - "Não reenviar" -> registra no fingerprint que este insucesso NUNCA
    será duplicado e avisa o ATENDIMENTO por e-mail (config
    email.email_atendimento).
  - "Reagendar" com data -> DUPLICA AGORA já com scheduled_start na
    data pedida (VUUPT) -- a roteirização só pega o pedido no dia certo
    (roteirizacao_dados.py::elegivel_para_data). Cidade com dia fixo de
    entrega (regioes_dia_fixo.py) tem a data ajustada pra próxima
    ocorrência do dia da região.
  - "Sim, reenviar" -> DUPLICA AGORA (próximo dia útil).

A ação sempre se aplica aos pedidos ATUALMENTE pendentes pro grupo
(fingerprint_aguardando_resposta.buscar_pendentes_por_grupo) -- não a
uma lista congelada no momento em que o e-mail foi mandado, mesmo
comportamento de antes (o marcador usado no e-mail só carregava a
chave do grupo, nunca uma lista fixa de pedidos).

Depois de aplicar, confirma pra VPS (POST /api/sync/ack) que o grupo
foi processado -- fecha aquele ciclo e libera um ciclo novo pro mesmo
grupo (sender_id, failed_reason_id) numa notificação futura. Sem o ack,
a VPS reenviaria o mesmo grupo respondido pra sempre, e reaplicar a
decisão no VUUPT não é seguro (duplicar/cancelar de novo).

COMO USAR (standalone -- normalmente chamado a partir de executar_tudo.py):
    py -3.11 sincronizar_respostas_insucesso.py
"""
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path

_RAIZ = Path(__file__).parent.parent  # sobe de insucesso_entrega/ pra raiz do projeto
sys.path.insert(0, str(_RAIZ))

import requests
import yaml

from email_utils import envelope_html, enviar_email, COR_PRIMARIA, COR_TEXTO, COR_BORDA, COR_FUNDO, COR_ERRO
from vuupt_client import VuuptClient
from motivos_falha import texto_do_motivo
from fingerprint_aguardando_resposta import buscar_pendentes_por_grupo, marcar_respondido
import tratativas

logger = logging.getLogger(__name__)

DB_PATH = _RAIZ / "dados" / "dados.db"

# Trava contra execução SIMULTÂNEA (herdada de ler_respostas_insucesso.py,
# 11/08): a sincronização roda tanto dentro do expedir_pedidos.py (a cada
# 30 min) quanto do executar_tudo.py (horários fixos) -- sem a trava, os
# dois processos puxariam a mesma resposta ao mesmo tempo (o ack só é
# gravado DEPOIS de aplicar) e aplicariam a ação duas vezes.
_LOCK_PATH = _RAIZ / "dados" / "sincronizar_respostas_insucesso.lock"
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
        logger.debug(f"Trava de sincronização indisponível ({e}) -- seguindo sem trava.")
        return True  # na dúvida não deixa a sincronização parar pra sempre


def _liberar_trava():
    try:
        _LOCK_PATH.unlink()
    except Exception:
        pass


def _email_do_remetente(sender_id) -> str:
    """E-mail de contato do remetente (tabela 'interno', mesma fonte
    usada pra mandar o aviso original -- ver
    notificar_insucesso_aguardando_resposta.py::_carregar_embarcadores_por_sender_id).
    Usado só pra endereçar os e-mails de confirmação/aviso depois da
    resposta -- a resposta em si não depende mais de e-mail nenhum."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT email FROM interno WHERE sender_id = ?", (sender_id,)).fetchone()
    conn.close()
    if not row or not row[0]:
        return ""
    emails = [e.strip() for e in re.split(r"[,;\t]+", row[0]) if e.strip() and "@" in e]
    return emails[0] if emails else ""


def _buscar_respostas_pendentes(config_resposta: dict) -> list[dict]:
    url_base = (config_resposta.get("url_base") or "").rstrip("/")
    sync_secret = config_resposta.get("sync_secret")
    if not url_base or not sync_secret:
        logger.warning("resposta_insucesso.url_base/sync_secret não configurados em config.yaml -- nada a sincronizar.")
        return []
    try:
        resp = requests.get(
            f"{url_base}/api/sync/respostas",
            headers={"X-Sync-Secret": sync_secret},
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning(f"Falha ao consultar respostas na VPS: {e}")
        return []
    return resp.json().get("respostas", [])


def _confirmar_aplicados(config_resposta: dict, tokens: list[str]) -> None:
    if not tokens:
        return
    url_base = (config_resposta.get("url_base") or "").rstrip("/")
    sync_secret = config_resposta.get("sync_secret")
    try:
        resp = requests.post(
            f"{url_base}/api/sync/ack",
            json={"tokens": tokens},
            headers={"X-Sync-Secret": sync_secret},
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning(f"Falha ao confirmar {len(tokens)} grupo(s) aplicado(s) na VPS -- "
                       f"serão reenviados no próximo pull, sem efeito colateral (os fingerprints "
                       f"locais já impedem duplicar/cancelar de novo): {e}")


def _cancelar_reentrega(pendente: dict, vuupt: "VuuptClient") -> bool:
    """
    Aplica a resposta "não reenviar" do remetente. O caso comum é o
    terceiro bloco abaixo -- nada existe ainda, só marca no fingerprint
    pra nunca ser duplicado. Os dois primeiros blocos cobrem pendências
    antigas que já tinham algo duplicado/agendado (resquício do fluxo
    anterior a 15/08, ou uma corrida rara entre duas sincronizações):

      - Se o serviço duplicado JÁ existe no VUUPT: DELETE nele
        (cancelar_servico) e marca cancelado_em no fingerprint de
        duplicação -- a linha fica lá, então ja_duplicado() continua
        True e o insucesso nunca é duplicado/importado de novo.
      - Se a duplicação estava só AGENDADA: cancela o agendamento.

    Retorna True se cancelou algo (ou registrou a recusa).
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
            fingerprint_duplicacao_insucesso.marcar_cancelado(service_id)
            logger.warning(f"  Reentrega {novo_code} de {code} não encontrada no VUUPT -- "
                           "marcada como cancelada no fingerprint.")
            return True
        except Exception as e:
            logger.error(f"  Falha ao cancelar reentrega {novo_code} de {code}: {e}")
            return False

    if fingerprint_duplicacao_agendada.cancelar_agendamento(service_id):
        fingerprint_duplicacao_insucesso.marcar_duplicado(service_id, "")
        fingerprint_duplicacao_insucesso.marcar_cancelado(service_id)
        logger.info(f"  Duplicação agendada de {code} cancelada antes de vencer.")
        return True

    fingerprint_duplicacao_insucesso.marcar_duplicado(service_id, "")
    fingerprint_duplicacao_insucesso.marcar_cancelado(service_id)
    logger.info(f"  {code}: reenvio recusado -- registrado no fingerprint pra nunca ser duplicado.")
    return True


def _ajustar_data_por_dia_fixo(servico: dict, nova_data: date) -> tuple[date, str | None]:
    """
    Regras de dia fixo de entrega (regioes_dia_fixo.py) valem também
    pro reagendamento pedido pelo remetente (pedido do Hugo, 12/08): se
    a data pedida cai num dia fora da regra, empurra pra PRÓXIMA data
    válida a partir dela. Retorna (data_final, aviso) -- aviso é None
    quando nada mudou.
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
    Reagenda a reentrega de um insucesso pra data escolhida pelo
    remetente na página de resposta:

      - Regra de dia fixo: a data pedida é ajustada pra próxima data
        válida (_ajustar_data_por_dia_fixo).
      - Se o serviço duplicado JÁ existe no VUUPT: grava scheduled_start/
        scheduled_end na data (08h-16h) -- a roteirização só pega o
        pedido no dia certo (roteirizacao_dados.py::elegivel_para_data).
      - Senão: duplica AGORA já com scheduled_start na data, e cancela
        o agendamento local se havia um.

    Retorna a DATA final agendada (pode diferir da pedida por causa do
    dia fixo), ou None se não conseguiu reagendar.
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
        logger.error(f"  {code} duplicado, mas falha ao gravar o agendamento: {e}")
        return None


def _notificar_atendimento_cancelamento(config_email: dict, motivo_texto: str,
                                        itens: list[tuple[str, bool]],
                                        resumo: str, remetente_email: str) -> None:
    """Avisa o ATENDIMENTO que um embarcador respondeu que NÃO quer o
    reenvio. `itens`: [(code, registrou_ok)]."""
    destino = (config_email.get("email_atendimento")
               or config_email.get("email_responsavel")
               or config_email.get("remetente"))
    if not destino:
        logger.warning("Sem destinatário de atendimento configurado -- notificação de recusa não enviada.")
        return

    import html as _html
    linhas = ""
    for code, ok in itens:
        status = ("Reenvio não será feito" if ok
                  else "FALHA ao registrar a recusa — verificar manualmente")
        cor = COR_TEXTO if ok else COR_ERRO
        linhas += f"""
    <tr>
      <td style="padding:8px 14px;border-bottom:1px solid {COR_BORDA};">{_html.escape('#' + (code or '').lstrip('#'))}</td>
      <td style="padding:8px 14px;border-bottom:1px solid {COR_BORDA};color:{cor};">{status}</td>
    </tr>"""

    conteudo = f"""
<p style="margin:0 0 4px 0;font-size:12px;font-weight:800;color:{COR_ERRO};letter-spacing:0.5px;">
  ATENDIMENTO — REENVIO RECUSADO
</p>
<p style="margin:0 0 16px 0;font-size:20px;font-weight:800;color:{COR_PRIMARIA};">
  Embarcador não quer o reenvio
</p>
<p style="margin:0 0 20px 0;font-size:14px;color:{COR_TEXTO};line-height:1.6;">
  O remetente <strong>{_html.escape(remetente_email or '(sem e-mail cadastrado)')}</strong> respondeu à pergunta de insucesso
  (motivo: {_html.escape(motivo_texto)}) dizendo que <strong>não quer o reenvio</strong>.<br>
  {_html.escape(resumo or '')}
</p>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
      style="border:1px solid {COR_BORDA};border-radius:8px;overflow:hidden;">
<thead><tr style="background:{COR_FUNDO};">
<th style="padding:8px 14px;text-align:left;font-size:11px;color:{COR_PRIMARIA};">Pedido</th>
<th style="padding:8px 14px;text-align:left;font-size:11px;color:{COR_PRIMARIA};">Situação</th>
</tr></thead><tbody>{linhas}</tbody></table>
"""
    falhas = sum(1 for _, ok in itens if not ok)
    assunto = f"[Freshlog] Atendimento: reenvio recusado — {len(itens)} pedido(s)"
    if falhas:
        assunto += f" ({falhas} com FALHA)"
    corpo = envelope_html(conteudo, rodape="Mensagem automática — Agente Stokki Eventos.",
                          cor_acento=COR_ERRO)
    enviar_email([destino], assunto, corpo, config_email)


def _notificar_remetente_reagendamento(config_email: dict, remetente_email: str,
                                       motivo_texto: str,
                                       itens: list[tuple[str, "date | None"]],
                                       data_pedida: date) -> None:
    """Confirma ao remetente a data em que cada reentrega foi agendada
    após o pedido de reagendamento. `itens`: [(code, data_final|None)]."""
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
                                               remetente_email: str, data_pedida: date) -> None:
    """Avisa o atendimento quando algum reagendamento pedido pelo
    remetente NÃO pôde ser aplicado."""
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
  O remetente <strong>{_html.escape(remetente_email or '(sem e-mail cadastrado)')}</strong> pediu o reagendamento da
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
    Puxa da VPS as respostas dadas na página de resposta ao insucesso,
    aplica no VUUPT (cancelamento/reagendamento/duplicação) e confirma
    de volta pra VPS (ack) os grupos processados.

    Retorna {"processados", "grupos_atualizados", "duplicados",
    "cancelados", "reagendados", "nao_entendidos"}
    """
    if not _adquirir_trava():
        logger.info("Outra sincronização de respostas de insucesso em andamento -- pulando este ciclo.")
        return {"processados": 0, "grupos_atualizados": 0, "duplicados": 0, "cancelados": 0, "reagendados": 0, "nao_entendidos": 0}

    try:
        return _processar_respostas_insucesso_travado(config)
    finally:
        _liberar_trava()


def _processar_respostas_insucesso_travado(config: dict) -> dict:
    """Corpo real de processar_respostas_insucesso -- só roda segurando a trava."""
    cfg_email = config.get("email", {})
    cfg_vuupt = config.get("vuupt_api", {})
    cfg_resposta = config.get("resposta_insucesso", {})
    vuupt_token = cfg_vuupt.get("token", "")

    resultado_vazio = {"processados": 0, "grupos_atualizados": 0, "duplicados": 0,
                       "cancelados": 0, "reagendados": 0, "nao_entendidos": 0}
    if not vuupt_token:
        logger.warning("Token VUUPT não configurado em config.yaml (seção vuupt_api).")
        return resultado_vazio

    respostas = _buscar_respostas_pendentes(cfg_resposta)
    if not respostas:
        logger.info("Nenhuma resposta nova na página de resposta de insucesso.")
        return resultado_vazio

    from expedir_pedidos import duplicar_servico_por_insucesso
    import fingerprint_duplicacao_insucesso
    import fingerprint_duplicacao_agendada

    vuupt = VuuptClient(vuupt_token)

    processados = grupos_atualizados = duplicados = cancelados = reagendados = nao_entendidos = 0
    tokens_aplicados = []

    for resposta in respostas:
        token = resposta.get("token")
        sender_id = resposta.get("sender_id")
        failed_reason_id = resposta.get("failed_reason_id")
        acao = resposta.get("acao") or "manter"
        processados += 1

        pendentes = buscar_pendentes_por_grupo(sender_id, failed_reason_id)
        if not pendentes:
            logger.info(f"Grupo (sender_id={sender_id}, failed_reason_id={failed_reason_id}) "
                       f"sem pedido pendente -- já resolvido localmente, só confirmando na VPS.")
            tokens_aplicados.append(token)
            continue

        motivo_texto = texto_do_motivo(failed_reason_id)
        remetente_email = _email_do_remetente(sender_id)

        nova_data = None
        if acao == "reagendar":
            try:
                nova_data = date.fromisoformat(str(resposta.get("data_pedida") or ""))
            except ValueError:
                nova_data = None
            if not nova_data:
                # Não deveria acontecer -- a página já exige data válida
                # antes de aceitar o POST. Defensivo: não aplica nada,
                # confirma o ack pra não ficar reprocessando pra sempre,
                # e sinaliza pra verificação manual.
                logger.error(f"Grupo (sender_id={sender_id}, failed_reason_id={failed_reason_id}) "
                            f"pediu reagendamento sem data válida ({resposta.get('data_pedida')!r}) "
                            f"-- verificar manualmente.")
                nao_entendidos += 1
                tokens_aplicados.append(token)
                continue
            if nova_data < date.today():
                nova_data = date.today()  # data pedida já passou -- usa hoje em vez de rejeitar

        itens_cancelamento = []
        itens_reagendamento = []
        for p in pendentes:
            resposta_texto = f"Resposta via página web: {acao}" + (
                f" (data pedida: {nova_data.isoformat()})" if nova_data else "")
            marcar_respondido(p["service_id"], resposta_texto, acao != "cancelar")
            grupos_atualizados += 1
            if p.get("code"):
                tratativas.registrar_evento(
                    p["code"], "INSUCESSO_ENTREGA", "RESPOSTA_RECEBIDA",
                    service_id=p.get("service_id"), motivo_id=failed_reason_id,
                    motivo_texto=motivo_texto, decisao=acao,
                    remetente_email=remetente_email, texto=resposta_texto,
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
                # Embarcador confirmou o reenvio -- como regra (desde
                # 15/08) nada é duplicado antes da resposta, isso SEMPRE
                # duplica agora (a checagem abaixo só evita duplicar de
                # novo se este mesmo grupo for processado duas vezes).
                if (p.get("code")
                        and not fingerprint_duplicacao_insucesso.ja_duplicado(p["service_id"])
                        and not fingerprint_duplicacao_agendada.ja_agendado(p["service_id"])):
                    servico_original = vuupt.buscar_servico_por_code(p["code"])
                    if servico_original:
                        novo = duplicar_servico_por_insucesso(vuupt, servico_original)
                        if novo:
                            fingerprint_duplicacao_insucesso.marcar_duplicado(p["service_id"], novo.get("code", ""))
                            duplicados += 1
                            tratativas.registrar_evento(
                                p["code"], "INSUCESSO_ENTREGA", "REENVIO_AUTOMATICO",
                                service_id=p.get("service_id"), motivo_id=failed_reason_id,
                                motivo_texto=motivo_texto,
                                texto=f"Reenvio confirmado pelo embarcador (novo código: {novo.get('code', '')}).",
                            )
                    else:
                        logger.warning(f"  Não achei o serviço {p['code']} no VUUPT pra duplicar — pulando.")

        if acao == "cancelar":
            _notificar_atendimento_cancelamento(cfg_email, motivo_texto, itens_cancelamento,
                                                "Resposta dada pela página web.", remetente_email)
        elif acao == "reagendar":
            _notificar_remetente_reagendamento(cfg_email, remetente_email, motivo_texto,
                                               itens_reagendamento, nova_data)
            codes_falha = [c for c, d in itens_reagendamento if not d]
            if codes_falha:
                _notificar_atendimento_falha_reagendamento(cfg_email, motivo_texto, codes_falha,
                                                            remetente_email, nova_data)

        logger.info(
            f"Grupo (sender_id={sender_id}, failed_reason_id={failed_reason_id}, {motivo_texto}) "
            f"respondido: acao={acao}"
            + (f" (nova data {nova_data.strftime('%d/%m/%Y')})" if nova_data else "")
            + f" ({len(pendentes)} pedido(s))"
        )
        tokens_aplicados.append(token)

    _confirmar_aplicados(cfg_resposta, tokens_aplicados)

    logger.info(
        f"Sincronização de respostas de insucesso concluída: {processados} grupo(s) processado(s), "
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
            logging.FileHandler(_RAIZ / "dados" / "sincronizar_respostas_insucesso.log", encoding="utf-8"),
        ],
    )
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    resultado = processar_respostas_insucesso(config)
    logger.info(f"Resultado: {resultado}")


if __name__ == "__main__":
    main()
