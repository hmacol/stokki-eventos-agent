# -*- coding: utf-8 -*-
"""
motoristas_banco.py

Único módulo que lê e grava a tabela bd_motoristas (Hugo, 29/10/2026: a
planilha BD_MOTORISTAS.xlsx vai pro banco). A planilha passa a ser um
espelho SOMENTE LEITURA gerado daqui (escrever_espelho).

- A chave é `id`; AGENT_ID_VUUPT pode repetir de propósito (sem UNIQUE).
- CPF e telefone são sempre texto (zero à esquerda, sem ".0").
- Toda mudança em atualizar() fica em bd_motoristas_alteracoes com a origem.
"""
import logging
import os
import re
import sqlite3
from pathlib import Path

import pandas as pd

from nucleo import banco
from regras.preferencias_motoristas import _normalizar_texto, _parse_bool, _parse_texto_numerico

logger = logging.getLogger(__name__)

COLUNAS = [
    "AGENT_ID_VUUPT", "VEHICLE_ID_VUUPT", "NOME_MOTORISTA", "ACEITA_VIAGENS", "DIAS_DISPONIVEIS",
    "MAX_ROTAS_DIA", "ATIVO", "ZONAS_PREFERIDAS", "TELEFONE_MOTORISTA", "EMAIL_MOTORISTA", "PLACA",
    "CPF_MOTORISTA", "TIPO_VEICULO", "APENAS_CARGA_SECA", "SEM_APP",
]
_INTEIROS = {"AGENT_ID_VUUPT", "VEHICLE_ID_VUUPT", "MAX_ROTAS_DIA"}


def caminho_espelho() -> Path:
    """Calculado na chamada: acompanha banco.DB_PATH (testes redirecionam)."""
    return banco.DB_PATH.parent / "BD_MOTORISTAS.xlsx"


def _vazio(valor) -> bool:
    if valor is None:
        return True
    if isinstance(valor, float) and pd.isna(valor):
        return True
    return str(valor).strip() == ""


def _limpar(coluna: str, valor):
    """Valor de célula/campo -> o que vai pro banco (None, int ou texto)."""
    if _vazio(valor):
        return None
    if coluna in _INTEIROS:
        return int(float(str(valor).strip().replace(",", ".")))
    return _parse_texto_numerico(valor)


def _linha_para_dict(linha: sqlite3.Row) -> dict:
    return {k: linha[k] for k in linha.keys()}


def tem_registros(conn) -> bool:
    return conn.execute("SELECT 1 FROM bd_motoristas LIMIT 1").fetchone() is not None


def listar(conn) -> list[dict]:
    return [_linha_para_dict(r) for r in conn.execute("SELECT * FROM bd_motoristas ORDER BY id")]


def por_cpf(conn, cpf: str) -> list[dict]:
    return [_linha_para_dict(r) for r in
            conn.execute("SELECT * FROM bd_motoristas WHERE CPF_MOTORISTA = ? ORDER BY id", (cpf,))]


def _historico(conn, motorista_id, agent_id, campo, antigo, novo, origem, quando):
    conn.execute(
        "INSERT INTO bd_motoristas_alteracoes (motorista_id, agent_id, campo, valor_antigo, valor_novo, origem, alterado_em)"
        " VALUES (?,?,?,?,?,?,?)",
        (motorista_id, agent_id, campo, None if antigo is None else str(antigo),
         None if novo is None else str(novo), origem, quando))


def registrar_historico(conn, motorista_id: int, campo: str, valor_novo, origem: str) -> None:
    """Anota no historico sem mudar a coluna (valor_antigo = valor atual)."""
    if campo not in COLUNAS:
        raise ValueError(f"campo desconhecido: {campo}")
    atual = conn.execute("SELECT * FROM bd_motoristas WHERE id = ?", (motorista_id,)).fetchone()
    if atual is None:
        raise ValueError(f"motorista id={motorista_id} nao existe")
    _historico(conn, motorista_id, atual["AGENT_ID_VUUPT"], campo, atual[campo], valor_novo, origem, banco.agora())
    conn.commit()


def inserir(conn, registro: dict, origem: str) -> int:
    agora = banco.agora()
    valores = [_limpar(c, registro.get(c)) for c in COLUNAS]
    ativo = valores[COLUNAS.index("ATIVO")]
    inativado = agora if ativo is not None and not _parse_bool(ativo) else None
    cur = conn.execute(
        f"INSERT INTO bd_motoristas ({', '.join(COLUNAS)}, inativado_em, criado_em, atualizado_em)"
        f" VALUES ({', '.join('?' * len(COLUNAS))}, ?, ?, ?)",
        (*valores, inativado, agora, agora))
    novo_id = cur.lastrowid
    _historico(conn, novo_id, valores[0], "*criado*", None, None, origem, agora)
    conn.commit()
    return novo_id


def atualizar(conn, motorista_id: int, campos: dict, origem: str) -> list[dict]:
    atual = conn.execute("SELECT * FROM bd_motoristas WHERE id = ?", (motorista_id,)).fetchone()
    if atual is None:
        raise ValueError(f"motorista id={motorista_id} nao existe")
    desconhecidos = [c for c in campos if c not in COLUNAS]
    if desconhecidos:
        raise ValueError(f"campo desconhecido: {desconhecidos}")

    novos = {c: _limpar(c, v) for c, v in campos.items()}
    if "AGENT_ID_VUUPT" in novos and novos["AGENT_ID_VUUPT"] != atual["AGENT_ID_VUUPT"]:
        raise ValueError("AGENT_ID_VUUPT nao pode mudar")
    if "CPF_MOTORISTA" in novos and atual["CPF_MOTORISTA"] and novos["CPF_MOTORISTA"] != atual["CPF_MOTORISTA"]:
        raise ValueError("CPF_MOTORISTA ja preenchido nao pode mudar")
    for obrigatorio in ("NOME_MOTORISTA", "ATIVO"):
        if obrigatorio in novos and novos[obrigatorio] is None:
            raise ValueError(f"{obrigatorio} nao pode ficar vazio")

    mudancas = [{"campo": c, "de": atual[c], "para": v} for c, v in novos.items()
                if c != "AGENT_ID_VUUPT" and atual[c] != v]
    if not mudancas:
        return []

    agora = banco.agora()
    sets = {m["campo"]: m["para"] for m in mudancas}
    if "ATIVO" in sets:
        sets["inativado_em"] = None if _parse_bool(sets["ATIVO"]) else agora
    sets["atualizado_em"] = agora
    conn.execute(f"UPDATE bd_motoristas SET {', '.join(f'{c} = ?' for c in sets)} WHERE id = ?",
                 (*sets.values(), motorista_id))
    for m in mudancas:
        _historico(conn, motorista_id, atual["AGENT_ID_VUUPT"], m["campo"], m["de"], m["para"], origem, agora)
    conn.commit()
    return mudancas


def escrever_espelho(conn, caminho: Path | None = None) -> bool:
    """Grava a planilha-espelho (só COLUNAS, na ordem de id) via arquivo
    temporário + os.replace. Nunca levanta: erro vira log e False."""
    destino = Path(caminho) if caminho else caminho_espelho()
    tmp = destino.with_name(destino.name + ".tmp")
    try:
        linhas = [[r[c] for c in COLUNAS] for r in conn.execute(
            f"SELECT {', '.join(COLUNAS)} FROM bd_motoristas ORDER BY id")]
        df = pd.DataFrame(linhas, columns=COLUNAS, dtype=object)  # object: inteiro com vazio nao vira float
        df.to_excel(tmp, index=False, engine="openpyxl")
        os.replace(tmp, destino)
        return True
    except Exception:
        logger.exception(f"Falha ao escrever o espelho {destino}")
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def importar_planilha(conn, caminho: Path, substituir: bool = False) -> int:
    if tem_registros(conn) and not substituir:
        raise RuntimeError("bd_motoristas ja tem linhas")
    df = pd.read_excel(caminho, dtype={"CPF_MOTORISTA": str})
    df.columns = [re.sub(r"[^A-Z0-9_]", "", _normalizar_texto(c)) for c in df.columns]
    agora = banco.agora()
    try:
        if substituir:
            conn.execute("DELETE FROM bd_motoristas")
        n = 0
        for _, linha in df.iterrows():
            valores = [_limpar(c, linha[c]) if c in df.columns else None for c in COLUNAS]
            if all(v is None for v in valores):
                continue
            ativo = valores[COLUNAS.index("ATIVO")]
            inativado = agora if ativo is not None and not _parse_bool(ativo) else None
            cur = conn.execute(
                f"INSERT INTO bd_motoristas ({', '.join(COLUNAS)}, inativado_em, criado_em, atualizado_em)"
                f" VALUES ({', '.join('?' * len(COLUNAS))}, ?, ?, ?)",
                (*valores, inativado, agora, agora))
            _historico(conn, cur.lastrowid, valores[0], "*criado*", None, None, "importacao", agora)
            n += 1
        conn.commit()
        return n
    except Exception:
        conn.rollback()
        raise
