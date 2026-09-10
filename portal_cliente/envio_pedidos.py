# -*- coding: utf-8 -*-
"""
portal_cliente/envio_pedidos.py

Máscara de envio de pedidos (pedido do Hugo, 08/09/2026): o embarcador (ou
a equipe Fresh Log em nome dele) sobe os XMLs das NF-e pelo portal, a
gente valida, guarda o XML original numa tabela própria e enfileira a
criação do pedido na Stokki (worker: enviar_stokki.py). É a primeira
camada do "sistema próprio por cima da infra da Stokki".

Tabelas (dados/dados.db):
  portal_envios          -- um por NF-e (chave única): dados extraídos do
                            XML, caminho do XML original, status da fila,
                            resposta da Stokki, horário/agendamento do
                            destinatário, código PS-xxxxx quando conciliado.
  portal_destinatarios   -- horário de recebimento (e flag de agendamento)
                            por CNPJ/CPF de destinatário, POR embarcador --
                            perguntado uma vez só (decisão do Hugo, item 8).
  portal_solicitacoes    -- cancelar / tirar da rota (em espera) /
                            reagendar / reenviar pedidos por parte do
                            cliente (item 12); o que não dá pra aplicar
                            sozinho vira pendência pra operação, com e-mail.
  portal_clientes_envio  -- parâmetros do wizard da Stokki por embarcador
                            (regra de transformação do XML, armazém, tipo
                            de transporte, embalagem). client_id vem de
                            interno.stkkc_id.

Status da fila (portal_envios.status):
  NA_FILA -> ENVIANDO -> CRIADO | ERRO | DUPLICADO ; CANCELADO (antes de ir)

Origem do pedido (portal_envios.origem), 09/09/2026:
  xml       -- NF-e (fluxo original); chave_nfe é a chave de acesso real.
  planilha  -- pedido do Hugo, 09/09: o cliente sobe uma planilha
               .xlsx/.xls no modelo Fresh Log (uma linha por item; linhas
               com o mesmo "Pedido" viram um pedido só). Não há NF-e:
               chave_nfe recebe uma chave sintética PLANILHA-<cnpj>-<ref>
               (só pra dedupe), os itens ficam em itens_json e o worker
               cria na Stokki pelo wizard de importação por Excel
               (inventory/outbound/create/excel -- um xlsx SKU/Quantidade/
               Valor Unitário por pedido + destinatário/PO/data no form).
"""
import hashlib
import io
import json
import logging
import re
import secrets
import sqlite3
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))

logger = logging.getLogger(__name__)

DB_PATH = _RAIZ / "dados" / "dados.db"
PASTA_XMLS = _RAIZ / "dados" / "portal_envios"
PASTA_TEMP = PASTA_XMLS / "_temporarios"
TEMP_VALIDADE_SEGUNDOS = 2 * 3600
DIAS_LISTAGEM = 30

NS_NFE = "http://www.portalfiscal.inf.br/nfe"
NS = {"nfe": NS_NFE}

STATUS_NA_FILA = "NA_FILA"
STATUS_ENVIANDO = "ENVIANDO"
STATUS_CRIADO = "CRIADO"
STATUS_ERRO = "ERRO"
STATUS_DUPLICADO = "DUPLICADO"
STATUS_CANCELADO = "CANCELADO"
STATUS_ABERTOS = (STATUS_NA_FILA, STATUS_ENVIANDO)
ROTULOS_STATUS = {
    STATUS_NA_FILA: "Na fila",
    STATUS_ENVIANDO: "Enviando à Stokki",
    STATUS_CRIADO: "Criado na Stokki",
    STATUS_ERRO: "Erro",
    STATUS_DUPLICADO: "Já existia na Stokki",
    STATUS_CANCELADO: "Cancelado",
}

ORIGEM_XML = "xml"
ORIGEM_PLANILHA = "planilha"

TIPOS_SOLICITACAO = ("cancelar", "em_espera", "reagendar", "reenviar")
ROTULOS_SOLICITACAO = {
    "cancelar": "Cancelamento",
    "em_espera": "Tirar da rota (em espera)",
    "reagendar": "Reagendamento",
    "reenviar": "Reenvio",
}

# Regras de transformação do XML (as MESMAS do importador por e-mail,
# agente_importacao_stokki/etapa2_processamento_xml/processar_xml.py) --
# decisão do Hugo, item 7: manter as existentes, novas só quando precisar.
REGRAS_XML = {
    "nenhuma": "XML como veio",
    "emporio_quatro_estrelas": "Consolida os itens num único produto (Quatro Estrelas)",
    "muai": "Converte itens em KG pra caixas (Muai)",
}
REGRAS_PADRAO_POR_STKKC = {98: "emporio_quatro_estrelas", 99: "muai"}


class ErroEnvio(Exception):
    pass


# ── Banco ──────────────────────────────────────────────────────────────────────

def conectar() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS portal_envios (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            cnpj_embarcador         TEXT NOT NULL,
            chave_nfe               TEXT NOT NULL UNIQUE,
            numero_nf               TEXT,
            serie                   TEXT,
            emitida_em              TEXT,
            destinatario_doc        TEXT,
            destinatario_nome       TEXT,
            destinatario_endereco   TEXT,
            destinatario_bairro     TEXT,
            destinatario_municipio  TEXT,
            destinatario_uf         TEXT,
            destinatario_cep        TEXT,
            destinatario_telefone   TEXT,
            volumes                 INTEGER,
            peso_kg                 REAL,
            valor_nf                REAL,
            itens                   INTEGER,
            xml_path                TEXT NOT NULL,
            regra_xml               TEXT,
            status                  TEXT NOT NULL,
            tentativas              INTEGER NOT NULL DEFAULT 0,
            erro                    TEXT,
            resposta_stokki         TEXT,
            codigo_pedido           TEXT,
            horario_inicio          TEXT,
            horario_fim             TEXT,
            requer_agendamento      INTEGER NOT NULL DEFAULT 0,
            agendamento_data        TEXT,
            agendamento_hora_inicio TEXT,
            agendamento_hora_fim    TEXT,
            agendamento_pendente    INTEGER NOT NULL DEFAULT 0,
            agendamento_aplicado_em TEXT,
            observacoes             TEXT,
            enviado_por             TEXT,
            criado_em               TEXT NOT NULL,
            atualizado_em           TEXT NOT NULL,
            enviado_stokki_em       TEXT,
            criado_stokki_em        TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_portal_envios_emb ON portal_envios (cnpj_embarcador, criado_em);
        CREATE INDEX IF NOT EXISTS idx_portal_envios_status ON portal_envios (status);
        CREATE TABLE IF NOT EXISTS portal_destinatarios (
            cnpj_embarcador     TEXT NOT NULL,
            documento           TEXT NOT NULL,
            nome                TEXT,
            horario_inicio      TEXT,
            horario_fim         TEXT,
            requer_agendamento  INTEGER NOT NULL DEFAULT 0,
            atualizado_em       TEXT NOT NULL,
            PRIMARY KEY (cnpj_embarcador, documento)
        );
        CREATE TABLE IF NOT EXISTS portal_solicitacoes (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            envio_id        INTEGER NOT NULL,
            cnpj_embarcador TEXT NOT NULL,
            tipo            TEXT NOT NULL,
            detalhes        TEXT,
            status          TEXT NOT NULL DEFAULT 'PENDENTE',
            solicitado_por  TEXT,
            criado_em       TEXT NOT NULL,
            concluido_em    TEXT,
            resposta        TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_portal_solic_envio ON portal_solicitacoes (envio_id, status);
        CREATE TABLE IF NOT EXISTS portal_clientes_envio (
            cnpj            TEXT PRIMARY KEY,
            regra_xml       TEXT,
            warehouse_id    TEXT,
            tipo_transporte TEXT,
            embalagem       TEXT,
            envio_ativo     INTEGER NOT NULL DEFAULT 1,
            atualizado_em   TEXT NOT NULL
        );
    """)
    # Colunas que entraram depois (09/09, importação por planilha) -- as
    # tabelas já existem em produção, então é ALTER TABLE guardado.
    _garantir_colunas(conn, "portal_envios", {
        "origem": "TEXT NOT NULL DEFAULT 'xml'",
        "referencia": "TEXT",
        "itens_json": "TEXT",
        "data_expedicao": "TEXT",
        "linhas_planilha": "TEXT",
    })
    _garantir_colunas(conn, "portal_clientes_envio", {"carrier_id": "TEXT", "prioridade": "TEXT"})
    conn.commit()
    return conn


def _garantir_colunas(conn: sqlite3.Connection, tabela: str, colunas: dict[str, str]) -> None:
    existentes = {r[1] for r in conn.execute(f"PRAGMA table_info({tabela})")}
    for nome, tipo in colunas.items():
        if nome not in existentes:
            conn.execute(f"ALTER TABLE {tabela} ADD COLUMN {nome} {tipo}")


def _agora() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _so_digitos(valor) -> str:
    return re.sub(r"\D", "", str(valor or ""))


def formatar_documento(doc: str) -> str:
    d = _so_digitos(doc)
    if len(d) == 14:
        return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"
    if len(d) == 11:
        return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}"
    return d


def _hora_valida(valor) -> str | None:
    v = str(valor or "").strip()
    if re.fullmatch(r"\d{2}:\d{2}", v):
        h, m = int(v[:2]), int(v[3:])
        if 0 <= h <= 23 and 0 <= m <= 59:
            return v
    return None


def rotulo_envio(envio: dict) -> str:
    """'NF 12345' pro XML, 'Pedido ABC-1' pra planilha -- usado em mensagens,
    e-mails e na tela."""
    if envio.get("numero_nf"):
        return f"NF {envio['numero_nf']}"
    return f"Pedido {envio.get('referencia') or '?'}"


# ── Leitura do XML da NF-e ─────────────────────────────────────────────────────

def _texto(el, caminho: str) -> str:
    if el is None:
        return ""
    achado = el.find(caminho, NS)
    return (achado.text or "").strip() if achado is not None and achado.text else ""


def ler_nfe(conteudo: bytes, nome_arquivo: str = "") -> dict:
    """Extrai os campos que a máscara mostra/valida. Levanta ErroEnvio com
    mensagem pro usuário quando o arquivo não é uma NF-e (validação 1 do
    item 9)."""
    try:
        root = ET.fromstring(conteudo)
    except ET.ParseError as e:
        raise ErroEnvio(f"{nome_arquivo or 'Arquivo'}: não é um XML válido ({e}).")

    inf = root.find(".//nfe:infNFe", NS)
    if inf is None:
        raise ErroEnvio(f"{nome_arquivo or 'Arquivo'}: não é o XML de uma NF-e (sem <infNFe>).")

    chave = _so_digitos(inf.get("Id", ""))
    if len(chave) != 44:
        chave = _so_digitos(_texto(root, ".//nfe:protNFe/nfe:infProt/nfe:chNFe"))
    if len(chave) != 44:
        raise ErroEnvio(f"{nome_arquivo or 'Arquivo'}: chave de acesso da NF-e não encontrada.")

    ide, emit, dest = inf.find("nfe:ide", NS), inf.find("nfe:emit", NS), inf.find("nfe:dest", NS)
    if dest is None:
        raise ErroEnvio(f"{nome_arquivo or 'Arquivo'}: NF-e sem destinatário (<dest>).")
    ender = dest.find("nfe:enderDest", NS)
    tipo_nf = _texto(ide, "nfe:tpNF")
    if tipo_nf == "0":
        raise ErroEnvio(f"{nome_arquivo or 'Arquivo'}: NF-e de ENTRADA (tpNF=0) -- só notas de saída viram pedido de entrega.")

    dets = inf.findall("nfe:det", NS)
    volumes = 0
    peso = 0.0
    for vol in inf.findall("nfe:transp/nfe:vol", NS):
        try:
            volumes += int(float(_texto(vol, "nfe:qVol") or 0))
        except ValueError:
            pass
        try:
            peso += float(_texto(vol, "nfe:pesoB") or _texto(vol, "nfe:pesoL") or 0)
        except ValueError:
            pass
    try:
        valor = float(_texto(inf, "nfe:total/nfe:ICMSTot/nfe:vNF") or 0)
    except ValueError:
        valor = 0.0

    logradouro = _texto(ender, "nfe:xLgr")
    numero = _texto(ender, "nfe:nro")
    compl = _texto(ender, "nfe:xCpl")
    endereco = ", ".join(p for p in (logradouro, numero) if p)
    if compl:
        endereco = f"{endereco} - {compl}" if endereco else compl

    emitida = _texto(ide, "nfe:dhEmi") or _texto(ide, "nfe:dEmi")
    return {
        "chave_nfe": chave,
        "numero_nf": _texto(ide, "nfe:nNF"),
        "serie": _texto(ide, "nfe:serie"),
        "emitida_em": emitida[:19].replace("T", " ") if emitida else "",
        "emitente_cnpj": _so_digitos(_texto(emit, "nfe:CNPJ") or _texto(emit, "nfe:CPF")),
        "emitente_nome": _texto(emit, "nfe:xFant") or _texto(emit, "nfe:xNome"),
        "destinatario_doc": _so_digitos(_texto(dest, "nfe:CNPJ") or _texto(dest, "nfe:CPF")),
        "destinatario_nome": _texto(dest, "nfe:xNome"),
        "destinatario_endereco": endereco,
        "destinatario_bairro": _texto(ender, "nfe:xBairro"),
        "destinatario_municipio": _texto(ender, "nfe:xMun"),
        "destinatario_uf": _texto(ender, "nfe:UF"),
        "destinatario_cep": _so_digitos(_texto(ender, "nfe:CEP")),
        "destinatario_telefone": _so_digitos(_texto(ender, "nfe:fone")),
        "volumes": volumes or 1,
        "peso_kg": round(peso, 3),
        "valor_nf": round(valor, 2),
        "itens": len(dets),
        "nome_arquivo": nome_arquivo or f"{chave}.xml",
    }


EXTENSOES_PLANILHA = (".xlsx", ".xlsm", ".xls")


def e_planilha(nome: str, conteudo: bytes = b"") -> bool:
    nome_baixo = (nome or "").lower()
    if nome_baixo.endswith(EXTENSOES_PLANILHA):
        return True
    # .xls antigo (OLE2) sem extensão confiável
    return conteudo[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def expandir_upload(nome: str, conteudo: bytes) -> list[tuple[str, bytes]]:
    """Um .xml/.xlsx vira ele mesmo; um .zip vira cada .xml/.xlsx de dentro
    (o cliente costuma receber os XMLs zipados do emissor). Cuidado: .xlsx
    também começa com 'PK' -- só trata como ZIP quando NÃO for planilha."""
    nome_baixo = (nome or "").lower()
    if not e_planilha(nome, conteudo) and (nome_baixo.endswith(".zip") or conteudo[:2] == b"PK"):
        saida = []
        try:
            with zipfile.ZipFile(io.BytesIO(conteudo)) as zf:
                for info in zf.infolist():
                    if info.is_dir() or not info.filename.lower().endswith((".xml",) + EXTENSOES_PLANILHA):
                        continue
                    if info.file_size > 5 * 1024 * 1024:
                        continue
                    saida.append((Path(info.filename).name, zf.read(info)))
        except zipfile.BadZipFile:
            raise ErroEnvio(f"{nome}: ZIP inválido ou corrompido.")
        if not saida:
            raise ErroEnvio(f"{nome}: o ZIP não tem nenhum arquivo .xml ou planilha .xlsx dentro.")
        return saida
    return [(Path(nome or "arquivo.xml").name, conteudo)]


# ── Importação por planilha (XLS/XLSX) -- pedido do Hugo, 09/09/2026 ───────────
# Modelo próprio da Fresh Log: UMA LINHA POR ITEM; linhas com o mesmo valor em
# "Pedido" formam um pedido (dados do destinatário vêm da primeira linha do
# grupo). O worker converte cada pedido no xlsx que a Stokki importa
# (SKU / Quantidade / Valor Unitário) e manda o resto pelo formulário.

# (chave, rótulo no modelo, obrigatória, apelidos aceitos no cabeçalho, largura, exemplo)
COLUNAS_PLANILHA: list[tuple[str, str, bool, tuple[str, ...], int, str]] = [
    ("referencia", "Pedido (nº ou referência)", True,
     ("pedido", "n pedido", "numero do pedido", "num pedido", "referencia", "po", "ordem", "order", "codigo do pedido"), 22, "PED-1001"),
    ("numero_nf", "Nº da NF (opcional)", False,
     ("nf", "n nf", "numero nf", "numero da nf", "nota", "nota fiscal", "n da nf", "nfe", "nf e"), 16, "45120"),
    ("destinatario_doc", "CNPJ/CPF do destinatário", True,
     ("cnpj", "cpf", "cnpj cpf", "cnpj/cpf", "documento", "cnpj/cpf do destinatario", "cnpj destinatario", "cpf cnpj", "doc"), 22, "12.345.678/0001-90"),
    ("destinatario_nome", "Nome do destinatário", True,
     ("destinatario", "nome", "cliente", "razao social", "nome do destinatario", "nome fantasia", "loja"), 30, "Mercado Bom Preço Ltda"),
    ("cep", "CEP", True, ("cep",), 12, "01310-100"),
    ("logradouro", "Endereço (rua/avenida)", True, ("endereco", "logradouro", "rua", "avenida", "endereco de entrega"), 30, "Av. Paulista"),
    ("numero", "Número", True, ("numero", "n", "no", "num", "nro"), 10, "1578"),
    ("complemento", "Complemento", False, ("complemento", "compl", "apto", "sala"), 16, "Loja 2"),
    ("bairro", "Bairro", True, ("bairro",), 18, "Bela Vista"),
    ("cidade", "Cidade", True, ("cidade", "municipio"), 18, "São Paulo"),
    ("uf", "UF", True, ("uf", "estado"), 6, "SP"),
    ("telefone", "Telefone", False, ("telefone", "fone", "celular", "contato", "whatsapp"), 16, "(11) 99999-0000"),
    ("email", "E-mail do destinatário", False, ("email", "e mail", "e-mail"), 24, "compras@bompreco.com.br"),
    ("data_expedicao", "Data de expedição (DD/MM/AAAA)", False,
     ("data de expedicao", "expedicao", "data expedicao", "data", "data de saida", "data de coleta", "data entrega", "data de entrega"), 18, ""),
    ("volumes", "Volumes", False, ("volumes", "vol", "qtd volumes", "caixas", "qtd caixas"), 10, "3"),
    ("peso_kg", "Peso (kg)", False, ("peso", "peso kg", "peso (kg)", "peso bruto"), 10, "12,5"),
    ("valor_total", "Valor total do pedido (R$)", False, ("valor", "valor total", "valor do pedido", "total", "valor nf", "valor da nf"), 16, "450,00"),
    ("observacoes", "Observações", False, ("observacoes", "observacao", "obs", "instrucoes"), 30, "Entregar no fundo"),
    ("sku", "SKU do produto", True, ("sku", "codigo", "codigo do produto", "cod produto", "produto", "ean", "gtin", "item", "cod"), 18, "QE-0001"),
    ("quantidade", "Quantidade", True, ("quantidade", "qtd", "qtde", "qte", "quant", "qtd itens"), 12, "10"),
    ("valor_unitario", "Valor unitário (R$)", False,
     ("valor unitario", "vlr unitario", "vlr unit", "valor unit", "preco", "preco unitario", "unitario"), 16, "45,00"),
]
_COLUNAS_POR_CHAVE = {c[0]: c for c in COLUNAS_PLANILHA}
MAX_LINHAS_PLANILHA = 2000
UFS = {"AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA", "MT", "MS", "MG", "PA", "PB", "PR", "PE", "PI", "RJ",
       "RN", "RS", "RO", "RR", "SC", "SP", "SE", "TO"}
NOMES_UF = {"AC": "Acre", "AL": "Alagoas", "AP": "Amapá", "AM": "Amazonas", "BA": "Bahia", "CE": "Ceará", "DF": "Distrito Federal",
            "ES": "Espírito Santo", "GO": "Goiás", "MA": "Maranhão", "MT": "Mato Grosso", "MS": "Mato Grosso do Sul",
            "MG": "Minas Gerais", "PA": "Pará", "PB": "Paraíba", "PR": "Paraná", "PE": "Pernambuco", "PI": "Piauí",
            "RJ": "Rio de Janeiro", "RN": "Rio Grande do Norte", "RS": "Rio Grande do Sul", "RO": "Rondônia", "RR": "Roraima",
            "SC": "Santa Catarina", "SP": "São Paulo", "SE": "Sergipe", "TO": "Tocantins"}


def _normalizar_texto(valor) -> str:
    """minúsculas, sem acento, só letras/dígitos/espaço/barra -- pra casar
    cabeçalhos ('Nº da NF' -> 'n da nf', 'CNPJ/CPF' -> 'cnpj/cpf')."""
    t = unicodedata.normalize("NFKD", str(valor or "")).encode("ascii", "ignore").decode()
    t = t.lower().replace("º", "").replace("°", "")
    t = re.sub(r"[^a-z0-9/ ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


_APELIDOS_NORMALIZADOS: dict[str, str] = {}
for _c in COLUNAS_PLANILHA:
    _APELIDOS_NORMALIZADOS[_normalizar_texto(_c[1])] = _c[0]
    for _a in _c[3]:
        _APELIDOS_NORMALIZADOS.setdefault(_normalizar_texto(_a), _c[0])


def _celula_texto(valor) -> str:
    if valor is None:
        return ""
    if isinstance(valor, bool):
        return "1" if valor else ""
    if isinstance(valor, float):
        return str(int(valor)) if valor.is_integer() else repr(valor)
    if isinstance(valor, int):
        return str(valor)
    if isinstance(valor, datetime):
        return valor.strftime("%d/%m/%Y")
    if isinstance(valor, date):
        return valor.strftime("%d/%m/%Y")
    return str(valor).strip()


def _numero_br(valor) -> float | None:
    """'12,5' / '1.234,56' / 12.5 -> float; vazio -> None; lixo -> ValueError."""
    if valor is None or (isinstance(valor, str) and not valor.strip()):
        return None
    if isinstance(valor, (int, float)) and not isinstance(valor, bool):
        return float(valor)
    t = str(valor).strip().replace("R$", "").replace(" ", "")
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    elif t.count(".") > 1:
        t = t.replace(".", "")
    return float(t)


def _data_planilha(valor) -> date | None:
    if valor is None or (isinstance(valor, str) and not valor.strip()):
        return None
    if isinstance(valor, datetime):
        return valor.date()
    if isinstance(valor, date):
        return valor
    t = str(valor).strip()
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(t[:10], fmt).date()
        except ValueError:
            continue
    raise ValueError(t)


def chave_planilha(cnpj_embarcador: str, referencia: str) -> str:
    """Chave sintética única por embarcador+pedido (vai em chave_nfe só pra
    dedupe -- não é uma chave de NF-e)."""
    ref = re.sub(r"[^A-Z0-9]+", "-", str(referencia or "").upper()).strip("-")[:40]
    digest = hashlib.sha1(f"{_so_digitos(cnpj_embarcador)}|{str(referencia or '').strip().upper()}".encode()).hexdigest()[:10]
    return f"PLANILHA-{_so_digitos(cnpj_embarcador)}-{ref}-{digest}"


def _abrir_planilha(conteudo: bytes, nome: str):
    """Devolve lista de linhas (lista de valores) da primeira aba com dados.
    .xlsx via openpyxl; .xls só se xlrd estiver instalado."""
    nome_baixo = (nome or "").lower()
    if nome_baixo.endswith(".xls") or conteudo[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        try:
            import xlrd  # noqa: F401
        except ImportError:
            raise ErroEnvio(f"{nome}: o formato .xls (Excel 97-2003) não é suportado -- salve a planilha como .xlsx e envie de novo.")
        try:
            wb = xlrd.open_workbook(file_contents=conteudo)
        except Exception as e:
            raise ErroEnvio(f"{nome}: não consegui abrir a planilha ({e}).")
        for sh in wb.sheets():
            linhas = [[sh.cell_value(r, c) for c in range(sh.ncols)] for r in range(min(sh.nrows, MAX_LINHAS_PLANILHA + 20))]
            if any(any(_celula_texto(v) for v in l) for l in linhas):
                return linhas
        return []
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(conteudo), read_only=True, data_only=True)
    except Exception as e:
        raise ErroEnvio(f"{nome}: não consegui abrir a planilha ({type(e).__name__}). Envie um .xlsx no modelo Fresh Log.")
    try:
        for ws in wb.worksheets:
            if (ws.title or "").strip().lower().startswith("instru"):
                continue
            linhas = []
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i > MAX_LINHAS_PLANILHA + 20:
                    break
                linhas.append(list(row))
            if any(any(_celula_texto(v) for v in l) for l in linhas):
                return linhas
        return []
    finally:
        wb.close()


def _achar_cabecalho(linhas: list[list]) -> tuple[int, dict[int, str]]:
    """Procura nas primeiras 15 linhas a que mais parece o cabeçalho
    (>= 3 colunas conhecidas). Devolve (índice da linha, {coluna: chave})."""
    melhor = (0, -1, {})
    for i, linha in enumerate(linhas[:15]):
        mapa = {}
        for j, v in enumerate(linha):
            chave = _APELIDOS_NORMALIZADOS.get(_normalizar_texto(v))
            if chave and chave not in mapa.values():
                mapa[j] = chave
        if len(mapa) > melhor[0]:
            melhor = (len(mapa), i, mapa)
    if melhor[0] < 3:
        raise ErroEnvio("Não achei o cabeçalho da planilha -- use o modelo Fresh Log (botão \"Baixar modelo\") "
                        "e mantenha a primeira linha com os nomes das colunas.")
    return melhor[1], melhor[2]


def ler_planilha(conteudo: bytes, nome: str, cnpj_embarcador: str) -> tuple[list[dict], list[dict]]:
    """Lê a planilha no modelo Fresh Log. Devolve (pedidos, rejeitados):
    cada pedido tem o MESMO formato do dict de ler_nfe (pra prévia, validação
    e gravação serem as mesmas) mais origem/referencia/itens_lista/
    data_expedicao/linhas; rejeitados = {arquivo, rotulo, linha, erro}."""
    linhas = _abrir_planilha(conteudo, nome)
    if not linhas:
        raise ErroEnvio(f"{nome}: a planilha está vazia.")
    i_cab, mapa = _achar_cabecalho(linhas)
    faltando = [c[1] for c in COLUNAS_PLANILHA if c[2] and c[0] not in mapa.values()]
    if faltando:
        raise ErroEnvio(f"{nome}: faltam colunas obrigatórias no cabeçalho: {', '.join(faltando)}. "
                        f"Baixe o modelo Fresh Log pra conferir os nomes.")
    emb = _so_digitos(cnpj_embarcador)
    hoje = date.today()
    grupos: dict[str, dict] = {}
    rejeitados: list[dict] = []
    erros_grupo: dict[str, list[str]] = {}
    total_linhas = 0
    for n, linha in enumerate(linhas[i_cab + 1:], start=i_cab + 2):
        campos = {chave: (linha[j] if j < len(linha) else None) for j, chave in mapa.items()}
        if not any(_celula_texto(v) for v in campos.values()):
            continue
        total_linhas += 1
        if total_linhas > MAX_LINHAS_PLANILHA:
            raise ErroEnvio(f"{nome}: a planilha tem mais de {MAX_LINHAS_PLANILHA} linhas -- divida em arquivos menores.")
        ref = _celula_texto(campos.get("referencia"))
        if not ref:
            rejeitados.append({"arquivo": nome, "rotulo": f"linha {n}", "linha": n, "erro": "Sem o número/referência do pedido."})
            continue
        chave_grupo = ref.strip().upper()
        sku = _celula_texto(campos.get("sku"))
        try:
            qtd = _numero_br(campos.get("quantidade"))
        except ValueError:
            qtd = None
            erros_grupo.setdefault(chave_grupo, []).append(f"linha {n}: quantidade inválida ({_celula_texto(campos.get('quantidade'))}).")
        try:
            vu = _numero_br(campos.get("valor_unitario"))
        except ValueError:
            vu = None
            erros_grupo.setdefault(chave_grupo, []).append(f"linha {n}: valor unitário inválido ({_celula_texto(campos.get('valor_unitario'))}).")
        if not sku:
            erros_grupo.setdefault(chave_grupo, []).append(f"linha {n}: sem SKU.")
        elif qtd is None or qtd <= 0:
            erros_grupo.setdefault(chave_grupo, []).append(f"linha {n}: quantidade precisa ser maior que zero.")
        item = {"sku": sku, "quantidade": qtd or 0, "valor_unitario": vu, "linha": n}

        g = grupos.get(chave_grupo)
        if g is None:
            doc = _so_digitos(campos.get("destinatario_doc"))
            cep = _so_digitos(campos.get("cep"))
            uf = _celula_texto(campos.get("uf")).upper()[:2]
            logradouro = _celula_texto(campos.get("logradouro"))
            numero = _celula_texto(campos.get("numero"))
            compl = _celula_texto(campos.get("complemento"))
            endereco = ", ".join(p for p in (logradouro, numero) if p)
            if compl:
                endereco = f"{endereco} - {compl}" if endereco else compl
            erros = erros_grupo.setdefault(chave_grupo, [])
            if len(doc) not in (11, 14):
                erros.append("CNPJ/CPF do destinatário inválido (precisa ter 14 ou 11 dígitos).")
            if not _celula_texto(campos.get("destinatario_nome")):
                erros.append("Sem o nome do destinatário.")
            if len(cep) != 8:
                erros.append("CEP inválido (8 dígitos).")
            if not logradouro:
                erros.append("Sem o endereço (rua/avenida).")
            if not numero:
                erros.append("Sem o número do endereço.")
            if not _celula_texto(campos.get("bairro")):
                erros.append("Sem o bairro.")
            if not _celula_texto(campos.get("cidade")):
                erros.append("Sem a cidade.")
            if uf not in UFS:
                erros.append(f"UF inválida ({_celula_texto(campos.get('uf')) or 'vazia'}).")
            data_exp = None
            try:
                data_exp = _data_planilha(campos.get("data_expedicao"))
            except ValueError:
                erros.append(f"Data de expedição inválida ({_celula_texto(campos.get('data_expedicao'))}) -- use DD/MM/AAAA.")
            if data_exp and data_exp < hoje:
                erros.append(f"Data de expedição {data_exp.strftime('%d/%m/%Y')} já passou.")
            volumes = peso = valor_total = None
            try:
                volumes = _numero_br(campos.get("volumes"))
                peso = _numero_br(campos.get("peso_kg"))
                valor_total = _numero_br(campos.get("valor_total"))
            except ValueError:
                erros.append("Volumes, peso ou valor total com número inválido.")
            g = grupos[chave_grupo] = {
                "chave_nfe": chave_planilha(emb, ref),
                "origem": ORIGEM_PLANILHA,
                "referencia": ref[:60],
                "numero_nf": _so_digitos(campos.get("numero_nf"))[:20],
                "serie": "",
                "emitida_em": "",
                "emitente_cnpj": emb,
                "emitente_nome": "",
                "destinatario_doc": doc,
                "destinatario_nome": _celula_texto(campos.get("destinatario_nome"))[:120],
                "destinatario_endereco": endereco[:200],
                "destinatario_logradouro": logradouro[:120],
                "destinatario_numero": numero[:20],
                "destinatario_complemento": compl[:60],
                "destinatario_bairro": _celula_texto(campos.get("bairro"))[:80],
                "destinatario_municipio": _celula_texto(campos.get("cidade"))[:80],
                "destinatario_uf": uf,
                "destinatario_cep": cep,
                "destinatario_telefone": _so_digitos(campos.get("telefone"))[:20],
                "destinatario_email": _celula_texto(campos.get("email"))[:120],
                "data_expedicao": (data_exp or hoje).isoformat(),
                "volumes": int(volumes) if volumes else 0,
                "peso_kg": round(peso or 0.0, 3),
                "valor_nf": round(valor_total or 0.0, 2),
                "observacoes": _celula_texto(campos.get("observacoes"))[:500],
                "itens_lista": [],
                "linhas": [],
                "nome_arquivo": nome,
            }
        else:
            doc_linha = _so_digitos(campos.get("destinatario_doc"))
            if doc_linha and doc_linha != g["destinatario_doc"]:
                erros_grupo.setdefault(chave_grupo, []).append(
                    f"linha {n}: destinatário diferente do da primeira linha do pedido {ref} -- use outra referência.")
        g["itens_lista"].append(item)
        g["linhas"].append(n)

    pedidos = []
    for chave_grupo, g in grupos.items():
        erros = erros_grupo.get(chave_grupo) or []
        if not g["itens_lista"] or not any(i["sku"] and i["quantidade"] > 0 for i in g["itens_lista"]):
            erros.append("Nenhum item válido (SKU + quantidade).")
        if erros:
            rejeitados.append({"arquivo": nome, "rotulo": f"Pedido {g['referencia']}", "linha": g["linhas"][0] if g["linhas"] else None,
                               "erro": " ".join(dict.fromkeys(erros))})
            continue
        g["itens"] = len(g["itens_lista"])
        if not g["valor_nf"]:
            g["valor_nf"] = round(sum((i["valor_unitario"] or 0) * i["quantidade"] for i in g["itens_lista"]), 2)
        if not g["volumes"]:
            g["volumes"] = 1
        pedidos.append(g)
    return pedidos, rejeitados


def validar_skus(conn: sqlite3.Connection, pedido: dict, cfg: dict) -> tuple[list[str], list[str]]:
    """Confere os SKUs no catálogo local do Stokki (wms_produtos, sincronizado
    pelo WMS). Se o catálogo tem produtos desse embarcador e o SKU não está
    lá, é ERRO (a Stokki vai recusar); sem catálogo do embarcador, só AVISO."""
    erros, avisos = [], []
    try:
        tem = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='wms_produtos'").fetchone()
    except sqlite3.Error:
        tem = None
    if not tem:
        return erros, ["Catálogo de produtos indisponível -- os SKUs serão conferidos pela Stokki."]
    cid = cfg.get("client_id")
    nome = (cfg.get("nome") or "").strip()
    cond, args = [], []
    if cid:
        cond.append("embarcador_id = ?")
        args.append(int(cid))
    if nome:
        cond.append("LOWER(embarcador) = LOWER(?)")
        args.append(nome)
    if not cond:
        return erros, avisos
    rows = conn.execute(f"SELECT sku, ean, dun, descricao FROM wms_produtos WHERE ativo = 1 AND ({' OR '.join(cond)})", args).fetchall()
    if not rows:
        return erros, ["Catálogo do seu embarcador ainda não sincronizado -- os SKUs serão conferidos pela Stokki."]
    conhecidos = {}
    for r in rows:
        for k in (r["sku"], r["ean"], r["dun"]):
            if k:
                conhecidos[str(k).strip().upper()] = r["descricao"]
    faltam = sorted({i["sku"] for i in pedido.get("itens_lista", []) if str(i["sku"]).strip().upper() not in conhecidos})
    if faltam:
        erros.append("SKU não encontrado no catálogo: " + ", ".join(faltam[:8]) + ("…" if len(faltam) > 8 else "") + ".")
    else:
        for i in pedido.get("itens_lista", []):
            i["descricao"] = conhecidos.get(str(i["sku"]).strip().upper(), "")
    return erros, avisos


def gerar_modelo_planilha(nome_cliente: str = "") -> bytes:
    """Modelo .xlsx pra download: aba Pedidos (cabeçalho + 3 linhas de
    exemplo = 2 pedidos) e aba Instruções."""
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Pedidos"
    cab = Font(bold=True, color="FFFFFF")
    fundo_ob = PatternFill("solid", fgColor="0EA575")
    fundo_op = PatternFill("solid", fgColor="6B7280")
    for j, (chave, rotulo, obrig, _ap, larg, _ex) in enumerate(COLUNAS_PLANILHA, start=1):
        c = ws.cell(row=1, column=j, value=rotulo)
        c.font, c.fill = cab, (fundo_ob if obrig else fundo_op)
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(j)].width = larg
    ws.row_dimensions[1].height = 34
    ws.freeze_panes = "A2"
    exemplos = [
        {c[0]: c[5] for c in COLUNAS_PLANILHA},
        {"referencia": "PED-1001", "sku": "QE-0002", "quantidade": "4", "valor_unitario": "12,50"},
        {"referencia": "PED-1002", "numero_nf": "", "destinatario_doc": "123.456.789-09", "destinatario_nome": "Ana Souza",
         "cep": "04538-132", "logradouro": "Rua Joaquim Floriano", "numero": "100", "complemento": "Apto 31", "bairro": "Itaim Bibi",
         "cidade": "São Paulo", "uf": "SP", "telefone": "(11) 98888-7777", "email": "", "data_expedicao": "", "volumes": "1",
         "peso_kg": "2", "valor_total": "", "observacoes": "Portaria 24h", "sku": "QE-0010", "quantidade": "2", "valor_unitario": "30,00"},
    ]
    for i, ex in enumerate(exemplos, start=2):
        for j, col in enumerate(COLUNAS_PLANILHA, start=1):
            v = ex.get(col[0], "")
            if v != "":
                ws.cell(row=i, column=j, value=v)
    for i in range(2, 2 + len(exemplos)):
        for j in range(1, len(COLUNAS_PLANILHA) + 1):
            ws.cell(row=i, column=j).font = Font(italic=True, color="9CA3AF")

    wi = wb.create_sheet("Instruções")
    wi.column_dimensions["A"].width = 110
    texto = [
        f"Modelo Fresh Log de importação de pedidos{(' · ' + nome_cliente) if nome_cliente else ''}",
        "",
        "1. Cada LINHA é um ITEM (SKU + quantidade). Linhas com o mesmo valor em \"Pedido\" formam um pedido só; os dados do",
        "   destinatário e da entrega são lidos da PRIMEIRA linha do pedido (nas outras podem ficar em branco).",
        "2. Colunas em verde são obrigatórias; em cinza, opcionais. Mantenha os nomes do cabeçalho (a ordem pode mudar).",
        "3. CNPJ/CPF, CEP e telefone podem vir com ou sem pontuação. UF com 2 letras (SP, RJ...).",
        "4. Data de expedição em DD/MM/AAAA; em branco = hoje. Não pode ser uma data passada.",
        "5. SKU é o código do produto cadastrado na Fresh Log/Stokki (também aceita o EAN/GTIN). Quantidade maior que zero.",
        "6. Valor total em branco = soma de quantidade × valor unitário. Volumes em branco = 1.",
        "7. Nº da NF é opcional (use quando a nota já existir -- ajuda a localizar o pedido depois).",
        "8. Apague as linhas de exemplo (em cinza) antes de enviar. Limite: 2.000 linhas por arquivo.",
        "",
        "Ao subir a planilha no portal, você vê uma prévia por pedido, confere o horário de recebimento de cada destinatário",
        "e confirma. Os pedidos entram na fila e são criados na Stokki automaticamente.",
    ]
    for i, t in enumerate(texto, start=1):
        c = wi.cell(row=i, column=1, value=t)
        if i == 1:
            c.font = Font(bold=True, size=13)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def xlsx_pedido_stokki(itens: list[dict]) -> bytes:
    """Gera o xlsx no formato do modelo da Stokki
    (modelo_cadastro_de_pedido_de_saida.xlsx: SKU | Quantidade | Valor
    Unitário) pra um pedido -- é esse arquivo que vai em file_excel[]."""
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Planilha1"
    ws.append(["SKU", "Quantidade", "Valor Unitário"])
    for it in itens:
        q = it.get("quantidade") or 0
        q = int(q) if float(q).is_integer() else float(q)
        vu = it.get("valor_unitario")
        ws.append([str(it.get("sku") or "").strip(), q, float(vu) if vu is not None else None])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ── Arquivos temporários (entre "analisar" e "confirmar") ──────────────────────
# Um token por arquivo: .xml (NF-e), .xlsx/.xls (planilha original) ou .json
# (um pedido lido da planilha, já validado, apontando pro token da planilha).

_EXT_TEMP = (".xml", ".xlsx", ".xls", ".xlsm", ".json")


def guardar_temporario(conteudo: bytes, extensao: str = "xml") -> str:
    PASTA_TEMP.mkdir(parents=True, exist_ok=True)
    ext = (extensao or "xml").lower().lstrip(".")
    if f".{ext}" not in _EXT_TEMP:
        ext = "bin"
    token = secrets.token_urlsafe(18)
    (PASTA_TEMP / f"{token}.{ext}").write_bytes(conteudo)
    return token


def guardar_temporario_pedido(pedido: dict, token_planilha: str) -> str:
    dados = {k: v for k, v in pedido.items()}
    dados["arquivo_token"] = token_planilha
    return guardar_temporario(json.dumps(dados, ensure_ascii=False).encode("utf-8"), "json")


def _caminho_temporario(token: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_\-]{10,40}", token or ""):
        raise ErroEnvio("Arquivo temporário inválido.")
    for ext in _EXT_TEMP:
        caminho = PASTA_TEMP / f"{token}{ext}"
        if caminho.is_file():
            return caminho
    raise ErroEnvio("O arquivo analisado já expirou -- envie o arquivo de novo.")


def limpar_temporarios() -> int:
    if not PASTA_TEMP.is_dir():
        return 0
    limite = time.time() - TEMP_VALIDADE_SEGUNDOS
    removidos = 0
    for p in PASTA_TEMP.iterdir():
        try:
            if p.is_file() and p.stat().st_mtime < limite:
                p.unlink()
                removidos += 1
        except OSError:
            pass
    return removidos


def _caminho_definitivo(cnpj_embarcador: str, chave: str) -> Path:
    pasta = PASTA_XMLS / _so_digitos(cnpj_embarcador)
    pasta.mkdir(parents=True, exist_ok=True)
    return pasta / f"{chave}.xml"


def _caminho_definitivo_planilha(cnpj_embarcador: str, nome_original: str) -> Path:
    pasta = PASTA_XMLS / _so_digitos(cnpj_embarcador) / "planilhas"
    pasta.mkdir(parents=True, exist_ok=True)
    seguro = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(nome_original or "planilha.xlsx").name)[:80] or "planilha.xlsx"
    return pasta / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(3)}_{seguro}"


# ── Validações (item 9: NF-e válida, chave já enviada, emitente = cliente) ─────

def validar_item(conn: sqlite3.Connection, item: dict, cnpj_cliente: str) -> dict:
    """Devolve {ok, erros[], avisos[], envio_existente} pro item lido."""
    erros, avisos = [], []
    planilha = item.get("origem") == ORIGEM_PLANILHA
    existente = conn.execute(
        "SELECT id, status, criado_em, codigo_pedido FROM portal_envios WHERE chave_nfe = ?", (item["chave_nfe"],)
    ).fetchone()
    if item["emitente_cnpj"] != _so_digitos(cnpj_cliente):
        erros.append(f"O CNPJ emitente da nota ({formatar_documento(item['emitente_cnpj'])} · {item['emitente_nome']}) "
                     f"não é o da sua empresa.")
    if existente and existente["status"] not in (STATUS_ERRO, STATUS_CANCELADO):
        quando = (existente["criado_em"] or "")[:16].replace("-", "/")
        if quando:
            quando = f"{quando[8:10]}/{quando[5:7]}/{quando[:4]} {quando[11:16]}"
        o_que = f"O pedido {item.get('referencia')} já foi enviado" if planilha else "Essa NF-e já foi enviada"
        erros.append(f"{o_que} em {quando} ({ROTULOS_STATUS.get(existente['status'], existente['status'])}"
                     + (f", pedido {existente['codigo_pedido']}" if existente["codigo_pedido"] else "") + ").")
    elif existente:
        avisos.append(f"Já esteve na fila ({ROTULOS_STATUS[existente['status']]}) -- será reenviado.")
    if not item["destinatario_doc"]:
        erros.append("Sem CNPJ/CPF de destinatário." if planilha else "NF-e sem CNPJ/CPF de destinatário.")
    if not item["destinatario_endereco"]:
        avisos.append("Endereço do destinatário vazio" + ("." if planilha else " no XML."))
    return {"ok": not erros, "erros": erros, "avisos": avisos,
            "envio_existente": dict(existente) if existente else None}


# ── Destinatários: horário de recebimento + agendamento (item 8) ───────────────

def _conjunto_agendamento(config: dict | None) -> set[str]:
    """Documentos flegados como "Agendamento" na planilha BD_CLIENTES
    (mesma fonte da roteirização)."""
    try:
        from regras.clientes_agendamento import carregar_clientes_agendamento
        caminho = ((config or {}).get("clientes_agendamento") or {}).get("planilha", "")
        return carregar_clientes_agendamento(caminho) if caminho else set()
    except Exception as e:
        logger.warning(f"[envios] planilha de agendamento indisponível: {e}")
        return set()


def _horarios_conhecidos(config: dict | None, documentos: set[str]) -> dict[str, tuple[str, str]]:
    """Horário já conhecido pela roteirização: ajuste manual (tela de
    Planejamento) > planilha BD_CLIENTES."""
    saida: dict[str, tuple[str, str]] = {}
    try:
        from regras.complexidade_entrega import carregar_ajustes_manuais, carregar_horarios
        for doc, aj in carregar_ajustes_manuais().items():
            if doc in documentos:
                saida[doc] = (aj["horario_atendimento_inicio"], aj["horario_atendimento_fim"])
        caminho = ((config or {}).get("complexidade_entrega") or {}).get("planilha", "")
        if caminho:
            for doc, (ini, fim) in carregar_horarios(caminho).items():
                if doc in documentos and doc not in saida:
                    saida[doc] = (ini, fim)
    except Exception as e:
        logger.warning(f"[envios] horários da roteirização indisponíveis: {e}")
    return saida


def info_destinatarios(conn: sqlite3.Connection, cnpj_embarcador: str, itens: list[dict], config: dict | None) -> dict[str, dict]:
    """{documento: {nome, horario_inicio, horario_fim, requer_agendamento,
    conhecido}} -- 'conhecido' False = o portal precisa perguntar o horário
    (uma vez por CNPJ/CPF, item 8)."""
    docs = {i["destinatario_doc"] for i in itens if i.get("destinatario_doc")}
    if not docs:
        return {}
    nomes = {}
    for i in itens:
        nomes.setdefault(i.get("destinatario_doc"), i.get("destinatario_nome"))
    emb = _so_digitos(cnpj_embarcador)
    proprios = {r["documento"]: dict(r) for r in conn.execute(
        f"SELECT * FROM portal_destinatarios WHERE cnpj_embarcador = ? AND documento IN ({','.join('?' * len(docs))})",
        (emb, *sorted(docs)))}
    externos = _horarios_conhecidos(config, docs)
    agendamento = _conjunto_agendamento(config)
    saida = {}
    for doc in docs:
        p = proprios.get(doc)
        if p and p.get("horario_inicio") and p.get("horario_fim"):
            ini, fim, conhecido, origem = p["horario_inicio"], p["horario_fim"], True, "portal"
        elif doc in externos:
            ini, fim = externos[doc]
            conhecido, origem = True, "cadastro"
        else:
            ini, fim, conhecido, origem = "", "", False, ""
        saida[doc] = {
            "documento": doc, "documento_formatado": formatar_documento(doc), "nome": nomes.get(doc) or (p or {}).get("nome") or "",
            "horario_inicio": ini, "horario_fim": fim, "conhecido": conhecido, "origem": origem,
            "requer_agendamento": bool((p or {}).get("requer_agendamento")) or (doc in agendamento),
        }
    return saida


def gravar_destinatario(conn: sqlite3.Connection, cnpj_embarcador: str, documento: str, nome: str,
                        horario_inicio: str, horario_fim: str, requer_agendamento: bool, config: dict | None = None) -> None:
    """Guarda o horário de recebimento do destinatário e repassa pra
    roteirização (ajustes_complexidade_cliente, a mesma tabela que a tela
    de Planejamento usa -- prioridade sobre a planilha)."""
    ini, fim = _hora_valida(horario_inicio), _hora_valida(horario_fim)
    if not ini or not fim:
        raise ErroEnvio("Informe o horário de recebimento no formato HH:MM (início e fim).")
    if ini >= fim:
        raise ErroEnvio("O horário final de recebimento precisa ser depois do inicial.")
    doc = _so_digitos(documento)
    conn.execute("""
        INSERT INTO portal_destinatarios (cnpj_embarcador, documento, nome, horario_inicio, horario_fim, requer_agendamento, atualizado_em)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(cnpj_embarcador, documento) DO UPDATE SET
            nome = COALESCE(NULLIF(excluded.nome, ''), portal_destinatarios.nome),
            horario_inicio = excluded.horario_inicio, horario_fim = excluded.horario_fim,
            requer_agendamento = excluded.requer_agendamento, atualizado_em = excluded.atualizado_em
    """, (_so_digitos(cnpj_embarcador), doc, nome or "", ini, fim, int(bool(requer_agendamento)), _agora()))
    conn.commit()
    try:
        from regras.complexidade_entrega import (carregar_ajustes_manuais, carregar_niveis, classificar_nivel,
                                                 definir_ajuste_manual)
        ajuste = carregar_ajustes_manuais().get(doc)
        if ajuste:
            nivel = ajuste["nivel_dificuldade"]
        else:
            mapa = {}
            caminho = ((config or {}).get("complexidade_entrega") or {}).get("planilha", "")
            if caminho:
                try:
                    mapa = carregar_niveis(caminho)
                except Exception:
                    mapa = {}
            nivel, _, _ = classificar_nivel(doc, mapa)
        definir_ajuste_manual(doc, nivel, ini, fim)
    except Exception as e:
        logger.warning(f"[envios] não consegui repassar o horário de {doc} pra roteirização: {e}")


# ── Parâmetros da Stokki por embarcador ────────────────────────────────────────

def config_stokki_cliente(conn: sqlite3.Connection, cnpj_embarcador: str, config: dict | None) -> dict:
    """Parâmetros do wizard de importação pra este embarcador: client_id da
    tabela interno (stkkc_id); o resto de portal_clientes_envio, com
    padrão em config.yaml (portal_cliente.stokki_padrao)."""
    emb = _so_digitos(cnpj_embarcador)
    row = conn.execute("SELECT cnpj_embarcador, apelido, nome_remetente, stkkc_id, sender_id, email FROM interno WHERE cnpj_embarcador = ?",
                       (emb,)).fetchone()
    if not row:
        raise ErroEnvio("Embarcador não cadastrado (tabela interno).")
    padrao = ((config or {}).get("portal_cliente") or {}).get("stokki_padrao") or {}
    proprio = conn.execute("SELECT * FROM portal_clientes_envio WHERE cnpj = ?", (emb,)).fetchone()
    proprio = dict(proprio) if proprio else {}
    stkkc = row["stkkc_id"]
    regra = proprio.get("regra_xml") or REGRAS_PADRAO_POR_STKKC.get(int(stkkc) if stkkc else -1, "nenhuma")
    return {
        "cnpj": emb,
        "nome": row["apelido"] or row["nome_remetente"] or emb,
        "emails": [e.strip() for e in re.split(r"[,;\t]+", str(row["email"] or "")) if "@" in e],
        "sender_id": row["sender_id"],
        "client_id": str(stkkc) if stkkc else "",
        "warehouse_id": str(proprio.get("warehouse_id") or padrao.get("warehouse_id") or "1"),
        "tipo_transporte": proprio.get("tipo_transporte") or padrao.get("tipo_transporte") or "Fractional (LTL)",
        "embalagem": proprio.get("embalagem") or padrao.get("embalagem") or "Loose Cargo (Boxes)",
        "url_importacao": padrao.get("url_importacao")
        or "https://freshlog.stokki.com.br/pt-br/administrator/inventory/outbound/sale/xml/create/Sale",
        # Importação por planilha (09/09): wizard Excel da Stokki. carrier_id é
        # obrigatório lá (select carregado por cliente); vazio = o worker pega a
        # primeira transportadora do cliente (ou a que casar com carrier_nome).
        "url_importacao_excel": padrao.get("url_importacao_excel")
        or "https://freshlog.stokki.com.br/pt-br/administrator/inventory/outbound/create/excel",
        "carrier_id": str(proprio.get("carrier_id") or padrao.get("carrier_id") or ""),
        "carrier_nome": padrao.get("carrier_nome") or "",
        "prioridade": proprio.get("prioridade") or padrao.get("prioridade") or "",
        "regra_xml": regra if regra in REGRAS_XML else "nenhuma",
        "envio_ativo": bool(proprio.get("envio_ativo", 1)),
    }


def definir_parametros_cliente(conn: sqlite3.Connection, cnpj: str, **campos) -> None:
    permitidos = {"regra_xml", "warehouse_id", "tipo_transporte", "embalagem", "envio_ativo", "carrier_id", "prioridade"}
    campos = {k: v for k, v in campos.items() if k in permitidos and v is not None}
    if "regra_xml" in campos and campos["regra_xml"] not in REGRAS_XML:
        raise ErroEnvio(f"Regra de XML desconhecida: {campos['regra_xml']!r} (válidas: {', '.join(REGRAS_XML)}).")
    atual = conn.execute("SELECT * FROM portal_clientes_envio WHERE cnpj = ?", (_so_digitos(cnpj),)).fetchone()
    base = dict(atual) if atual else {"regra_xml": None, "warehouse_id": None, "tipo_transporte": None, "embalagem": None,
                                      "envio_ativo": 1, "carrier_id": None, "prioridade": None}
    base.update(campos)
    conn.execute("""
        INSERT INTO portal_clientes_envio (cnpj, regra_xml, warehouse_id, tipo_transporte, embalagem, envio_ativo, carrier_id, prioridade, atualizado_em)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(cnpj) DO UPDATE SET regra_xml = excluded.regra_xml, warehouse_id = excluded.warehouse_id,
            tipo_transporte = excluded.tipo_transporte, embalagem = excluded.embalagem,
            envio_ativo = excluded.envio_ativo, carrier_id = excluded.carrier_id, prioridade = excluded.prioridade,
            atualizado_em = excluded.atualizado_em
    """, (_so_digitos(cnpj), base["regra_xml"], base["warehouse_id"], base["tipo_transporte"], base["embalagem"],
          int(bool(base["envio_ativo"])), base.get("carrier_id") or None, base.get("prioridade") or None, _agora()))
    conn.commit()


# ── Confirmar envio (entra na fila) ────────────────────────────────────────────

def confirmar_envios(conn: sqlite3.Connection, cnpj_embarcador: str, itens: list[dict], enviado_por: str,
                     config: dict | None, regra_xml: str) -> list[dict]:
    """Cada item: {token, horario_inicio?, horario_fim?, agendamento_data?,
    agendamento_hora_inicio?, agendamento_hora_fim?, agendamento_pendente?,
    observacoes?}. Relê o XML temporário (nunca confia no que o navegador
    mandou), revalida, grava o definitivo e cria a linha NA_FILA."""
    emb = _so_digitos(cnpj_embarcador)
    criados = []
    lidos = []
    for item in itens:
        caminho = _caminho_temporario(item.get("token", ""))
        conteudo = caminho.read_bytes()
        if caminho.suffix == ".json":
            # pedido lido de uma planilha (gravado pelo servidor em analisar)
            nfe = json.loads(conteudo.decode("utf-8"))
            if nfe.get("origem") != ORIGEM_PLANILHA or _so_digitos(nfe.get("emitente_cnpj")) != emb:
                raise ErroEnvio("Pedido temporário inválido -- envie a planilha de novo.")
            if nfe.get("data_expedicao") and nfe["data_expedicao"] < date.today().isoformat():
                nfe["data_expedicao"] = date.today().isoformat()
        else:
            nfe = ler_nfe(conteudo)
            nfe["origem"] = ORIGEM_XML
        v = validar_item(conn, nfe, emb)
        if not v["ok"]:
            raise ErroEnvio(f"{rotulo_envio(nfe)}: " + " ".join(v["erros"]))
        lidos.append((item, caminho, conteudo, nfe, v["envio_existente"]))

    dest_info = info_destinatarios(conn, emb, [x[3] for x in lidos], config)
    hoje = date.today()
    planilhas_movidas: dict[str, Path] = {}
    for item, caminho, conteudo, nfe, existente in lidos:
        d = dest_info.get(nfe["destinatario_doc"], {})
        ini = _hora_valida(item.get("horario_inicio")) or d.get("horario_inicio") or ""
        fim = _hora_valida(item.get("horario_fim")) or d.get("horario_fim") or ""
        if not ini or not fim:
            raise ErroEnvio(f"{rotulo_envio(nfe)}: informe o horário de recebimento de {nfe['destinatario_nome']}.")
        if not d.get("conhecido") or (item.get("horario_inicio") and (ini, fim) != (d.get("horario_inicio"), d.get("horario_fim"))):
            gravar_destinatario(conn, emb, nfe["destinatario_doc"], nfe["destinatario_nome"], ini, fim,
                                bool(item.get("requer_agendamento", d.get("requer_agendamento"))), config)

        requer = bool(item.get("requer_agendamento", d.get("requer_agendamento")))
        ag_data = ag_ini = ag_fim = None
        pendente = 0
        if requer:
            if item.get("agendamento_pendente"):
                pendente = 1
            else:
                try:
                    ag = date.fromisoformat(str(item.get("agendamento_data") or ""))
                except ValueError:
                    raise ErroEnvio(f"{rotulo_envio(nfe)}: {nfe['destinatario_nome']} recebe só com agendamento -- "
                                    f"informe a data ou marque 'Agendamento pendente'.")
                if ag < hoje:
                    raise ErroEnvio(f"{rotulo_envio(nfe)}: a data de agendamento precisa ser a partir de hoje.")
                ag_data = ag.isoformat()
                ag_ini = _hora_valida(item.get("agendamento_hora_inicio")) or ini
                ag_fim = _hora_valida(item.get("agendamento_hora_fim")) or fim

        planilha = nfe.get("origem") == ORIGEM_PLANILHA
        if planilha:
            # a planilha original é guardada UMA vez por arquivo; cada pedido
            # aponta pra ela (xml_path) e leva os próprios itens em itens_json
            tk = nfe.get("arquivo_token", "")
            destino = planilhas_movidas.get(tk)
            if destino is None:
                origem_plan = _caminho_temporario(tk)
                destino = _caminho_definitivo_planilha(emb, nfe.get("nome_arquivo") or origem_plan.name)
                destino.write_bytes(origem_plan.read_bytes())
                planilhas_movidas[tk] = destino
                try:
                    origem_plan.unlink()
                except OSError:
                    pass
        else:
            destino = _caminho_definitivo(emb, nfe["chave_nfe"])
            destino.write_bytes(conteudo)
        try:
            caminho.unlink()
        except OSError:
            pass
        agora = _agora()
        obs = (item.get("observacoes") or nfe.get("observacoes") or "")[:500]
        campos = {
            "cnpj_embarcador": emb, "chave_nfe": nfe["chave_nfe"], "numero_nf": nfe["numero_nf"], "serie": nfe["serie"],
            "emitida_em": nfe["emitida_em"], "destinatario_doc": nfe["destinatario_doc"], "destinatario_nome": nfe["destinatario_nome"],
            "destinatario_endereco": nfe["destinatario_endereco"], "destinatario_bairro": nfe["destinatario_bairro"],
            "destinatario_municipio": nfe["destinatario_municipio"], "destinatario_uf": nfe["destinatario_uf"],
            "destinatario_cep": nfe["destinatario_cep"], "destinatario_telefone": nfe["destinatario_telefone"],
            "volumes": nfe["volumes"], "peso_kg": nfe["peso_kg"], "valor_nf": nfe["valor_nf"], "itens": nfe["itens"],
            "xml_path": str(destino.relative_to(_RAIZ)), "regra_xml": None if planilha else regra_xml,
            "status": STATUS_NA_FILA, "tentativas": 0,
            "erro": None, "resposta_stokki": None, "codigo_pedido": None, "horario_inicio": ini, "horario_fim": fim,
            "requer_agendamento": int(requer), "agendamento_data": ag_data, "agendamento_hora_inicio": ag_ini,
            "agendamento_hora_fim": ag_fim, "agendamento_pendente": pendente, "agendamento_aplicado_em": None,
            "observacoes": obs, "enviado_por": enviado_por,
            "criado_em": agora, "atualizado_em": agora, "enviado_stokki_em": None, "criado_stokki_em": None,
            "origem": ORIGEM_PLANILHA if planilha else ORIGEM_XML,
            "referencia": nfe.get("referencia") if planilha else None,
            "itens_json": json.dumps({
                "itens": nfe.get("itens_lista") or [],
                "destinatario": {k: nfe.get(k, "") for k in ("destinatario_logradouro", "destinatario_numero", "destinatario_complemento",
                                                             "destinatario_email")},
            }, ensure_ascii=False) if planilha else None,
            "data_expedicao": nfe.get("data_expedicao") if planilha else None,
            "linhas_planilha": ",".join(str(n) for n in nfe.get("linhas") or []) if planilha else None,
        }
        if existente:
            sets = ", ".join(f"{k} = ?" for k in campos if k != "chave_nfe")
            conn.execute(f"UPDATE portal_envios SET {sets} WHERE id = ?",
                         (*[v for k, v in campos.items() if k != "chave_nfe"], existente["id"]))
            envio_id = existente["id"]
        else:
            cols = ", ".join(campos)
            conn.execute(f"INSERT INTO portal_envios ({cols}) VALUES ({','.join('?' * len(campos))})", tuple(campos.values()))
            envio_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        criados.append({"id": envio_id, "numero_nf": nfe["numero_nf"], "referencia": nfe.get("referencia"),
                        "origem": campos["origem"], "destinatario_nome": nfe["destinatario_nome"]})
    conn.commit()
    return criados


# ── Listagem / detalhe ─────────────────────────────────────────────────────────

def _dt_br(valor: str | None) -> str:
    if not valor:
        return ""
    try:
        return datetime.strptime(valor[:19], "%Y-%m-%d %H:%M:%S").strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return valor


def _linha(r: sqlite3.Row, solicitacoes: dict[int, list]) -> dict:
    d = dict(r)
    pend = [s for s in solicitacoes.get(d["id"], []) if s["status"] == "PENDENTE"]
    d.setdefault("origem", ORIGEM_XML)
    d.update({
        "status_rotulo": ROTULOS_STATUS.get(d["status"], d["status"]),
        "criado_em_br": _dt_br(d["criado_em"]),
        "criado_stokki_em_br": _dt_br(d["criado_stokki_em"]),
        "emitida_em_br": _dt_br(d["emitida_em"]),
        "destinatario_doc_formatado": formatar_documento(d["destinatario_doc"] or ""),
        "endereco_curto": ", ".join(p for p in (d["destinatario_endereco"], d["destinatario_bairro"], d["destinatario_municipio"]) if p),
        "agendamento_data_br": "/".join(reversed(d["agendamento_data"].split("-"))) if d.get("agendamento_data") else "",
        "rotulo": rotulo_envio(d),
        "documento_curto": d.get("numero_nf") or d.get("referencia") or "",
        "data_expedicao_br": "/".join(reversed(d["data_expedicao"].split("-"))) if d.get("data_expedicao") else "",
        "arquivo_ext": Path(d.get("xml_path") or "").suffix.lstrip(".").lower() or "xml",
        "solicitacoes_pendentes": [{"tipo": s["tipo"], "rotulo": ROTULOS_SOLICITACAO.get(s["tipo"], s["tipo"]),
                                    "criado_em_br": _dt_br(s["criado_em"]), "detalhes": s["detalhes"]} for s in pend],
        "pode_cancelar": d["status"] in (STATUS_NA_FILA, STATUS_CRIADO, STATUS_ERRO, STATUS_DUPLICADO) and not any(s["tipo"] == "cancelar" for s in pend),
        "pode_em_espera": d["status"] in (STATUS_CRIADO, STATUS_DUPLICADO) and not any(s["tipo"] in ("em_espera", "cancelar") for s in pend),
        "pode_reagendar": d["status"] in (STATUS_NA_FILA, STATUS_CRIADO, STATUS_DUPLICADO),
        "pode_reenviar": d["status"] in (STATUS_ERRO, STATUS_CANCELADO),
    })
    d.pop("xml_path", None)
    d.pop("itens_json", None)
    return d


def listar_envios(conn: sqlite3.Connection, cnpj_embarcador: str, dias: int = DIAS_LISTAGEM) -> list[dict]:
    desde = (datetime.now() - timedelta(days=dias)).strftime("%Y-%m-%d 00:00:00")
    emb = _so_digitos(cnpj_embarcador)
    rows = conn.execute(
        "SELECT * FROM portal_envios WHERE cnpj_embarcador = ? AND (criado_em >= ? OR status IN ('NA_FILA','ENVIANDO')) "
        "ORDER BY criado_em DESC, id DESC", (emb, desde)).fetchall()
    sol = {}
    for s in conn.execute("SELECT * FROM portal_solicitacoes WHERE cnpj_embarcador = ? AND status = 'PENDENTE'", (emb,)):
        sol.setdefault(s["envio_id"], []).append(dict(s))
    return [_linha(r, sol) for r in rows]


def resumo_envios(envios: list[dict]) -> dict:
    hoje = date.today().strftime("%d/%m/%Y")
    return {
        "total": len(envios),
        "hoje": sum(1 for e in envios if (e.get("criado_em_br") or "").startswith(hoje)),
        "na_fila": sum(1 for e in envios if e["status"] in STATUS_ABERTOS),
        "criados": sum(1 for e in envios if e["status"] == STATUS_CRIADO),
        "erros": sum(1 for e in envios if e["status"] == STATUS_ERRO),
        "agendamento_pendente": sum(1 for e in envios if e.get("agendamento_pendente") and e["status"] != STATUS_CANCELADO),
        "solicitacoes_pendentes": sum(len(e["solicitacoes_pendentes"]) for e in envios),
    }


def buscar_envio(conn: sqlite3.Connection, envio_id: int, cnpj_embarcador: str) -> dict | None:
    r = conn.execute("SELECT * FROM portal_envios WHERE id = ? AND cnpj_embarcador = ?",
                     (envio_id, _so_digitos(cnpj_embarcador))).fetchone()
    return dict(r) if r else None


def caminho_xml(envio: dict) -> Path:
    return _RAIZ / envio["xml_path"]


# ── Ações do cliente (item 12) ─────────────────────────────────────────────────

def _registrar_solicitacao(conn, envio: dict, tipo: str, detalhes: str, por: str, status: str = "PENDENTE") -> int:
    conn.execute("""
        INSERT INTO portal_solicitacoes (envio_id, cnpj_embarcador, tipo, detalhes, status, solicitado_por, criado_em, concluido_em)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (envio["id"], envio["cnpj_embarcador"], tipo, detalhes, status, por, _agora(), _agora() if status != "PENDENTE" else None))
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def aplicar_acao(conn: sqlite3.Connection, envio: dict, tipo: str, dados: dict, por: str) -> dict:
    """Devolve {aplicado: bool, mensagem, precisa_operacao: bool}.
    - cancelar/reenviar antes de ir pra Stokki: resolve aqui mesmo.
    - o resto (pedido já criado na Stokki) vira solicitação PENDENTE pra
      operação (e-mail é disparado por quem chama), e o acompanhamento
      mostra a pendência."""
    if tipo not in TIPOS_SOLICITACAO:
        raise ErroEnvio("Ação inválida.")
    st = envio["status"]
    agora = _agora()
    if tipo == "cancelar":
        if st == STATUS_ENVIANDO:
            raise ErroEnvio("Esse pedido está sendo enviado à Stokki agora -- tente de novo em alguns instantes.")
        if st == STATUS_CANCELADO:
            raise ErroEnvio("Esse pedido já está cancelado.")
        if st in (STATUS_NA_FILA, STATUS_ERRO):
            conn.execute("UPDATE portal_envios SET status = ?, atualizado_em = ? WHERE id = ?", (STATUS_CANCELADO, agora, envio["id"]))
            _registrar_solicitacao(conn, envio, tipo, dados.get("motivo", ""), por, status="CONCLUIDA")
            conn.commit()
            return {"aplicado": True, "precisa_operacao": False, "mensagem": "Pedido cancelado -- não será enviado à Stokki."}
        _registrar_solicitacao(conn, envio, tipo, dados.get("motivo", ""), por)
        conn.commit()
        return {"aplicado": False, "precisa_operacao": True,
                "mensagem": "Pedido de cancelamento registrado. A Fresh Log vai cancelar o pedido na Stokki e você verá aqui quando estiver feito."}

    if tipo == "reenviar":
        if st not in (STATUS_ERRO, STATUS_CANCELADO):
            raise ErroEnvio("Só pedidos com erro ou cancelados podem ser reenviados.")
        conn.execute("UPDATE portal_envios SET status = ?, erro = NULL, atualizado_em = ? WHERE id = ?",
                     (STATUS_NA_FILA, agora, envio["id"]))
        _registrar_solicitacao(conn, envio, tipo, "", por, status="CONCLUIDA")
        conn.commit()
        return {"aplicado": True, "precisa_operacao": False, "mensagem": "Pedido de volta à fila -- será enviado à Stokki em instantes."}

    if tipo == "em_espera":
        if st not in (STATUS_CRIADO, STATUS_DUPLICADO):
            raise ErroEnvio("Só um pedido já criado na Stokki pode ser tirado da rota.")
        _registrar_solicitacao(conn, envio, tipo, dados.get("motivo", ""), por)
        conn.commit()
        return {"aplicado": False, "precisa_operacao": True,
                "mensagem": "Pedido de retirada da rota registrado. A Fresh Log vai colocar o pedido em espera e avisar aqui."}

    # reagendar
    try:
        nova = date.fromisoformat(str(dados.get("data") or ""))
    except ValueError:
        raise ErroEnvio("Informe a nova data de entrega.")
    if nova < date.today():
        raise ErroEnvio("A nova data precisa ser a partir de hoje.")
    ini = _hora_valida(dados.get("hora_inicio")) or envio.get("horario_inicio") or "08:00"
    fim = _hora_valida(dados.get("hora_fim")) or envio.get("horario_fim") or "18:00"
    conn.execute("""
        UPDATE portal_envios SET agendamento_data = ?, agendamento_hora_inicio = ?, agendamento_hora_fim = ?,
            agendamento_pendente = 0, requer_agendamento = 1, agendamento_aplicado_em = NULL, atualizado_em = ? WHERE id = ?
    """, (nova.isoformat(), ini, fim, agora, envio["id"]))
    detalhe = f"{nova.strftime('%d/%m/%Y')} {ini}-{fim}"
    if st in (STATUS_NA_FILA, STATUS_ERRO):
        _registrar_solicitacao(conn, envio, tipo, detalhe, por, status="CONCLUIDA")
        conn.commit()
        return {"aplicado": True, "precisa_operacao": False, "mensagem": f"Agendamento definido pra {detalhe}."}
    if envio.get("codigo_pedido"):
        # já sabemos o PS-xxxxx: entra no fluxo normal de agendamento da
        # roteirização (atualizar_agendamentos_confirmados aplica no VUUPT)
        registrar_agendamento_pedido(conn, {**envio, "agendamento_data": nova.isoformat(),
                                            "agendamento_hora_inicio": ini, "agendamento_hora_fim": fim}, por)
        _registrar_solicitacao(conn, envio, tipo, detalhe, por, status="CONCLUIDA")
        conn.commit()
        return {"aplicado": True, "precisa_operacao": False,
                "mensagem": f"Reagendado pra {detalhe}. A nova data entra na roteirização automaticamente."}
    _registrar_solicitacao(conn, envio, tipo, detalhe, por)
    conn.commit()
    return {"aplicado": False, "precisa_operacao": True,
            "mensagem": f"Reagendamento pra {detalhe} registrado -- a Fresh Log aplica assim que o pedido estiver identificado."}


def registrar_agendamento_pedido(conn: sqlite3.Connection, envio: dict, por: str = "portal") -> bool:
    """Grava em agendamentos_pedido (status RESPONDIDO) pra
    atualizar_agendamentos_confirmados.py aplicar a data no VUUPT --
    precisa do código PS-xxxxx e da data. Formato de data lá é DD/MM/YYYY."""
    if not envio.get("codigo_pedido") or not envio.get("agendamento_data"):
        return False
    data_br = "/".join(reversed(envio["agendamento_data"].split("-")))
    try:
        conn.execute("""
            INSERT INTO agendamentos_pedido (pedido, cnpj_destinatario, nome_destinatario, cnpj_embarcador, numero_nf,
                email_embarcador, status, data_agendada, horario_inicio_agendado, horario_fim_agendado, resposta_texto,
                solicitado_em, respondido_em)
            VALUES (?, ?, ?, ?, ?, NULL, 'RESPONDIDO', ?, ?, ?, ?, datetime('now','localtime'), datetime('now','localtime'))
            ON CONFLICT(pedido) DO UPDATE SET status = 'RESPONDIDO', data_agendada = excluded.data_agendada,
                horario_inicio_agendado = excluded.horario_inicio_agendado, horario_fim_agendado = excluded.horario_fim_agendado,
                resposta_texto = excluded.resposta_texto, respondido_em = excluded.respondido_em, aplicado_vuupt_em = NULL
        """, (envio["codigo_pedido"], envio.get("destinatario_doc") or "", envio.get("destinatario_nome"), envio["cnpj_embarcador"],
              envio.get("numero_nf"), data_br, envio.get("agendamento_hora_inicio"), envio.get("agendamento_hora_fim"),
              f"Definido pelo portal do cliente ({por})"))
        conn.execute("UPDATE portal_envios SET agendamento_aplicado_em = ?, atualizado_em = ? WHERE id = ?",
                     (_agora(), _agora(), envio["id"]))
        conn.commit()
        return True
    except sqlite3.OperationalError as e:
        logger.warning(f"[envios] agendamentos_pedido indisponível ({e}) -- agendamento do envio {envio['id']} fica só no portal.")
        return False


def listar_solicitacoes_pendentes(conn: sqlite3.Connection, cnpj_embarcador: str | None = None) -> list[dict]:
    sql = ("SELECT s.*, e.numero_nf, e.referencia, e.origem, e.destinatario_nome, e.codigo_pedido, e.status AS status_envio "
           "FROM portal_solicitacoes s JOIN portal_envios e ON e.id = s.envio_id WHERE s.status = 'PENDENTE'")
    args: tuple = ()
    if cnpj_embarcador:
        sql += " AND s.cnpj_embarcador = ?"
        args = (_so_digitos(cnpj_embarcador),)
    return [dict(r) for r in conn.execute(sql + " ORDER BY s.criado_em", args)]


def concluir_solicitacao(conn: sqlite3.Connection, solicitacao_id: int, resposta: str, recusada: bool = False) -> None:
    conn.execute("UPDATE portal_solicitacoes SET status = ?, resposta = ?, concluido_em = ? WHERE id = ?",
                 ("RECUSADA" if recusada else "CONCLUIDA", resposta, _agora(), solicitacao_id))
    conn.commit()
