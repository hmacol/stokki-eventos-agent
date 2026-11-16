# -*- coding: utf-8 -*-
"""
portal_cliente/anexos_pedido.py

Documentos (PDF) que o cliente anexa ao pedido no portal -- na previa do
envio (um por pedido, ou varios de uma vez: o sistema le cada PDF e sugere
o pedido) ou depois, na lista de enviados, enquanto a rota nao saiu
(Hugo, 11/11/2026).

O anexo vale como documento da nota: quando o envio ganha o codigo PS, cada
anexo entra em documentos_processados (origem 'portal', status ENVIADO) e o
romaneio (roteirizacao/gerar_pdf_romaneios.py) imprime junto. Boleto e NF
contam como tais; os outros tipos saem depois dos boletos do pedido.

O usuario SEMPRE confirma que cada documento esta no pedido certo (as rotas
em app.py recusam sem `anexos_conferidos`): a sugestao do sistema e so
sugestao.

Os arquivos ficam em dados/portal_envios/_anexos/ (pasta plana, nome unico),
que documentos_pedido/localizar_arquivos.py conhece.
"""
import hashlib
import io
import json
import logging
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

import envio_pedidos as ep

# append (nao insert): documentos_pedido/ entra por ultimo pra nao sombrear
# modulo do portal (mesmo cuidado do gerar_pdf_romaneios.py).
_PASTA_DOCS = ep._RAIZ / "documentos_pedido"
if str(_PASTA_DOCS) not in sys.path:
    sys.path.append(str(_PASTA_DOCS))

import boleto_parser  # noqa: E402
import classificador  # noqa: E402
import matcher  # noqa: E402

logger = logging.getLogger(__name__)

MAX_BYTES_PDF = 10 * 1024 * 1024
MAX_ANEXOS_POR_PEDIDO = 10
MAX_PAGINAS_PDF = 30           # o anexo e impresso inteiro no romaneio
MAX_PDFS_POR_ENVIO = 30        # por requisicao: cada PDF e aberto e lido
ORIGEM_DOCUMENTO = "portal"

# Envio que ainda aceita documento (o que ja morreu nao aceita)
_STATUS_ACEITA = (ep.STATUS_NA_FILA, ep.STATUS_ENVIANDO, ep.STATUS_CRIADO, ep.STATUS_DUPLICADO,
                  ep.STATUS_ERRO, ep.STATUS_AGUARDANDO_LIBERACAO, ep.STATUS_EM_ESPERA_CLIENTE,
                  ep.STATUS_RETOMANDO)


def _pasta() -> Path:
    return ep.PASTA_XMLS / "_anexos"


def garantir_tabela(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS portal_envios_anexos (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            envio_id         INTEGER NOT NULL,
            cnpj_embarcador  TEXT NOT NULL,
            nome_original    TEXT NOT NULL,
            arquivo          TEXT NOT NULL,
            hash_conteudo    TEXT NOT NULL,
            tipo             TEXT,
            numero_nf        TEXT,
            numero_parcela   INTEGER,
            total_parcelas   INTEGER,
            cnpj_contraparte TEXT,
            casado_por       TEXT,
            conferido_por    TEXT NOT NULL,
            criado_em        TEXT NOT NULL,
            registrado_em    TEXT,
            UNIQUE (envio_id, hash_conteudo)
        )""")


def _nf(valor) -> str:
    return re.sub(r"\D", "", str(valor or "")).lstrip("0")


def e_pdf(nome: str, conteudo: bytes) -> bool:
    return (nome or "").lower().endswith(".pdf") or conteudo[:5] == b"%PDF-"


def ler_pdf(nome: str, conteudo: bytes) -> dict:
    """Valida o PDF e le o que da pra casar com um pedido: tipo, numero da NF
    e CNPJ do destinatario/pagador. Reaproveita o classificador e os leitores
    de documentos_pedido/ (os mesmos dos documentos que chegam por e-mail)."""
    nome = Path(nome or "documento.pdf").name
    if b"%PDF-" not in conteudo[:1024]:
        raise ep.ErroEnvio(f"{nome}: não é um PDF. Os documentos do pedido precisam estar em PDF.")
    if len(conteudo) > MAX_BYTES_PDF:
        raise ep.ErroEnvio(f"{nome}: o PDF passa de {MAX_BYTES_PDF // (1024 * 1024)} MB.")
    try:
        from pypdf import PdfReader
        leitor = PdfReader(io.BytesIO(conteudo))
        if leitor.is_encrypted:
            leitor.decrypt("")
        paginas = len(leitor.pages)
    except Exception:
        paginas = 0
    if not paginas:
        raise ep.ErroEnvio(f"{nome}: não foi possível abrir o PDF (arquivo corrompido ou com senha).")
    if paginas > MAX_PAGINAS_PDF:
        raise ep.ErroEnvio(f"{nome}: o PDF tem {paginas} páginas; o limite é {MAX_PAGINAS_PDF}.")

    info = {"nome": nome, "paginas": paginas, "hash": hashlib.sha256(conteudo).hexdigest(),
            "tipo": "Outro", "numero_nf": None, "cnpj": None, "numero_parcela": None, "total_parcelas": None}
    # O classificador olha o NOME do arquivo: grava num temporario com o nome original.
    with tempfile.TemporaryDirectory() as pasta:
        caminho = Path(pasta) / (re.sub(r"[^\w.\- ]+", "_", nome).lstrip(". ")[-100:] or "documento.pdf")
        caminho.write_bytes(conteudo)
        try:
            info["tipo"] = classificador.classificar_documento(caminho)["tipo"]
            texto = ""
            if info["tipo"] in ("Boleto", "Nota Fiscal"):
                import pdfplumber
                with pdfplumber.open(caminho) as pdf:
                    texto = "\n".join(p.extract_text() or "" for p in pdf.pages[:6])
            if info["tipo"] == "Boleto":
                meta = boleto_parser.extrair_metadados_boleto(caminho, texto)
                info.update(numero_nf=meta["numero_nf"], cnpj=meta["cnpj_pagador"],
                            numero_parcela=meta["parcela_atual"], total_parcelas=meta["total_parcelas"])
            elif info["tipo"] == "Nota Fiscal":
                info["numero_nf"], info["cnpj"] = matcher.extrair_nf_da_danfe(texto)
        except Exception as e:  # leitura e so pra sugerir: PDF estranho segue como "Outro"
            logger.warning(f"anexo {nome}: leitura falhou ({type(e).__name__}: {e})")
    info["numero_nf"] = _nf(info["numero_nf"]) or None
    return info


def sugerir_pedido(info: dict, pedidos: list[dict]) -> tuple[str | None, str]:
    """A qual pedido o documento pertence. pedidos: [{chave, numero_nf,
    destinatario_doc, referencia}]. Devolve (chave | None, motivo). So sugere
    quando UM pedido bate -- na duvida fica sem pedido e o usuario escolhe."""
    def unico(candidatos):
        return candidatos[0]["chave"] if len(candidatos) == 1 else None

    nf = _nf(info.get("numero_nf"))
    if nf:
        chave = unico([p for p in pedidos if _nf(p.get("numero_nf")) == nf])
        if chave:
            return chave, "número da NF lido no documento"
    nome = Path(info.get("nome") or "").stem
    numeros = {_nf(n) for n in re.findall(r"\d{3,}", nome)} - {""}
    nome_limpo = re.sub(r"[^a-z0-9]", "", nome.lower())

    def bate_nome(p):
        ref = re.sub(r"[^a-z0-9]", "", str(p.get("referencia") or "").lower())
        return _nf(p.get("numero_nf")) in numeros or (len(ref) >= 4 and ref in nome_limpo)
    chave = unico([p for p in pedidos if bate_nome(p)])
    if chave:
        return chave, "número do pedido no nome do arquivo"
    cnpj = re.sub(r"\D", "", str(info.get("cnpj") or ""))
    if cnpj:
        chave = unico([p for p in pedidos if re.sub(r"\D", "", str(p.get("destinatario_doc") or "")) == cnpj])
        if chave:
            return chave, "CNPJ do destinatário lido no documento"
    if len(pedidos) == 1:
        return pedidos[0]["chave"], "único pedido"
    return None, "não identificado"


# ── Temporario (entre a leitura e a confirmacao do usuario) ───────────────────

def guardar_previa(nome: str, conteudo: bytes, cnpj: str) -> dict:
    """Le o PDF e guarda no temporario do envio, amarrado ao embarcador que
    enviou (so ele usa o token). Devolve o que a tela mostra."""
    info = ler_pdf(nome, conteudo)
    info["dono"] = ep._so_digitos(cnpj)
    token = ep.guardar_temporario(conteudo, "pdf")
    (ep.PASTA_TEMP / f"{token}.meta").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    return {"token": token, **{k: info[k] for k in ("nome", "tipo", "numero_nf", "paginas")}}


def _ler_temporario(token: str, cnpj: str) -> tuple[dict, Path]:
    invalido = ep.ErroEnvio("Documento temporário inválido -- anexe o arquivo de novo.")
    if not isinstance(token, str):
        raise invalido
    caminho = ep._caminho_temporario(token)
    meta = ep.PASTA_TEMP / f"{token}.meta"
    if caminho.suffix != ".pdf" or not meta.is_file():
        raise invalido
    info = json.loads(meta.read_text(encoding="utf-8"))
    if not info.get("dono") or info["dono"] != ep._so_digitos(cnpj):
        raise invalido
    return info, caminho


def validar_tokens(tokens: list[str], cnpj: str) -> None:
    """Antes de criar qualquer pedido: todos os documentos do lote ainda
    existem e sao do embarcador."""
    for t in tokens:
        _ler_temporario(t, cnpj)


# ── Gravacao ──────────────────────────────────────────────────────────────────

def anexar(conn: sqlite3.Connection, envio: dict, anexos: list[dict], por: str, dono: str | None = None) -> list[dict]:
    """anexos: [{token, casado_por?}] ja conferidos pelo usuario. Move cada PDF
    pro definitivo, grava a linha e, se o envio ja tem codigo PS, registra pro
    romaneio. Nao faz commit. `dono`: CNPJ que guardou os tokens (o do login,
    no grupo); sem ele vale o embarcador do envio."""
    garantir_tabela(conn)
    emb = ep._so_digitos(envio["cnpj_embarcador"])
    ja = conn.execute("SELECT COUNT(*) FROM portal_envios_anexos WHERE envio_id = ?", (envio["id"],)).fetchone()[0]
    if ja + len(anexos) > MAX_ANEXOS_POR_PEDIDO:
        raise ep.ErroEnvio(f"{ep.rotulo_envio(envio)}: no máximo {MAX_ANEXOS_POR_PEDIDO} documentos por pedido.")
    lidos = [(a, *_ler_temporario(a.get("token", ""), dono or emb)) for a in anexos]
    vistos = set()
    for _a, info, _c in lidos:
        if info["hash"] in vistos:
            raise ep.ErroEnvio(f"{info['nome']}: o mesmo documento aparece duas vezes neste pedido.")
        vistos.add(info["hash"])
        outro = conn.execute(
            "SELECT e.* FROM portal_envios_anexos a JOIN portal_envios e ON e.id = a.envio_id "
            "WHERE a.hash_conteudo = ? AND a.cnpj_embarcador = ?", (info["hash"], emb)).fetchone()
        # So entre os pedidos do PROPRIO embarcador: responder diferente pra arquivo
        # de outro cliente viraria um jeito de sondar o que os outros anexaram
        # (revisao de seguranca, 11/11). Colisao entre clientes e resolvida em
        # registrar_no_romaneio, sem avisar quem enviou.
        if outro:
            onde = "neste pedido" if outro["id"] == envio["id"] else f"no pedido {ep.rotulo_envio(dict(outro))}"
            raise ep.ErroEnvio(f"{info['nome']}: este documento já está {onde}.")
    _pasta().mkdir(parents=True, exist_ok=True)
    gravados, escritos = [], []
    try:
        for a, info, caminho in lidos:
            seguro = re.sub(r"[^A-Za-z0-9._-]+", "_", info["nome"])[-80:] or "documento.pdf"
            arquivo = f"{info['hash'][:16]}_{envio['id']}_{seguro}"
            escritos.append(_pasta() / arquivo)
            escritos[-1].write_bytes(caminho.read_bytes())
            _gravar_linha(conn, envio, emb, a, info, arquivo, por)
            gravados.append({"nome": info["nome"], "tipo": info["tipo"]})
        registrar_no_romaneio(conn, envio["id"])
    except Exception:
        # quem chamou desfaz a transacao: os arquivos saem junto
        for p in escritos:
            p.unlink(missing_ok=True)
        raise
    for _a, _i, caminho in lidos:
        for p in (caminho, caminho.with_suffix(".meta")):
            p.unlink(missing_ok=True)
    return gravados


def _gravar_linha(conn, envio, emb, a, info, arquivo, por) -> None:
    casado = a.get("casado_por") if a.get("casado_por") in ("automatico", "manual") else "manual"
    conn.execute(
        "INSERT INTO portal_envios_anexos (envio_id, cnpj_embarcador, nome_original, arquivo, hash_conteudo, tipo, "
        "numero_nf, numero_parcela, total_parcelas, cnpj_contraparte, casado_por, conferido_por, criado_em) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (envio["id"], emb, info["nome"], arquivo, info["hash"], info["tipo"], info["numero_nf"],
         info["numero_parcela"], info["total_parcelas"], info["cnpj"], casado, por, ep._agora()))


def _codigo(valor) -> str | None:
    m = re.fullmatch(r"#?PS-?(\d+)", str(valor or "").strip(), re.IGNORECASE)
    return f"PS-{m.group(1)}" if m else None


def registrar_no_romaneio(conn: sqlite3.Connection, envio_id: int) -> int:
    """Anexos do envio que ainda nao estao em documentos_processados entram
    la com o codigo PS -- e de la que o romaneio le. Sem codigo ainda, nao faz
    nada: o worker chama de novo quando grava o PS (_gravar_codigo). Usa a
    MESMA conexao de quem chamou (nada de segunda conexao escrevendo). Nao
    faz commit."""
    garantir_tabela(conn)
    r = conn.execute("SELECT codigo_pedido FROM portal_envios WHERE id = ?", (envio_id,)).fetchone()
    codigo = _codigo(r[0]) if r else None
    if not codigo:
        return 0
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'documentos_processados'").fetchone():
        logger.warning("documentos_processados nao existe neste banco: anexos nao registrados pro romaneio")
        return 0
    cols = "id, arquivo, hash_conteudo, tipo, numero_nf, numero_parcela, total_parcelas, cnpj_contraparte"
    n = 0
    for a in conn.execute(f"SELECT {cols} FROM portal_envios_anexos WHERE envio_id = ? AND registrado_em IS NULL",
                          (envio_id,)).fetchall():
        aid, arquivo, hash_, tipo, numero_nf, parcela, total, cnpj = a
        existente = conn.execute("SELECT origem, status, codigo_pedido FROM documentos_processados WHERE hash_conteudo = ?",
                                 (hash_,)).fetchone()
        # O hash e a chave da tabela: o portal nunca toma o documento de outro
        # dono (revisao de seguranca, 11/11). So grava quando nao ha linha, ou
        # quando a linha nao e da Stokki e ainda nao tem pedido (nao casou com
        # ninguem) ou ja e deste mesmo pedido. Qualquer outra -- de outro
        # pedido ou cliente, em qualquer status -- fica como esta.
        dono_alheio = existente and (existente[0] == "stokki" or existente[2] not in (None, "", codigo))
        if dono_alheio:
            logger.warning(f"anexo {arquivo} (envio {envio_id}): arquivo identico ja registrado em {existente[2]} "
                           f"({existente[0]}); nao registrado pro romaneio de {codigo}")
        else:
            # 'Nota Fiscal' do portal entra como 'Outro' e sem numero: quem diz
            # qual e a NF do pedido e a Stokki. Senao o PDF do cliente entraria
            # no indice NF->pedido (puxando boleto de outro cliente) e faria o
            # robo deixar de baixar a DANFE real (revisao de seguranca, 12/11).
            if tipo == "Nota Fiscal":
                tipo, numero_nf, cnpj = "Outro", None, None
            conn.execute(
                "INSERT OR REPLACE INTO documentos_processados (hash_conteudo, origem, nome_arquivo, tipo, codigo_pedido, "
                "status, motivo, gcs_path, processado_em, numero_parcela, total_parcelas, numero_nf, cnpj_contraparte) "
                "VALUES (?,?,?,?,?,'ENVIADO',NULL,NULL,?,?,?,?,?)",
                (hash_, ORIGEM_DOCUMENTO, arquivo, tipo or "Outro", codigo, ep._agora(), parcela, total, numero_nf, cnpj))
        conn.execute("UPDATE portal_envios_anexos SET registrado_em = ? WHERE id = ?", (ep._agora(), aid))
        n += 1
    return n


# ── Leitura / remocao ─────────────────────────────────────────────────────────

def pode_anexar(conn: sqlite3.Connection, envio: dict) -> bool:
    """Ate a rota sair: envio vivo e pedido ainda ABERTO no nucleo (ou ainda
    nem chegou la)."""
    if envio.get("status") not in _STATUS_ACEITA:
        return False
    codigo = _codigo(envio.get("codigo_pedido"))
    if not codigo:
        return True
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'nucleo_pedidos'").fetchone():
        return True
    status = [r[0] for r in conn.execute(
        "SELECT status FROM nucleo_pedidos WHERE codigo = ? OR codigo LIKE ?", (codigo, codigo + "-%"))]
    return not status or "ABERTO" in status


def anotar(conn: sqlite3.Connection, linhas: list[dict]) -> None:
    """Poe `anexos` e `pode_anexar` em cada linha de listar_envios."""
    garantir_tabela(conn)
    por_envio: dict[int, list] = {}
    ids = [l["id"] for l in linhas]
    for i in range(0, len(ids), 900):
        lote = ids[i:i + 900]
        for a in conn.execute(
                f"SELECT id, envio_id, nome_original, tipo, numero_nf FROM portal_envios_anexos "
                f"WHERE envio_id IN ({','.join('?' * len(lote))}) ORDER BY id", lote):
            por_envio.setdefault(a[1], []).append({"id": a[0], "nome": a[2], "tipo": a[3], "numero_nf": a[4]})
    for l in linhas:
        l["anexos"] = por_envio.get(l["id"], [])
        l["pode_anexar"] = pode_anexar(conn, l)


def buscar(conn: sqlite3.Connection, envio_id: int, anexo_id: int) -> dict | None:
    garantir_tabela(conn)
    conn.row_factory = sqlite3.Row
    r = conn.execute("SELECT * FROM portal_envios_anexos WHERE id = ? AND envio_id = ?", (anexo_id, envio_id)).fetchone()
    return dict(r) if r else None


def caminho(anexo: dict) -> Path:
    return _pasta() / anexo["arquivo"]


def remover(conn: sqlite3.Connection, anexo: dict) -> None:
    """Tira o documento do pedido (e do romaneio). Nao faz commit."""
    conn.execute("DELETE FROM portal_envios_anexos WHERE id = ?", (anexo["id"],))
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'documentos_processados'").fetchone():
        conn.execute("DELETE FROM documentos_processados WHERE hash_conteudo = ? AND origem = ? AND nome_arquivo = ?",
                     (anexo["hash_conteudo"], ORIGEM_DOCUMENTO, anexo["arquivo"]))
    caminho(anexo).unlink(missing_ok=True)
