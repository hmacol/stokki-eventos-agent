# -*- coding: utf-8 -*-
"""
regras/embarcadores.py

Lógica de identificação de embarcador compartilhada entre pipeline.py e
inventario.py -- extraída daqui porque as duas cópias já tinham
divergido uma vez (a correção do guard de nome vazio em
_embarcador_prioritario foi aplicada só em pipeline.py e o mesmo bug
voltou em inventario.py). Um lugar só evita esse tipo de fix que não
propaga silenciosamente.
"""
import re
import sqlite3
import unicodedata
from pathlib import Path


_PADRAO_STKKC_ID = re.compile(r"#stkkc-(\d+)")


def _normalizar(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s).upper().strip()
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def embarcador_prioritario(linha, prioritarios: dict) -> bool:
    """
    True se a linha pertence a um embarcador prioritário (todos os status).
    Aceita tanto o dict da linha do aaData quanto o nome do cliente já
    extraído como string.

    prioritarios: dict {id_stokki: nome_legivel} (ex: EMBARCADORES_IMPORTAR_ABERTOS).

    28/09: decide pelo id estável (#stkkc-NN, que vem no campo 'client'
    da linha e é o mesmo id do filtro cliente= da Fonte 1) sempre que ele
    existe. O casamento por pedaço de nome, nos dois sentidos, descartava
    da Fonte 2 embarcador de nome parecido (filial, outro CNPJ) -- e ele
    também não vinha na Fonte 1, então sumia da importação sem registro.
    O nome só é usado quando a linha não traz o id.
    """
    texto = str(linha.get("client", "")) if isinstance(linha, dict) else str(linha or "")
    m_id = _PADRAO_STKKC_ID.search(texto)
    if m_id:
        return m_id.group(1) in {str(k) for k in prioritarios.keys()}
    nome = _normalizar(texto)
    if not nome:
        # Nome vazio nunca é prioritário — sem isso, "" in <qualquer nome>
        # dava True e descartava TODOS os pedidos da Fonte 2.
        return False
    return any(
        _normalizar(n) in nome or nome in _normalizar(n)
        for n in prioritarios.values()
    )


def resolver_stkkc_id_por_nome(busca: str, db_path: Path) -> int | None:
    """
    Busca um stkkc_id na tabela 'interno' cujo nome_remetente/apelido
    contenha o texto de busca (normalizado, sem acentos). Retorna None
    se o banco não existir ou nada bater.
    """
    if not db_path.exists():
        return None
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT stkkc_id, nome_remetente, apelido FROM interno WHERE stkkc_id IS NOT NULL"
    ).fetchall()
    conn.close()
    alvo = _normalizar(busca)
    match = next(
        (r for r in rows if alvo in _normalizar(r["nome_remetente"] or "") or
         alvo in _normalizar(r["apelido"] or "")), None
    )
    return match["stkkc_id"] if match else None
