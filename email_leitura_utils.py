# -*- coding: utf-8 -*-
"""
email_leitura_utils.py

Helpers de leitura/parsing de e-mail via IMAP compartilhados entre
ler_respostas_agendamento.py e ler_respostas_insucesso.py -- extraídos
daqui porque as duas cópias já tinham divergido (a segunda perdeu o
fetch em lote da primeira e ficou fazendo 1 round-trip IMAP por
e-mail, bem mais lento pra caixas com muitos e-mails).
"""
import re
from email.header import decode_header


def fetch_em_lote(mail, ids: list, parte: str, tamanho_lote: int = 50) -> dict:
    """
    Busca múltiplos e-mails de uma vez, em lotes (em vez de um fetch por
    e-mail) — reduz drasticamente o número de round-trips de rede ao
    servidor IMAP, que é o principal gargalo de performance ao processar
    centenas de e-mails (cada round-trip tem latência fixa, independente
    do tamanho do conteúdo transferido).

    Extrai o ID real de cada resposta a partir do próprio cabeçalho
    retornado pelo servidor (formato "N (BODY[...] {tamanho}") — NÃO
    confia na posição/ordem das respostas, já que se algum e-mail do
    lote falhar ou for ignorado pelo servidor, a quantidade de respostas
    pode ser menor que a solicitada, e pareamento posicional associaria
    o conteúdo ao ID errado silenciosamente.

    Retorna um dict {msg_id: dados_brutos} só para os e-mails encontrados.
    """
    resultado = {}
    ids_str = [i.decode() if isinstance(i, bytes) else str(i) for i in ids]

    for inicio in range(0, len(ids_str), tamanho_lote):
        lote = ids_str[inicio:inicio + tamanho_lote]
        ids_lote_str = ",".join(lote)
        try:
            status, dados = mail.fetch(ids_lote_str, parte)
        except Exception:
            continue
        if status != "OK":
            continue

        for entrada in dados:
            if not isinstance(entrada, tuple) or not entrada[0]:
                continue
            match = re.match(rb"^(\d+)\s*\(", entrada[0])
            if not match:
                continue
            msg_id_real = match.group(1).decode()
            resultado[msg_id_real] = entrada

    return resultado


def remover_acentos(texto: str) -> str:
    substituicoes = str.maketrans(
        "áàâãäéèêëíìîïóòôõöúùûüçñÁÀÂÃÄÉÈÊËÍÌÎÏÓÒÔÕÖÚÙÛÜÇÑ",
        "aaaaaeeeeiiiiooooouuuucnAAAAAEEEEIIIIOOOOOUUUUCN",
    )
    return texto.translate(substituicoes)


def decodificar_header(valor) -> str:
    if not valor:
        return ""
    partes = decode_header(valor)
    resultado = ""
    for texto, encoding in partes:
        if isinstance(texto, bytes):
            enc = encoding or "utf-8"
            try:
                resultado += texto.decode(enc, errors="ignore")
            except (LookupError, UnicodeDecodeError):
                resultado += texto.decode("latin-1", errors="ignore")
        else:
            resultado += texto
    return resultado


def remover_texto_citado(corpo: str) -> str:
    padroes_corte = [
        r"\nEm .{0,80}escreveu:",
        r"\n_{5,}",
        r"\nDe:\s.{0,80}\nEnviado:",
        r"\n>{1,}",
        r"\n-{2,}\s*Mensagem original",
    ]
    texto = corpo
    for padrao in padroes_corte:
        match = re.search(padrao, texto, re.IGNORECASE)
        if match:
            texto = texto[:match.start()]
    return texto.strip()


def extrair_texto_corpo(msg) -> str:
    if msg.is_multipart():
        for parte in msg.walk():
            if parte.get_content_type() == "text/plain":
                try:
                    return parte.get_payload(decode=True).decode(
                        parte.get_content_charset() or "utf-8", errors="ignore"
                    )
                except Exception:
                    continue
        for parte in msg.walk():
            if parte.get_content_type() == "text/html":
                try:
                    html_texto = parte.get_payload(decode=True).decode(
                        parte.get_content_charset() or "utf-8", errors="ignore"
                    )
                    return re.sub(r"<[^>]+>", " ", html_texto)
                except Exception:
                    continue
        return ""
    else:
        try:
            return msg.get_payload(decode=True).decode(
                msg.get_content_charset() or "utf-8", errors="ignore"
            )
        except Exception:
            return str(msg.get_payload())
