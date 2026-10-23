# -*- coding: utf-8 -*-
"""
migrar_transportadoras_planilha.py

Copia a planilha BD_TRANSPORTADORAS.xlsx para a tabela transportadoras do
dados.db (carga inicial, origem PLANILHA) e prova que o catalogo do banco
resolve igual ao da planilha (--comparar).

Uso:
  py -3.11 migrar_transportadoras_planilha.py --modo-teste   (banco em memoria, so mostra)
  py -3.11 migrar_transportadoras_planilha.py                (importa; idempotente)
  py -3.11 migrar_transportadoras_planilha.py --comparar     (exit 1 se houver diferenca)
"""
import argparse
import re
import sqlite3
import sys
from pathlib import Path

import openpyxl

import transportadoras_cadastro as cad
from batimento.medir import _RE_CNPJ
from regras.transportadoras import CatalogoTransportadoras

PLANILHA = Path(__file__).parent / "dados" / "BD_TRANSPORTADORAS.xlsx"


def _txt(v) -> str:
    return "" if v is None else str(v).strip()


def _cnpj_da_linha(row) -> str:
    """Mesma varredura de CatalogoTransportadoras.carregar (colunas J..T)."""
    for idx in range(9, min(len(row), 20)):
        v = _txt(row[idx])
        if not v.isdigit():
            continue
        if len(v) == 13:
            v = v.zfill(14)
        if len(v) >= 14:
            return v
    return ""


def _linhas(xlsx: Path):
    """Linhas da planilha com nome e tipo; devolve (validas, ignoradas)."""
    wb = openpyxl.load_workbook(xlsx, read_only=True, data_only=True)
    validas, ignoradas = [], []
    for row in wb.active.iter_rows(min_row=2, values_only=True):
        if not row or not row[0] or not row[1]:
            continue
        nome, tipo = _txt(row[0]), _txt(row[1]).upper()
        if tipo not in cad.TIPOS:
            ignoradas.append(nome)
            continue
        row = tuple(row) + (None,) * (20 - len(row))
        validas.append({
            "nome": nome, "tipo": tipo,
            "uf": _txt(row[2]), "municipio": _txt(row[3]).rstrip("\xa0"), "bairro": _txt(row[4]),
            "logradouro": _txt(row[5]), "numero": _txt(row[6]), "complemento": _txt(row[7]),
            "cep": _txt(row[8]),
            "cidades_atendidas": _txt(row[16]), "ufs_atendidas": _txt(row[17]),
            "cnpj": _cnpj_da_linha(row), "email": _txt(row[19]),
        })
    wb.close()
    return validas, ignoradas


def importar(xlsx: Path, conn) -> dict:
    """INSERT direto (traz a planilha como esta, sem validar conflito). Idempotente."""
    validas, ignoradas = _linhas(xlsx)
    agora = cad._agora()
    importadas = ja = 0
    for l in validas:
        existe = conn.execute(
            "SELECT 1 FROM transportadoras WHERE nome=? AND origem='PLANILHA' AND tipo=? AND IFNULL(cnpj,'')=?",
            (l["nome"], l["tipo"], l["cnpj"])).fetchone()
        if existe:
            ja += 1
            continue
        cols = list(l) + ["situacao", "origem", "criado_em", "atualizado_em", "atualizado_por"]
        vals = list(l.values()) + ["ATIVA", "PLANILHA", agora, agora, "migracao"]
        conn.execute(f"INSERT INTO transportadoras ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
        importadas += 1
    conn.commit()
    return {"importadas": importadas, "ja_existiam": ja, "ignoradas": ignoradas}


def _assinatura(r) -> tuple:
    return (r.tipo, str(r.endereco_redespacho), r.email, r.conflito, r.desconhecida)


def comparar(xlsx: Path, db: Path, consultas: list[tuple[str, str]]) -> list[str]:
    cat_x = CatalogoTransportadoras.carregar(xlsx)
    cat_b = CatalogoTransportadoras.carregar_do_banco(db)
    validas, _ = _linhas(xlsx)
    todas = list(dict.fromkeys([(l["nome"], l["cnpj"]) for l in validas] + list(consultas)))
    difs = []
    for nome, cnpj in todas:
        a, b = _assinatura(cat_x.resolver(nome, cnpj=cnpj)), _assinatura(cat_b.resolver(nome, cnpj=cnpj))
        if a != b:
            difs.append(f"{nome!r} cnpj={cnpj!r}: planilha={a} banco={b}")
    return difs


def consultas_recentes(db: Path, dias: int = 60) -> list[tuple[str, str]]:
    """(nome, cnpj) distintos de batimento_pedidos.transportadora nos ultimos dias."""
    conn = sqlite3.connect(f"file:{Path(db).as_posix()}?mode=ro", uri=True)
    try:
        textos = [r[0] for r in conn.execute(
            "SELECT DISTINCT transportadora FROM batimento_pedidos "
            "WHERE transportadora IS NOT NULL AND transportadora != '' "
            "AND visto_em >= datetime('now', ?)", (f"-{int(dias)} days",))]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    saida = []
    for t in textos:
        m = _RE_CNPJ.search(t)
        cnpj = re.sub(r"\D", "", m.group(0)) if m else ""
        saida.append((_RE_CNPJ.sub("", t).strip(), cnpj))
    return list(dict.fromkeys(saida))


def main():
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--modo-teste", action="store_true", help="importa em banco em memoria, nao escreve no --db")
    ap.add_argument("--comparar", action="store_true", help="compara planilha x banco; exit 1 se houver diferenca")
    ap.add_argument("--planilha", type=Path, default=PLANILHA)
    ap.add_argument("--db", type=Path, default=cad.DB_PATH)
    a = ap.parse_args()

    if a.comparar:
        difs = comparar(a.planilha, a.db, consultas_recentes(a.db))
        for d in difs:
            print(d)
        print(f"{len(difs)} diferenca(s).")
        sys.exit(1 if difs else 0)

    conn = cad.conectar(Path(":memory:") if a.modo_teste else a.db)
    r = importar(a.planilha, conn)
    print(f"{'[modo-teste, em memoria] ' if a.modo_teste else ''}importadas={r['importadas']} "
          f"ja_existiam={r['ja_existiam']} ignoradas={len(r['ignoradas'])}")
    for n in r["ignoradas"]:
        print(f"  ignorada (tipo invalido): {n}")
    conn.close()


if __name__ == "__main__":
    main()
