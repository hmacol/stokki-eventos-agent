"""
ler_respostas_agendamento.py
Lê a caixa de entrada do Gmail (via IMAP), identifica respostas a e-mails
de confirmação de AGENDAMENTO de entrega (data + horário por pedido
específico), usa a API da Anthropic (Claude) para extrair a informação
do texto livre, e atualiza o pedido correspondente.

Portado do agente_relatorio (mesmo arquivo, mesma lógica de matching
por marcador oculto / cnpj_embarcador / e-mail do remetente — só o
DB_PATH foi adaptado pra estrutura deste projeto). Trabalha em conjunto
com agendamento_confirmacao.py (que envia a solicitação) — este arquivo
só lê e processa as respostas.

Identifica pelo PEDIDO (não pelo CNPJ do destinatário) — já que o
mesmo cliente pode ter agendamentos diferentes em pedidos diferentes.
Atualiza a tabela agendamentos_pedido (ver adicionar_agendamento.py).

COMO USAR (standalone — normalmente chamado a partir de executar_tudo.py):
    py -3.11 ler_respostas_agendamento.py
"""

import email
import imaplib
import json
import logging
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from email.header import decode_header
from pathlib import Path

import requests
import yaml

_RAIZ = Path(__file__).parent

logger = logging.getLogger(__name__)

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
DB_PATH = Path(__file__).parent / "dados" / "dados.db"


def _fetch_em_lote(mail, ids: list, parte: str, tamanho_lote: int = 50) -> dict:
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


def _ja_processado(message_id: str, origem: str = "AGENDAMENTO") -> bool:
    """Verifica se este e-mail (pelo Message-ID) já foi processado anteriormente por este leitor."""
    if not message_id:
        return False
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT 1 FROM emails_processados_respostas WHERE message_id = ? AND origem = ?",
        (message_id, origem),
    ).fetchone()
    conn.close()
    return row is not None


def _marcar_processado(message_id: str, remetente_email: str, origem: str = "AGENDAMENTO"):
    """Registra este e-mail como processado, para não reprocessar em execuções futuras."""
    if not message_id:
        return
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR IGNORE INTO emails_processados_respostas (message_id, origem, remetente_email) VALUES (?, ?, ?)",
        (message_id, origem, remetente_email),
    )
    conn.commit()
    conn.close()


def _remover_acentos(texto: str) -> str:
    substituicoes = str.maketrans(
        "áàâãäéèêëíìîïóòôõöúùûüçñÁÀÂÃÄÉÈÊËÍÌÎÏÓÒÔÕÖÚÙÛÜÇÑ",
        "aaaaaeeeeiiiiooooouuuucnAAAAAEEEEIIIIOOOOOUUUUCN",
    )
    return texto.translate(substituicoes)


def _decodificar_header(valor) -> str:
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


def _remover_texto_citado(corpo: str) -> str:
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


def _extrair_texto_corpo(msg) -> str:
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
                    html = parte.get_payload(decode=True).decode(
                        parte.get_content_charset() or "utf-8", errors="ignore"
                    )
                    return re.sub(r"<[^>]+>", " ", html)
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


def _extrair_pedido_do_corpo(corpo: str) -> str:
    """
    Procura o marcador oculto [[PEDIDO:PS-XXXXX]] no corpo (quoted text).
    '#' opcional na frente do código — o agente_relatorio usa códigos
    com '#' (ex: #PS-XXXXX), mas este projeto (agente_stokki_eventos)
    NUNCA usa '#' no codigo_ps (extrair_codigo_ps_da_linha sempre monta
    "PS-{id}" puro) — sem essa flexibilização, o marcador que a gente
    mesma coloca no e-mail nunca seria reconhecido de volta.
    """
    match = re.search(r"\[\[PEDIDO:(#?PS-[\w.]+)\]\]", corpo)
    return match.group(1) if match else ""


def _extrair_embarcador_do_corpo(corpo: str) -> str:
    match = re.search(r"\[\[AGENTE_AGENDAMENTO_EMBARCADOR:(\d+)\]\]", corpo)
    return match.group(1) if match else ""


def _extrair_agendamento_via_claude(texto_resposta: str, nome_cliente: str, pedido: str, api_key: str) -> dict:
    """
    Usa a API da Anthropic para extrair data + horário de entrega do texto
    livre da resposta do embarcador, referente a um pedido específico.

    Retorna dict: {"data": "DD/MM/YYYY", "inicio": "HH:MM", "fim": "HH:MM", "nao_entendido": bool}
    """
    hoje = datetime.now().strftime("%d/%m/%Y")
    prompt = f"""Você vai analisar a resposta de um embarcador a um e-mail de confirmação de AGENDAMENTO DE ENTREGA do pedido "{pedido}" do cliente "{nome_cliente}".

A data de hoje é {hoje}. Se o embarcador mencionar apenas o dia (ex: "dia 25"), assuma o mês/ano correntes ou o próximo mais próximo se a data já tiver passado.

Resposta do embarcador:
\"\"\"
{texto_resposta.strip()[:2000]}
\"\"\"

Extraia a DATA e o HORÁRIO de entrega agendados para esse pedido. Se o embarcador mencionar só um horário (sem intervalo), use o mesmo valor para início e fim, ou um intervalo razoável de 1 hora se fizer sentido pelo contexto.

Responda APENAS com um JSON válido neste formato exato, sem texto antes ou depois:
{{"data": "DD/MM/YYYY", "inicio": "HH:MM", "fim": "HH:MM", "nao_entendido": false}}

Se não conseguir identificar uma data e horário claros na resposta, retorne: {{"data": "", "inicio": "", "fim": "", "nao_entendido": true}}
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
                "max_tokens": 200,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        texto_resposta_ia = resp.json()["content"][0]["text"].strip()
        texto_resposta_ia = re.sub(r"^```json\s*|\s*```$", "", texto_resposta_ia.strip())
        return json.loads(texto_resposta_ia)

    except Exception as e:
        logger.error(f"Erro ao extrair agendamento via Claude: {e}")
        return {"data": "", "inicio": "", "fim": "", "nao_entendido": True}


def _atualizar_agendamento(pedido_id: int, data: str, inicio: str, fim: str, resposta_texto: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        UPDATE agendamentos_pedido
        SET status = 'RESPONDIDO', data_agendada = ?, horario_inicio_agendado = ?,
            horario_fim_agendado = ?, resposta_texto = ?, respondido_em = datetime('now','localtime')
        WHERE id = ?
    """, (data, inicio, fim, resposta_texto[:1000], pedido_id))
    conn.commit()
    conn.close()


def processar_respostas_agendamento(config: dict) -> dict:
    """
    Conecta no Gmail via IMAP, busca e-mails não lidos que sejam respostas
    de confirmação de agendamento, extrai data/horário via Claude e atualiza
    a tabela agendamentos_pedido (não o cadastro de clientes).

    Retorna {"processados": int, "atualizados": int, "nao_entendidos": int}
    """
    cfg_email = config.get("email", {})
    cfg_anthropic = config.get("anthropic", {})

    usuario_imap = cfg_email.get("remetente", "")
    senha_app    = cfg_email.get("senha_app", "")
    api_key      = cfg_anthropic.get("api_key", "")

    if not usuario_imap or not senha_app:
        logger.warning("IMAP desativado — remetente/senha_app não configurados em config.yaml.")
        return {"processados": 0, "atualizados": 0, "nao_entendidos": 0}

    if not api_key or api_key == "SUA_CHAVE_AQUI":
        logger.warning("Chave da API Anthropic não configurada em config.yaml (seção anthropic).")
        return {"processados": 0, "atualizados": 0, "nao_entendidos": 0}

    processados    = 0
    atualizados    = 0
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
            return {"processados": 0, "atualizados": 0, "nao_entendidos": 0}

        ids = dados[0].split()
        logger.info(f"E-mails encontrados nos últimos {dias_retroativos} dia(s): {len(ids)} (filtrando por assunto a seguir)")

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
            if "agendamento de entrega" not in assunto_normalizado:
                continue

            message_id = msg_header.get("Message-ID", "")
            if _ja_processado(message_id):
                continue  # já processado em execução anterior — evita duplicar

            ids_para_processar.append(msg_id)

        corpos_por_id = _fetch_em_lote(mail, ids_para_processar, "(RFC822)") if ids_para_processar else {}

        for msg_id in ids_para_processar:
            msg_id_str = msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)
            entrada = corpos_por_id.get(msg_id_str)
            if not entrada:
                continue
            try:
                msg = email.message_from_bytes(entrada[1])
            except Exception as e:
                logger.warning(f"Falha ao ler e-mail {msg_id}: {e} — pulando.")
                continue

            assunto = _decodificar_header(msg.get("Subject", ""))

            remetente_raw = _decodificar_header(msg.get("From", ""))
            remetente_email = re.search(r"[\w\.\-+]+@[\w\.\-]+", remetente_raw)
            remetente_email = remetente_email.group(0).lower() if remetente_email else ""

            eh_resposta = bool(re.match(r"^\s*(re|res|fwd|fw)\s*:", assunto, re.IGNORECASE))
            nosso_email = (cfg_email.get("remetente", "") or "").lower()
            if remetente_email == nosso_email and not eh_resposta:
                continue

            corpo = _extrair_texto_corpo(msg)
            corpo_sem_citacao = _remover_texto_citado(corpo)
            processados += 1

            pedido_thread = _extrair_pedido_do_corpo(corpo)
            cnpj_emb = _extrair_embarcador_do_corpo(corpo)

            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row

            agendamento = None
            if pedido_thread:
                agendamento = conn.execute("""
                    SELECT * FROM agendamentos_pedido
                    WHERE pedido = ? AND status = 'PENDENTE'
                    ORDER BY id DESC LIMIT 1
                """, (pedido_thread,)).fetchone()

            if not agendamento and cnpj_emb:
                agendamento = conn.execute("""
                    SELECT * FROM agendamentos_pedido
                    WHERE cnpj_embarcador = ? AND status = 'PENDENTE'
                    ORDER BY id DESC LIMIT 1
                """, (cnpj_emb,)).fetchone()

            if not agendamento:
                row = conn.execute("""
                    SELECT * FROM agendamentos_pedido
                    WHERE email_embarcador LIKE ? AND status = 'PENDENTE'
                    ORDER BY id DESC LIMIT 1
                """, (f"%{remetente_email}%",)).fetchone()
                agendamento = row

            conn.close()

            if not agendamento:
                logger.info(f"Nenhum agendamento pendente encontrado para o e-mail de {remetente_email} — ignorando.")
                _marcar_processado(message_id, remetente_email)
                continue

            agendamento = dict(agendamento)
            resultado_ia = _extrair_agendamento_via_claude(
                corpo_sem_citacao, agendamento["nome_destinatario"], agendamento["pedido"], api_key
            )

            if resultado_ia.get("nao_entendido") or not resultado_ia.get("data"):
                logger.warning(
                    f"Não foi possível extrair data/horário da resposta de {remetente_email} "
                    f"para o pedido {agendamento['pedido']}."
                )
                nao_entendidos += 1
                _marcar_processado(message_id, remetente_email)
                continue

            data_agendada = resultado_ia["data"]
            inicio        = resultado_ia.get("inicio", "") or "08:00"
            fim           = resultado_ia.get("fim", "") or "18:00"

            _atualizar_agendamento(agendamento["id"], data_agendada, inicio, fim, corpo_sem_citacao)
            atualizados += 1
            logger.info(
                f"✅ Agendamento confirmado: pedido {agendamento['pedido']} "
                f"({agendamento['nome_destinatario']}) -> {data_agendada} {inicio}-{fim}"
            )

            _marcar_processado(message_id, remetente_email)

        mail.logout()

    except Exception as e:
        logger.exception(f"Erro ao processar respostas de agendamento: {e}")

    logger.info(
        f"Leitura de respostas de agendamento concluída: {processados} processado(s), "
        f"{atualizados} pedido(s) atualizado(s), {nao_entendidos} não entendido(s)."
    )
    return {"processados": processados, "atualizados": atualizados, "nao_entendidos": nao_entendidos}


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
            logging.FileHandler(_RAIZ / "dados" / "ler_respostas_agendamento.log", encoding="utf-8"),
        ],
    )
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    resultado = processar_respostas_agendamento(config)
    logger.info(f"Resultado: {resultado}")


if __name__ == "__main__":
    main()
