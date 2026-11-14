# -*- coding: utf-8 -*-
"""
painel_agentes/documentos_email.py

Documentos fiscais do pedido pela tela /pedidos (Hugo, 13/11): consulta,
download e envio por e-mail do XML da NF-e, da DANFE e do(s) boleto(s), de um
ou de varios pedidos num clique. Caso que originou: boleto que nao foi impresso
nem enviado e o cliente cobrou.

Nada e buscado na Stokki aqui: so o que ja esta guardado.
  - DANFE e boleto: documentos_processados (status ENVIADO) + arquivo em disco
    (documentos_pedido/localizar_arquivos.resolver_arquivo_local);
  - XML: dados/xml_stokki/PS-<n>.xml (coleta noturna, coletar_xml_nfe.py).

Nome dos anexos (pedido do Hugo): XML_NF<numero>, DANFE_NF<numero>,
BOLETO_NF<numero>. Boleto parcelado ganha _1, _2... Sem numero de nota
conhecido, vai o codigo do pedido no lugar (BOLETO_PS-123).

O e-mail sai DE VERDADE pro endereco digitado (e um clique de uma pessoa, nao
um aviso automatico: nao passa por forcar_destino). O endereco usado fica
lembrado por destinatario e por pedido: no envio seguinte a tela pergunta se
vai pro mesmo ou se altera.
"""
import html
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))

import email_utils  # noqa: E402
from documentos_pedido.localizar_arquivos import resolver_arquivo_local  # noqa: E402

PASTA_XML = _RAIZ / "dados" / "xml_stokki"
MAX_PEDIDOS = 50
MAX_EMAILS = 5
MAX_BYTES_EMAIL = 18 * 1024 * 1024   # o Gmail recusa acima de 25 MB ja codificado (base64 cresce ~37%)
TIPOS = ("XML", "DANFE", "BOLETO")
ROTULOS = {"XML": "XML", "DANFE": "DANFE", "BOLETO": "boleto"}
FMT = "%Y-%m-%d %H:%M:%S"

_RE_PS = re.compile(r"PS-(\d+)", re.IGNORECASE)
_RE_EMAIL = re.compile(r"[^@\s,;<>\"'()]+@[^@\s,;<>\"'()]+\.[A-Za-z]{2,}")
_RE_NNF = re.compile(rb"<nNF>\s*(\d+)\s*</nNF>")


class ErroDocumentos(Exception):
    """Recusa com texto pronto pra tela."""


def garantir_tabelas(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS pedidos_documentos_destinos (
            chave         TEXT PRIMARY KEY,   -- 'D:<DESTINATARIO>' ou 'P:PS-<n>'
            emails        TEXT NOT NULL,
            atualizado_em TEXT NOT NULL,
            por           TEXT
        );
        CREATE TABLE IF NOT EXISTS pedidos_documentos_envios (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            quando   TEXT NOT NULL,
            usuario  TEXT,
            emails   TEXT NOT NULL,
            codigos  TEXT NOT NULL,           -- JSON
            arquivos TEXT NOT NULL,           -- JSON: nomes dos anexos
            partes   INTEGER NOT NULL,
            ok       INTEGER NOT NULL,
            erro     TEXT
        );
    """)


def ps_do_codigo(codigo) -> list[str]:
    """'#PS-10-R2' -> ['PS-10'] (a reentrega usa os documentos do original)."""
    vistos = []
    for n in _RE_PS.findall(str(codigo or "")):
        ps = f"PS-{int(n)}"
        if ps not in vistos:
            vistos.append(ps)
    return vistos


def _nf(valor) -> str | None:
    digitos = re.sub(r"\D", "", str(valor or "")).lstrip("0")
    return digitos or None


def _nf_do_xml(caminho: Path) -> str | None:
    try:
        with open(caminho, "rb") as f:
            m = _RE_NNF.search(f.read(20000))
    except OSError:
        return None
    return _nf(m.group(1).decode()) if m else None


def _docs_do_banco(conn, ps: str) -> list[sqlite3.Row]:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'documentos_processados'").fetchone():
        return []
    numero = ps[3:]
    return conn.execute(
        "SELECT hash_conteudo, tipo, nome_arquivo, numero_nf, numero_parcela, processado_em FROM documentos_processados "
        "WHERE status = 'ENVIADO' AND tipo IN ('Nota Fiscal', 'Boleto') AND codigo_pedido IN (?, ?, ?) "
        "ORDER BY processado_em DESC, hash_conteudo", (ps, numero, f"#{ps}")).fetchall()


def _arquivos_do_ps(conn, ps: str, pasta_xml: Path, resolver) -> list[dict]:
    """XML, DANFE(s) e boleto(s) guardados de um PS, ainda sem o nome final."""
    arquivos = []
    danfes, boletos = {}, []
    for r in _docs_do_banco(conn, ps):
        caminho = resolver(r["nome_arquivo"])
        if caminho is None:
            continue
        item = {"ref": r["hash_conteudo"], "nf": _nf(r["numero_nf"]), "caminho": Path(caminho),
                "parcela": r["numero_parcela"] or 0}
        if r["tipo"] == "Nota Fiscal":
            danfes.setdefault(item["nf"], item)   # a mesma nota chega pela Stokki e por e-mail: fica a mais nova
        else:
            boletos.append(item)
    xml = Path(pasta_xml) / f"{ps}.xml"
    nf_xml = _nf_do_xml(xml) if xml.is_file() else None
    numeros = [n for n in danfes if n]
    nf_pedido = numeros[0] if len(numeros) == 1 else (nf_xml if not numeros else None)
    if xml.is_file():
        arquivos.append({"ref": f"xml:{ps}", "tipo": "XML", "nf": nf_xml or nf_pedido, "caminho": xml, "ext": ".xml"})
    for item in danfes.values():
        arquivos.append({**item, "tipo": "DANFE", "nf": item["nf"] or nf_pedido, "ext": ".pdf"})
    for item in sorted(boletos, key=lambda b: (b["parcela"], b["ref"])):
        arquivos.append({**item, "tipo": "BOLETO", "nf": item["nf"] or nf_pedido, "ext": ".pdf"})
    for a in arquivos:
        a["ps"] = ps
        a["tamanho"] = a["caminho"].stat().st_size
    return arquivos


def _nomear(arquivos: list[dict]) -> None:
    """XML_NF123 / DANFE_NF123 / BOLETO_NF123. Nome repetido (boleto parcelado,
    a mesma nota em dois pedidos) ganha _1, _2..."""
    grupos = {}
    for a in arquivos:
        a["base"] = f"{a['tipo']}_" + (f"NF{a['nf']}" if a["nf"] else a["ps"])
        grupos.setdefault(a["base"].upper() + a["ext"], []).append(a)
    for grupo in grupos.values():
        for i, a in enumerate(grupo, 1):
            a["nome"] = a["base"] + (f"_{i}" if len(grupo) > 1 else "") + a["ext"]


def levantar(conn, codigos: list[str], pasta_xml: Path | None = None, resolver=None) -> list[dict]:
    """Um item por codigo: {"codigo", "ps", "arquivos": [{ref, tipo, nf, nome, tamanho, caminho}], "faltando": [tipos]}.
    Dois codigos do mesmo PS (original e reentrega) contam uma vez so."""
    pasta_xml = pasta_xml or PASTA_XML
    resolver = resolver or resolver_arquivo_local
    pedidos, ps_vistos, todos = [], set(), []
    for codigo in codigos:
        codigo = str(codigo or "").strip().lstrip("#")
        lista_ps = [p for p in ps_do_codigo(codigo) if p not in ps_vistos]
        if ps_do_codigo(codigo) and not lista_ps:
            continue
        ps_vistos.update(lista_ps)
        arquivos = [a for ps in lista_ps for a in _arquivos_do_ps(conn, ps, pasta_xml, resolver)]
        todos += arquivos
        tem = {a["tipo"] for a in arquivos}
        pedidos.append({"codigo": codigo, "ps": lista_ps, "arquivos": arquivos,
                        "faltando": [t for t in TIPOS if t not in tem]})
    _nomear(todos)
    return pedidos


def arquivo(conn, codigo: str, ref: str, pasta_xml: Path | None = None, resolver=None) -> tuple[Path, str] | None:
    """(caminho, nome pra baixar) de um documento do pedido; None se a referencia nao e dele."""
    for pedido in levantar(conn, [codigo], pasta_xml, resolver):
        for a in pedido["arquivos"]:
            if a["ref"] == ref:
                return a["caminho"], a["nome"]
    return None


def validar_emails(texto) -> list[str]:
    """Um ou mais enderecos separados por virgula, ponto e virgula ou espaco."""
    partes = [p for p in re.split(r"[,;\s]+", str(texto or "").strip()) if p]
    if not partes:
        raise ErroDocumentos("Informe o e-mail de quem vai receber.")
    if len(partes) > MAX_EMAILS:
        raise ErroDocumentos(f"No máximo {MAX_EMAILS} e-mails por envio.")
    emails = []
    for p in partes:
        if not _RE_EMAIL.fullmatch(p) or len(p) > 254:
            raise ErroDocumentos(f"E-mail inválido: {p[:80]}")
        if p.lower() not in emails:
            emails.append(p.lower())
    return emails


def chaves(itens: list[dict]) -> list[str]:
    """Onde o e-mail usado fica lembrado: o destinatario (nome) e cada pedido."""
    saida = []
    for it in itens:
        nome = re.sub(r"\s+", " ", str(it.get("destinatario") or "")).strip().upper()
        for chave in ([f"D:{nome}"] if nome else []) + [f"P:{ps}" for ps in ps_do_codigo(it.get("codigo"))]:
            if chave not in saida:
                saida.append(chave)
    return saida


def lembrados(conn, itens: list[dict]) -> list[str]:
    """[e-mail do ultimo envio] quando TODOS os pedidos da selecao tem lembranca e ela e a mesma
    (pelo destinatario ou pelo pedido); senao []. Lote com clientes diferentes nao sugere o
    e-mail de um deles: o documento de um cliente iria pro outro num clique."""
    vistos = set()
    for it in itens:
        lista = chaves([it])
        linha = lista and conn.execute(
            f"SELECT emails FROM pedidos_documentos_destinos WHERE chave IN ({','.join('?' * len(lista))}) "
            "ORDER BY atualizado_em DESC, chave LIMIT 1", lista).fetchone()
        if not linha:
            return []
        vistos.add(linha[0])
    return list(vistos) if len(vistos) == 1 else []


def _partes(arquivos: list[dict], limite: int) -> list[list[dict]]:
    partes, atual, soma = [], [], 0
    for a in arquivos:
        if atual and soma + a["tamanho"] > limite:
            partes.append(atual)
            atual, soma = [], 0
        atual.append(a)
        soma += a["tamanho"]
    return partes + ([atual] if atual else [])


def resumo(pedidos: list[dict]) -> dict:
    """O que a tela mostra na conferencia (sem caminho de arquivo)."""
    arquivos = [a for p in pedidos for a in p["arquivos"]]
    return {
        "pedidos": [{"codigo": p["codigo"], "faltando": p["faltando"],
                     "arquivos": [{"ref": a["ref"], "tipo": a["tipo"], "nome": a["nome"], "tamanho": a["tamanho"]}
                                  for a in p["arquivos"]]} for p in pedidos],
        "total_arquivos": len(arquivos),
        "total_bytes": sum(a["tamanho"] for a in arquivos),
        "partes": len(_partes(arquivos, MAX_BYTES_EMAIL)),
    }


def _assunto(arquivos: list[dict], pedidos: list[dict], i: int, n: int) -> str:
    notas = []
    for a in arquivos:
        if a["nf"] and a["nf"] not in notas:
            notas.append(a["nf"])
    if notas:
        alvo = "NF " + ", ".join(notas[:6]) + (f" e mais {len(notas) - 6}" if len(notas) > 6 else "")
    else:
        alvo = ", ".join(p["codigo"] for p in pedidos[:6])
    return f"Documentos fiscais - {alvo}" + (f" ({i}/{n})" if n > 1 else "")


def _corpo(parte: list[dict], i: int, n: int) -> str:
    linhas = "".join(f"<li>{html.escape(a['nome'])}</li>" for a in parte)
    extra = f"<p>Este é o e-mail {i} de {n}: os documentos foram divididos pelo tamanho dos anexos.</p>" if n > 1 else ""
    return email_utils.envelope_html(
        "<p>Olá,</p><p>Seguem em anexo os documentos fiscais solicitados:</p>"
        f"<ul>{linhas}</ul>{extra}<p>Qualquer dúvida, é só responder este e-mail.</p>",
        rodape="Freshlog")


def enviar(conn, itens: list[dict], emails_texto, usuario: str, config_email: dict, *, enviar_email=None,
           pasta_xml: Path | None = None, resolver=None, agora: datetime | None = None,
           limite_bytes: int = MAX_BYTES_EMAIL) -> dict:
    """Manda os documentos guardados dos pedidos pro(s) e-mail(s). Um e-mail so, ou
    mais de um quando os anexos passam do limite. Registra o envio e lembra o endereco.
    Devolve {"ok", "arquivos", "partes", "emails", "faltando": {codigo: [tipos]}}."""
    enviar_email = enviar_email or email_utils.enviar_email
    if not itens:
        raise ErroDocumentos("Nenhum pedido selecionado.")
    if len(itens) > MAX_PEDIDOS:
        raise ErroDocumentos(f"No máximo {MAX_PEDIDOS} pedidos por envio.")
    emails = validar_emails(emails_texto)
    pedidos = levantar(conn, [str(it.get("codigo") or "") for it in itens], pasta_xml, resolver)
    arquivos = [a for p in pedidos for a in p["arquivos"]]
    if not arquivos:
        raise ErroDocumentos("Nenhum documento guardado para os pedidos selecionados.")
    garantir_tabelas(conn)
    partes = _partes(arquivos, limite_bytes)
    quando = (agora or datetime.now()).strftime(FMT)
    erro = None
    for i, parte in enumerate(partes, 1):
        ok = enviar_email(emails, _assunto(parte, pedidos, i, len(partes)), _corpo(parte, i, len(partes)),
                          config_email, anexos=[(a["caminho"], a["nome"]) for a in parte])
        if not ok:
            erro = (f"Falha ao enviar o e-mail {i} de {len(partes)}"
                    + (f" (os {i - 1} anteriores já saíram)" if i > 1 else "") + ". Tente de novo em instantes.")
            break
    with conn:
        conn.execute("INSERT INTO pedidos_documentos_envios (quando, usuario, emails, codigos, arquivos, partes, ok, erro) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (quando, usuario, ", ".join(emails), json.dumps([p["codigo"] for p in pedidos]),
                      json.dumps([a["nome"] for a in arquivos]), len(partes), int(erro is None), erro))
        if erro is None:
            conn.executemany("INSERT OR REPLACE INTO pedidos_documentos_destinos (chave, emails, atualizado_em, por) "
                             "VALUES (?, ?, ?, ?)", [(c, ", ".join(emails), quando, usuario) for c in chaves(itens)])
    if erro:
        raise ErroDocumentos(erro)
    return {"ok": True, "arquivos": len(arquivos), "partes": len(partes), "emails": emails,
            "faltando": {p["codigo"]: p["faltando"] for p in pedidos if p["faltando"]}}
