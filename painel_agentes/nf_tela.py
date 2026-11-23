# -*- coding: utf-8 -*-
"""
painel_agentes/nf_tela.py

Apoio das telas do painel para mostrar/buscar a NF ao lado do PS (decisao do
Hugo, 23/11). O valor da NF vem de nucleo/nf_pedido (uma busca em lote por
tela); aqui fica so a busca REVERSA (digitou a NF, acha o PS) e o texto
"PS-1 · NF 123" usado em e-mails, mensagens e planilhas.
"""
import sqlite3

from nucleo import nf_pedido
from nucleo.nf_pedido import SEM_NF


def texto_nf(nf) -> str:
    """"123" / "123, 456" / "" (sem NF vira o traco)."""
    return str(nf or "").strip() or SEM_NF


def ps_nf(codigo, nf) -> str:
    """"PS-1 · NF 123": o par que as telas e mensagens mostram."""
    return f"{codigo} · NF {texto_nf(nf)}"


def completar_nf(pedidos: list[dict], campo_codigo: str = "codigo") -> None:
    """Completa `nf` (so onde falta) dos pedidos de uma folha/lista. UMA busca em lote."""
    mapa = nf_pedido.nf_por_codigo([p.get(campo_codigo) for p in pedidos])
    for p in pedidos:
        p["nf"] = p.get("nf") or nf_pedido.nf_do_codigo(p.get(campo_codigo), mapa)


def codigos_por_nf(termo, conn: sqlite3.Connection | None = None, limite: int = 200) -> list[str]:
    """Codigos base (PS-N) cuja NF CONTEM o termo (so digitos, 3+). Mesmas fontes
    de nucleo/nf_pedido. Falha de banco devolve []."""
    termo = str(termo or "").strip()
    if len(termo) < 3 or not termo.isdigit():
        return []
    proprio = conn is None
    try:
        if proprio:
            from nucleo.banco import conectar
            conn = conectar()
        achados: set[str] = set()
        for sql in ("SELECT id_pedido FROM documentos_pedido WHERE tipo_doc='nf' "
                    "AND status IN ('casado','baixado') AND numero_nf LIKE ? LIMIT ?",
                    "SELECT codigo_pedido FROM documentos_processados WHERE status='ENVIADO' "
                    "AND tipo='Nota Fiscal' AND numero_nf LIKE ? LIMIT ?"):
            try:
                achados.update(nf_pedido.codigo_base(r[0]) for r in conn.execute(sql, (f"%{termo}%", limite)) if r[0])
            except sqlite3.Error:
                continue
        return sorted(a for a in achados if a.startswith("PS-"))[:limite]
    except Exception:  # noqa: BLE001
        return []
    finally:
        if proprio and conn is not None:
            conn.close()
