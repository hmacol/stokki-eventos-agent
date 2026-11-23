# -*- coding: utf-8 -*-
"""
Etapa Receber do recebimento por etapas: checklist da chegada do caminhao
(fotos e temperatura). So biblioteca padrao: o portal do cliente tambem carrega
este modulo, entao NAO importar wms nem outro modulo do painel.
"""
import io
import sqlite3
import uuid as _uuid
from datetime import datetime
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
DB_PATH = _RAIZ / "dados" / "dados.db"
PASTA_FOTOS = DB_PATH.parent / "chegadas"
MAX_FOTO_BYTES = 12 * 1024 * 1024
TIPOS_FOTO = ("CAMINHAO_ABERTO", "PLACA", "NOTA_FISCAL", "TERMOMETRO", "CARGA", "AVARIA")

FAIXAS_PADRAO = {"REFRIGERADO": (0.0, 7.0), "CONGELADO": (-18.0, -12.0)}
CLASSES_FRIAS = ("REFRIGERADO", "CONGELADO")


class ErroChegada(ValueError):
    pass


_SCHEMA = """
CREATE TABLE IF NOT EXISTS wms_chegadas (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid                  TEXT NOT NULL UNIQUE,
    estado                TEXT NOT NULL DEFAULT 'RASCUNHO',
    motorista             TEXT NOT NULL DEFAULT '',
    placa                 TEXT NOT NULL DEFAULT '',
    transportadora        TEXT NOT NULL DEFAULT '',
    volumes               INTEGER,
    lacre                 TEXT NOT NULL DEFAULT '',
    integra               INTEGER,
    observacao            TEXT NOT NULL DEFAULT '',
    chegada_em            TEXT NOT NULL DEFAULT '',
    operador_id           INTEGER,
    operador_nome         TEXT NOT NULL DEFAULT '',
    concluida_em          TEXT NOT NULL DEFAULT '',
    liberacao_via         TEXT NOT NULL DEFAULT '',
    liberacao_nome        TEXT NOT NULL DEFAULT '',
    liberacao_texto       TEXT NOT NULL DEFAULT '',
    liberacao_em          TEXT NOT NULL DEFAULT '',
    resposta_cliente      TEXT NOT NULL DEFAULT '',
    resposta_cliente_nome TEXT NOT NULL DEFAULT '',
    resposta_cliente_em   TEXT NOT NULL DEFAULT '',
    nao_recebida_motivo   TEXT NOT NULL DEFAULT '',
    nao_recebida_por      TEXT NOT NULL DEFAULT '',
    criado_em             TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS wms_chegada_recebimentos (
    chegada_id     INTEGER NOT NULL REFERENCES wms_chegadas(id),
    recebimento_id INTEGER NOT NULL,
    PRIMARY KEY (chegada_id, recebimento_id)
);
CREATE INDEX IF NOT EXISTS idx_wms_chegada_receb ON wms_chegada_recebimentos(recebimento_id);
CREATE TABLE IF NOT EXISTS wms_chegada_fotos (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chegada_id INTEGER NOT NULL REFERENCES wms_chegadas(id),
    tipo       TEXT NOT NULL,
    arquivo    TEXT NOT NULL,
    criado_em  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_wms_chegada_fotos ON wms_chegada_fotos(chegada_id);
CREATE TABLE IF NOT EXISTS wms_chegada_temperaturas (
    chegada_id INTEGER NOT NULL REFERENCES wms_chegadas(id),
    classe     TEXT NOT NULL,
    medida_c   REAL NOT NULL,
    faixa_min  REAL NOT NULL,
    faixa_max  REAL NOT NULL,
    fora       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chegada_id, classe)
);
CREATE TABLE IF NOT EXISTS wms_chegada_envios (
    chave      TEXT PRIMARY KEY,
    enviado_em TEXT NOT NULL DEFAULT ''
);
"""


def garantir_tabelas(conn) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def _agora() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def conectar(caminho: Path | None = None) -> sqlite3.Connection:
    caminho = Path(caminho) if caminho else DB_PATH
    caminho.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(caminho, timeout=30)
    conn.row_factory = sqlite3.Row
    garantir_tabelas(conn)
    return conn


def faixas(config: dict) -> dict:
    """Faixa por classe: o padrao, sobrescrito por config['wms']['temperatura_faixas']."""
    resultado = dict(FAIXAS_PADRAO)
    brutas = ((config or {}).get("wms") or {}).get("temperatura_faixas")
    if not isinstance(brutas, dict):
        return resultado
    for classe in FAIXAS_PADRAO:
        par = brutas.get(classe)
        if isinstance(par, (list, tuple)) and len(par) == 2:
            try:
                minimo, maximo = float(par[0]), float(par[1])
            except (TypeError, ValueError):
                continue
            if minimo <= maximo:
                resultado[classe] = (minimo, maximo)
    return resultado


def _produtos_da_carga(conn, recebimento_ids):
    ids = [int(i) for i in recebimento_ids]
    if not ids:
        return []
    marcas = ",".join("?" * len(ids))
    return conn.execute(
        "SELECT DISTINCT p.id, p.temperatura, p.temp_min, p.temp_max "
        "FROM wms_recebimento_itens i JOIN wms_produtos p ON p.id = i.produto_id "
        f"WHERE i.recebimento_id IN ({marcas})", ids).fetchall()


def classes_da_carga(conn, recebimento_ids) -> list:
    presentes = {p["temperatura"] for p in _produtos_da_carga(conn, recebimento_ids)}
    return [c for c in CLASSES_FRIAS if c in presentes]


def _faixas_efetivas(conn, recebimento_ids, classe, config) -> list:
    """Faixa de cada produto da classe na carga: a propria (substitui a da classe) ou a da classe."""
    da_classe = faixas(config)[classe]
    efetivas = []
    for p in _produtos_da_carga(conn, recebimento_ids):
        if p["temperatura"] != classe:
            continue
        if p["temp_min"] is not None and p["temp_max"] is not None:
            efetivas.append((float(p["temp_min"]), float(p["temp_max"])))
        else:
            efetivas.append(da_classe)
    return efetivas


def faixa_da_carga(conn, recebimento_ids, classe, config) -> tuple:
    """Faixa esperada da carga (so para mostrar): intersecao das faixas efetivas dos produtos da classe.
    Sem produto da classe ou com intersecao vazia, devolve a faixa da classe."""
    da_classe = faixas(config)[classe]
    efetivas = _faixas_efetivas(conn, recebimento_ids, classe, config)
    if not efetivas:
        return da_classe
    minimo = max(f[0] for f in efetivas)
    maximo = min(f[1] for f in efetivas)
    return (minimo, maximo) if minimo <= maximo else da_classe


def medida_dentro(conn, recebimento_ids, classe, medida, config) -> bool:
    """True so se a medida esta dentro (limites inclusos) da faixa efetiva de TODOS os produtos da classe."""
    efetivas = _faixas_efetivas(conn, recebimento_ids, classe, config) or [faixas(config)[classe]]
    return all(minimo <= medida <= maximo for minimo, maximo in efetivas)


def detalhe(conn, chegada_id: int) -> dict:
    linha = conn.execute("SELECT * FROM wms_chegadas WHERE id = ?", (chegada_id,)).fetchone()
    if not linha:
        raise ErroChegada("Chegada não encontrada.")
    d = dict(linha)
    d["recebimentos"] = [dict(r) for r in conn.execute(
        "SELECT r.id, r.codigo, r.embarcador FROM wms_chegada_recebimentos c "
        "JOIN wms_recebimentos r ON r.id = c.recebimento_id WHERE c.chegada_id = ? ORDER BY r.id",
        (chegada_id,))]
    d["fotos"] = [dict(r) for r in conn.execute(
        "SELECT id, tipo, arquivo, criado_em FROM wms_chegada_fotos WHERE chegada_id = ? ORDER BY id",
        (chegada_id,))]
    d["temperaturas"] = [dict(r) for r in conn.execute(
        "SELECT classe, medida_c, faixa_min, faixa_max, fora FROM wms_chegada_temperaturas "
        "WHERE chegada_id = ? ORDER BY classe", (chegada_id,))]
    return d


def abrir(conn, *, uuid: str, recebimento_ids: list, operador: dict) -> dict:
    """Abre a chegada (RASCUNHO) de um ou mais recebimentos. Idempotente por uuid."""
    existente = conn.execute("SELECT id FROM wms_chegadas WHERE uuid = ?", (uuid,)).fetchone()
    if existente:
        return detalhe(conn, existente["id"])
    ids = sorted({int(i) for i in (recebimento_ids or [])})
    if not ids:
        raise ErroChegada("Escolha ao menos um recebimento.")
    conn.execute("BEGIN IMMEDIATE")
    try:
        existente = conn.execute("SELECT id FROM wms_chegadas WHERE uuid = ?", (uuid,)).fetchone()
        if existente:  # outra chamada com o mesmo uuid venceu a corrida
            conn.rollback()
            return detalhe(conn, existente["id"])
        for rid in ids:
            rec =conn.execute("SELECT estado FROM wms_recebimentos WHERE id = ?", (rid,)).fetchone()
            if not rec:
                raise ErroChegada("Recebimento não encontrado.")
            if rec["estado"] != "ESPERADO":
                raise ErroChegada("Esse recebimento já foi encerrado.")
            if conn.execute(
                    "SELECT 1 FROM wms_chegada_recebimentos cr JOIN wms_chegadas c ON c.id = cr.chegada_id "
                    "WHERE cr.recebimento_id = ? AND c.estado != 'NAO_RECEBIDA'", (rid,)).fetchone():
                raise ErroChegada("Esse recebimento já tem chegada registrada.")
        agora = _agora()
        cur = conn.execute(
            "INSERT INTO wms_chegadas (uuid, estado, chegada_em, operador_id, operador_nome, criado_em) "
            "VALUES (?, 'RASCUNHO', ?, ?, ?, ?)",
            (uuid, agora, (operador or {}).get("id"), (operador or {}).get("nome") or "", agora))
        chegada_id = cur.lastrowid
        conn.executemany("INSERT INTO wms_chegada_recebimentos (chegada_id, recebimento_id) VALUES (?, ?)",
                         [(chegada_id, rid) for rid in ids])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return detalhe(conn, chegada_id)


def salvar_foto(conn, chegada_id: int, tipo: str, conteudo: bytes, pasta: Path | None = None) -> dict:
    """Valida, regrava como JPEG (lado maior 2200 px) e registra. Erro nao deixa arquivo nem linha."""
    from PIL import Image, ImageOps
    if tipo not in TIPOS_FOTO:
        raise ErroChegada("Tipo de foto inválido.")
    if len(conteudo or b"") > MAX_FOTO_BYTES:
        raise ErroChegada("Foto grande demais (máximo 12 MB).")
    if not conn.execute("SELECT 1 FROM wms_chegadas WHERE id = ?", (chegada_id,)).fetchone():
        raise ErroChegada("Chegada não encontrada.")
    try:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(conteudo or b"")))
        img.thumbnail((2200, 2200))
        img = img.convert("RGB")
    except Exception:
        raise ErroChegada("Não consegui ler a foto. Tire de novo pela câmera do aparelho.")
    pasta = Path(pasta) if pasta else PASTA_FOTOS
    pasta.mkdir(parents=True, exist_ok=True)
    nome = f"CHG-{chegada_id}-{_uuid.uuid4().hex[:8]}.jpg"
    destino = pasta / nome
    try:
        img.save(destino, format="JPEG", quality=85)
    except Exception:
        destino.unlink(missing_ok=True)
        raise ErroChegada("Não consegui gravar a foto.")
    try:
        cur = conn.execute(
            "INSERT INTO wms_chegada_fotos (chegada_id, tipo, arquivo, criado_em) VALUES (?, ?, ?, ?)",
            (chegada_id, tipo, nome, _agora()))
        conn.commit()
    except Exception:
        conn.rollback()
        destino.unlink(missing_ok=True)
        raise
    return {"id": cur.lastrowid, "tipo": tipo, "arquivo": nome}


def _foto(conn, chegada_id, foto_id):
    foto = conn.execute("SELECT * FROM wms_chegada_fotos WHERE id = ? AND chegada_id = ?",
                        (foto_id, chegada_id)).fetchone()
    if not foto:
        raise ErroChegada("Foto não encontrada.")
    return foto


def remover_foto(conn, chegada_id: int, foto_id: int, pasta: Path | None = None) -> None:
    """So com a chegada em RASCUNHO. Apaga a linha e o arquivo."""
    foto = _foto(conn, chegada_id, foto_id)
    estado = conn.execute("SELECT estado FROM wms_chegadas WHERE id = ?", (chegada_id,)).fetchone()["estado"]
    if estado != "RASCUNHO":
        raise ErroChegada("A chegada já foi concluída: não dá mais para remover fotos.")
    conn.execute("DELETE FROM wms_chegada_fotos WHERE id = ?", (foto_id,))
    conn.commit()
    (Path(pasta) if pasta else PASTA_FOTOS).joinpath(foto["arquivo"]).unlink(missing_ok=True)


def caminho_foto(conn, chegada_id: int, foto_id: int, pasta: Path | None = None) -> Path:
    foto = _foto(conn, chegada_id, foto_id)
    return (Path(pasta) if pasta else PASTA_FOTOS) / foto["arquivo"]
