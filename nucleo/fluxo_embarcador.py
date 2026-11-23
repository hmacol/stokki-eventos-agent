# -*- coding: utf-8 -*-
"""
nucleo/fluxo_embarcador.py

Fluxo operacional de cada embarcador (Hugo, 24/10/2026 -- spec
docs/superpowers/specs/2026-10-24-importacao-fluxo-cross-design.md):

  armazenagem  "Full": trabalha com estoque, separacao por SKU.
  full_cross   mercadoria chega na quantidade exata e e separada por pedido;
               importacao normal (XML como vem).
  cross        pedidos ja chegam separados; importacao com o item XD do
               embarcador e depois troca da NF pelo XML original.

Um embarcador pode ter mais de um; mora em `interno.fluxos` (texto separado
por virgula; vazio = armazenagem). O codigo XD NAO e cadastrado: e o produto
do embarcador no catalogo sincronizado da Stokki (`wms_produtos`) cujo SKU
comeca com "XD" (ex. XDDOURADO). Portal e importador por e-mail leem daqui;
ninguem mais guarda copia do fluxo.
"""
import re
import sqlite3
import unicodedata
from datetime import datetime

FLUXOS = ("armazenagem", "full_cross", "cross")
ROTULOS = {"armazenagem": "Armazenagem", "full_cross": "Full cross", "cross": "Cross"}
PADRAO = "armazenagem"


def _so_digitos(valor) -> str:
    return re.sub(r"\D", "", str(valor or ""))


def _tem_coluna(conn: sqlite3.Connection) -> bool:
    return any(r[1] == "fluxos" for r in conn.execute("PRAGMA table_info(interno)"))


def garantir_coluna(conn: sqlite3.Connection) -> None:
    if not _tem_coluna(conn):
        conn.execute("ALTER TABLE interno ADD COLUMN fluxos TEXT")
        conn.commit()


def normalizar_fluxos(texto: str | None) -> list[str]:
    """Separa por virgula, ignora desconhecidos e repetidos, mantem a ordem
    dada. Nada valido = ["armazenagem"] (comportamento de antes do campo)."""
    vistos: list[str] = []
    for parte in str(texto or "").split(","):
        f = parte.strip().lower()
        if f in FLUXOS and f not in vistos:
            vistos.append(f)
    return vistos or [PADRAO]


def definir_fluxos(conn: sqlite3.Connection, cnpj: str, fluxos: list[str]) -> None:
    invalidos = [f for f in fluxos if f not in FLUXOS]
    if invalidos or not fluxos:
        raise ValueError(f"Fluxo invalido: {invalidos or fluxos} (validos: {', '.join(FLUXOS)}).")
    garantir_coluna(conn)
    texto = ",".join(f for f in FLUXOS if f in fluxos)
    cur = conn.execute("UPDATE interno SET fluxos = ?, atualizado_em = ? WHERE cnpj_embarcador = ?",
                       (texto, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), _so_digitos(cnpj)))
    if cur.rowcount == 0:
        raise ValueError(f"Embarcador {cnpj} nao esta na tabela interno.")
    conn.commit()


def nome_item_xd(apelido: str) -> str:
    return f"Mercadoria {(apelido or '').strip()}".strip()


def _chave_apelido(apelido: str) -> str:
    s = unicodedata.normalize("NFKD", apelido or "").encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def resolver_codigo_xd(conn: sqlite3.Connection, stkkc: int, *nomes: str) -> tuple[str | None, str | None]:
    """(codigo, erro). Um XD ativo no catalogo do embarcador = ele; varios =
    o que for "XD" + um dos nomes (apelido ou nome_remetente: em producao a
    razao social e o nome curto aparecem nos dois campos); sem casamento ou
    nenhum = erro."""
    try:
        skus = sorted({(r[0] or "").strip() for r in conn.execute(
            "SELECT sku FROM wms_produtos WHERE embarcador_id = ? AND ativo = 1 AND UPPER(sku) LIKE 'XD%'",
            (int(stkkc),))} - {""})
    except sqlite3.OperationalError:
        return None, "catálogo de produtos da Stokki indisponível"
    if len(skus) == 1:
        return skus[0], None
    if not skus:
        return None, "nenhum produto XD no catálogo do embarcador"
    esperados = {"XD" + _chave_apelido(n) for n in nomes if n}
    casados = [s for s in skus if s.upper() in esperados]
    if len(casados) == 1:
        return casados[0], None
    return None, f"mais de um produto XD no catálogo: {', '.join(skus)}"


def fluxos_do_embarcador(conn: sqlite3.Connection, cnpj: str | None = None, stkkc: int | None = None) -> dict:
    """Fluxos do embarcador e, quando ele tem Cross, o XD resolvido.
    Aceita CNPJ (portal) ou stkkc (importador por e-mail, cujo config tem
    so o client_id da Stokki)."""
    coluna = ", fluxos" if _tem_coluna(conn) else ""
    if cnpj:
        row = conn.execute(f"SELECT cnpj_embarcador, apelido, nome_remetente, stkkc_id{coluna} FROM interno "
                           "WHERE cnpj_embarcador = ?", (_so_digitos(cnpj),)).fetchone()
    elif stkkc is not None:
        row = conn.execute(f"SELECT cnpj_embarcador, apelido, nome_remetente, stkkc_id{coluna} FROM interno "
                           "WHERE stkkc_id = ? ORDER BY cnpj_embarcador LIMIT 1", (int(stkkc),)).fetchone()
    else:
        raise ValueError("Informe cnpj ou stkkc.")
    if not row:
        raise ValueError(f"Embarcador {cnpj or stkkc} nao esta na tabela interno.")
    apelido = row[1] or row[2] or row[0]
    # nome curto pro item XD: o menor dos dois campos (ver resolver_codigo_xd)
    curto = min([n.strip() for n in (row[1], row[2]) if n and n.strip()] or [row[0]], key=len)
    fluxos = normalizar_fluxos(row[4] if coluna else None)
    codigo = erro = None
    if "cross" in fluxos:
        if row[3] is None:
            erro = "embarcador sem id da Stokki (stkkc) no cadastro"
        else:
            codigo, erro = resolver_codigo_xd(conn, row[3], row[1] or "", row[2] or "")
    return {"cnpj": row[0], "stkkc": row[3], "apelido": apelido, "fluxos": fluxos,
            "codigo_xd": codigo, "nome_item": nome_item_xd(curto), "erro_xd": erro}


def _fluxo_do_texto_livre(texto: str) -> str | None:
    t = re.sub(r"[\s_-]", "", (texto or "").lower())
    return "full_cross" if t == "fullcross" else None


def carga_de_marcacoes(docs: list[dict]) -> dict[str, list[str]]:
    """Documentos da pagina "Fluxo por embarcador" ({cnpj, armazenagem,
    cross, outro}) -> {cnpj: fluxos}. "Full cross" escrito em Outro vira
    full_cross; embarcador sem nenhuma marcacao fica de fora."""
    saida: dict[str, list[str]] = {}
    for d in docs:
        marcados = set()
        if d.get("armazenagem"):
            marcados.add("armazenagem")
        if d.get("cross"):
            marcados.add("cross")
        extra = _fluxo_do_texto_livre(d.get("outro") or "")
        if extra:
            marcados.add(extra)
        if marcados:
            saida[_so_digitos(d.get("cnpj"))] = [f for f in FLUXOS if f in marcados]
    return saida
