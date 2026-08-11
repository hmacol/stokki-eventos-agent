# -*- coding: utf-8 -*-
"""
ler_respostas_insucesso.py

Lê a caixa de entrada do Gmail (via IMAP), identifica respostas aos
e-mails de "insucesso na entrega -- aguardando retorno" (ver
notificar_insucesso_aguardando_resposta.py) e usa a API da Anthropic
(Claude) pra decidir, a partir do texto livre da resposta, o que fazer.

NOVA REGRA (pedido do Hugo, 11/08): como TODO insucesso agora é
duplicado NA HORA e o e-mail é um AVISO ("já duplicamos; responda se
quiser cancelar"), a decisão aqui virou CANCELAR ou MANTER:
  - Remetente pediu cancelamento -> cancela a reentrega no VUUPT
    (serviço duplicado, ou o agendamento se ainda não venceu) e marca
    no fingerprint -- o insucesso nunca mais é duplicado.
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
from datetime import datetime, timedelta
from pathlib import Path

_RAIZ = Path(__file__).parent.parent  # sobe de insucesso_entrega/ pra raiz do projeto
sys.path.insert(0, str(_RAIZ))

import requests
import yaml

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


def _extrair_decisao_via_claude(texto_resposta: str, motivo_texto: str,
                                codigos: list[str], api_key: str) -> dict:
    """
    Usa a API da Anthropic pra decidir, a partir da resposta em texto
    livre do remetente, se a REENTREGA (já criada/agendada) dos pedidos
    deve ser CANCELADA ou mantida.

    Retorna dict: {"cancelar": bool, "resumo": str, "nao_entendido": bool}
    """
    prompt = f"""Você vai analisar a resposta de um embarcador a um e-mail sobre um INSUCESSO NA ENTREGA.

Motivo do insucesso: "{motivo_texto}"
Pedido(s) afetado(s): {", ".join(codigos)}

O e-mail original AVISOU o embarcador de que esses pedidos JÁ FORAM DUPLICADOS para uma nova
tentativa de entrega (reentrega) no próximo dia útil, e que ele poderia responder caso NÃO
quisesse o reenvio.

Resposta do embarcador:
\"\"\"
{texto_resposta.strip()[:2000]}
\"\"\"

Com base nessa resposta, a reentrega deve ser CANCELADA?
Considere "cancelar" quando o embarcador pedir pra não reenviar, disser que o pedido foi
cancelado, que vai resolver por outro meio, ou recusar a nova tentativa de qualquer forma.
Considere "manter" (cancelar=false) quando ele confirmar/agradecer o reenvio, der aval,
combinar horário/data pra nova entrega, ou não pedir cancelamento.

Responda APENAS com um JSON válido neste formato exato, sem texto antes ou depois:
{{"cancelar": true, "resumo": "breve resumo de 1 frase da resposta", "nao_entendido": false}}

Se não conseguir entender a resposta o suficiente pra decidir, retorne:
{{"cancelar": false, "resumo": "", "nao_entendido": true}}
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
        return {"cancelar": False, "resumo": "", "nao_entendido": True}


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


def processar_respostas_insucesso(config: dict) -> dict:
    """
    Conecta no Gmail via IMAP, busca respostas aos e-mails de aviso de
    duplicação por insucesso, decide via Claude se a reentrega deve ser
    CANCELADA ou mantida, aplica a decisão (cancelamento no VUUPT, ou
    duplicação tardia pra pendências do fluxo antigo) e marca o grupo
    como respondido.

    Retorna {"processados", "grupos_atualizados", "duplicados",
    "cancelados", "nao_entendidos"}
    """
    if not _adquirir_trava():
        logger.info("Outra leitura de respostas de insucesso em andamento -- pulando este ciclo.")
        return {"processados": 0, "grupos_atualizados": 0, "duplicados": 0, "cancelados": 0, "nao_entendidos": 0}

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
        return {"processados": 0, "grupos_atualizados": 0, "duplicados": 0, "cancelados": 0, "nao_entendidos": 0}
    if not api_key or api_key == "SUA_CHAVE_AQUI":
        logger.warning("Chave da API Anthropic não configurada em config.yaml (seção anthropic).")
        return {"processados": 0, "grupos_atualizados": 0, "duplicados": 0, "cancelados": 0, "nao_entendidos": 0}

    from expedir_pedidos import duplicar_servico_por_insucesso
    import fingerprint_duplicacao_insucesso
    import fingerprint_duplicacao_agendada

    vuupt = VuuptClient(vuupt_token)

    processados = 0
    grupos_atualizados = 0
    duplicados = 0
    cancelados = 0
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
            return {"processados": 0, "grupos_atualizados": 0, "duplicados": 0, "cancelados": 0, "nao_entendidos": 0}

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

            cancelar_decisao = bool(decisao.get("cancelar"))
            resumo = decisao.get("resumo", "")

            for p in pendentes:
                marcar_respondido(p["service_id"], corpo_sem_citacao, not cancelar_decisao)
                grupos_atualizados += 1

                if cancelar_decisao:
                    if _cancelar_reentrega(p, vuupt):
                        cancelados += 1
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

            logger.info(
                f"Grupo {grupo} ({motivo_texto}) respondido: cancelar={cancelar_decisao} "
                f"-- \"{resumo}\" ({len(pendentes)} pedido(s))"
            )
            _marcar_email_processado(message_id, remetente_email)

        mail.logout()

    except Exception as e:
        logger.exception(f"Erro ao processar respostas de insucesso: {e}")

    logger.info(
        f"Leitura de respostas de insucesso concluída: {processados} e-mail(s) processado(s), "
        f"{grupos_atualizados} pedido(s) atualizado(s), {duplicados} duplicado(s), "
        f"{cancelados} reentrega(s) cancelada(s), {nao_entendidos} não entendido(s)."
    )
    return {
        "processados": processados, "grupos_atualizados": grupos_atualizados,
        "duplicados": duplicados, "cancelados": cancelados,
        "nao_entendidos": nao_entendidos,
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
