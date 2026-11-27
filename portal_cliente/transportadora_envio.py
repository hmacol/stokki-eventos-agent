# -*- coding: utf-8 -*-
"""Transportadora escolhida pelo embarcador no envio de pedidos (XML).

A Stokki toma a transportadora do <transporta> do XML. Quando a da nota está
errada, o embarcador escolhe outra do cadastro (/transportadoras, só ATIVA) na
prévia; a escolha fica em portal_envios.transportadora_json e o worker reescreve
o <transporta> na CÓPIA do lote (enviar_stokki.aplicar_transportadora_no_lote),
nunca no XML original do cliente. Mesmo padrão do endereço de entrega
(endereco_entrega.py)."""
import json
import re
import unicodedata
from xml.sax.saxutils import escape


class ErroTransportadora(ValueError):
    pass


def _so_digitos(valor) -> str:
    return re.sub(r"\D", "", str(valor or ""))


def opcoes(conn) -> list[dict]:
    """Transportadoras ATIVAS do cadastro, pra lista da prévia."""
    try:
        linhas = conn.execute("SELECT id, nome, cnpj, tipo FROM transportadoras WHERE situacao = 'ATIVA' "
                              "ORDER BY nome COLLATE NOCASE").fetchall()
    except Exception:  # banco sem a tabela (ambiente de teste)
        return []
    return [{"id": r[0], "nome": r[1], "cnpj": r[2] or "", "tipo": r[3]} for r in linhas]


def resolver(conn, transportadora_id) -> dict:
    """{id, nome, cnpj, tipo} da transportadora ATIVA escolhida; erro se não existe."""
    try:
        tid = int(transportadora_id)
    except (TypeError, ValueError):
        raise ErroTransportadora("transportadora inválida.")
    for o in opcoes(conn):
        if o["id"] == tid:
            return o
    raise ErroTransportadora("transportadora não encontrada no cadastro.")


def _norm(texto) -> str:
    t = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", t)).strip()


def achar_por_texto(conn, texto) -> dict:
    """Transportadora ATIVA do cadastro que a coluna "Transportadora" da planilha
    indica (nome ou CNPJ). Nome: igual, ou contido no outro quando só uma casa.
    Erro diz o que foi lido e por que não serve."""
    bruto = str(texto or "").strip()
    cadastro = opcoes(conn)
    doc = _so_digitos(bruto)
    if len(doc) in (11, 14):
        achadas = [o for o in cadastro if _so_digitos(o["cnpj"]) == doc]
    else:
        alvo = _norm(bruto)
        achadas = [o for o in cadastro if _norm(o["nome"]) == alvo]
        if not achadas and len(alvo) >= 4:
            achadas = [o for o in cadastro if alvo in _norm(o["nome"]) or _norm(o["nome"]) in alvo]
    if len(achadas) == 1:
        return achadas[0]
    if not achadas:
        raise ErroTransportadora(f'transportadora "{bruto[:60]}" não encontrada no cadastro -- confira o nome ou o CNPJ.')
    raise ErroTransportadora(f'transportadora "{bruto[:60]}" casa com mais de uma do cadastro '
                             f'({", ".join(o["nome"] for o in achadas[:4])}) -- use o CNPJ.')


def casar_opcao_stokki(opcoes_stokki: list, transp: dict) -> str:
    """Valor do <option> de #carrier_id (wizard Excel da Stokki; lista própria
    por cliente) cujo texto é a transportadora escolhida; "" se não há (ou se
    há mais de uma possível: nunca chuta). `opcoes_stokki` = [(valor, texto)]."""
    alvo = _norm(transp.get("nome"))
    if not alvo:
        return ""
    cand = [(v, t) for v, t in opcoes_stokki if v and _norm(t) == alvo]
    if not cand:
        cand = [(v, t) for v, t in opcoes_stokki if v and len(_norm(t)) >= 4 and (alvo in _norm(t) or _norm(t) in alvo)]
    return str(cand[0][0]) if len(cand) == 1 else ""


def aplicar_no_nfe(nfe: dict, transp: dict) -> dict:
    """Cópia do nfe com a transportadora escolhida (a da nota fica em
    transportadora_original, pra tela)."""
    novo = dict(nfe)
    novo.update({"transportadora_nome": transp["nome"], "transportadora_cnpj": _so_digitos(transp.get("cnpj")),
                 "transportadora_original": nfe.get("transportadora_nome") or ""})
    return novo


def gravar_json(transp: dict) -> str:
    return json.dumps({"id": transp["id"], "nome": transp["nome"], "cnpj": _so_digitos(transp.get("cnpj"))},
                      ensure_ascii=False)


def da_linha(envio: dict) -> dict | None:
    """Transportadora gravada no envio (portal_envios.transportadora_json) ou None."""
    bruto = envio.get("transportadora_json")
    if not bruto:
        return None
    try:
        t = json.loads(bruto)
    except ValueError:
        return None
    return t if isinstance(t, dict) and t.get("nome") else None


_TRANSPORTA = re.compile(rb"<(\w+:)?transporta(?:\s*/>|>.*?</(?:\w+:)?transporta>)", re.S)
_FECHA_MODFRETE = re.compile(rb"</(\w+:)?modFrete>")


def trocar_transporta_xml(conteudo: bytes, nome: str, cnpj: str) -> bytes:
    """Troca o grupo <transporta> por texto (sem reescrever namespaces nem o
    resto); sem o grupo, insere depois de </modFrete>."""
    achado = _TRANSPORTA.search(conteudo)
    if achado:
        ini, fim = achado.span()
        pre = (achado.group(1) or b"").decode()
    else:
        achado = _FECHA_MODFRETE.search(conteudo)
        if not achado:
            raise ValueError("XML sem o grupo <transp>")
        ini = fim = achado.end()
        pre = (achado.group(1) or b"").decode()
    doc = _so_digitos(cnpj)
    partes = []
    if len(doc) in (11, 14):
        partes.append(f"<{pre}{'CPF' if len(doc) == 11 else 'CNPJ'}>{doc}</{pre}{'CPF' if len(doc) == 11 else 'CNPJ'}>")
    partes.append(f"<{pre}xNome>{escape(str(nome).strip()[:60])}</{pre}xNome>")
    bloco = f"<{pre}transporta>{''.join(partes)}</{pre}transporta>".encode("utf-8")
    return conteudo[:ini] + bloco + conteudo[fim:]
