# -*- coding: utf-8 -*-
"""
nucleo/nf_pedido.py

NF (numero da nota fiscal) de cada pedido PS, para mostrar ao lado do PS em
telas, e-mails e mensagens. Fonte unica (decisao do Hugo, 23/11):

  1. documentos_pedido (tipo_doc='nf', status casado/baixado) -- principal;
  2. documentos_processados (status ENVIADO, tipo 'Nota Fiscal') -- apoio,
     so para o pedido que a principal nao tem.

Chave: codigo BASE do pedido ("PS-123"; "#PS-123-R1" -> "PS-123"): o documento
e casado sob o codigo base, reentrega mostra a NF do original.
Varias NFs viram "111, 222" (ordenadas); sem NF = "" (a tela mostra "—").
"""
import re
import sqlite3

SEM_NF = "—"
_BASE = re.compile(r"PS-\d+", re.I)


def codigo_base(codigo) -> str:
    """'#PS-36327-R1' -> 'PS-36327'. Sem PS reconhecivel devolve o texto limpo."""
    texto = str(codigo or "").strip().lstrip("#").strip()
    m = _BASE.search(texto)
    return m.group(0).upper() if m else texto.upper()


def codigos_base_lista(codigo) -> list[str]:
    """Aceita '#PS-1, #PS-2' (servico com mais de um pedido)."""
    return [codigo_base(c) for c in str(codigo or "").split(",") if c.strip()]


def _lotes(lista, tamanho=450):
    for i in range(0, len(lista), tamanho):
        yield lista[i:i + tamanho]


def nf_por_codigo(codigos, conn: sqlite3.Connection | None = None) -> dict[str, str]:
    """{codigo base: "111, 222"} dos codigos pedidos (qualquer formato).
    Falha de banco devolve {} (a tela mostra "—", nunca quebra)."""
    bases = sorted({b for c in (codigos or []) for b in codigos_base_lista(c) if b})
    if not bases:
        return {}
    proprio = conn is None
    try:
        if proprio:
            from nucleo.banco import conectar
            conn = conectar()
        achados: dict[str, set[str]] = {}
        for lote in _lotes(bases):
            com_hash = [f"#{b}" for b in lote]
            marc = ",".join("?" * (len(lote) * 2))
            for id_pedido, nf in conn.execute(
                    "SELECT id_pedido, numero_nf FROM documentos_pedido "
                    "WHERE tipo_doc='nf' AND status IN ('casado','baixado') "
                    f"AND numero_nf IS NOT NULL AND numero_nf != '' AND id_pedido IN ({marc})",
                    lote + com_hash):
                achados.setdefault(codigo_base(id_pedido), set()).add(str(nf).strip())
        faltam = [b for b in bases if b not in achados]
        for lote in _lotes(faltam):
            marc = ",".join("?" * (len(lote) * 2))
            for cod, nf in conn.execute(
                    "SELECT codigo_pedido, numero_nf FROM documentos_processados "
                    "WHERE status='ENVIADO' AND tipo='Nota Fiscal' "
                    f"AND numero_nf IS NOT NULL AND numero_nf != '' AND codigo_pedido IN ({marc})",
                    lote + [f"#{b}" for b in lote]):
                achados.setdefault(codigo_base(cod), set()).add(str(nf).strip())
        return {b: ", ".join(sorted(v)) for b, v in achados.items()}
    except Exception:  # noqa: BLE001
        return {}
    finally:
        if proprio and conn is not None:
            conn.close()


def nf_do_codigo(codigo, mapa: dict[str, str]) -> str:
    """NF(s) de um codigo (ou lista 'PS-1, PS-2') a partir do mapa; "" se nao ha."""
    vistos: list[str] = []
    for b in codigos_base_lista(codigo):
        for nf in (mapa.get(b) or "").split(", "):
            if nf and nf not in vistos:
                vistos.append(nf)
    return ", ".join(vistos)


def anotar_nf(itens, campo_codigo="codigo", campo_nf="nf", conn=None):
    """Poe item[campo_nf] ("" se nao ha) em cada dict de `itens`, com UMA busca
    em lote. Devolve a mesma lista."""
    itens = list(itens or [])
    mapa = nf_por_codigo([i.get(campo_codigo) for i in itens if isinstance(i, dict)], conn)
    for i in itens:
        if isinstance(i, dict):
            i[campo_nf] = nf_do_codigo(i.get(campo_codigo), mapa)
    return itens
