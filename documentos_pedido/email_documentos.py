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
"""
import email
import imaplib
import logging
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


def _conectar_controle():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS documentos_emails_processados (
            message_id     TEXT PRIMARY KEY,
            processado_em  TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def _ja_processado(message_id: str) -> bool:
    if not message_id:
        return False
    conn = _conectar_controle()
    row = conn.execute(
        "SELECT 1 FROM documentos_emails_processados WHERE message_id = ?", (message_id,)
    ).fetchone()
    conn.close()
    return row is not None


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


def buscar_pdfs_por_email(config: dict, dias_retroativos: int = 7) -> list[dict]:
    """
    Conecta no IMAP, procura e-mails recentes com anexo PDF ainda não
    processados, baixa os anexos pra uma pasta temporária.

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

        data_limite = (datetime.now() - timedelta(days=dias_retroativos)).strftime("%d-%b-%Y")
        status, dados = mail.search(None, f"(SINCE {data_limite})")
        if status != "OK":
            logger.warning("Falha ao buscar e-mails no IMAP.")
            return []

        ids = dados[0].split()
        logger.info(f"E-mails encontrados nos últimos {dias_retroativos} dia(s): {len(ids)}")

        for msg_id in ids:
            status_fetch, dados_msg = mail.fetch(msg_id, "(RFC822)")
            if status_fetch != "OK" or not dados_msg or not dados_msg[0]:
                continue
            try:
                msg = email.message_from_bytes(dados_msg[0][1])
            except Exception:
                continue

            message_id = msg.get("Message-ID", "")
            if _ja_processado(message_id):
                continue

            assunto = _decodificar_header(msg.get("Subject", ""))
            remetente = _decodificar_header(msg.get("From", ""))

            algum_pdf_nesse_email = False
            if msg.is_multipart():
                for parte in msg.walk():
                    nome_anexo = parte.get_filename()
                    if not nome_anexo or not nome_anexo.lower().endswith(".pdf"):
                        continue
                    nome_anexo = _decodificar_header(nome_anexo)
                    conteudo = parte.get_payload(decode=True)
                    if not conteudo:
                        continue

                    algum_pdf_nesse_email = True
                    caminho_local = PASTA_TEMP_ANEXOS / f"{msg_id.decode()}_{nome_anexo}"
                    with open(caminho_local, "wb") as f:
                        f.write(conteudo)

                    resultado.append({
                        "caminho_local": caminho_local, "nome_arquivo": nome_anexo,
                        "assunto_email": assunto, "remetente_email": remetente,
                        "message_id": message_id,
                    })

            if algum_pdf_nesse_email:
                _marcar_processado(message_id)

        mail.logout()

    except Exception as e:
        logger.exception(f"Erro ao buscar PDFs por e-mail: {e}")

    logger.info(f"{len(resultado)} PDF(s) novo(s) baixado(s) de e-mail.")
    return resultado
