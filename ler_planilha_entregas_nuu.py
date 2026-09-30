# -*- coding: utf-8 -*-
"""
ler_planilha_entregas_nuu.py

Lê a planilha ENTREGAS.xlsx que a NUU Alimentos (Maria Dolores) manda
por e-mail em dias úteis (assunto "ENTREGAS NUU ALIMENTOS SP DD.MM"),
extrai por Nota Fiscal: se já há uma data de agendamento confirmada
(coluna AGENDA) e se há endereço de entrega diferente do fiscal (coluna
OBSERVAÇÃO) -- pedido do Hugo, 11/08 ("dá pra puxar esses relatórios
pra já processar os próximos e-mails automaticamente?").

Formato real confirmado por amostra (11/08): aba com cabeçalho CD,
Estado Origem, Destino, Número Nota, Cliente, Boleto, AGENDA,
OBSERVAÇÃO. AGENDA é texto livre ("AGENDADO 13/08", "agendado 13.08",
"URGENTE - NEMA", "X" = sem data). OBSERVAÇÃO é texto livre, às vezes
com "Endereço de Entrega: ..." quando difere do cadastro fiscal.

Diferente do fluxo de agendamento_confirmacao.py / ler_respostas_
agendamento.py (que PEDE confirmação por e-mail e lê a resposta em
texto livre via IA): aqui o embarcador já manda a informação
PROATIVAMENTE numa planilha semiestruturada -- não precisa pedir nada,
só ler e casar com o pedido certo.

Fluxo:
  1. Busca e-mails dos remetentes conhecidos (REMETENTES_PLANILHA_
     ENTREGAS) com assunto "ENTREGAS" (não "DESCARGA" -- mesmo
     remetente, outro tipo de e-mail, sem essa planilha) e anexo
     .xlsx/.xls, na pasta "Todos os e-mails" (mesmo padrão de
     email_documentos.py -- esses e-mails nem sempre caem na INBOX).
  2. Pra cada linha (1 por Nota Fiscal): extrai Número da Nota,
     Cliente, texto bruto de AGENDA e de OBSERVAÇÃO.
  3. Casa a Nota Fiscal com o pedido (PS-XXXXX) no VUUPT pela mesma
     regra "referência no título" já usada em documentos_pedido/
     matcher.py (regra 3b) -- só aceita casamento SEM ambiguidade.
  4. Se AGENDA tiver uma data reconhecível, grava em agendamentos_pedido
     como já CONFIRMADO (status='RESPONDIDO', origem='PLANILHA_NUU') --
     é a MESMA tabela que pipeline.py consulta (buscar_confirmacao) ao
     criar o serviço no VUUPT, e que atualizar_agendamentos_confirmados.py
     aplica em serviços já existentes -- não duplica essa lógica aqui.
     Uma confirmação já recebida por e-mail (resposta humana real) NUNCA
     é sobrescrita pela planilha.
  5. Se OBSERVAÇÃO tiver um endereço de entrega diferente do fiscal,
     registra em entregas_nuu_planilha (NÃO aplica sozinho no VUUPT --
     endereço errado manda caminhão pro lugar errado, exige conferência
     humana) e manda 1 e-mail resumo pro time de operação só com o que
     for NOVO/diferente desde a última planilha lida (evita notificar a
     mesma observação todo dia até a NF sair do relatório da NUU).

COMO USAR (normalmente chamado a partir de executar_tudo.py, na mesma
etapa de ler_respostas_agendamento.py -- ANTES do Pipeline, pra
pipeline.py já enxergar a confirmação na mesma execução):
    py -3.11 ler_planilha_entregas_nuu.py
    py -3.11 ler_planilha_entregas_nuu.py --modo-teste
"""
import argparse
import email
import html
import imaplib
import logging
import re
import sqlite3
import sys
import unicodedata
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path

_RAIZ = Path(__file__).parent
sys.path.insert(0, str(_RAIZ))

import openpyxl
import yaml

from email_leitura_utils import decodificar_header as _decodificar_header, remover_acentos as _remover_acentos
from email_utils import (enviar_email, envelope_html, COR_PRIMARIA, COR_PRIMARIA_CLARA,
                         COR_TEXTO, COR_BORDA, COR_ERRO)
from vuupt_client import VuuptClient

logger = logging.getLogger(__name__)

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
PASTA_TODOS_OS_EMAILS = '"[Gmail]/Todos os e-mails"'
DB_PATH = _RAIZ / "dados" / "dados.db"
EMAIL_OPERACAO = "hugo@freshlogbr.com"

# Remetentes conhecidos que mandam a planilha ENTREGAS -- por enquanto só
# a NUU/Maria Dolores (pedido do Hugo, 11/08); outro embarcador que adotar
# o mesmo formato entra aqui depois.
REMETENTES_PLANILHA_ENTREGAS = ["faturamento@nuualimentos.com.br"]

COLUNAS_NF = {"NUMERONOTA", "NUMERONF", "NOTA", "NF"}
COLUNAS_CLIENTE = {"CLIENTE"}
COLUNAS_BOLETO = {"BOLETO"}
COLUNAS_AGENDA = {"AGENDA"}
COLUNAS_OBSERVACAO = {"OBSERVACAO", "OBSERVACOES"}


def _normalizar_cabecalho(s) -> str:
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^A-Z0-9]", "", s.upper())


# ── Controle de e-mails/tabelas ──────────────────────────────────────────
def _garantir_tabelas():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entregas_nuu_emails_processados (
            message_id     TEXT PRIMARY KEY,
            processado_em  TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entregas_nuu_planilha (
            numero_nf                  TEXT PRIMARY KEY,
            cliente                    TEXT,
            boleto_texto               TEXT,
            agenda_bruto                TEXT,
            data_agendamento_extraida  TEXT,
            observacao_bruto           TEXT,
            endereco_extraido          TEXT,
            codigo_pedido              TEXT,
            metodo_casamento           TEXT,
            message_id_origem          TEXT,
            primeira_vez_em            TEXT DEFAULT (datetime('now','localtime')),
            atualizado_em              TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    # Coluna nova em tabela já existente (agendamentos_pedido, ver
    # adicionar_agendamento.py) -- ALTER TABLE seguro pra rodar quantas
    # vezes for preciso, mesmo padrão de atualizar_agendamentos_confirmados.py.
    try:
        conn.execute("ALTER TABLE agendamentos_pedido ADD COLUMN origem TEXT")
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            raise
    # Migração 09/09: a planilha nunca informou hora, mas até aqui todo
    # registro dela saía com 08:00-18:00 chumbado. Agora hora não
    # informada fica NULL (a roteirização passou a respeitar a janela e
    # não pode tratar chute como janela real). Idempotente: só zera o
    # par chumbado de origem PLANILHA_NUU; hora real extraída do texto
    # da AGENDA (extrair_hora_agenda) nunca é 08:00-18:00 exatos.
    conn.execute("""
        UPDATE agendamentos_pedido SET horario_inicio_agendado = NULL, horario_fim_agendado = NULL
        WHERE origem = 'PLANILHA_NUU' AND horario_inicio_agendado = '08:00' AND horario_fim_agendado = '18:00'
    """)
    conn.commit()
    conn.close()


def _ja_processado_email(message_id: str) -> bool:
    if not message_id:
        return False
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT 1 FROM entregas_nuu_emails_processados WHERE message_id = ?", (message_id,)
    ).fetchone()
    conn.close()
    return row is not None


def _marcar_processado_email(message_id: str):
    if not message_id:
        return
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR IGNORE INTO entregas_nuu_emails_processados (message_id, processado_em) VALUES (?, ?)",
        (message_id, agora),
    )
    conn.commit()
    conn.close()


# ── Download dos e-mails/planilhas ───────────────────────────────────────
def buscar_planilhas_entregas(config: dict, dias_retroativos: int = 5) -> list[dict]:
    """
    Conecta no IMAP e baixa o(s) ENTREGAS.xlsx/.xls mais recente(s)
    ainda não processado(s), dos remetentes conhecidos. Ignora e-mails
    de "DESCARGA" (mesmo remetente, sem essa planilha).

    Retorna lista de {"conteudo": bytes, "nome_arquivo", "assunto",
    "message_id"}.
    """
    cfg_email = config.get("email", {})
    usuario = cfg_email.get("remetente", "")
    senha_app = cfg_email.get("senha_app", "")
    if not usuario or not senha_app:
        logger.warning("IMAP desativado -- remetente/senha_app não configurados em config.yaml.")
        return []

    resultado = []
    try:
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=30)
        mail.login(usuario, senha_app)
        mail.select(PASTA_TODOS_OS_EMAILS)

        data_limite = (datetime.now() - timedelta(days=dias_retroativos)).strftime("%d-%b-%Y")

        for remetente in REMETENTES_PLANILHA_ENTREGAS:
            status, dados = mail.search(None, f'(FROM "{remetente}" SINCE {data_limite})')
            if status != "OK":
                logger.warning(f"Falha ao buscar e-mails de {remetente!r}.")
                continue

            ids = dados[0].split()
            for msg_id in ids:
                status_fetch, dados_msg = mail.fetch(msg_id, "(RFC822)")
                if status_fetch != "OK" or not dados_msg or not dados_msg[0]:
                    continue
                try:
                    msg = email.message_from_bytes(dados_msg[0][1])
                except Exception:
                    continue

                message_id = msg.get("Message-ID", "")
                if _ja_processado_email(message_id):
                    continue

                assunto = _decodificar_header(msg.get("Subject", ""))
                assunto_norm = assunto.upper()
                if "ENTREGAS" not in assunto_norm or "DESCARGA" in assunto_norm:
                    _marcar_processado_email(message_id)  # não é o tipo de e-mail que interessa
                    continue

                achou_planilha = False
                if msg.is_multipart():
                    for parte in msg.walk():
                        nome_anexo = parte.get_filename()
                        if not nome_anexo or not nome_anexo.lower().endswith((".xlsx", ".xls")):
                            continue
                        nome_anexo = _decodificar_header(nome_anexo)
                        conteudo = parte.get_payload(decode=True)
                        if not conteudo:
                            continue
                        resultado.append({
                            "conteudo": conteudo, "nome_arquivo": nome_anexo,
                            "assunto": assunto, "message_id": message_id,
                        })
                        achou_planilha = True

                _marcar_processado_email(message_id)
                if not achou_planilha:
                    logger.info(f"  E-mail '{assunto}' sem planilha .xlsx/.xls anexada -- ignorado.")

        mail.logout()
    except Exception as e:
        logger.exception(f"Erro ao buscar planilhas de entregas por e-mail: {e}")

    logger.info(f"{len(resultado)} planilha(s) nova(s) de entregas encontrada(s) por e-mail.")
    return resultado


# ── Parsing da planilha ──────────────────────────────────────────────────
def parsear_planilha(conteudo: bytes) -> list[dict]:
    """
    Lê a aba com os dados por Nota Fiscal (identificada por ter uma
    coluna de Número da Nota no cabeçalho -- a outra aba do arquivo real
    é só uma legenda de texto, sem essa coluna). Retorna 1 dict por
    linha de dado: {"numero_nf", "cliente", "boleto_texto",
    "agenda_bruto", "observacao_bruto"}.
    """
    wb = openpyxl.load_workbook(BytesIO(conteudo), data_only=True)

    aba_dados = None
    indices = None
    for nome_aba in wb.sheetnames:
        ws = wb[nome_aba]
        try:
            linha_cabecalho = next(ws.iter_rows(min_row=1, max_row=1))
        except StopIteration:
            continue
        cabecalho = [_normalizar_cabecalho(c.value) for c in linha_cabecalho]
        if any(c in COLUNAS_NF for c in cabecalho):
            aba_dados = ws
            indices = {
                "nf": next((i for i, c in enumerate(cabecalho) if c in COLUNAS_NF), None),
                "cliente": next((i for i, c in enumerate(cabecalho) if c in COLUNAS_CLIENTE), None),
                "boleto": next((i for i, c in enumerate(cabecalho) if c in COLUNAS_BOLETO), None),
                "agenda": next((i for i, c in enumerate(cabecalho) if c in COLUNAS_AGENDA), None),
                "observacao": next((i for i, c in enumerate(cabecalho) if c in COLUNAS_OBSERVACAO), None),
            }
            break

    if aba_dados is None:
        logger.warning("Nenhuma aba com coluna de Número da Nota encontrada nesta planilha -- pulando.")
        return []

    def _valor(linha, idx):
        return linha[idx] if idx is not None and idx < len(linha) else None

    linhas_saida = []
    for linha in aba_dados.iter_rows(min_row=2, values_only=True):
        if linha is None or all(v is None for v in linha):
            continue
        digitos_nf = re.sub(r"\D", "", str(_valor(linha, indices["nf"]) or ""))
        numero_nf = digitos_nf.lstrip("0") or digitos_nf
        if not numero_nf:
            continue  # linha sem NF -- não dá pra casar com pedido nenhum

        linhas_saida.append({
            "numero_nf": numero_nf,
            "cliente": str(_valor(linha, indices["cliente"]) or "").strip(),
            "boleto_texto": str(_valor(linha, indices["boleto"]) or "").strip(),
            "agenda_bruto": str(_valor(linha, indices["agenda"]) or "").strip(),
            "observacao_bruto": str(_valor(linha, indices["observacao"]) or "").strip(),
        })

    return linhas_saida


_MESES_PT = {
    "jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
    "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12,
}


def _rolar_para_futuro_se_muito_antiga(data_extraida: date, hoje: date) -> date:
    """Sem ano explícito na planilha (ex: '13/08' ou '11 de agosto'), se
    a data cair mais de 20 dias no passado é porque o ano virou -- a
    planilha é sempre sobre entregas futuras/recentes, nunca do ano
    passado."""
    if data_extraida < hoje - timedelta(days=20):
        try:
            return date(data_extraida.year + 1, data_extraida.month, data_extraida.day)
        except ValueError:
            pass
    return data_extraida


def _data_sem_ano_e_plausivel(data_extraida: date, hoje: date) -> bool:
    """Sem ano explícito, o chute (ano atual ou virada pra o próximo) só
    é confiável perto de hoje -- a NUU nunca agenda com mais de ~2 meses
    de antecedência. Um resultado fora dessa janela (ex: 'AGENDADO
    24/04' lido em agosto virando 24/04/2027 pela virada de ano) é mais
    provável erro de digitação na planilha (mês/dia trocado) do que uma
    data real -- falha fechada é melhor que aplicar uma data absurda
    como se fosse confirmada."""
    return hoje - timedelta(days=20) <= data_extraida <= hoje + timedelta(days=60)


def extrair_data_agenda(texto: str, hoje: date | None = None) -> str | None:
    """
    Extrai DD/MM/YYYY de um texto livre de agendamento. Cobre os 3
    formatos reais observados na planilha da NUU: 'AGENDADO 13/08' ou
    'agendado 13.08' (numérico, sem ano), '2026-08-12 00:00:00' (célula
    Excel com tipo Data -- vira ISO ao ler como texto), e '11 de Agosto'
    (data por extenso). Sem data reconhecível (ex: 'X', 'URGENTE -
    NEMA', vazio) -- retorna None (não é tratado como confirmado; falha
    fechada é melhor que arriscar aplicar data errada).
    """
    hoje = hoje or date.today()
    texto = texto or ""

    m_iso = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", texto)
    if m_iso:
        ano, mes, dia = int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3))
        try:
            return date(ano, mes, dia).strftime("%d/%m/%Y")
        except ValueError:
            return None

    m_extenso = re.search(r"(\d{1,2})\s+de\s+([A-Za-zçÇ]+)", texto, re.IGNORECASE)
    if m_extenso:
        dia = int(m_extenso.group(1))
        chave_mes = _remover_acentos(m_extenso.group(2)).lower()[:3]
        mes = _MESES_PT.get(chave_mes)
        if mes and 1 <= dia <= 31:
            try:
                resultado = _rolar_para_futuro_se_muito_antiga(date(hoje.year, mes, dia), hoje)
            except ValueError:
                return None
            if not _data_sem_ano_e_plausivel(resultado, hoje):
                return None
            return resultado.strftime("%d/%m/%Y")

    m = re.search(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?", texto)
    if not m:
        return None
    dia, mes = int(m.group(1)), int(m.group(2))
    if not (1 <= dia <= 31 and 1 <= mes <= 12):
        return None
    ano_bruto = m.group(3)
    if ano_bruto:
        ano = int(ano_bruto) + 2000 if len(ano_bruto) == 2 else int(ano_bruto)
        try:
            return date(ano, mes, dia).strftime("%d/%m/%Y")
        except ValueError:
            return None
    try:
        resultado = _rolar_para_futuro_se_muito_antiga(date(hoje.year, mes, dia), hoje)
    except ValueError:
        return None
    if not _data_sem_ano_e_plausivel(resultado, hoje):
        return None
    return resultado.strftime("%d/%m/%Y")


# Hora dentro do texto da coluna AGENDA (09/09 -- até então a hora era
# CHUTADA como 08:00-18:00 pra todo agendamento da planilha, e a
# roteirização passou a respeitar a janela de horário; um chute
# gravado como se fosse janela real distorceria as rotas). Padrões:
# "às 14h", "14:30", "14h30", "entre 8h e 12h", "8h às 12h", "até 11h",
# "após 14h"/"a partir das 14h", "manhã", "tarde". Nada disso no texto
# -> None (hora NÃO informada, fica NULL no banco).
_PADRAO_HORA = r"(\d{1,2})(?::(\d{2})|h(\d{2})?)"
# 1º lado de um intervalo pode vir sem "h"/":" ("das 8 as 12h")
_PADRAO_HORA_OPC = r"(\d{1,2})(?::(\d{2})|h(\d{2})?)?"


def _hhmm(h: str, m: str | None) -> str | None:
    hora, minuto = int(h), int(m or 0)
    if hora > 23 or minuto > 59:
        return None
    return f"{hora:02d}:{minuto:02d}"


def _mais_uma_hora(hhmm: str) -> str:
    hora, minuto = map(int, hhmm.split(":"))
    return f"{min(hora + 1, 23):02d}:{minuto:02d}"


def extrair_hora_agenda(texto: str) -> tuple[str, str] | None:
    """(inicio, fim) em HH:MM quando o texto da AGENDA traz hora; None
    quando não traz (a NUU normalmente só informa o dia)."""
    texto = _remover_acentos((texto or "").lower())
    if not texto:
        return None
    # célula Excel com tipo Data vira '2026-08-12 00:00:00' -- o
    # '00:00:00' é só meia-noite implícita, não hora informada; hora
    # diferente de meia-noite nessa célula é hora de verdade
    m_iso = re.search(r"\d{4}-\d{1,2}-\d{1,2}(?:[ t](\d{2}):(\d{2})(?::\d{2})?)?", texto)
    if m_iso:
        texto = texto[:m_iso.start()] + " " + texto[m_iso.end():]
        if m_iso.group(1) and (m_iso.group(1), m_iso.group(2)) != ("00", "00"):
            ini = _hhmm(m_iso.group(1), m_iso.group(2))
            return (ini, _mais_uma_hora(ini)) if ini else None
    # intervalo: "entre 8h e 12h", "8h as 12h", "08:00 - 12:00", "das 8 as 12h"
    # (?<![\d/.:]) impede que o "08" de "13/08 as 14h" vire início do intervalo
    m = re.search(rf"(?<![\d/.:]){_PADRAO_HORA_OPC}\s*(?:as|a|-|e|ate)\s*(?:as\s+)?{_PADRAO_HORA}\b", texto)
    if m:
        ini = _hhmm(m.group(1), m.group(2) or m.group(3))
        fim = _hhmm(m.group(4), m.group(5) or m.group(6))
        if ini and fim and ini < fim:
            return ini, fim
    m = re.search(rf"\bate\s+(?:as\s+)?{_PADRAO_HORA}\b", texto)
    if m:
        fim = _hhmm(m.group(1), m.group(2) or m.group(3))
        return ("00:00", fim) if fim else None
    m = re.search(rf"\b(?:apos|a partir(?: das| de)?|depois das)\s+{_PADRAO_HORA}\b", texto)
    if m:
        ini = _hhmm(m.group(1), m.group(2) or m.group(3))
        return (ini, "23:59") if ini else None
    # hora única: "às 14h", "14:30", "14h30" (o "h" ou ":" evita casar
    # com os dígitos da data "13/08")
    m = re.search(rf"(?:\bas\s+)?\b(\d{{1,2}})(?::(\d{{2}})|h(\d{{2}})?)\b", texto)
    if m and (m.group(2) is not None or "h" in m.group(0)):
        ini = _hhmm(m.group(1), m.group(2) or m.group(3))
        if ini:
            return ini, _mais_uma_hora(ini)
    if "manha" in texto:
        return "08:00", "12:00"
    if "tarde" in texto:
        return "13:00", "18:00"
    return None


def extrair_endereco_novo(texto: str) -> str | None:
    """
    Extrai o endereço de entrega quando a OBSERVAÇÃO indica que é
    diferente do fiscal (padrão real: 'Endereço de Entrega: <endereço>
    ...'). Observações sem esse padrão (ex: 'X', 'NEMA', 'AGUARDANDO
    CONFIRMAÇÃO') retornam None.
    """
    if not texto:
        return None
    m = re.search(r"(?i)endere[çc]o\s+de\s+entrega\s*:?\s*(.+)", texto, re.DOTALL)
    if not m:
        return None
    endereco = re.sub(r"\s+", " ", m.group(1)).strip()
    return endereco or None


# ── Casamento com pedido no VUUPT ────────────────────────────────────────
_RE_SUFIXO_REENTREGA = re.compile(r"-R\d+$")


def casar_nf_com_pedido(numero_nf: str, vuupt: VuuptClient) -> dict | None:
    """
    Mesma regra 3b de documentos_pedido/matcher.py (referência da NF no
    título do serviço): tenta o formato com zero-padding de 6 dígitos
    primeiro (padrão real dos títulos), cai pro número cru só se
    diferente. Só aceita casamento SEM ambiguidade -- errar aqui manda a
    data de agendamento ou o endereço pro pedido errado.

    A busca no VUUPT é por substring ("contains"), o que pode trazer
    pedidos de OUTROS embarcadores cuja referência só COMEÇA com os
    mesmos dígitos (ex: NF 40287 bate em '40287623831/PADRAO PURO LTDA',
    uma referência maior de outro cliente) -- filtra client-side exigindo
    a NF como número isolado no título (sem dígito colado antes/depois).
    Reentregas (código com sufixo '-RN', ex: 'PS-36316-R1') contam como
    o MESMO pedido pra fins de ambiguidade -- é o mesmo embarque, só uma
    nova tentativa de entrega; nesse caso prefere a variante de
    reentrega (é a que está de fato pendente de entrega).
    """
    ref6 = numero_nf.zfill(6)
    termos = [ref6] + ([numero_nf] if numero_nf != ref6 else [])
    for termo in termos:
        try:
            servicos = vuupt.listar_servicos(
                [{"field": "title", "operator": "contains", "value": termo}], per_page=10
            )
        except Exception as e:
            logger.warning(f"  Falha na busca por referência {termo!r} no VUUPT: {e}")
            continue
        if not servicos:
            continue
        padrao_isolado = re.compile(rf"(?<!\d){re.escape(termo)}(?!\d)")
        validos = [s for s in servicos if padrao_isolado.search(s.get("title", "") or "")]
        if not validos:
            continue
        codigos_base = {_RE_SUFIXO_REENTREGA.sub("", (s.get("code") or "").lstrip("#")) for s in validos}
        codigos_base.discard("")
        if len(codigos_base) == 1:
            codigo = next(iter(codigos_base))
            reentrega = next((s for s in validos if _RE_SUFIXO_REENTREGA.search((s.get("code") or "").lstrip("#"))), None)
            servico = reentrega or validos[0]
            return {"codigo_pedido": codigo, "metodo": "nf_referencia_titulo", "servico": servico}
    return None


# ── Persistência ──────────────────────────────────────────────────────────
def _upsert_planilha(item: dict) -> str | None:
    """Grava/atualiza a linha em entregas_nuu_planilha. Retorna o
    endereço extraído SE for novo/diferente do que já estava salvo
    (None se não mudou nada -- evita notificar de novo todo dia a
    mesma observação, já que a mesma NF pode aparecer em várias
    planilhas até ser entregue)."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    existente = conn.execute(
        "SELECT endereco_extraido FROM entregas_nuu_planilha WHERE numero_nf = ?",
        (item["numero_nf"],),
    ).fetchone()

    endereco_para_notificar = None
    if item["endereco_extraido"] and (not existente or existente["endereco_extraido"] != item["endereco_extraido"]):
        endereco_para_notificar = item["endereco_extraido"]

    conn.execute("""
        INSERT INTO entregas_nuu_planilha
            (numero_nf, cliente, boleto_texto, agenda_bruto, data_agendamento_extraida,
             observacao_bruto, endereco_extraido, codigo_pedido, metodo_casamento, message_id_origem)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(numero_nf) DO UPDATE SET
            cliente = excluded.cliente, boleto_texto = excluded.boleto_texto,
            agenda_bruto = excluded.agenda_bruto,
            data_agendamento_extraida = excluded.data_agendamento_extraida,
            observacao_bruto = excluded.observacao_bruto,
            endereco_extraido = excluded.endereco_extraido,
            codigo_pedido = excluded.codigo_pedido, metodo_casamento = excluded.metodo_casamento,
            message_id_origem = excluded.message_id_origem,
            atualizado_em = datetime('now','localtime')
    """, (item["numero_nf"], item["cliente"], item["boleto_texto"], item["agenda_bruto"],
         item["data_agendamento_extraida"], item["observacao_bruto"], item["endereco_extraido"],
         item["codigo_pedido"], item["metodo_casamento"], item["message_id_origem"]))
    conn.commit()
    conn.close()
    return endereco_para_notificar


def _cnpj_e_email_embarcador_nuu() -> tuple[str, str]:
    """CNPJ + e-mail do embarcador NUU/Maria Dolores, da tabela 'interno'
    (mesmo cadastro usado pelo pipeline de importação) -- evita
    hardcodar esses valores aqui."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT cnpj_embarcador, email FROM interno WHERE email LIKE '%nuualimentos.com.br%' LIMIT 1"
    ).fetchone()
    conn.close()
    if not row:
        return "", ""
    primeiro_email = (row["email"] or "").split(",")[0].strip()
    return row["cnpj_embarcador"] or "", primeiro_email


def _registrar_agendamento_confirmado(codigo_pedido: str, data_agendada: str, servico: dict,
                                      vuupt: VuuptClient, cnpj_emb: str, email_emb: str,
                                      hora_agendada: tuple[str, str] | None = None) -> bool:
    """
    Grava em agendamentos_pedido como já CONFIRMADO (status='RESPONDIDO')
    -- reaproveita a mesma tabela/coluna que agendamento_confirmacao.py e
    ler_respostas_agendamento.py usam, então pipeline.py (na criação do
    serviço) e atualizar_agendamentos_confirmados.py (em serviço já
    existente) aplicam essa data no VUUPT automaticamente, sem duplicar
    essa lógica aqui.

    `hora_agendada` (09/09): (inicio, fim) quando o texto da AGENDA
    trouxe hora (extrair_hora_agenda); None = hora NÃO informada, fica
    NULL (quem aplica na VUUPT usa o padrão 08:00-18:00 só no payload; a
    roteirização sabe que NULL não é janela).

    Uma confirmação já recebida por e-mail (resposta humana real, origem
    != 'PLANILHA_NUU') NUNCA é sobrescrita pela planilha -- a planilha só
    preenche o que ainda não tinha confirmação. Retorna True se
    gravou/atualizou, False se pulou por já ter confirmação manual.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    existente = conn.execute(
        "SELECT status, origem FROM agendamentos_pedido WHERE pedido = ?", (codigo_pedido,)
    ).fetchone()
    if existente and existente["status"] == "RESPONDIDO" and (existente["origem"] or "") != "PLANILHA_NUU":
        conn.close()
        logger.info(f"  {codigo_pedido}: já tem agendamento confirmado por e-mail (origem manual) -- "
                   f"planilha não sobrescreve.")
        return False

    customer_id = servico.get("customer_id")
    nome_destinatario = ""
    cnpj_destinatario = ""
    if customer_id:
        customer = vuupt.buscar_customer_por_id(customer_id)
        if customer:
            nome_destinatario = customer.get("name", "") or ""
            cnpj_destinatario = customer.get("code", "") or ""

    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    hora_ini, hora_fim = hora_agendada if hora_agendada else (None, None)
    texto_hora = f" {hora_ini}-{hora_fim}" if hora_agendada else ""
    conn.execute("""
        INSERT INTO agendamentos_pedido
            (pedido, cnpj_destinatario, nome_destinatario, cnpj_embarcador, email_embarcador,
             status, data_agendada, horario_inicio_agendado, horario_fim_agendado,
             resposta_texto, solicitado_em, respondido_em, origem)
        VALUES (?, ?, ?, ?, ?, 'RESPONDIDO', ?, ?, ?, ?, ?, ?, 'PLANILHA_NUU')
        ON CONFLICT(pedido) DO UPDATE SET
            status = 'RESPONDIDO', data_agendada = excluded.data_agendada,
            horario_inicio_agendado = excluded.horario_inicio_agendado,
            horario_fim_agendado = excluded.horario_fim_agendado,
            resposta_texto = excluded.resposta_texto, respondido_em = excluded.respondido_em,
            origem = 'PLANILHA_NUU'
    """, (codigo_pedido, cnpj_destinatario, nome_destinatario, cnpj_emb, email_emb,
         data_agendada, hora_ini, hora_fim, f"[Planilha NUU] AGENDA={data_agendada}{texto_hora}", agora, agora))
    conn.commit()
    conn.close()
    return True


# ── Notificação de endereço divergente ───────────────────────────────────
def _notificar_enderecos_novos(itens: list[dict], config_email: dict) -> bool:
    linhas_html = "".join(f"""
    <tr>
      <td style="padding:8px 14px;border-bottom:1px solid {COR_BORDA};">#{html.escape(item['codigo_pedido'])}</td>
      <td style="padding:8px 14px;border-bottom:1px solid {COR_BORDA};">{html.escape(item['cliente'])}</td>
      <td style="padding:8px 14px;border-bottom:1px solid {COR_BORDA};">NF {html.escape(item['numero_nf'])}</td>
      <td style="padding:8px 14px;border-bottom:1px solid {COR_BORDA};">{html.escape(item['endereco'])}</td>
    </tr>""" for item in itens)

    conteudo = f"""
    <p style="margin:0 0 4px 0;font-size:12px;font-weight:800;color:{COR_ERRO};letter-spacing:0.5px;">CONFERÊNCIA MANUAL</p>
    <p style="margin:0 0 16px 0;font-size:20px;font-weight:800;color:{COR_PRIMARIA};">
      Endereço de entrega diferente do fiscal -- NUU / Maria Dolores
    </p>
    <p style="margin:0 0 20px 0;font-size:14px;color:{COR_TEXTO};line-height:1.6;">
      A planilha de entregas trouxe {len(itens)} pedido(s) com endereço de entrega diferente do
      cadastro fiscal. Confira e atualize o endereço no VUUPT manualmente antes da roteirização
      -- este agente não aplica o endereço sozinho.
    </p>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
          style="border:1px solid {COR_BORDA};border-radius:8px;overflow:hidden;">
    <thead><tr style="background:{COR_PRIMARIA_CLARA};">
    <th style="padding:8px 14px;text-align:left;font-size:11px;color:{COR_PRIMARIA};">Pedido</th>
    <th style="padding:8px 14px;text-align:left;font-size:11px;color:{COR_PRIMARIA};">Cliente</th>
    <th style="padding:8px 14px;text-align:left;font-size:11px;color:{COR_PRIMARIA};">NF</th>
    <th style="padding:8px 14px;text-align:left;font-size:11px;color:{COR_PRIMARIA};">Endereço informado</th>
    </tr></thead><tbody>{linhas_html}</tbody></table>
    <p style="margin:20px 0 0 0;font-size:14px;color:{COR_TEXTO};line-height:1.6;">
      Atenciosamente,<br><strong>Agente Stokki Eventos</strong>
    </p>
    """
    corpo = envelope_html(conteudo, rodape="Mensagem automática — leitura da planilha ENTREGAS NUU.",
                          cor_acento=COR_ERRO)
    return enviar_email([EMAIL_OPERACAO], f"[Conferência] {len(itens)} endereço(s) de entrega diferentes -- NUU",
                        corpo, config_email)


# ── Retentativa de NFs pendentes ─────────────────────────────────────────
def _reprocessar_pendencias(vuupt: VuuptClient, cnpj_emb: str, email_emb: str, modo_teste: bool) -> int:
    """
    Retenta o casamento de NFs que ficaram sem pedido em execuções
    anteriores. Cobre o caso comum de a planilha ser lida ANTES do
    pedido daquela NF ter sido importado no VUUPT (Etapa 1a roda antes
    do Pipeline) -- sem isso, uma NF que não casou na 1ª tentativa fica
    presa pra sempre, a não ser que reapareça numa planilha de um dia
    futuro. Retorna quantos agendamentos foram aplicados nesta
    retentativa.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    pendentes = conn.execute("""
        SELECT numero_nf, agenda_bruto FROM entregas_nuu_planilha
        WHERE codigo_pedido IS NULL
    """).fetchall()
    conn.close()

    aplicados = 0
    for row in pendentes:
        # Reextrai a data do texto bruto em vez de confiar no valor já
        # persistido -- autocorrige pendências gravadas com uma data
        # ruim por uma versão anterior da extração (ex: 'AGENDADO
        # 24/04' que já foi aceito como 24/04/2027) mesmo quando o
        # casamento com o pedido ainda não resolve nesta tentativa.
        data_agenda = extrair_data_agenda(row["agenda_bruto"])
        hora_agenda = extrair_hora_agenda(row["agenda_bruto"])
        correspondencia = casar_nf_com_pedido(row["numero_nf"], vuupt)
        codigo_pedido = correspondencia["codigo_pedido"] if correspondencia else None
        metodo = correspondencia["metodo"] if correspondencia else None

        if not modo_teste:
            conn = sqlite3.connect(DB_PATH)
            conn.execute("""
                UPDATE entregas_nuu_planilha SET codigo_pedido = ?, metodo_casamento = ?,
                    data_agendamento_extraida = ?, atualizado_em = datetime('now','localtime')
                WHERE numero_nf = ?
            """, (codigo_pedido, metodo, data_agenda, row["numero_nf"]))
            conn.commit()
            conn.close()

        if not correspondencia:
            continue
        logger.info(f"  [retentativa] NF {row['numero_nf']} -> {codigo_pedido}: casamento pendente resolvido.")

        if not data_agenda:
            continue
        if modo_teste:
            logger.info(f"  [retentativa][TESTE] NF {row['numero_nf']} -> {codigo_pedido}: "
                       f"aplicaria agendamento {data_agenda}.")
            aplicados += 1
        elif _registrar_agendamento_confirmado(codigo_pedido, data_agenda, correspondencia["servico"], vuupt,
                                               cnpj_emb, email_emb, hora_agenda):
            aplicados += 1
            logger.info(f"  [retentativa] NF {row['numero_nf']} -> {codigo_pedido}: agendamento "
                       f"{data_agenda}{' ' + '-'.join(hora_agenda) if hora_agenda else ''} registrado.")
    return aplicados


# ── Orquestração ──────────────────────────────────────────────────────────
def processar_planilhas_entregas(config: dict, modo_teste: bool = False) -> dict:
    _garantir_tabelas()
    vuupt = VuuptClient(config.get("vuupt_api", {}).get("token", ""))
    cnpj_emb, email_emb = _cnpj_e_email_embarcador_nuu()

    planilhas = buscar_planilhas_entregas(config)
    total_linhas = casados = agendamentos_aplicados = 0
    enderecos_novos = []

    for planilha in planilhas:
        try:
            linhas = parsear_planilha(planilha["conteudo"])
        except Exception as e:
            # openpyxl só lê .xlsx (zip); um .xls antigo (formato binário
            # legado, raro mas já visto anexado com esse nome/extensão)
            # derrubaria a execução inteira -- pula só essa planilha e
            # segue com as demais.
            logger.warning(f"  '{planilha['assunto']}' ({planilha['nome_arquivo']}): falha ao abrir a "
                          f"planilha -- {e}. Pulando (revisão manual).")
            continue
        logger.info(f"  '{planilha['assunto']}': {len(linhas)} linha(s) de NF na planilha.")
        total_linhas += len(linhas)

        for linha in linhas:
            correspondencia = casar_nf_com_pedido(linha["numero_nf"], vuupt)
            codigo_pedido = correspondencia["codigo_pedido"] if correspondencia else None
            servico = correspondencia["servico"] if correspondencia else None
            metodo = correspondencia["metodo"] if correspondencia else None
            if codigo_pedido:
                casados += 1

            data_agenda = extrair_data_agenda(linha["agenda_bruto"])
            hora_agenda = extrair_hora_agenda(linha["agenda_bruto"])
            endereco = extrair_endereco_novo(linha["observacao_bruto"])

            item = {**linha, "data_agendamento_extraida": data_agenda, "endereco_extraido": endereco,
                   "codigo_pedido": codigo_pedido, "metodo_casamento": metodo,
                   "message_id_origem": planilha["message_id"]}
            endereco_p_notificar = _upsert_planilha(item)
            if endereco_p_notificar and codigo_pedido:
                enderecos_novos.append({"codigo_pedido": codigo_pedido, "numero_nf": linha["numero_nf"],
                                        "cliente": linha["cliente"], "endereco": endereco_p_notificar})

            if data_agenda and codigo_pedido and servico:
                if modo_teste:
                    logger.info(f"  [TESTE] NF {linha['numero_nf']} -> {codigo_pedido}: "
                               f"aplicaria agendamento {data_agenda}.")
                    agendamentos_aplicados += 1
                elif _registrar_agendamento_confirmado(codigo_pedido, data_agenda, servico, vuupt,
                                                       cnpj_emb, email_emb, hora_agenda):
                    agendamentos_aplicados += 1
                    logger.info(f"  NF {linha['numero_nf']} -> {codigo_pedido}: agendamento {data_agenda}"
                               f"{' ' + '-'.join(hora_agenda) if hora_agenda else ''} registrado (via planilha).")
            elif data_agenda and not codigo_pedido:
                logger.warning(f"  NF {linha['numero_nf']}: tem data de agendamento ({data_agenda}) na "
                              f"planilha mas não casou com nenhum pedido no VUUPT -- revisão manual.")

    if enderecos_novos:
        if modo_teste:
            logger.info(f"  [TESTE] {len(enderecos_novos)} endereço(s) novo(s)/alterado(s) seriam "
                       f"notificados: {[e['codigo_pedido'] for e in enderecos_novos]}")
        else:
            _notificar_enderecos_novos(enderecos_novos, config.get("email", {}))

    agendamentos_retentativa = _reprocessar_pendencias(vuupt, cnpj_emb, email_emb, modo_teste)
    agendamentos_aplicados += agendamentos_retentativa

    resultado = {"planilhas": len(planilhas), "linhas": total_linhas, "casados": casados,
                "agendamentos_aplicados": agendamentos_aplicados, "enderecos_novos": len(enderecos_novos),
                "agendamentos_retentativa": agendamentos_retentativa}
    logger.info(f"Planilhas de entregas NUU: {resultado}")
    return resultado


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
            logging.FileHandler(_RAIZ / "dados" / "ler_planilha_entregas_nuu.log", encoding="utf-8"),
        ],
    )

    parser = argparse.ArgumentParser(description="Lê a planilha ENTREGAS.xlsx da NUU e aplica agendamento/endereço")
    parser.add_argument("--modo-teste", action="store_true", help="Mostra o que seria feito, sem gravar nada")
    args = parser.parse_args()

    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    resultado = processar_planilhas_entregas(config, modo_teste=args.modo_teste)
    logger.info(f"Resultado: {resultado}")


if __name__ == "__main__":
    main()
