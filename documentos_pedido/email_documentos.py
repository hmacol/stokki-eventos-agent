# -*- coding: utf-8 -*-
"""
email_documentos.py

Busca e-mails com anexos PDF (nota fiscal, boleto, carta de correção,
agendamento, etc) via IMAP -- pedido do Hugo, 05/08. Mesmo servidor/
credenciais já usados em ler_respostas_agendamento.py e
ler_respostas_insucesso.py (config.yaml -> email.remetente/.senha_app).

Diferente dos outros leitores de e-mail deste projeto (que procuram
RESPOSTAS a um e-mail específico que o agente mandou), esse procura
e-mails RECEBIDOS de terceiros com PDF anexado -- não precisa de
marcador oculto nem de casar com um e-mail enviado antes.

COMO A BUSCA FICA RÁPIDA (reestruturação de 12/08 -- antes cada
execução re-baixava o RFC822 INTEIRO, com anexos, de todo e-mail dos
últimos 7 dias só pra então checar o Message-ID no banco; ~60s por
execução com 0 e-mail novo):

  1. Cursor incremental por caixa (tabela documentos_emails_cursor):
     guarda o último UID coberto + UIDVALIDITY. A busca vira
     "UID último+1:*" -- só o que chegou desde a execução anterior.
     Sem cursor (primeira execução) ou com UIDVALIDITY trocado pelo
     servidor, cai pro comportamento antigo (SINCE N dias) uma vez,
     pra semear.
  2. Checagem de Message-ID ANTES do download pesado: um FETCH em
     lote só de headers (BODY.PEEK, não marca como lido) resolve o
     Message-ID de todos os candidatos numa chamada; cruza com o
     banco carregado em memória; só os realmente novos ganham o
     fetch completo do RFC822.

O cursor só é salvo DEPOIS que a caixa foi processada sem exceção --
se a execução morrer no meio, a próxima re-varre o mesmo intervalo e
a checagem de Message-ID pula o que já tinha sido salvo.
"""
import email
import imaplib
import logging
import re
import sqlite3
from datetime import datetime, timedelta
from email.header import decode_header
from pathlib import Path

logger = logging.getLogger(__name__)

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993

_RAIZ = Path(__file__).parent.parent
DB_PATH = _RAIZ / "dados" / "dados.db"
PASTA_TEMP_ANEXOS = Path(__file__).parent / "dados" / "anexos_temp"

# UIDs por comando FETCH de headers -- limita o tamanho da linha de
# comando IMAP quando a primeira execução pega a janela de 7 dias inteira.
_LOTE_FETCH_HEADERS = 500


def _conectar_controle():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS documentos_emails_processados (
            message_id     TEXT PRIMARY KEY,
            processado_em  TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS documentos_emails_cursor (
            mailbox        TEXT PRIMARY KEY,
            uidvalidity    INTEGER NOT NULL,
            ultimo_uid     INTEGER NOT NULL,
            atualizado_em  TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def _ids_ja_processados() -> set[str]:
    """Todos os Message-IDs já processados, numa query só -- substitui
    o SELECT por mensagem (que abria uma conexão sqlite cada vez)."""
    conn = _conectar_controle()
    rows = conn.execute("SELECT message_id FROM documentos_emails_processados").fetchall()
    conn.close()
    return {r[0] for r in rows}


def _marcar_processado(message_id: str):
    if not message_id:
        return
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _conectar_controle()
    conn.execute(
        "INSERT OR IGNORE INTO documentos_emails_processados (message_id, processado_em) VALUES (?, ?)",
        (message_id, agora),
    )
    conn.commit()
    conn.close()


def _ler_cursor(mailbox: str):
    conn = _conectar_controle()
    row = conn.execute(
        "SELECT uidvalidity, ultimo_uid FROM documentos_emails_cursor WHERE mailbox = ?",
        (mailbox,),
    ).fetchone()
    conn.close()
    return row  # (uidvalidity, ultimo_uid) ou None


def _salvar_cursor(mailbox: str, uidvalidity: int, ultimo_uid: int):
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _conectar_controle()
    conn.execute(
        "INSERT OR REPLACE INTO documentos_emails_cursor "
        "(mailbox, uidvalidity, ultimo_uid, atualizado_em) VALUES (?, ?, ?, ?)",
        (mailbox, uidvalidity, ultimo_uid, agora),
    )
    conn.commit()
    conn.close()


def _decodificar_header(valor) -> str:
    if not valor:
        return ""
    partes = decode_header(valor)
    resultado = ""
    for texto, encoding in partes:
        if isinstance(texto, bytes):
            try:
                resultado += texto.decode(encoding or "utf-8", errors="ignore")
            except (LookupError, UnicodeDecodeError):
                resultado += texto.decode("latin-1", errors="ignore")
        else:
            resultado += texto
    return resultado


# ── Mecânica da busca incremental ───────────────────────────────────────────

def _status_mailbox(mail, mailbox: str) -> tuple[int, int]:
    status, dados = mail.status(mailbox, "(UIDVALIDITY UIDNEXT)")
    if status != "OK" or not dados or not dados[0]:
        raise RuntimeError(f"STATUS falhou pra {mailbox}: {status}")
    texto = dados[0].decode(errors="ignore")
    m_val = re.search(r"UIDVALIDITY (\d+)", texto)
    m_next = re.search(r"UIDNEXT (\d+)", texto)
    if not m_val or not m_next:
        raise RuntimeError(f"STATUS sem UIDVALIDITY/UIDNEXT pra {mailbox}: {texto!r}")
    return int(m_val.group(1)), int(m_next.group(1))


def _abrir_janela_busca(mail, mailbox: str, dias_retroativos: int) -> dict:
    """
    Decide o critério de busca da caixa (cursor UID incremental, ou
    fallback SINCE na primeira execução / UIDVALIDITY trocado) e
    captura o UIDNEXT atual. O UIDNEXT é capturado ANTES das buscas de
    propósito: e-mail que chegar durante a execução tem UID >= ele,
    então continua coberto pela próxima janela mesmo que a busca de
    agora não o veja.
    """
    uidvalidity, uidnext = _status_mailbox(mail, mailbox)
    cursor = _ler_cursor(mailbox)

    ultimo_uid = 0
    if cursor and cursor[0] == uidvalidity:
        ultimo_uid = cursor[1]
        criterio = f"UID {ultimo_uid + 1}:*"
    else:
        if cursor:
            logger.info(f"UIDVALIDITY de {mailbox} mudou ({cursor[0]} -> {uidvalidity}) "
                       f"-- cursor descartado, re-semeando pela janela de {dias_retroativos} dia(s).")
        data_limite = (datetime.now() - timedelta(days=dias_retroativos)).strftime("%d-%b-%Y")
        criterio = f"SINCE {data_limite}"

    return {"mailbox": mailbox, "uidvalidity": uidvalidity, "uidnext": uidnext,
            "ultimo_uid": ultimo_uid, "criterio": criterio}


def _buscar_uids(mail, janela: dict, criterio_extra: str = "") -> list[int]:
    criterio = (f"({criterio_extra} {janela['criterio']})" if criterio_extra
                else f"({janela['criterio']})")
    status, dados = mail.uid("search", None, criterio)
    if status != "OK":
        raise RuntimeError(f"SEARCH falhou em {janela['mailbox']}: {criterio}")
    # "UID n:*" devolve sempre a última mensagem da caixa mesmo quando o
    # UID dela é menor que n (o "*" resolve pro maior UID existente e o
    # servidor trata o intervalo como min:max) -- filtra de verdade aqui.
    uids = [int(u) for u in dados[0].split()]
    return [u for u in uids if u > janela["ultimo_uid"]]


def _fechar_janela_busca(janela: dict):
    """Salva o cursor -- chamar só depois de processar a caixa inteira
    sem exceção (ver docstring do módulo)."""
    _salvar_cursor(janela["mailbox"], janela["uidvalidity"], janela["uidnext"] - 1)


def _message_ids_dos_uids(mail, uids: list[int]) -> dict[int, str]:
    """Message-ID de cada UID via FETCH em lote só de headers
    (BODY.PEEK -- não baixa anexo nem marca como lido). É o que
    permite pular e-mail já processado ANTES do download pesado."""
    resultado: dict[int, str] = {}
    for i in range(0, len(uids), _LOTE_FETCH_HEADERS):
        lote = uids[i:i + _LOTE_FETCH_HEADERS]
        conjunto = ",".join(str(u) for u in lote)
        status, dados = mail.uid("fetch", conjunto, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
        if status != "OK":
            continue
        for parte in dados:
            if not isinstance(parte, tuple) or len(parte) < 2:
                continue
            m = re.search(rb"UID (\d+)", parte[0])
            if not m:
                continue
            headers = email.message_from_bytes(parte[1])
            resultado[int(m.group(1))] = headers.get("Message-ID", "") or ""
    return resultado


def _baixar_mensagem(mail, uid: int):
    status, dados = mail.uid("fetch", str(uid), "(RFC822)")
    if status != "OK" or not dados or not dados[0] or not isinstance(dados[0], tuple):
        return None
    try:
        return email.message_from_bytes(dados[0][1])
    except Exception:
        return None


def _extrair_pdfs_anexados(msg, uid: int, message_id: str) -> list[dict]:
    """Salva os PDFs anexados de uma mensagem em PASTA_TEMP_ANEXOS e
    devolve 1 item por PDF, no formato que o pipeline consome."""
    itens = []
    if not msg.is_multipart():
        return itens
    assunto = _decodificar_header(msg.get("Subject", ""))
    remetente = _decodificar_header(msg.get("From", ""))
    for parte in msg.walk():
        nome_anexo = parte.get_filename()
        if not nome_anexo or not nome_anexo.lower().endswith(".pdf"):
            continue
        # Usa só o nome-base do anexo (sem diretórios) -- o nome vem do
        # header Content-Disposition do e-mail, que é controlado pelo
        # remetente e pode conter "../" para tentar escrever fora de
        # PASTA_TEMP_ANEXOS.
        nome_anexo = Path(_decodificar_header(nome_anexo)).name
        # Header dobrado (RFC 2822) deixa "\r\n " no meio do nome (visto
        # em e-mail real da INBOX, 12/08) e o Windows não aceita quebra
        # de linha nem <>:"|?* em nome de arquivo.
        nome_anexo = re.sub(r"\s+", " ", nome_anexo)
        nome_anexo = re.sub(r'[<>:"|?*]', "_", nome_anexo).strip()
        if not nome_anexo or not nome_anexo.lower().endswith(".pdf"):
            continue
        conteudo = parte.get_payload(decode=True)
        if not conteudo:
            continue
        caminho_local = PASTA_TEMP_ANEXOS / f"{uid}_{nome_anexo}"
        with open(caminho_local, "wb") as f:
            f.write(conteudo)
        itens.append({
            "caminho_local": caminho_local, "nome_arquivo": nome_anexo,
            "assunto_email": assunto, "remetente_email": remetente,
            "message_id": message_id,
        })
    return itens


def _processar_uids(mail, uids: list[int], ja_vistos: set[str],
                    marcar: bool = True) -> list[dict]:
    """Pra cada UID: pula se o Message-ID já foi processado (checado
    via headers em lote), senão baixa a mensagem completa e extrai os
    PDFs. Marca o Message-ID quando algum PDF foi salvo (mesma regra
    de antes: e-mail sem PDF não é marcado). marcar=False (modo teste)
    não grava nada no banco."""
    itens_total = []
    if not uids:
        return itens_total
    message_ids = _message_ids_dos_uids(mail, uids)
    for uid in uids:
        message_id = message_ids.get(uid, "")
        if message_id and message_id in ja_vistos:
            continue
        msg = _baixar_mensagem(mail, uid)
        if msg is None:
            continue
        itens = _extrair_pdfs_anexados(msg, uid, message_id)
        if itens:
            itens_total.extend(itens)
            if marcar:
                _marcar_processado(message_id)
            if message_id:
                ja_vistos.add(message_id)
    return itens_total


# ── Busca ampla: INBOX inteira ──────────────────────────────────────────────

def buscar_pdfs_por_email(config: dict, dias_retroativos: int = 7,
                          modo_teste: bool = False) -> list[dict]:
    """
    Conecta no IMAP, procura e-mails novos (desde o cursor da execução
    anterior; janela de dias_retroativos só na primeira execução) com
    anexo PDF ainda não processado, baixa os anexos pra uma pasta
    temporária.

    modo_teste=True baixa os PDFs normalmente mas NÃO grava nada
    (nem Message-ID nem cursor) -- um teste não pode "consumir"
    e-mails que a execução real depois pularia.

    Retorna lista de {"caminho_local", "nome_arquivo", "assunto_email",
    "remetente_email", "message_id"} -- 1 item por PDF anexado (um
    e-mail pode ter vários).
    """
    cfg_email = config.get("email", {})
    usuario = cfg_email.get("remetente", "")
    senha_app = cfg_email.get("senha_app", "")

    if not usuario or not senha_app:
        logger.warning("IMAP desativado — remetente/senha_app não configurados em config.yaml.")
        return []

    PASTA_TEMP_ANEXOS.mkdir(parents=True, exist_ok=True)
    resultado = []

    try:
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=30)
        mail.login(usuario, senha_app)
        mail.select("INBOX")

        janela = _abrir_janela_busca(mail, "INBOX", dias_retroativos)
        uids = _buscar_uids(mail, janela)
        logger.info(f"INBOX: {len(uids)} e-mail(s) na janela de busca "
                   f"({'incremental' if janela['ultimo_uid'] else f'{dias_retroativos} dia(s), primeira execução'}).")

        ja_vistos = _ids_ja_processados()
        resultado = _processar_uids(mail, uids, ja_vistos, marcar=not modo_teste)

        if not modo_teste:
            _fechar_janela_busca(janela)
        mail.logout()

    except Exception as e:
        logger.exception(f"Erro ao buscar PDFs por e-mail: {e}")

    logger.info(f"{len(resultado)} PDF(s) novo(s) baixado(s) de e-mail.")
    return resultado


# ── Busca direcionada: remetentes de embarcadores conhecidos ────────────────
# Diferente de buscar_pdfs_por_email() (busca ampla, varre a INBOX inteira
# procurando qualquer PDF) -- essa busca é restrita a remetentes específicos
# de embarcadores que mandam documentos por e-mail, e olha a pasta "Todos os
# e-mails" (o Gmail organiza esse tipo de e-mail em pastas/labels por
# cliente -- confirmado com o Hugo, 10/08 -- não fica garantido estar na
# INBOX).
#
# A chave é o que vai no FROM da busca IMAP -- que casa por SUBSTRING, então
# um domínio inteiro ("@detommaso.com.br") pega qualquer remetente de lá
# (pedro@ é o mais usado, mas pode variar -- pedido do Hugo, 11/08). Mesmo
# padrão pra Vida Veg ("@vidaveg.com.br"): logistica@ é o mais usado, mas o
# time deles manda de vários endereços (adm@, logistica1@..4@, artur.neto@)
# -- confirmado nos e-mails reais, 11/08.
# "tipos" é o conjunto de tipos de documento que aquele embarcador manda e
# que o pipeline deve aproveitar (o resto vira FORA_DE_ESCOPO): Dourado e
# NUU só mandam Boleto; o De Tommaso manda as NFs do dia (PDF consolidado,
# ver documento_splitter.py) junto com os boletos, em arquivos separados. A
# Vida Veg manda um único PDF consolidado ("DANFEs_Boletos_DD-MM-AAAA.pdf")
# que MISTURA NF e boleto no mesmo arquivo -- documento_splitter.py separa
# os dois corretamente mesmo intercalados.
REMETENTES_EMBARCADORES: dict[str, dict] = {
    "escritorio@laticiniosdourado.ind.br": {"nome": "Laticínios Dourado",
                                            "tipos": {"Boleto"}},
    "faturamento@nuualimentos.com.br": {"nome": "Maria Dolores (NUU)",
                                        "tipos": {"Boleto"}},
    "@detommaso.com.br": {"nome": "De Tommaso",
                          "tipos": {"Boleto", "Nota Fiscal"}},
    "@vidaveg.com.br": {"nome": "Vida Veg",
                        "tipos": {"Boleto", "Nota Fiscal"}},
}
PASTA_TODOS_OS_EMAILS = '"[Gmail]/Todos os e-mails"'


def buscar_pdfs_por_email_embarcadores(config: dict, dias_retroativos: int = 7,
                                       modo_teste: bool = False) -> list[dict]:
    """
    Busca PDFs anexados (soltos -- sem ZIP, fora de escopo por
    enquanto, ver pedido do Hugo 10/08) em e-mails novos (desde o
    cursor; janela de dias_retroativos só na primeira execução) vindos
    dos REMETENTES_EMBARCADORES acima. Mesmo formato de retorno de
    buscar_pdfs_por_email(), com um campo extra "tipos_permitidos"
    (os tipos que o embarcador daquele remetente manda) -- alimenta o
    mesmo pipeline depois. modo_teste: ver buscar_pdfs_por_email().

    O cursor é da CAIXA ("Todos os e-mails"), compartilhado pelos
    remetentes: a mesma janela UID é buscada uma vez por remetente
    (com FROM diferente) e fechada uma vez só, no final.
    """
    cfg_email = config.get("email", {})
    usuario = cfg_email.get("remetente", "")
    senha_app = cfg_email.get("senha_app", "")
    if not usuario or not senha_app:
        logger.warning("IMAP desativado — remetente/senha_app não configurados em config.yaml.")
        return []

    PASTA_TEMP_ANEXOS.mkdir(parents=True, exist_ok=True)
    resultado = []

    try:
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=30)
        mail.login(usuario, senha_app)
        mail.select(PASTA_TODOS_OS_EMAILS)

        janela = _abrir_janela_busca(mail, PASTA_TODOS_OS_EMAILS, dias_retroativos)
        ja_vistos = _ids_ja_processados()

        for remetente, cfg_embarcador in REMETENTES_EMBARCADORES.items():
            nome_embarcador = cfg_embarcador["nome"]
            tipos_permitidos = cfg_embarcador["tipos"]
            try:
                uids = _buscar_uids(mail, janela, criterio_extra=f'FROM "{remetente}"')
            except RuntimeError as e:
                logger.warning(str(e))
                continue

            logger.info(f"  {nome_embarcador} ({remetente}): {len(uids)} e-mail(s) na janela de busca.")

            itens = _processar_uids(mail, uids, ja_vistos, marcar=not modo_teste)
            for item in itens:
                item["tipos_permitidos"] = tipos_permitidos
            resultado.extend(itens)

        if not modo_teste:
            _fechar_janela_busca(janela)
        mail.logout()

    except Exception as e:
        logger.exception(f"Erro ao buscar PDFs de embarcadores conhecidos: {e}")

    logger.info(f"{len(resultado)} PDF(s) novo(s) baixado(s) de embarcadores conhecidos.")
    return resultado
