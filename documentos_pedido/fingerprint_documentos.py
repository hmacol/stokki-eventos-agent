# -*- coding: utf-8 -*-
"""
fingerprint_documentos.py

Registro local de documentos já processados (baixados, classificados,
casados com um pedido e enviados pro GCS) -- evita reprocessar o
mesmo arquivo em execuções seguintes, tanto vindo de e-mail quanto da
Stokki.

Chave de identidade: hash SHA-256 do conteúdo do arquivo (não do nome
-- o mesmo documento pode chegar com nomes diferentes por e-mail e
pela Stokki, mas o CONTEÚDO é idêntico).
"""
import hashlib
import sqlite3
from datetime import datetime
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
DB_PATH = _RAIZ / "dados" / "dados.db"


# Colunas adicionadas depois da criação original da tabela (spec de
# boletos parcelados, 11/08). cnpj_contraparte não estava na spec, mas
# é necessário pra Regra 2 do casamento (NF + CNPJ) funcionar ENTRE
# execuções: é o CNPJ do destinatário da NF (= pagador do boleto).
_COLUNAS_NOVAS = [
    ("numero_parcela", "INTEGER"),
    ("total_parcelas", "INTEGER"),
    ("numero_nf", "TEXT"),
    ("cnpj_contraparte", "TEXT"),
]


def _conectar():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS documentos_processados (
            hash_conteudo   TEXT PRIMARY KEY,
            origem          TEXT NOT NULL,
            nome_arquivo    TEXT,
            tipo            TEXT,
            codigo_pedido   TEXT,
            status          TEXT NOT NULL,
            motivo          TEXT,
            gcs_path        TEXT,
            processado_em   TEXT NOT NULL
        )
    """)
    existentes = {r["name"] for r in conn.execute("PRAGMA table_info(documentos_processados)")}
    for coluna, tipo_sql in _COLUNAS_NOVAS:
        if coluna not in existentes:
            conn.execute(f"ALTER TABLE documentos_processados ADD COLUMN {coluna} {tipo_sql}")
    conn.commit()
    return conn


def calcular_hash(caminho_arquivo: Path) -> str:
    """SHA-256 do conteúdo do arquivo -- identidade real do documento,
    independente do nome."""
    h = hashlib.sha256()
    with open(caminho_arquivo, "rb") as f:
        for bloco in iter(lambda: f.read(8192), b""):
            h.update(bloco)
    return h.hexdigest()


def ja_processado(hash_conteudo: str) -> bool:
    conn = _conectar()
    row = conn.execute(
        "SELECT 1 FROM documentos_processados WHERE hash_conteudo = ?", (hash_conteudo,)
    ).fetchone()
    conn.close()
    return row is not None


def marcar_processado(hash_conteudo: str, origem: str, nome_arquivo: str, tipo: str | None,
                      codigo_pedido: str | None, status: str, motivo: str | None = None,
                      gcs_path: str | None = None, numero_nf: str | None = None,
                      numero_parcela: int | None = None, total_parcelas: int | None = None,
                      cnpj_contraparte: str | None = None):
    """
    status: 'ENVIADO' (casou com pedido e foi pro GCS) ou
    'REVISAO_MANUAL' (não conseguiu casar com nenhum pedido -- motivo
    explica por quê).

    numero_nf/cnpj_contraparte: pra Nota Fiscal, alimentam o índice
    NF->pedido usado no casamento de boletos (matcher.IndexadorNF);
    pra Boleto, registram de qual NF/parcela o boleto é. Cada parcela
    tem hash de conteúdo próprio, então parcelas diferentes do mesmo
    pedido NUNCA colidem nesta tabela -- nada bloqueia parcela 2 por
    já existir a parcela 1.
    """
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _conectar()
    conn.execute("""
        INSERT INTO documentos_processados
            (hash_conteudo, origem, nome_arquivo, tipo, codigo_pedido, status, motivo, gcs_path,
             numero_nf, numero_parcela, total_parcelas, cnpj_contraparte, processado_em)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(hash_conteudo) DO UPDATE SET
            status = excluded.status, motivo = excluded.motivo,
            gcs_path = excluded.gcs_path, processado_em = excluded.processado_em,
            numero_nf = COALESCE(excluded.numero_nf, numero_nf),
            numero_parcela = COALESCE(excluded.numero_parcela, numero_parcela),
            total_parcelas = COALESCE(excluded.total_parcelas, total_parcelas),
            cnpj_contraparte = COALESCE(excluded.cnpj_contraparte, cnpj_contraparte)
    """, (hash_conteudo, origem, nome_arquivo, tipo, codigo_pedido, status, motivo, gcs_path,
          numero_nf, numero_parcela, total_parcelas, cnpj_contraparte, agora))
    conn.commit()
    conn.close()


def carregar_indice_nf() -> list[dict]:
    """Linhas de Nota Fiscal já casadas com pedido e com número de NF
    conhecido -- base do índice NF->pedido do matcher.IndexadorNF."""
    conn = _conectar()
    rows = conn.execute(
        "SELECT DISTINCT numero_nf, cnpj_contraparte, codigo_pedido FROM documentos_processados "
        "WHERE tipo = 'Nota Fiscal' AND numero_nf IS NOT NULL AND codigo_pedido IS NOT NULL"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def pedidos_nf_pendentes() -> set[str]:
    """Pedidos com Nota Fiscal registrada mas sem numero_nf preenchido
    (linhas anteriores à migração de 11/08) -- candidatos ao backfill
    a partir dos PDFs de DANFE ainda salvos localmente."""
    conn = _conectar()
    rows = conn.execute(
        "SELECT DISTINCT codigo_pedido FROM documentos_processados "
        "WHERE tipo = 'Nota Fiscal' AND numero_nf IS NULL AND codigo_pedido IS NOT NULL"
    ).fetchall()
    conn.close()
    return {r["codigo_pedido"] for r in rows}


def atualizar_nf_pedido(codigo_pedido: str, numero_nf: str, cnpj_contraparte: str | None):
    """Backfill: preenche numero_nf/cnpj_contraparte nas linhas de
    Nota Fiscal de um pedido que foram gravadas antes dessas colunas
    existirem (DANFEs processadas até 10/08)."""
    conn = _conectar()
    conn.execute(
        "UPDATE documentos_processados SET numero_nf = ?, cnpj_contraparte = ? "
        "WHERE codigo_pedido = ? AND tipo = 'Nota Fiscal' AND numero_nf IS NULL",
        (numero_nf, cnpj_contraparte, codigo_pedido),
    )
    conn.commit()
    conn.close()


def ja_enviado_para_pedido(codigo_pedido: str, tipo: str) -> bool:
    """True se já existe um documento desse tipo enviado com sucesso pra
    esse pedido. Usado SÓ pra pular a regeração da DANFE (tipo 'Nota
    Fiscal' -- ela muda de hash a cada geração, então precisa desse
    controle). NÃO usar pra Boleto: boletos parcelados têm várias
    parcelas legítimas do mesmo tipo pro mesmo pedido, e a deduplicação
    delas já acontece pelo hash de conteúdo (cada parcela é única)."""
    conn = _conectar()
    row = conn.execute(
        "SELECT 1 FROM documentos_processados WHERE codigo_pedido = ? AND tipo = ? AND status = 'ENVIADO'",
        (codigo_pedido, tipo),
    ).fetchone()
    conn.close()
    return row is not None


def listar_pendentes_revisao(limite: int = 100) -> list[dict]:
    """Documentos que não conseguiram ser casados com nenhum pedido --
    pra revisão manual do Hugo."""
    conn = _conectar()
    rows = conn.execute(
        "SELECT * FROM documentos_processados WHERE status = 'REVISAO_MANUAL' "
        "ORDER BY processado_em DESC LIMIT ?", (limite,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
