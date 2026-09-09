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
"""
import io
import json
import logging
import re
import secrets
import sqlite3
import sys
import time
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
    conn.commit()
    return conn


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


def expandir_upload(nome: str, conteudo: bytes) -> list[tuple[str, bytes]]:
    """Um .xml vira ele mesmo; um .zip vira cada .xml de dentro (o cliente
    costuma receber os XMLs zipados do emissor)."""
    nome_baixo = (nome or "").lower()
    if nome_baixo.endswith(".zip") or conteudo[:2] == b"PK":
        saida = []
        try:
            with zipfile.ZipFile(io.BytesIO(conteudo)) as zf:
                for info in zf.infolist():
                    if info.is_dir() or not info.filename.lower().endswith(".xml"):
                        continue
                    if info.file_size > 5 * 1024 * 1024:
                        continue
                    saida.append((Path(info.filename).name, zf.read(info)))
        except zipfile.BadZipFile:
            raise ErroEnvio(f"{nome}: ZIP inválido ou corrompido.")
        if not saida:
            raise ErroEnvio(f"{nome}: o ZIP não tem nenhum arquivo .xml dentro.")
        return saida
    return [(Path(nome or "arquivo.xml").name, conteudo)]


# ── Arquivos temporários (entre "analisar" e "confirmar") ──────────────────────

def guardar_temporario(conteudo: bytes) -> str:
    PASTA_TEMP.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(18)
    (PASTA_TEMP / f"{token}.xml").write_bytes(conteudo)
    return token


def _caminho_temporario(token: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_\-]{10,40}", token or ""):
        raise ErroEnvio("Arquivo temporário inválido.")
    caminho = PASTA_TEMP / f"{token}.xml"
    if not caminho.is_file():
        raise ErroEnvio("O arquivo analisado já expirou -- envie o XML de novo.")
    return caminho


def limpar_temporarios() -> int:
    if not PASTA_TEMP.is_dir():
        return 0
    limite = time.time() - TEMP_VALIDADE_SEGUNDOS
    removidos = 0
    for p in PASTA_TEMP.glob("*.xml"):
        try:
            if p.stat().st_mtime < limite:
                p.unlink()
                removidos += 1
        except OSError:
            pass
    return removidos


def _caminho_definitivo(cnpj_embarcador: str, chave: str) -> Path:
    pasta = PASTA_XMLS / _so_digitos(cnpj_embarcador)
    pasta.mkdir(parents=True, exist_ok=True)
    return pasta / f"{chave}.xml"


# ── Validações (item 9: NF-e válida, chave já enviada, emitente = cliente) ─────

def validar_item(conn: sqlite3.Connection, item: dict, cnpj_cliente: str) -> dict:
    """Devolve {ok, erros[], avisos[], envio_existente} pro item lido."""
    erros, avisos = [], []
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
        erros.append(f"Essa NF-e já foi enviada em {quando} ({ROTULOS_STATUS.get(existente['status'], existente['status'])}"
                     + (f", pedido {existente['codigo_pedido']}" if existente["codigo_pedido"] else "") + ").")
    elif existente:
        avisos.append(f"Já esteve na fila ({ROTULOS_STATUS[existente['status']]}) -- será reenviada.")
    if not item["destinatario_doc"]:
        erros.append("NF-e sem CNPJ/CPF de destinatário.")
    if not item["destinatario_endereco"]:
        avisos.append("Endereço do destinatário vazio no XML.")
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
        "regra_xml": regra if regra in REGRAS_XML else "nenhuma",
        "envio_ativo": bool(proprio.get("envio_ativo", 1)),
    }


def definir_parametros_cliente(conn: sqlite3.Connection, cnpj: str, **campos) -> None:
    permitidos = {"regra_xml", "warehouse_id", "tipo_transporte", "embalagem", "envio_ativo"}
    campos = {k: v for k, v in campos.items() if k in permitidos and v is not None}
    if "regra_xml" in campos and campos["regra_xml"] not in REGRAS_XML:
        raise ErroEnvio(f"Regra de XML desconhecida: {campos['regra_xml']!r} (válidas: {', '.join(REGRAS_XML)}).")
    atual = conn.execute("SELECT * FROM portal_clientes_envio WHERE cnpj = ?", (_so_digitos(cnpj),)).fetchone()
    base = dict(atual) if atual else {"regra_xml": None, "warehouse_id": None, "tipo_transporte": None, "embalagem": None, "envio_ativo": 1}
    base.update(campos)
    conn.execute("""
        INSERT INTO portal_clientes_envio (cnpj, regra_xml, warehouse_id, tipo_transporte, embalagem, envio_ativo, atualizado_em)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(cnpj) DO UPDATE SET regra_xml = excluded.regra_xml, warehouse_id = excluded.warehouse_id,
            tipo_transporte = excluded.tipo_transporte, embalagem = excluded.embalagem,
            envio_ativo = excluded.envio_ativo, atualizado_em = excluded.atualizado_em
    """, (_so_digitos(cnpj), base["regra_xml"], base["warehouse_id"], base["tipo_transporte"], base["embalagem"],
          int(bool(base["envio_ativo"])), _agora()))
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
        nfe = ler_nfe(conteudo)
        v = validar_item(conn, nfe, emb)
        if not v["ok"]:
            raise ErroEnvio(f"NF {nfe['numero_nf']}: " + " ".join(v["erros"]))
        lidos.append((item, caminho, conteudo, nfe, v["envio_existente"]))

    dest_info = info_destinatarios(conn, emb, [x[3] for x in lidos], config)
    hoje = date.today()
    for item, caminho, conteudo, nfe, existente in lidos:
        d = dest_info.get(nfe["destinatario_doc"], {})
        ini = _hora_valida(item.get("horario_inicio")) or d.get("horario_inicio") or ""
        fim = _hora_valida(item.get("horario_fim")) or d.get("horario_fim") or ""
        if not ini or not fim:
            raise ErroEnvio(f"NF {nfe['numero_nf']}: informe o horário de recebimento de {nfe['destinatario_nome']}.")
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
                    raise ErroEnvio(f"NF {nfe['numero_nf']}: {nfe['destinatario_nome']} recebe só com agendamento -- "
                                    f"informe a data ou marque 'Agendamento pendente'.")
                if ag < hoje:
                    raise ErroEnvio(f"NF {nfe['numero_nf']}: a data de agendamento precisa ser a partir de hoje.")
                ag_data = ag.isoformat()
                ag_ini = _hora_valida(item.get("agendamento_hora_inicio")) or ini
                ag_fim = _hora_valida(item.get("agendamento_hora_fim")) or fim

        destino = _caminho_definitivo(emb, nfe["chave_nfe"])
        destino.write_bytes(conteudo)
        try:
            caminho.unlink()
        except OSError:
            pass
        agora = _agora()
        campos = {
            "cnpj_embarcador": emb, "chave_nfe": nfe["chave_nfe"], "numero_nf": nfe["numero_nf"], "serie": nfe["serie"],
            "emitida_em": nfe["emitida_em"], "destinatario_doc": nfe["destinatario_doc"], "destinatario_nome": nfe["destinatario_nome"],
            "destinatario_endereco": nfe["destinatario_endereco"], "destinatario_bairro": nfe["destinatario_bairro"],
            "destinatario_municipio": nfe["destinatario_municipio"], "destinatario_uf": nfe["destinatario_uf"],
            "destinatario_cep": nfe["destinatario_cep"], "destinatario_telefone": nfe["destinatario_telefone"],
            "volumes": nfe["volumes"], "peso_kg": nfe["peso_kg"], "valor_nf": nfe["valor_nf"], "itens": nfe["itens"],
            "xml_path": str(destino.relative_to(_RAIZ)), "regra_xml": regra_xml, "status": STATUS_NA_FILA, "tentativas": 0,
            "erro": None, "resposta_stokki": None, "codigo_pedido": None, "horario_inicio": ini, "horario_fim": fim,
            "requer_agendamento": int(requer), "agendamento_data": ag_data, "agendamento_hora_inicio": ag_ini,
            "agendamento_hora_fim": ag_fim, "agendamento_pendente": pendente, "agendamento_aplicado_em": None,
            "observacoes": (item.get("observacoes") or "")[:500], "enviado_por": enviado_por,
            "criado_em": agora, "atualizado_em": agora, "enviado_stokki_em": None, "criado_stokki_em": None,
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
        criados.append({"id": envio_id, "numero_nf": nfe["numero_nf"], "destinatario_nome": nfe["destinatario_nome"]})
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
    d.update({
        "status_rotulo": ROTULOS_STATUS.get(d["status"], d["status"]),
        "criado_em_br": _dt_br(d["criado_em"]),
        "criado_stokki_em_br": _dt_br(d["criado_stokki_em"]),
        "emitida_em_br": _dt_br(d["emitida_em"]),
        "destinatario_doc_formatado": formatar_documento(d["destinatario_doc"] or ""),
        "endereco_curto": ", ".join(p for p in (d["destinatario_endereco"], d["destinatario_bairro"], d["destinatario_municipio"]) if p),
        "agendamento_data_br": "/".join(reversed(d["agendamento_data"].split("-"))) if d.get("agendamento_data") else "",
        "solicitacoes_pendentes": [{"tipo": s["tipo"], "rotulo": ROTULOS_SOLICITACAO.get(s["tipo"], s["tipo"]),
                                    "criado_em_br": _dt_br(s["criado_em"]), "detalhes": s["detalhes"]} for s in pend],
        "pode_cancelar": d["status"] in (STATUS_NA_FILA, STATUS_CRIADO, STATUS_ERRO, STATUS_DUPLICADO) and not any(s["tipo"] == "cancelar" for s in pend),
        "pode_em_espera": d["status"] in (STATUS_CRIADO, STATUS_DUPLICADO) and not any(s["tipo"] in ("em_espera", "cancelar") for s in pend),
        "pode_reagendar": d["status"] in (STATUS_NA_FILA, STATUS_CRIADO, STATUS_DUPLICADO),
        "pode_reenviar": d["status"] in (STATUS_ERRO, STATUS_CANCELADO),
    })
    d.pop("xml_path", None)
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
    sql = ("SELECT s.*, e.numero_nf, e.destinatario_nome, e.codigo_pedido, e.status AS status_envio "
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
