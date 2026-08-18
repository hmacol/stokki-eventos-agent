# -*- coding: utf-8 -*-
"""
aplicar_resposta_insucesso.py

Aplica no VUUPT a decisão do embarcador sobre um insucesso na entrega
(cancelar/reagendar/confirmar o reenvio) -- chamado SINCRONAMENTE de
dentro do POST /r/<token> da página pública `resposta_insucesso/app.py`
(hospedada na mesma VPS que já roda expedir_pedidos.py, desde a
migração de 17/08 -- ver DOC_EXECUCAO_CLAUDE_MIGRACAO_VPS.md). O clique
no botão já aplica a decisão na hora; não existe mais um passo de
sincronização/leitura em lote (nem por IMAP, nem por HTTP).

REGRA ATUAL (herdada de 15/08, "nenhum insucesso duplica antes de
perguntar" -- só muda O MOMENTO/CANAL da aplicação, não a regra):
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
(fingerprint_aguardando_resposta.buscar_pendentes_por_grupo) -- consulta
AO VIVO no momento do clique, não uma lista congelada em algum momento
anterior.
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

from email_utils import envelope_html, enviar_email, COR_PRIMARIA, COR_TEXTO, COR_BORDA, COR_FUNDO, COR_ERRO
from vuupt_client import VuuptClient
from motivos_falha import texto_do_motivo
from fingerprint_aguardando_resposta import buscar_pendentes_por_grupo, marcar_respondido
import tratativas

logger = logging.getLogger(__name__)

DB_PATH = _RAIZ / "dados" / "dados.db"

# Trava contra aplicação SIMULTÂNEA do mesmo grupo -- um clique duplo
# (duplo-clique, ou duas abas com o mesmo link) não deve duplicar/
# cancelar duas vezes. Coarse (1 lock global, não por grupo): o volume
# é baixo (cliques humanos, não um endpoint de alta concorrência) e o
# tempo de posse é curto (só a duração de aplicar 1 grupo).
_LOCK_PATH = _RAIZ / "dados" / "aplicar_resposta_insucesso.lock"
_LOCK_IDADE_MAX_S = 60  # bem menor que o antigo job em lote -- isso aqui roda em segundos, não minutos


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
        logger.debug(f"Trava de aplicação indisponível ({e}) -- seguindo sem trava.")
        return True  # na dúvida não deixa a resposta do embarcador travar pra sempre


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
    resposta -- a resposta em si não depende de e-mail nenhum."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT email FROM interno WHERE sender_id = ?", (sender_id,)).fetchone()
    conn.close()
    if not row or not row[0]:
        return ""
    emails = [e.strip() for e in re.split(r"[,;\t]+", row[0]) if e.strip() and "@" in e]
    return emails[0] if emails else ""


def _cancelar_reentrega(pendente: dict, vuupt: "VuuptClient") -> bool:
    """
    Aplica a resposta "não reenviar" do remetente. O caso comum é o
    terceiro bloco abaixo -- nada existe ainda, só marca no fingerprint
    pra nunca ser duplicado. Os dois primeiros blocos cobrem pendências
    antigas que já tinham algo duplicado/agendado (resquício do fluxo
    anterior a 15/08, ou uma corrida rara entre dois cliques).

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


def aplicar_decisao(sender_id, failed_reason_id, acao: str, nova_data: date | None,
                    config: dict) -> list[dict]:
    """
    Aplica a decisão do embarcador a TODOS os pedidos ATUALMENTE
    pendentes do grupo (sender_id, failed_reason_id) -- consulta
    buscar_pendentes_por_grupo AO VIVO, não recebe a lista de fora.
    Chamado direto do POST /r/<token> de resposta_insucesso/app.py.

    acao: "cancelar" | "reagendar" | "manter" (nome interno de sempre --
    "manter" = confirma o reenvio). nova_data só é usada quando
    acao == "reagendar" (já validada pelo chamador: data ISO >= hoje).

    Retorna [{"code", "resultado": "reenviado"|"cancelado"|"reagendado"|
    "falha", "data": date|None}] -- lista vazia quando não havia nada
    pendente pro grupo (já resolvido antes) ou quando a trava não foi
    conseguida (outro clique aplicando o mesmo grupo agora).
    """
    if not _adquirir_trava():
        logger.warning(f"Grupo (sender_id={sender_id}, failed_reason_id={failed_reason_id}) "
                       f"já está sendo processado por outro clique -- tente de novo em instantes.")
        return []
    try:
        return _aplicar_decisao_travado(sender_id, failed_reason_id, acao, nova_data, config)
    finally:
        _liberar_trava()


def _aplicar_decisao_travado(sender_id, failed_reason_id, acao: str, nova_data: date | None,
                             config: dict) -> list[dict]:
    cfg_email = config.get("email", {})
    vuupt_token = config.get("vuupt_api", {}).get("token", "")
    if not vuupt_token:
        raise RuntimeError("Token VUUPT não configurado em config.yaml (seção vuupt_api).")

    pendentes = buscar_pendentes_por_grupo(sender_id, failed_reason_id)
    if not pendentes:
        return []

    from expedir_pedidos import duplicar_servico_por_insucesso
    import fingerprint_duplicacao_insucesso
    import fingerprint_duplicacao_agendada

    vuupt = VuuptClient(vuupt_token)
    motivo_texto = texto_do_motivo(failed_reason_id)
    remetente_email = _email_do_remetente(sender_id)

    resultados = []
    itens_cancelamento = []
    itens_reagendamento = []

    for p in pendentes:
        code = p.get("code") or str(p["service_id"])
        resposta_texto = f"Resposta via página web: {acao}" + (
            f" (data pedida: {nova_data.isoformat()})" if nova_data else "")
        marcar_respondido(p["service_id"], resposta_texto, acao != "cancelar")
        if p.get("code"):
            tratativas.registrar_evento(
                p["code"], "INSUCESSO_ENTREGA", "RESPOSTA_RECEBIDA",
                service_id=p.get("service_id"), motivo_id=failed_reason_id,
                motivo_texto=motivo_texto, decisao=acao,
                remetente_email=remetente_email, texto=resposta_texto,
            )

        if acao == "cancelar":
            ok = _cancelar_reentrega(p, vuupt)
            itens_cancelamento.append((code, ok))
            resultados.append({"code": code, "resultado": "cancelado" if ok else "falha", "data": None})
        elif acao == "reagendar":
            data_final = _reagendar_reentrega(p, nova_data, vuupt)
            itens_reagendamento.append((code, data_final))
            resultados.append({"code": code, "resultado": "reagendado" if data_final else "falha", "data": data_final})
        else:
            # Embarcador confirmou o reenvio -- como regra (desde 15/08)
            # nada é duplicado antes da resposta, isso SEMPRE duplica
            # agora (a checagem abaixo só evita duplicar de novo se este
            # mesmo grupo for aplicado duas vezes).
            ok = True
            if (p.get("code")
                    and not fingerprint_duplicacao_insucesso.ja_duplicado(p["service_id"])
                    and not fingerprint_duplicacao_agendada.ja_agendado(p["service_id"])):
                servico_original = vuupt.buscar_servico_por_code(p["code"])
                if servico_original:
                    novo = duplicar_servico_por_insucesso(vuupt, servico_original)
                    if novo:
                        fingerprint_duplicacao_insucesso.marcar_duplicado(p["service_id"], novo.get("code", ""))
                        tratativas.registrar_evento(
                            p["code"], "INSUCESSO_ENTREGA", "REENVIO_AUTOMATICO",
                            service_id=p.get("service_id"), motivo_id=failed_reason_id,
                            motivo_texto=motivo_texto,
                            texto=f"Reenvio confirmado pelo embarcador (novo código: {novo.get('code', '')}).",
                        )
                    else:
                        ok = False
                else:
                    logger.warning(f"  Não achei o serviço {p['code']} no VUUPT pra duplicar — pulando.")
                    ok = False
            resultados.append({"code": code, "resultado": "reenviado" if ok else "falha", "data": None})

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
        f"respondido via página web: acao={acao}"
        + (f" (nova data {nova_data.strftime('%d/%m/%Y')})" if nova_data else "")
        + f" ({len(pendentes)} pedido(s))"
    )
    return resultados
