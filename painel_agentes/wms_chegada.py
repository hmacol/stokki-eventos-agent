# -*- coding: utf-8 -*-
"""
Etapa Receber do recebimento por etapas: checklist da chegada do caminhao
(fotos e temperatura). So biblioteca padrao: o portal do cliente tambem carrega
este modulo, entao NAO importar wms nem outro modulo do painel.
"""
import sqlite3
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
DB_PATH = _RAIZ / "dados" / "dados.db"

FAIXAS_PADRAO = {"REFRIGERADO": (0.0, 7.0), "CONGELADO": (-18.0, -12.0)}
CLASSES_FRIAS = ("REFRIGERADO", "CONGELADO")


def garantir_tabelas(conn) -> None:
    """As tabelas do checklist entram na proxima tarefa."""


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


def faixa_da_carga(conn, recebimento_ids, classe, config) -> tuple:
    """Faixa da classe estreitada pelas faixas proprias dos produtos dessa classe na carga."""
    minimo, maximo = faixas(config)[classe]
    for p in _produtos_da_carga(conn, recebimento_ids):
        if p["temperatura"] == classe and p["temp_min"] is not None and p["temp_max"] is not None:
            minimo = max(minimo, p["temp_min"])
            maximo = min(maximo, p["temp_max"])
    return (minimo, maximo)
